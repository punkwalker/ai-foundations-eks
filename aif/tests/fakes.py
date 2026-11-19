"""Test fakes: an in-memory Kubernetes API, Hub, and a clock that never sleeps."""

import copy
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import botocore.session
from botocore import UNSIGNED
from botocore.config import Config

from aif.hub_api import CapabilityState, CapabilityType, ClusterState, LaunchInputs
from aif.kube import CRDS, KubeError, Obj, Resource
from aif.waiting import Clock

# One shared botocore session, so service models load once for all tests.
_BOTOCORE_SESSION = botocore.session.get_session()


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds

    def clock(self) -> Clock:
        return Clock(now=self.now, sleep=self.sleep)


def _matches(obj: Obj, selector: str) -> bool:
    labels = obj.get("metadata", {}).get("labels") or {}
    for term in filter(None, selector.split(",")):
        key, _, value = term.partition("=")
        if labels.get(key) != value:
            return False
    return True


def _merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if value is None:
            target.pop(key, None)
        elif isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = copy.deepcopy(value)


def crd(group: str, kind: str, plural: str, version: str = "v1alpha1") -> Obj:
    return {
        "metadata": {"name": f"{plural}.{group}"},
        "spec": {
            "group": group,
            "names": {"kind": kind, "plural": plural},
            "scope": "Namespaced",
            "versions": [{"name": version, "storage": True}],
        },
    }


def obj(name: str, namespace: str | None = None, **meta: Any) -> Obj:
    metadata: dict[str, Any] = {"name": name, **meta}
    if namespace:
        metadata["namespace"] = namespace
    return {"metadata": metadata}


class FakeKube:
    """Objects live in memory. Deleting an object with finalizers marks it, unless the
    object's name is in `instant_delete` or the test removes its finalizers."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], dict[tuple[str | None, str], Obj]] = {}
        self.secrets: dict[tuple[str, str], Obj] = {}
        self.fail_next: list[KubeError] = []
        self.calls: list[tuple[str, str]] = []
        self.stuck: set[str] = set()
        """Names that keep their finalizers when deleted, like a stuck controller."""

    # Test setup helpers

    def add(self, res: Resource, item: Obj) -> None:
        meta = item["metadata"]
        self.objects.setdefault((res.group, res.plural), {})[
            (meta.get("namespace"), meta["name"])
        ] = copy.deepcopy(item)

    def add_crd(self, group: str, kind: str, plural: str) -> Resource:
        self.add(CRDS, crd(group, kind, plural))
        return Resource(group, "v1alpha1", plural, kind)

    def store(self, res: Resource) -> dict[tuple[str | None, str], Obj]:
        return self.objects.setdefault((res.group, res.plural), {})

    def _maybe_fail(self, call: str, what: str) -> None:
        self.calls.append((call, what))
        if self.fail_next:
            raise self.fail_next.pop(0)

    # KubeApi

    def list_objects(
        self, res: Resource, namespace: str | None = None, label_selector: str = ""
    ) -> list[Obj]:
        self._maybe_fail("list", res.plural)
        if (res.group, res.plural) not in self.objects and res != CRDS:
            raise KubeError(404, "NotFound", "the server could not find the requested resource")
        return [
            copy.deepcopy(o)
            for (ns, _), o in sorted(self.store(res).items(), key=lambda kv: kv[0][1])
            if (namespace is None or ns == namespace) and _matches(o, label_selector)
        ]

    def get_object(self, res: Resource, name: str, namespace: str | None = None) -> Obj | None:
        self._maybe_fail("get", f"{res.plural}/{name}")
        found = self.store(res).get((namespace, name))
        return copy.deepcopy(found) if found else None

    def patch_object(
        self, res: Resource, name: str, body: Obj, namespace: str | None = None
    ) -> Obj:
        self._maybe_fail("patch", f"{res.plural}/{name}")
        key = (namespace, name)
        current = self.store(res).get(key)
        if current is None:
            raise KubeError(404, "NotFound", f"{name} not found")
        _merge(current, body)
        meta = current["metadata"]
        if meta.get("deletionTimestamp") and not meta.get("finalizers"):
            del self.store(res)[key]
        return copy.deepcopy(current)

    def delete_object(self, res: Resource, name: str, namespace: str | None = None) -> None:
        self._maybe_fail("delete", f"{res.plural}/{name}")
        key = (namespace, name)
        current = self.store(res).get(key)
        if current is None:
            return
        if name in self.stuck and current["metadata"].get("finalizers"):
            current["metadata"]["deletionTimestamp"] = "now"
            return
        del self.store(res)[key]

    def get_secret(self, namespace: str, name: str) -> Obj | None:
        self._maybe_fail("get-secret", name)
        found = self.secrets.get((namespace, name))
        return copy.deepcopy(found) if found else None

    def patch_secret(self, namespace: str, name: str, body: Obj) -> Obj:
        self._maybe_fail("patch-secret", name)
        current = self.secrets[(namespace, name)]
        _merge(current, body)
        return copy.deepcopy(current)

    def create_secret(self, namespace: str, body: Obj) -> Obj:
        name = body["metadata"]["name"]
        self._maybe_fail("create-secret", name)
        if (namespace, name) in self.secrets:
            raise KubeError(409, "AlreadyExists", f"secret {name} exists")
        self.secrets[(namespace, name)] = copy.deepcopy(body)
        return copy.deepcopy(body)

    def list_secrets(self, namespace: str, label_selector: str = "") -> list[Obj]:
        self._maybe_fail("list-secrets", namespace)
        return [
            copy.deepcopy(s)
            for (ns, _), s in sorted(self.secrets.items())
            if ns == namespace and _matches(s, label_selector)
        ]


def app(
    name: str,
    stage: str | None = None,
    sync: str = "Synced",
    health: str = "Healthy",
    finalizer: bool = True,
    namespace: str = "argocd",
    **status: Any,
) -> Obj:
    labels = {"platform.aws/stage": stage} if stage else {}
    meta: dict[str, Any] = {"labels": labels}
    if finalizer:
        meta["finalizers"] = ["resources-finalizer.argocd.argoproj.io"]
    item = obj(name, namespace, **meta)
    item["status"] = {"sync": {"status": sync}, "health": {"status": health}, **status}
    return item


def hub_secret(labels: dict[str, str] | None = None) -> Obj:
    return obj(
        "hub", "argocd", labels={"argocd.argoproj.io/secret-type": "cluster", **(labels or {})}
    )


def offline_client(service: str) -> Any:
    """A boto3 client that signs nothing, so it never looks for AWS keys.
    Use it only inside a botocore Stubber."""
    session = _BOTOCORE_SESSION
    return session.create_client(
        service, region_name="us-west-2", config=Config(signature_version=UNSIGNED)
    )


class FakeHub:
    """An in-memory HubApi. Each status call moves a CREATING or DELETING thing one step."""

    def __init__(self, region: str = "us-west-2") -> None:
        self.region = region
        self.calls: list[str] = []
        self.cluster: ClusterState | None = None
        self.caps: dict[CapabilityType, CapabilityState] = {}
        self.fail_cluster = False
        self.fail_capability: CapabilityType | None = None
        self.stuck = False
        """True keeps everything in its CREATING or DELETING state."""

    def ensure_platform(self, inputs: LaunchInputs) -> None:
        self.calls.append("ensure_platform")

    def ensure_hub(self, inputs: LaunchInputs) -> None:
        self.calls.append("ensure_hub")
        if self.cluster is None:
            arn = f"arn:aws:eks:{self.region}:111122223333:cluster/aif-hub"
            self.cluster = ClusterState("aif-hub", arn, "CREATING", "https://hub.example", "Q0E=")

    def hub_status(self) -> ClusterState | None:
        c = self.cluster
        if c is None or self.stuck:
            return c
        if c.status == "CREATING":
            self.cluster = replace(c, status="FAILED" if self.fail_cluster else "ACTIVE")
        elif c.status == "DELETING":
            self.cluster = None
        return c

    def ensure_capabilities(self, inputs: LaunchInputs, argocd_mode: str) -> None:
        self.calls.append(f"ensure_capabilities:{argocd_mode}")
        types: tuple[CapabilityType, ...] = (
            ("argocd", "kro", "ack") if argocd_mode == "capability" else ("kro", "ack")
        )
        for t in types:
            self.caps.setdefault(t, CapabilityState(t, f"arn:cap/{t}", "CREATING"))

    def capability_status(self) -> Mapping[CapabilityType, CapabilityState]:
        now = dict(self.caps)
        if not self.stuck:
            for t, cap in list(self.caps.items()):
                if cap.status == "CREATING":
                    bad = t == self.fail_capability
                    status = "CREATE_FAILED" if bad else "ACTIVE"
                    self.caps[t] = replace(cap, status=status, reason="quota" if bad else "")
                elif cap.status == "DELETING":
                    del self.caps[t]
        return now

    def delete_capabilities(self) -> None:
        self.calls.append("delete_capabilities")
        self.caps = {t: replace(c, status="DELETING") for t, c in self.caps.items()}

    def delete_hub(self) -> None:
        self.calls.append("delete_hub")
        if self.cluster is not None:
            self.cluster = replace(self.cluster, status="DELETING")

    def delete_platform(self) -> None:
        self.calls.append("delete_platform")


class FakeDevNetwork:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def ensure(self) -> LaunchInputs:
        self.calls.append("ensure")
        return launch_inputs()

    def delete(self) -> None:
        self.calls.append("delete")


def launch_inputs() -> LaunchInputs:
    return LaunchInputs(
        region="us-west-2",
        vpc_id="vpc-1",
        public_subnet_ids=("subnet-a",),
        private_subnet_ids=("subnet-b", "subnet-c"),
        admin_role_arn="arn:aws:iam::111122223333:role/Admin",
        ide_role_arn="arn:aws:iam::111122223333:role/ide",
        ide_security_group_id="sg-1",
        launch_stack_name="launch",
        idc_instance_arn="arn:aws:sso:::instance/ssoins-1",
    )
