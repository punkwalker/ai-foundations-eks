"""A small Kubernetes interface for aif, and its implementation on the Kubernetes client.

Modules depend on the `KubeApi` protocol, so tests use an in-memory fake.
Objects are plain dicts in the same shape the API returns.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

Obj = dict[str, Any]

CRD_GROUP = "apiextensions.k8s.io"


class KubeError(Exception):
    """An API call failed. `status` is the HTTP status, 0 when there was no response."""

    def __init__(self, status: int, reason: str, message: str = "") -> None:
        super().__init__(f"{status} {reason}: {message}".strip())
        self.status = status
        self.reason = reason
        self.message = message


TRANSIENT_MARKERS = (
    "failed calling webhook",
    "no endpoints available for service",
    "connection refused",
    "the server could not find the requested resource",
    "etcdserver: request timed out",
)


def is_transient(err: KubeError) -> bool:
    """True for errors that go away on their own: a CRD or webhook that is not ready yet."""
    if err.status in (0, 429, 502, 503, 504):
        return True
    text = f"{err.reason} {err.message}".lower()
    return any(marker in text for marker in TRANSIENT_MARKERS)


@dataclass(frozen=True)
class Resource:
    """One custom resource type, as group, version and plural name."""

    group: str
    version: str
    plural: str
    kind: str = ""
    namespaced: bool = True

    @property
    def api_version(self) -> str:
        return f"{self.group}/{self.version}"


class KubeApi(Protocol):
    """The calls aif makes. Implemented by `ClientKubeApi` and by test fakes."""

    def list_objects(
        self, res: Resource, namespace: str | None = None, label_selector: str = ""
    ) -> list[Obj]: ...

    def get_object(self, res: Resource, name: str, namespace: str | None = None) -> Obj | None: ...

    def patch_object(
        self, res: Resource, name: str, body: Obj, namespace: str | None = None
    ) -> Obj: ...

    def delete_object(self, res: Resource, name: str, namespace: str | None = None) -> None: ...

    def get_secret(self, namespace: str, name: str) -> Obj | None: ...

    def patch_secret(self, namespace: str, name: str, body: Obj) -> Obj: ...

    def create_secret(self, namespace: str, body: Obj) -> Obj: ...

    def list_secrets(self, namespace: str, label_selector: str = "") -> list[Obj]: ...


CRDS = Resource(CRD_GROUP, "v1", "customresourcedefinitions", "CustomResourceDefinition", False)


def served_version(crd: Mapping[str, Any]) -> str:
    """The storage version of a CRD, or the first served one."""
    versions = crd["spec"]["versions"]
    for v in versions:
        if v.get("storage"):
            return str(v["name"])
    return str(versions[0]["name"])


def resource_from_crd(crd: Mapping[str, Any]) -> Resource:
    spec = crd["spec"]
    return Resource(
        group=spec["group"],
        version=served_version(crd),
        plural=spec["names"]["plural"],
        kind=spec["names"]["kind"],
        namespaced=spec.get("scope", "Namespaced") == "Namespaced",
    )


def list_crds(api: KubeApi) -> list[Resource]:
    return [resource_from_crd(crd) for crd in api.list_objects(CRDS)]


class ClientKubeApi:
    """`KubeApi` on the official Kubernetes Python client, for one kubeconfig context."""

    def __init__(self, context: str) -> None:
        from kubernetes import client, config

        api_client = config.new_client_from_config(context=context)
        self._custom = client.CustomObjectsApi(api_client)
        self._core = client.CoreV1Api(api_client)
        self._exc = client.exceptions.ApiException
        self.context = context

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except self._exc as err:
            raise KubeError(err.status or 0, str(err.reason), str(err.body or "")) from err
        except OSError as err:  # connection errors from urllib3 are OSError subclasses
            raise KubeError(0, type(err).__name__, str(err)) from err

    def _missing(self, fn: Any, *args: Any) -> Any:
        try:
            return self._call(fn, *args)
        except KubeError as err:
            if err.status == 404:
                return None
            raise

    def list_objects(
        self, res: Resource, namespace: str | None = None, label_selector: str = ""
    ) -> list[Obj]:
        c = self._custom
        if namespace and res.namespaced:
            out = self._call(
                c.list_namespaced_custom_object,
                res.group,
                res.version,
                namespace,
                res.plural,
                label_selector=label_selector,
            )
        else:
            out = self._call(
                c.list_cluster_custom_object,
                res.group,
                res.version,
                res.plural,
                label_selector=label_selector,
            )
        return list(out.get("items", []))

    def get_object(self, res: Resource, name: str, namespace: str | None = None) -> Obj | None:
        c = self._custom
        if namespace and res.namespaced:
            out = self._missing(
                c.get_namespaced_custom_object, res.group, res.version, namespace, res.plural, name
            )
        else:
            out = self._missing(
                c.get_cluster_custom_object, res.group, res.version, res.plural, name
            )
        return None if out is None else dict(out)

    def patch_object(
        self, res: Resource, name: str, body: Obj, namespace: str | None = None
    ) -> Obj:
        c = self._custom
        if namespace and res.namespaced:
            out = self._call(
                c.patch_namespaced_custom_object,
                res.group,
                res.version,
                namespace,
                res.plural,
                name,
                body,
            )
        else:
            out = self._call(
                c.patch_cluster_custom_object, res.group, res.version, res.plural, name, body
            )
        return dict(out)

    def delete_object(self, res: Resource, name: str, namespace: str | None = None) -> None:
        c = self._custom
        if namespace and res.namespaced:
            self._missing(
                c.delete_namespaced_custom_object,
                res.group,
                res.version,
                namespace,
                res.plural,
                name,
            )
        else:
            self._missing(c.delete_cluster_custom_object, res.group, res.version, res.plural, name)

    def get_secret(self, namespace: str, name: str) -> Obj | None:
        out = self._missing(self._core.read_namespaced_secret, name, namespace)
        if out is None:
            return None
        return dict(self._core.api_client.sanitize_for_serialization(out))

    def patch_secret(self, namespace: str, name: str, body: Obj) -> Obj:
        out = self._call(self._core.patch_namespaced_secret, name, namespace, body)
        return dict(self._core.api_client.sanitize_for_serialization(out))

    def create_secret(self, namespace: str, body: Obj) -> Obj:
        out = self._call(self._core.create_namespaced_secret, namespace, body)
        return dict(self._core.api_client.sanitize_for_serialization(out))

    def list_secrets(self, namespace: str, label_selector: str = "") -> list[Obj]:
        out = self._call(
            self._core.list_namespaced_secret, namespace, label_selector=label_selector
        )
        items = self._core.api_client.sanitize_for_serialization(out).get("items", [])
        return [dict(i) for i in items]
