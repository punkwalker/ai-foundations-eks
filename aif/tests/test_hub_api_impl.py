"""BotoHub and BotoDevNetwork join the Hub steps. Every client is a mock; nothing goes out."""

import unittest
from unittest import mock

from botocore.exceptions import ClientError

from aif.hub import api, make_dev_network, make_hub, remove
from aif.hub.common import Report
from aif.hub.policies import Scope
from aif.hub.up import HubOptions
from aif.hub_api import HubError, LaunchInputs

TAGS = {"project": "ai-foundations-eks"}
SCOPE = Scope("aws", "111122223333", "us-west-2")
INPUTS = LaunchInputs(
    region="us-west-2",
    vpc_id="vpc-1",
    public_subnet_ids=("subnet-a",),
    private_subnet_ids=("subnet-b", "subnet-c"),
    admin_role_arn="arn:aws:iam::111122223333:role/admin",
    ide_role_arn="arn:aws:iam::111122223333:role/aif/ide",
    ide_security_group_id="sg-1",
    launch_stack_name="stack",
    idc_instance_arn="arn:aws:sso:::instance/ssoins-1",
)


def error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "Op")


def hub(mode: str = "capability") -> tuple[api.BotoHub, mock.MagicMock]:
    clients = mock.MagicMock()
    clients.sts.get_caller_identity.return_value = {
        "Arn": "arn:aws:sts::111122223333:assumed-role/admin/me"
    }
    options = HubOptions(argocd_mode="capability" if mode == "capability" else "helm")
    return api.BotoHub(clients, "us-west-2", TAGS, options, log=lambda _line: None), clients


class BotoHubTest(unittest.TestCase):
    def test_steps_delegate_to_the_builder(self) -> None:
        h, clients = hub()
        clients.ssm.get_parameter.return_value = {"Parameter": {"Value": "d-1"}}
        with mock.patch("aif.hub.api.HubBuilder") as builder:
            h.ensure_platform(INPUTS)
            h.ensure_hub(INPUTS)
            h.ensure_capabilities(INPUTS, "capability")
        b = builder.return_value
        b.platform.assert_called_once()
        b.cluster.assert_called_once()
        b.capabilities.assert_called_once()
        b.publish.assert_called_once()
        self.assertEqual(b.check.call_args.args[0].idc.identity_store_id, "d-1")

    def test_a_new_mode_rebuilds_the_builder(self) -> None:
        h, clients = hub()
        clients.ssm.get_parameter.side_effect = error("ParameterNotFound")
        with mock.patch("aif.hub.api.HubBuilder") as builder:
            h.ensure_capabilities(INPUTS, "helm")
        self.assertEqual(builder.call_args.args[3].argocd_mode, "helm")
        with self.assertRaisesRegex(HubError, "capability or helm"):
            api._mode("other")

    def test_hub_status(self) -> None:
        h, clients = hub()
        clients.eks.describe_cluster.return_value = {
            "cluster": {"name": "aif-hub", "arn": "a", "status": "ACTIVE", "endpoint": "e"}
        }
        state = h.hub_status()
        self.assertEqual(
            (state.status, state.endpoint, state.ca_data) if state else None, ("ACTIVE", "e", "")
        )
        clients.eks.describe_cluster.side_effect = error("ResourceNotFoundException")
        self.assertIsNone(h.hub_status())
        clients.eks.describe_cluster.side_effect = error("AccessDenied")
        with self.assertRaises(ClientError):
            h.hub_status()

    def test_capability_status_and_delete(self) -> None:
        h, clients = hub()
        clients.eks.list_capabilities.return_value = {
            "capabilities": [{"capabilityName": "ack"}, {"capabilityName": "other"}]
        }
        clients.eks.describe_capability.return_value = {
            "capability": {
                "arn": "arn:ack",
                "status": "CREATE_FAILED",
                "health": {"issues": [{"message": "bad role"}]},
            }
        }
        states = h.capability_status()
        self.assertEqual(list(states), ["ack"])
        self.assertEqual(states["ack"].reason, "bad role")
        h.delete_capabilities()
        clients.eks.delete_capability.assert_called_once_with(
            clusterName="aif-hub", capabilityName="ack"
        )
        h.delete_hub()
        clients.eks.delete_cluster.assert_called_once_with(name="aif-hub")

    def test_delete_platform_runs_every_remover(self) -> None:
        h, clients = hub()
        clients.ssm.get_parameter.return_value = {"Parameter": {"Value": "d-1"}}
        clients.ssm.get_paginator.return_value.paginate.return_value = [
            {"Parameters": [{"Name": "/aif/platform/key-arn"}]}
        ]
        names = (
            "delete_role",
            "delete_boundary",
            "delete_key",
            "delete_bucket",
            "delete_repositories",
            "delete_secrets",
            "delete_log_group",
            "delete_groups",
            "delete_outputs",
        )
        with mock.patch.multiple(remove, **{n: mock.DEFAULT for n in names}) as fakes:
            h.delete_platform()
        self.assertEqual(fakes["delete_role"].call_count, len(api.ROLE_NAMES))
        for name in names[1:]:
            fakes[name].assert_called_once()
        self.assertEqual(fakes["delete_outputs"].call_args.args[1], ["/aif/platform/key-arn"] * 4)

    def test_delete_platform_skips_groups_without_a_store(self) -> None:
        h, clients = hub()
        clients.ssm.get_parameter.side_effect = error("ParameterNotFound")
        clients.ssm.get_paginator.return_value.paginate.return_value = []
        with mock.patch.multiple(
            remove,
            delete_role=mock.DEFAULT,
            delete_boundary=mock.DEFAULT,
            delete_key=mock.DEFAULT,
            delete_bucket=mock.DEFAULT,
            delete_repositories=mock.DEFAULT,
            delete_secrets=mock.DEFAULT,
            delete_log_group=mock.DEFAULT,
            delete_groups=mock.DEFAULT,
            delete_outputs=mock.DEFAULT,
        ) as fakes:
            h.delete_platform()
        fakes["delete_groups"].assert_not_called()


class DevNetworkTest(unittest.TestCase):
    def _net(self, arn: str) -> tuple[api.BotoDevNetwork, mock.MagicMock]:
        clients = mock.MagicMock()
        clients.sts.get_caller_identity.return_value = {"Arn": arn}
        return api.BotoDevNetwork(clients, "us-west-2", TAGS, log=lambda _line: None), clients

    def test_admin_role_comes_from_the_caller(self) -> None:
        net, _ = self._net("arn:aws:sts::111122223333:assumed-role/admin/me")
        self.assertEqual(net._admin_role(), "arn:aws:iam::111122223333:role/admin")

    def test_a_user_is_refused(self) -> None:
        net, _ = self._net("arn:aws:iam::111122223333:user/me")
        with self.assertRaisesRegex(HubError, "assumed IAM role"):
            net._admin_role()

    def test_ensure_and_delete(self) -> None:
        net, clients = self._net("arn:aws:sts::111122223333:assumed-role/admin/me")
        report = Report()
        report.created("vpc")
        with (
            mock.patch("aif.devnet.up", return_value=report) as up,
            mock.patch("aif.launch.read_inputs", return_value=INPUTS),
            mock.patch("aif.devnet.down", return_value=report) as down,
        ):
            self.assertEqual(net.ensure(), INPUTS)
            net.delete()
        self.assertTrue(up.call_args.kwargs["dev_standin"])
        self.assertTrue(down.call_args.kwargs["dev_standin"])
        self.assertEqual(up.call_args.args[3], "arn:aws:iam::111122223333:role/admin")
        self.assertIs(up.call_args.args[0], clients)


class FactoryTest(unittest.TestCase):
    def test_factories_build_the_boto_classes(self) -> None:
        with (
            mock.patch("aif.hub.up.HubClients.create"),
            mock.patch("aif.devnet.DevNetClients.create"),
            mock.patch.dict("os.environ", {"AIF_ARGOCD_MODE": "helm"}),
        ):
            built = make_hub("us-west-2", TAGS)
            self.assertEqual(built.options.argocd_mode, "helm")
            self.assertIsInstance(make_dev_network("us-west-2", TAGS), api.BotoDevNetwork)


def tagged(**kw: str) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in kw.items()]


class RemoveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.rep = Report()

    def test_role_is_deleted_only_when_ours(self) -> None:
        iam = mock.MagicMock()
        iam.list_role_tags.return_value = {"Tags": tagged(project="ai-foundations-eks")}
        iam.list_attached_role_policies.return_value = {"AttachedPolicies": [{"PolicyArn": "p"}]}
        iam.list_role_policies.return_value = {"PolicyNames": ["inline"]}
        remove.delete_role(iam, "r", TAGS, self.rep)
        iam.detach_role_policy.assert_called_once_with(RoleName="r", PolicyArn="p")
        iam.delete_role_policy.assert_called_once()
        iam.delete_role.assert_called_once_with(RoleName="r")
        iam.reset_mock()
        iam.list_role_tags.return_value = {"Tags": tagged(project="other")}
        remove.delete_role(iam, "r", TAGS, self.rep)
        iam.delete_role.assert_not_called()
        iam.list_role_tags.side_effect = error("NoSuchEntity")
        remove.delete_role(iam, "r", TAGS, self.rep)
        iam.list_role_tags.side_effect = error("AccessDenied")
        with self.assertRaises(ClientError):
            remove.delete_role(iam, "r", TAGS, self.rep)

    def test_boundary(self) -> None:
        iam = mock.MagicMock()
        iam.list_policy_tags.return_value = {"Tags": tagged(project="ai-foundations-eks")}
        iam.list_policy_versions.return_value = {
            "Versions": [
                {"VersionId": "v1", "IsDefaultVersion": False},
                {"VersionId": "v2", "IsDefaultVersion": True},
            ]
        }
        remove.delete_boundary(iam, SCOPE, TAGS, self.rep)
        iam.delete_policy_version.assert_called_once_with(
            PolicyArn=SCOPE.boundary_arn, VersionId="v1"
        )
        iam.delete_policy.assert_called_once()
        iam.reset_mock()
        iam.list_policy_tags.return_value = {"Tags": []}
        remove.delete_boundary(iam, SCOPE, TAGS, self.rep)
        iam.delete_policy.assert_not_called()
        iam.list_policy_tags.side_effect = error("NoSuchEntity")
        remove.delete_boundary(iam, SCOPE, TAGS, self.rep)

    def test_key(self) -> None:
        kms = mock.MagicMock()
        kms.describe_key.return_value = {"KeyMetadata": {"KeyId": "k", "KeyState": "Enabled"}}
        kms.list_resource_tags.return_value = {
            "Tags": [{"TagKey": "project", "TagValue": "ai-foundations-eks"}]
        }
        remove.delete_key(kms, TAGS, self.rep)
        kms.schedule_key_deletion.assert_called_once_with(KeyId="k", PendingWindowInDays=7)
        kms.reset_mock()
        kms.describe_key.return_value = {
            "KeyMetadata": {"KeyId": "k", "KeyState": "PendingDeletion"}
        }
        remove.delete_key(kms, TAGS, self.rep)
        kms.schedule_key_deletion.assert_not_called()
        kms.list_resource_tags.return_value = {"Tags": []}
        remove.delete_key(kms, TAGS, self.rep)
        kms.delete_alias.assert_called_once()
        kms.describe_key.side_effect = error("NotFoundException")
        remove.delete_key(kms, TAGS, self.rep)

    def test_bucket(self) -> None:
        s3 = mock.MagicMock()
        s3.get_bucket_tagging.return_value = {"TagSet": tagged(project="ai-foundations-eks")}
        s3.get_paginator.return_value.paginate.return_value = [
            {"Versions": [{"Key": "a", "VersionId": "1"}], "DeleteMarkers": []},
            {},
        ]
        remove.delete_bucket(s3, "111122223333", "us-west-2", TAGS, self.rep)
        s3.delete_objects.assert_called_once()
        s3.delete_bucket.assert_called_once_with(Bucket="aif-models-111122223333-us-west-2")
        s3.reset_mock()
        s3.get_bucket_tagging.return_value = {"TagSet": []}
        remove.delete_bucket(s3, "111122223333", "us-west-2", TAGS, self.rep)
        s3.get_bucket_tagging.side_effect = error("NoSuchTagSet")
        remove.delete_bucket(s3, "111122223333", "us-west-2", TAGS, self.rep)
        s3.get_bucket_tagging.side_effect = error("NoSuchBucket")
        remove.delete_bucket(s3, "111122223333", "us-west-2", TAGS, self.rep)
        s3.delete_bucket.assert_not_called()

    def test_repositories_and_secrets(self) -> None:
        ecr, sm = mock.MagicMock(), mock.MagicMock()
        ecr.describe_repositories.return_value = {"repositories": [{"repositoryArn": "arn"}]}
        ecr.list_tags_for_resource.return_value = {"tags": tagged(project="ai-foundations-eks")}
        remove.delete_repositories(ecr, TAGS, self.rep)
        self.assertEqual(ecr.delete_repository.call_count, 2)
        ecr.reset_mock()
        ecr.list_tags_for_resource.return_value = {"tags": []}
        remove.delete_repositories(ecr, TAGS, self.rep)
        ecr.describe_repositories.side_effect = error("RepositoryNotFoundException")
        remove.delete_repositories(ecr, TAGS, self.rep)
        ecr.delete_repository.assert_not_called()
        sm.describe_secret.return_value = {"Tags": tagged(project="ai-foundations-eks")}
        remove.delete_secrets(sm, TAGS, self.rep)
        self.assertEqual(sm.delete_secret.call_count, 2)
        sm.reset_mock()
        sm.describe_secret.return_value = {}
        remove.delete_secrets(sm, TAGS, self.rep)
        sm.describe_secret.side_effect = error("ResourceNotFoundException")
        remove.delete_secrets(sm, TAGS, self.rep)
        sm.delete_secret.assert_not_called()

    def test_log_group_groups_and_outputs(self) -> None:
        logs, ids, ssm = mock.MagicMock(), mock.MagicMock(), mock.MagicMock()
        remove.delete_log_group(logs, self.rep)
        logs.delete_log_group.side_effect = error("ResourceNotFoundException")
        remove.delete_log_group(logs, self.rep)
        ids.get_group_id.return_value = {"GroupId": "g"}
        remove.delete_groups(ids, "d-1", self.rep)
        self.assertEqual(ids.delete_group.call_count, 3)
        ids.reset_mock()
        ids.get_group_id.side_effect = error("ResourceNotFoundException")
        remove.delete_groups(ids, "d-1", self.rep)
        ids.delete_group.assert_not_called()
        remove.delete_outputs(ssm, [f"/aif/x/{i}" for i in range(11)], self.rep)
        self.assertEqual(ssm.delete_parameters.call_count, 2)
        self.assertTrue(any("deleted" in line for line in self.rep.lines))


if __name__ == "__main__":
    unittest.main()
