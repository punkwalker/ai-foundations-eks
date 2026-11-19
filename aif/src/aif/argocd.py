"""Read and nudge Argo CD Applications through the Kubernetes API.

The same code works for the managed Argo CD capability and for a Helm install,
because both use the argoproj.io CRDs on the Hub.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from aif.kube import KubeApi, KubeError, Obj, Resource, is_transient
from aif.waiting import Clock, WaitTimeoutError, until

APPLICATIONS = Resource("argoproj.io", "v1alpha1", "applications", "Application")
APPLICATIONSETS = Resource("argoproj.io", "v1alpha1", "applicationsets", "ApplicationSet")
REFRESH_ANNOTATION = "argocd.argoproj.io/refresh"
CASCADE_FINALIZER = "resources-finalizer.argocd.argoproj.io"
STAGE_LABEL = "platform.aws/stage"
"""Label the stage ApplicationSets put on each generated Application, value NN."""


@dataclass(frozen=True)
class AppReport:
    """Why one Application is not ready."""

    app: str
    sync: str
    health: str
    condition: str
    last_operation: str

    def __str__(self) -> str:
        return (
            f"{self.app}: sync={self.sync} health={self.health} "
            f"condition={self.condition or '-'} last operation={self.last_operation or '-'}"
        )


@dataclass(frozen=True)
class WaitResult:
    ok: bool
    ready: tuple[str, ...] = ()
    reports: tuple[AppReport, ...] = field(default_factory=tuple)
    error: str = ""

    def summary(self) -> str:
        if self.ok:
            return f"{len(self.ready)} Applications synced and healthy"
        lines = [self.error] if self.error else []
        lines.extend(str(r) for r in self.reports)
        return "\n".join(lines)


def _get(obj: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def report(app: Obj) -> AppReport:
    conditions = _get(app, "status", "conditions") or []
    condition = "; ".join(f"{c.get('type')}: {c.get('message', '')}" for c in conditions)
    return AppReport(
        app=str(_get(app, "metadata", "name")),
        sync=str(_get(app, "status", "sync", "status") or "Unknown"),
        health=str(_get(app, "status", "health", "status") or "Unknown"),
        condition=condition,
        last_operation=str(_get(app, "status", "operationState", "message") or ""),
    )


def is_ready(app: Obj) -> bool:
    r = report(app)
    return r.sync == "Synced" and r.health == "Healthy"


def list_applications(api: KubeApi, namespace: str, selector: str = "") -> list[Obj]:
    return api.list_objects(APPLICATIONS, namespace, selector)


def list_applicationsets(api: KubeApi, namespace: str, selector: str = "") -> list[Obj]:
    return api.list_objects(APPLICATIONSETS, namespace, selector)


def hard_refresh(api: KubeApi, namespace: str, name: str) -> None:
    """Ask Argo CD to re-read Git and the cluster for one Application."""
    body = {"metadata": {"annotations": {REFRESH_ANNOTATION: "hard"}}}
    api.patch_object(APPLICATIONS, name, body, namespace)


def refresh_all(api: KubeApi, namespace: str, selector: str = "") -> list[str]:
    names = [str(a["metadata"]["name"]) for a in list_applications(api, namespace, selector)]
    for name in names:
        hard_refresh(api, namespace, name)
    return names


def missing_cascade_finalizer(apps: Iterable[Obj]) -> list[str]:
    """Names of Applications that would leave their resources behind when deleted."""
    return [
        str(a["metadata"]["name"])
        for a in apps
        if CASCADE_FINALIZER not in (a["metadata"].get("finalizers") or [])
    ]


@dataclass
class _Poll:
    apps: list[Obj] = field(default_factory=list)
    error: str = ""


def wait_for_apps(
    api: KubeApi,
    namespace: str,
    selector: str,
    timeout: float,
    clock: Clock | None = None,
    interval: float = 5.0,
) -> WaitResult:
    """Wait until every Application matching `selector` is Synced and Healthy.

    Transient errors (CRD or webhook not ready yet) are retried with backoff until
    the timeout. Other API errors stop the wait at once. Returns a report either way.
    """

    def check() -> tuple[bool, _Poll]:
        try:
            apps = list_applications(api, namespace, selector)
        except KubeError as err:
            if not is_transient(err):
                raise
            return False, _Poll(error=f"transient API error, retrying: {err}")
        if not apps:
            return False, _Poll(error=f"no Applications match selector {selector!r} yet")
        return all(is_ready(a) for a in apps), _Poll(apps=apps)

    def describe(poll: _Poll) -> str:
        bad = [str(report(a)) for a in poll.apps if not is_ready(a)]
        return "; ".join([poll.error, *bad] if poll.error else bad)

    try:
        poll = until(
            check,
            timeout=timeout,
            what=f"Applications {selector!r}",
            describe=describe,
            interval=interval,
            clock=clock,
        )
    except WaitTimeoutError as err:
        last = _last_state(api, namespace, selector)
        return WaitResult(ok=False, reports=tuple(last), error=str(err))
    except KubeError as err:
        return WaitResult(ok=False, error=f"Argo CD API error: {err}")
    return WaitResult(ok=True, ready=tuple(str(a["metadata"]["name"]) for a in poll.apps))


def _last_state(api: KubeApi, namespace: str, selector: str) -> list[AppReport]:
    try:
        apps = list_applications(api, namespace, selector)
    except KubeError:
        return []
    return [report(a) for a in apps if not is_ready(a)]
