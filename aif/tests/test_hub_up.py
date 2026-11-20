import unittest
from typing import Any
from unittest import mock

from botocore.stub import Stubber
from fakes import offline_client

from aif.config import ArgoCdMode
from aif.hub import capabilities, cluster, inputs, storage, up
from aif.hub import iam as hub_iam
from aif.hub.common import HubError
from aif.hub.inputs import HubInputs, Idc, InputError

TAGS = {"project": "ai-foundations-eks", "owner": "someone"}
IDC = Idc("arn:aws:sso:::instance/ssoins-0123456789abcdef", "d-0123456789")
FOUND = HubInputs(
    vpc_id="vpc-1",
    private_subnets=("subnet-1", "subnet-2"),
    admin_role_arn="arn:aws:iam::111122223333:role/admin",
    ide_role_arn="arn:aws:iam::111122223333:role/aif/ide",
    ide_security_group="sg-ide",
    idc=IDC,
)
HUB = {
    "arn": "arn:aws:eks:us-west-2:111122223333:cluster/aif-hub",
    "endpoint": "https://x.eks.amazonaws.com",
    "identity": {"oidc": {"issuer": "https://oidc.eks.us-west-2.amazonaws.com/id/ABC"}},
    "resourcesVpcConfig": {"clusterSecurityGroupId": "sg-c"},
}


class InputsTest(unittest.TestCase):
    def _params(self, values: dict[str, str]) -> Any:
        ssm = offline_client("ssm")
        stub = Stubber(ssm)
        stub.add_response(
            "get_parameters",
            {"Parameters": [{"Name": k, "Value": v} for k, v in values.items()]},
            {"Names": list(inputs.REQUIRED + inputs.OPTIONAL)},
        )
        stub.activate()
        return ssm

    def test_reads_every_input(self) -> None:
        ssm = self._params(
            {
                "/aif/vpc-id": "vpc-1",
                "/aif/private-subnet-ids": "subnet-1,subnet-2",
                "/aif/admin-role-arn": FOUND.admin_role_arn,
                "/aif/ide-role-arn": FOUND.ide_role_arn,
                "/aif/ide-security-group-id": "sg-ide",
                "/aif/idc-instance-arn": IDC.instance_arn,
                "/aif/identity-store-id": IDC.identity_store_id,
            }
        )
        self.assertEqual(inputs.read(ssm), FOUND)

    def test_names_missing_parameters(self) -> None:
        with self.assertRaisesRegex(InputError, "/aif/ide-security-group-id"):
            inputs.read(self._params({"/aif/vpc-id": "vpc-1"}))

    def test_idc_needs_both_parameters(self) -> None:
        ssm = self._params(
            {
                "/aif/vpc-id": "v",
                "/aif/private-subnet-ids": "s",
                "/aif/admin-role-arn": "a",
                "/aif/ide-role-arn": "i",
                "/aif/ide-security-group-id": "g",
                "/aif/idc-instance-arn": IDC.instance_arn,
            }
        )
        with self.assertRaisesRegex(InputError, "both"):
            inputs.read(ssm)


class OptionsTest(unittest.TestCase):
    def test_private_by_default(self) -> None:
        self.assertEqual(up.HubOptions.from_env({}).public_cidrs, ())

    def test_reads_cidrs_and_refuses_broad_ones(self) -> None:
        opts = up.HubOptions.from_env({"AIF_HUB_PUBLIC_CIDRS": "203.0.113.7/32, 198.51.100.0/24"})
        self.assertEqual(opts.public_cidrs, ("203.0.113.7/32", "198.51.100.0/24"))
        for bad in ("0.0.0.0/0", "10.0.0.0/8", "nope", "10.0.0.1/16"):
            with self.assertRaises(HubError, msg=bad):
                up.HubOptions.from_env({"AIF_HUB_PUBLIC_CIDRS": bad})


class UpTest(unittest.TestCase):
    def _clients(self) -> up.HubClients:
        sts = offline_client("sts")
        stub = Stubber(sts)
        stub.add_response(
            "get_caller_identity",
            {
                "Account": "111122223333",
                "Arn": "arn:aws:sts::111122223333:assumed-role/admin/s",
                "UserId": "x",
            },
        )
        stub.activate()
        others = {
            n: mock.Mock()
            for n in (
                "ssm",
                "iam",
                "kms",
                "logs",
                "eks",
                "ec2",
                "identitystore",
                "s3",
                "ecr",
                "secretsmanager",
            )
        }
        return up.HubClients(sts=sts, **others)

    def _run(
        self, found: HubInputs, mode: ArgoCdMode = "capability"
    ) -> tuple[up.HubResult, dict[str, mock.Mock]]:
        patches = {
            "read": mock.patch.object(inputs, "read", return_value=found),
            "boundary": mock.patch.object(hub_iam, "ensure_boundary", return_value="arn:boundary"),
            "role": mock.patch.object(
                hub_iam, "ensure_role", side_effect=lambda _c, spec, *_a: f"arn:{spec.name}"
            ),
            "key": mock.patch.object(cluster, "ensure_key", return_value="arn:key"),
            "logs": mock.patch.object(cluster, "ensure_log_group"),
            "cluster": mock.patch.object(cluster, "ensure_cluster", return_value=HUB),
            "ingress": mock.patch.object(cluster, "allow_api_from"),
            "entry": mock.patch.object(cluster, "ensure_admin_entry"),
            "groups": mock.patch.object(
                capabilities,
                "ensure_groups",
                return_value={"ADMIN": "ga", "EDITOR": "ge", "VIEWER": "gv"},
            ),
            "cap": mock.patch.object(
                capabilities,
                "ensure_capability",
                side_effect=lambda _e, _c, spec, *_a, **_k: f"arn:{spec.name}",
            ),
            "bucket": mock.patch.object(storage, "ensure_bucket"),
            "repo": mock.patch.object(storage, "ensure_repository"),
            "secret": mock.patch.object(storage, "ensure_secret"),
            "publish": mock.patch.object(storage, "publish"),
        }
        mocks = {k: p.start() for k, p in patches.items()}
        self.addCleanup(mock.patch.stopall)
        result = up.up(
            self._clients(), "us-west-2", TAGS, up.HubOptions(argocd_mode=mode), log=lambda _m: None
        )
        return result, mocks

    def test_capability_mode_creates_three_capabilities_and_groups(self) -> None:
        result, m = self._run(FOUND)
        caps = [c.args[2] for c in m["cap"].call_args_list]
        self.assertEqual([c.type for c in caps], ["ACK", "KRO", "ARGOCD"])
        argo = caps[2].configuration["argoCd"]
        self.assertEqual(len(argo["rbacRoleMappings"]), 3)
        self.assertEqual(argo["awsIdc"]["idcInstanceArn"], IDC.instance_arn)
        roles = [c.args[1].name for c in m["role"].call_args_list]
        self.assertIn("aif-argocd-capability", roles)
        self.assertIn("aif-ack-platform-clusters", roles)
        self.assertEqual(result.outputs["/aif/argocd/groups/viewer-id"], "gv")
        self.assertEqual(result.outputs["/aif/clusters/hub/oidc-issuer-id"], "ABC")
        self.assertEqual(result.outputs["/aif/argocd/mode"], "capability")
        tags = m["cluster"].call_args.args[2]
        self.assertEqual(tags, {**TAGS, "stage": "platform"})
        principals = [c.args[2] for c in m["entry"].call_args_list]
        self.assertEqual(principals, [FOUND.admin_role_arn, FOUND.ide_role_arn])

    def test_helm_mode_skips_argocd_and_groups(self) -> None:
        result, m = self._run(FOUND, "helm")
        self.assertEqual([c.args[2].type for c in m["cap"].call_args_list], ["ACK", "KRO"])
        m["groups"].assert_not_called()
        self.assertNotIn(
            "aif-argocd-capability", [c.args[1].name for c in m["role"].call_args_list]
        )
        self.assertEqual(result.outputs["/aif/argocd/mode"], "helm")

    def test_capability_mode_without_idc_stops_before_any_change(self) -> None:
        with self.assertRaisesRegex(HubError, "idc-instance-arn"):
            self._run(HubInputs(**{**FOUND.__dict__, "idc": None}))

    def test_cluster_gets_private_subnets_and_roles(self) -> None:
        _, m = self._run(FOUND)
        spec = m["cluster"].call_args.args[1]
        self.assertEqual(spec.subnets, FOUND.private_subnets)
        self.assertEqual(spec.role_arn, "arn:aif-hub-cluster")
        self.assertEqual(spec.public_cidrs, ())
        m["ingress"].assert_called_once()
        self.assertEqual(m["ingress"].call_args.args[1:3], ("sg-c", "sg-ide"))
