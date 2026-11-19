"""Stage bring-up for `aif up`: turn stages on in order and wait for their Applications.

A failed stage stops only its own group: later stages in the same
group are skipped, other groups carry on. A failed Base stage stops everything.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from aif.argocd import STAGE_LABEL, list_applicationsets, refresh_all, wait_for_apps
from aif.kube import KubeApi, KubeError, is_transient
from aif.labels import group_label, set_labels
from aif.stages import Stage
from aif.waiting import Clock, WaitTimeoutError, until

TIMEOUT_FACTOR = 2
"""Each stage may take twice its budget before aif gives up on it."""
MIN_TIMEOUT_SECONDS = 300
NO_BUDGET_TIMEOUT_SECONDS = 600


def stage_timeout(stage: Stage) -> float:
    if stage.budget_minutes is None:
        return NO_BUDGET_TIMEOUT_SECONDS
    return max(MIN_TIMEOUT_SECONDS, stage.budget_minutes * 60 * TIMEOUT_FACTOR)


@dataclass
class UpResult:
    ready: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.skipped


def format_plan(order: Sequence[Stage]) -> str:
    return "\n".join(
        f"{n}. stage {s.id} [{s.group}] {s.name}: timeout {stage_timeout(s):.0f}s"
        for n, s in enumerate(order, 1)
    )


def wait_for_argocd(
    hub: KubeApi,
    namespace: str,
    timeout: float,
    clock: Clock | None = None,
) -> str | None:
    """Wait until the Hub serves the Argo CD ApplicationSet API.

    Returns None when ready, or the reason it is not ready after `timeout`.
    """

    def check() -> tuple[bool, str]:
        try:
            list_applicationsets(hub, namespace)
        except KubeError as err:
            if not is_transient(err):
                raise
            return False, f"Argo CD API not ready: {err}"
        return True, ""

    try:
        until(check, timeout=timeout, what="Argo CD on the Hub", describe=str, clock=clock)
    except WaitTimeoutError as err:
        return str(err)
    except KubeError as err:
        return f"Argo CD API error: {err}"
    return None


def seed_group_labels(
    hub: KubeApi, namespace: str, secret: str, groups: Sequence[str]
) -> dict[str, bool]:
    """Turn on the label of each enabled group. Stage labels follow, one stage at a time."""
    return set_labels(hub, namespace, secret, {group_label(g): True for g in groups})


def bring_up(
    order: Sequence[Stage],
    hub: KubeApi,
    namespace: str,
    secret: str,
    log: Callable[[str], None] = print,
    clock: Clock | None = None,
) -> UpResult:
    result = UpResult()
    broken_groups: set[str] = set()
    for stage in order:
        if stage.group in broken_groups or "base" in broken_groups:
            result.skipped.append(stage.id)
            log(f"stage {stage.id}: skipped, an earlier stage in its group failed")
            continue
        set_labels(hub, namespace, secret, {stage.label: True, group_label(stage.group): True})
        selector = f"{STAGE_LABEL}={stage.id}"
        refresh_all(hub, namespace, selector)
        wait = wait_for_apps(hub, namespace, selector, stage_timeout(stage), clock=clock)
        if wait.ok:
            result.ready.append(stage.id)
            log(f"stage {stage.id}: ready ({wait.summary()})")
        else:
            result.failed[stage.id] = wait.summary()
            broken_groups.add(stage.group)
            log(f"stage {stage.id}: FAILED\n{wait.summary()}")
    return result
