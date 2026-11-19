import unittest
from typing import Any
from unittest import mock

from fakes import crd

from aif import kube
from aif.kube import KubeError, Resource

NS_RES = Resource("g.io", "v1", "things", "Thing")
CLUSTER_RES = Resource("g.io", "v1", "globals", "Global", namespaced=False)


class FakeApiError(Exception):
    def __init__(self, status: int, reason: str = "r", body: str = "") -> None:
        self.status = status
        self.reason = reason
        self.body = body


class TransientTest(unittest.TestCase):
    def test_transient_cases(self) -> None:
        transient = [
            KubeError(0, "ConnectionError"),
            KubeError(503, "Unavailable"),
            KubeError(404, "NotFound", "the server could not find the requested resource"),
            KubeError(500, "Internal", 'failed calling webhook "validate.kyverno.svc"'),
        ]
        for err in transient:
            with self.subTest(err=str(err)):
                self.assertTrue(kube.is_transient(err))

    def test_permanent_cases(self) -> None:
        for err in (KubeError(403, "Forbidden"), KubeError(422, "Invalid", "bad spec")):
            with self.subTest(err=str(err)):
                self.assertFalse(kube.is_transient(err))


class CrdTest(unittest.TestCase):
    def test_resource_from_crd_prefers_storage_version(self) -> None:
        item = crd("s3.services.k8s.aws", "Bucket", "buckets")
        item["spec"]["versions"] = [{"name": "v1beta1"}, {"name": "v1alpha1", "storage": True}]
        res = kube.resource_from_crd(item)
        self.assertEqual(res, Resource("s3.services.k8s.aws", "v1alpha1", "buckets", "Bucket"))
        self.assertEqual(res.api_version, "s3.services.k8s.aws/v1alpha1")

    def test_first_version_without_storage_flag(self) -> None:
        item = crd("g", "K", "ks")
        item["spec"]["versions"] = [{"name": "v2"}]
        item["spec"]["scope"] = "Cluster"
        res = kube.resource_from_crd(item)
        self.assertEqual((res.version, res.namespaced), ("v2", False))


class ClientKubeApiTest(unittest.TestCase):
    """The wrapper calls the right client method and maps errors."""

    def setUp(self) -> None:
        patcher_cfg = mock.patch("kubernetes.config.new_client_from_config")
        patcher_custom = mock.patch("kubernetes.client.CustomObjectsApi")
        patcher_core = mock.patch("kubernetes.client.CoreV1Api")
        patcher_exc = mock.patch("kubernetes.client.exceptions.ApiException", FakeApiError)
        self.cfg = patcher_cfg.start()
        self.custom = patcher_custom.start().return_value
        self.core = patcher_core.start().return_value
        patcher_exc.start()
        self.addCleanup(mock.patch.stopall)
        self.api = kube.ClientKubeApi("hub")

    def test_uses_context(self) -> None:
        self.cfg.assert_called_once_with(context="hub")

    def test_list_namespaced_and_cluster(self) -> None:
        self.custom.list_namespaced_custom_object.return_value = {"items": [{"a": 1}]}
        self.custom.list_cluster_custom_object.return_value = {"items": []}
        self.assertEqual(self.api.list_objects(NS_RES, "ns", "x=y"), [{"a": 1}])
        self.custom.list_namespaced_custom_object.assert_called_once_with(
            "g.io", "v1", "ns", "things", label_selector="x=y"
        )
        self.assertEqual(self.api.list_objects(CLUSTER_RES, "ns"), [])
        self.custom.list_cluster_custom_object.assert_called_once()

    def test_get_maps_404_to_none(self) -> None:
        self.custom.get_namespaced_custom_object.side_effect = FakeApiError(404)
        self.assertIsNone(self.api.get_object(NS_RES, "x", "ns"))
        self.custom.get_cluster_custom_object.return_value = {"metadata": {"name": "g"}}
        self.assertEqual(self.api.get_object(CLUSTER_RES, "g"), {"metadata": {"name": "g"}})

    def test_errors_become_kube_errors(self) -> None:
        self.custom.list_cluster_custom_object.side_effect = FakeApiError(403, "Forbidden", "no")
        with self.assertRaises(KubeError) as ctx:
            self.api.list_objects(CLUSTER_RES)
        self.assertEqual((ctx.exception.status, ctx.exception.reason), (403, "Forbidden"))
        self.custom.list_cluster_custom_object.side_effect = ConnectionRefusedError("refused")
        with self.assertRaises(KubeError) as ctx:
            self.api.list_objects(CLUSTER_RES)
        self.assertEqual(ctx.exception.status, 0)

    def test_get_reraises_other_errors(self) -> None:
        self.custom.get_cluster_custom_object.side_effect = FakeApiError(500)
        with self.assertRaises(KubeError):
            self.api.get_object(CLUSTER_RES, "g")

    def test_patch_and_delete(self) -> None:
        body: dict[str, Any] = {"metadata": {}}
        self.custom.patch_namespaced_custom_object.return_value = {"ok": 1}
        self.custom.patch_cluster_custom_object.return_value = {"ok": 2}
        self.assertEqual(self.api.patch_object(NS_RES, "x", body, "ns"), {"ok": 1})
        self.assertEqual(self.api.patch_object(CLUSTER_RES, "x", body), {"ok": 2})
        self.custom.delete_namespaced_custom_object.side_effect = FakeApiError(404)
        self.api.delete_object(NS_RES, "x", "ns")
        self.api.delete_object(CLUSTER_RES, "x")
        self.custom.delete_cluster_custom_object.assert_called_once()

    def test_secrets(self) -> None:
        self.core.api_client.sanitize_for_serialization.side_effect = lambda o: {"s": o}
        self.core.read_namespaced_secret.return_value = "secret-object"
        self.assertEqual(self.api.get_secret("argocd", "hub"), {"s": "secret-object"})
        self.core.read_namespaced_secret.side_effect = FakeApiError(404)
        self.assertIsNone(self.api.get_secret("argocd", "hub"))
        self.core.patch_namespaced_secret.return_value = "patched"
        self.assertEqual(self.api.patch_secret("argocd", "hub", {}), {"s": "patched"})
