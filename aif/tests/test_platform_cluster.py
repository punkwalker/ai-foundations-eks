"""Schema test for the PlatformCluster RGD and its instances, with no cluster.

Checks every `${...}` reference, the ConfigMap keys the seed writes, the top-level spec
fields of each ACK kind (from the ACK CRDs: eks-controller v1.27.0, iam-controller
v1.12.0), and that each instance matches the RGD schema.
"""

import re
import unittest
from pathlib import Path
from typing import Any, ClassVar

import yaml

from aif.hub import seed
from aif.hub.inputs import HubInputs
from aif.hub.names import Param

ROOT = Path(__file__).resolve().parents[2]
RGD = ROOT / "platform" / "rgds" / "platform-cluster" / "rgd.yaml"
INSTANCES = sorted((ROOT / "platform" / "clusters").glob("*.yaml"))
EXPR = re.compile(r"\$\{([^}]*)\}")
REF = re.compile(r"\b([a-zA-Z]\w*)\.((?:\w+\.)*\w+)")

ACK_SPEC_FIELDS = {
    ("iam.services.k8s.aws/v1alpha1", "Role"): {
        "assumeRolePolicyDocument",
        "description",
        "inlinePolicies",
        "maxSessionDuration",
        "name",
        "path",
        "permissionsBoundary",
        "permissionsBoundaryRef",
        "policies",
        "policyRefs",
        "tags",
    },
    ("eks.services.k8s.aws/v1alpha1", "Cluster"): {
        "accessConfig",
        "bootstrapSelfManagedAddons",
        "clientRequestToken",
        "computeConfig",
        "controlPlaneScalingConfig",
        "deletionProtection",
        "encryptionConfig",
        "kubeAPIServerConfig",
        "kubeControllerManagerConfig",
        "kubeSchedulerConfig",
        "kubernetesNetworkConfig",
        "logging",
        "name",
        "outpostConfig",
        "remoteNetworkConfig",
        "resourcesVPCConfig",
        "roleARN",
        "roleRef",
        "storageConfig",
        "tags",
        "upgradePolicy",
        "version",
        "zonalShiftConfig",
    },
    ("eks.services.k8s.aws/v1alpha1", "AccessEntry"): {
        "accessPolicies",
        "clusterName",
        "clusterRef",
        "kubernetesGroups",
        "principalARN",
        "tags",
        "type",
        "username",
    },
    ("eks.services.k8s.aws/v1alpha1", "Capability"): {
        "clusterName",
        "clusterRef",
        "configuration",
        "deletePropagationPolicy",
        "name",
        "roleARN",
        "roleRef",
        "tags",
        "type",
    },
}
REQUIRED = {
    "Role": {"assumeRolePolicyDocument", "name"},
    "Cluster": {"name", "resourcesVPCConfig"},
    "AccessEntry": {"principalARN"},
    "Capability": {"name", "type", "deletePropagationPolicy"},
}


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text())


def _strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in _strings(k) + _strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings(v)]
    return []


def _schema_fields(rgd: Any) -> dict[str, str]:
    return {k: str(v) for k, v in rgd["spec"]["schema"]["spec"].items()}


class RgdTest(unittest.TestCase):
    rgd: ClassVar[Any] = _load(RGD)
    resources: ClassVar[list[Any]] = rgd["spec"]["resources"]
    ids: ClassVar[list[str]] = [r["id"] for r in resources]

    def test_ids_are_unique_and_camel_case(self) -> None:
        self.assertEqual(len(self.ids), len(set(self.ids)))
        for rid in self.ids:
            self.assertRegex(rid, r"^[a-z][a-zA-Z0-9]*$")

    def test_every_reference_resolves(self) -> None:
        fields = _schema_fields(self.rgd)
        known = {"schema", *self.ids}
        for text in _strings(self.rgd["spec"]):
            for expr in EXPR.findall(text):
                for root, path in REF.findall(expr):
                    if root in ("status", "data", "metadata"):
                        continue
                    self.assertIn(root, known, expr)
                    if root == "schema" and path.startswith("spec."):
                        self.assertIn(path.split(".")[1], fields, expr)

    def test_configmap_keys_match_the_seed(self) -> None:
        found = HubInputs("vpc", ("s1", "s2"), "arn:admin", "arn:ide", "sg", None)
        outputs = {
            Param.BOUNDARY_ARN: "b",
            Param.KEY_ARN: "k",
            Param.cluster("hub", "platform-clusters-role-arn"): "r",
            Param.cluster("hub", "argocd-role-arn"): "a",
        }
        keys = set(seed.manifests(outputs, found)[1]["data"])
        used = {
            m for t in _strings(self.rgd["spec"]) for m in re.findall(r"platform\.data\.(\w+)", t)
        }
        self.assertEqual(used - keys, set())

    def test_ack_templates_use_known_fields(self) -> None:
        for res in self.resources:
            tpl = res.get("template")
            if tpl is None:
                continue
            key = (tpl["apiVersion"], tpl["kind"])
            if key not in ACK_SPEC_FIELDS:
                continue
            spec = set(tpl["spec"])
            self.assertEqual(spec - ACK_SPEC_FIELDS[key], set(), res["id"])
            self.assertEqual(REQUIRED[tpl["kind"]] - spec, set(), res["id"])

    def test_capabilities_retain_and_roles_have_boundary(self) -> None:
        for res in self.resources:
            tpl = res.get("template", {})
            if tpl.get("kind") == "Capability":
                self.assertEqual(tpl["spec"]["deletePropagationPolicy"], "RETAIN")
            if tpl.get("kind") == "Role":
                self.assertEqual(tpl["spec"]["path"], "/aif/")
                self.assertIn("boundaryARN", tpl["spec"]["permissionsBoundary"])

    def test_cluster_is_private_and_gated_on_active(self) -> None:
        cluster = next(r for r in self.resources if r["id"] == "cluster")
        vpc = cluster["template"]["spec"]["resourcesVPCConfig"]
        self.assertTrue(vpc["endpointPrivateAccess"])
        self.assertFalse(vpc["endpointPublicAccess"])
        self.assertIn('"ACTIVE"', cluster["readyWhen"][0])

    def test_instances_match_the_schema(self) -> None:
        fields = _schema_fields(self.rgd)
        required = {k for k, v in fields.items() if "required=true" in v}
        self.assertEqual([p.stem for p in INSTANCES], ["dev", "prod"])
        for path in INSTANCES:
            inst = _load(path)
            self.assertEqual(inst["kind"], self.rgd["spec"]["schema"]["kind"], path)
            self.assertEqual(inst["apiVersion"], "platform.aws/v1alpha1", path)
            self.assertEqual(set(inst["spec"]) - set(fields), set(), path)
            self.assertEqual(required - set(inst["spec"]), set(), path)
            self.assertIn(inst["spec"]["env"], ("dev", "prod"))
            self.assertEqual(inst["metadata"]["namespace"], "platform-clusters")

    def test_seed_selector_targets_only_the_namespace(self) -> None:
        found = HubInputs("vpc", ("s1",), "a", "i", "sg", None)
        outputs = {
            Param.BOUNDARY_ARN: "b",
            Param.KEY_ARN: "k",
            Param.cluster("hub", "platform-clusters-role-arn"): "r",
        }
        selector = seed.manifests(outputs, found)[2]
        self.assertEqual(
            selector["spec"], {"arn": "r", "namespaceSelector": {"names": ["platform-clusters"]}}
        )
