import unittest
from typing import Any

from botocore.stub import ANY, Stubber
from fakes import FakeClock, offline_client

from aif.hub import cluster
from aif.hub.common import HubError, Report
from aif.hub.names import CLUSTER_ADMIN_POLICY
from aif.waiting import WaitTimeoutError

TAGS = {"project": "ai-foundations-eks", "stage": "platform"}
KEY = "arn:aws:kms:us-west-2:111122223333:key/k"
SPEC = cluster.ClusterSpec(
    name="aif-hub",
    version="1.37",
    role_arn="arn:aws:iam::111122223333:role/aif/c",
    node_role_arn="arn:aws:iam::111122223333:role/aif/n",
    key_arn=KEY,
    subnets=("subnet-1", "subnet-2"),
)


def _cluster(
    status: str,
    public: bool = False,
    cidrs: list[str] | None = None,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    vpc: dict[str, Any] = {
        "endpointPublicAccess": public,
        "endpointPrivateAccess": True,
        "clusterSecurityGroupId": "sg-c",
    }
    if cidrs is not None:
        vpc["publicAccessCidrs"] = cidrs
    return {
        "cluster": {
            "name": "aif-hub",
            "status": status,
            "resourcesVpcConfig": vpc,
            "tags": tags if tags is not None else TAGS,
        }
    }


class KeyTest(unittest.TestCase):
    def test_existing_key_by_alias(self) -> None:
        kms = offline_client("kms")
        with Stubber(kms) as stub:
            stub.add_response(
                "describe_key",
                {"KeyMetadata": {"KeyId": "k", "Arn": KEY}},
                {"KeyId": "alias/aif-platform"},
            )
            self.assertEqual(cluster.ensure_key(kms, TAGS, Report()), KEY)

    def test_creates_rotates_and_aliases(self) -> None:
        kms = offline_client("kms")
        with Stubber(kms) as stub:
            stub.add_client_error("describe_key", "NotFoundException")
            stub.add_response(
                "create_key",
                {"KeyMetadata": {"KeyId": "k", "Arn": KEY}},
                {
                    "Description": ANY,
                    "KeyUsage": "ENCRYPT_DECRYPT",
                    "KeySpec": "SYMMETRIC_DEFAULT",
                    "Tags": [
                        {"TagKey": "project", "TagValue": "ai-foundations-eks"},
                        {"TagKey": "stage", "TagValue": "platform"},
                    ],
                },
            )
            stub.add_response("enable_key_rotation", {}, {"KeyId": "k"})
            stub.add_response(
                "create_alias", {}, {"AliasName": "alias/aif-platform", "TargetKeyId": "k"}
            )
            self.assertEqual(cluster.ensure_key(kms, TAGS, Report()), KEY)
            stub.assert_no_pending_responses()


class LogGroupTest(unittest.TestCase):
    def test_creates_or_keeps_and_sets_retention(self) -> None:
        logs = offline_client("logs")
        with Stubber(logs) as stub:
            stub.add_client_error("create_log_group", "ResourceAlreadyExistsException")
            stub.add_response(
                "put_retention_policy",
                {},
                {"logGroupName": "/aws/eks/aif-hub/cluster", "retentionInDays": 7},
            )
            rep = Report()
            cluster.ensure_log_group(logs, "aif-hub", TAGS, rep)
        self.assertEqual(rep.lines, ["exists   log group /aws/eks/aif-hub/cluster"])


class ClusterTest(unittest.TestCase):
    def test_creates_private_auto_mode_cluster_and_waits(self) -> None:
        eks = offline_client("eks")
        clock = FakeClock()
        with Stubber(eks) as stub:
            stub.add_client_error("describe_cluster", "ResourceNotFoundException")
            stub.add_response(
                "create_cluster",
                {},
                {
                    "name": "aif-hub",
                    "version": "1.37",
                    "roleArn": SPEC.role_arn,
                    "resourcesVpcConfig": {
                        "subnetIds": ["subnet-1", "subnet-2"],
                        "endpointPrivateAccess": True,
                        "endpointPublicAccess": False,
                    },
                    "kubernetesNetworkConfig": {
                        "ipFamily": "ipv4",
                        "elasticLoadBalancing": {"enabled": True},
                    },
                    "logging": ANY,
                    "accessConfig": {
                        "authenticationMode": "API",
                        "bootstrapClusterCreatorAdminPermissions": False,
                    },
                    "encryptionConfig": [{"resources": ["secrets"], "provider": {"keyArn": KEY}}],
                    "computeConfig": {
                        "enabled": True,
                        "nodePools": ["general-purpose", "system"],
                        "nodeRoleArn": SPEC.node_role_arn,
                    },
                    "storageConfig": {"blockStorage": {"enabled": True}},
                    "bootstrapSelfManagedAddons": False,
                    "upgradePolicy": {"supportType": "STANDARD"},
                    "tags": TAGS,
                },
            )
            stub.add_response("describe_cluster", _cluster("CREATING"))
            stub.add_response("describe_cluster", _cluster("ACTIVE"))
            found = cluster.ensure_cluster(eks, SPEC, TAGS, Report(), clock=clock.clock())
            stub.assert_no_pending_responses()
        self.assertEqual(found["status"], "ACTIVE")
        self.assertEqual(clock.sleeps, [15.0])

    def test_public_cidrs_are_applied_to_an_existing_cluster(self) -> None:
        eks = offline_client("eks")
        spec = cluster.ClusterSpec(**{**SPEC.__dict__, "public_cidrs": ("203.0.113.7/32",)})
        with Stubber(eks) as stub:
            stub.add_response("describe_cluster", _cluster("ACTIVE"))
            stub.add_response("describe_cluster", _cluster("ACTIVE"))
            stub.add_response(
                "update_cluster_config",
                {"update": {"id": "u1"}},
                {
                    "name": "aif-hub",
                    "resourcesVpcConfig": {
                        "endpointPrivateAccess": True,
                        "endpointPublicAccess": True,
                        "publicAccessCidrs": ["203.0.113.7/32"],
                    },
                },
            )
            stub.add_response("describe_update", {"update": {"id": "u1", "status": "Successful"}})
            rep = Report()
            cluster.ensure_cluster(eks, spec, TAGS, rep, clock=FakeClock().clock())
            stub.assert_no_pending_responses()
        self.assertEqual(rep.lines, ["updated  eks cluster aif-hub endpoint access"])

    def test_refuses_a_cluster_without_the_project_tag(self) -> None:
        eks = offline_client("eks")
        with Stubber(eks) as stub:
            stub.add_response("describe_cluster", _cluster("ACTIVE", tags={}))
            with self.assertRaisesRegex(HubError, "without the project tag"):
                cluster.ensure_cluster(eks, SPEC, TAGS, Report())

    def test_failed_cluster_stops_the_wait(self) -> None:
        eks = offline_client("eks")
        with Stubber(eks) as stub:
            stub.add_response("describe_cluster", _cluster("FAILED"))
            with self.assertRaisesRegex(HubError, "FAILED"):
                cluster.wait_active(eks, "aif-hub", FakeClock().clock(), 60)

    def test_wait_times_out_with_the_status(self) -> None:
        eks = offline_client("eks")
        with Stubber(eks) as stub:
            for _ in range(5):
                stub.add_response("describe_cluster", _cluster("CREATING"))
            with self.assertRaisesRegex(WaitTimeoutError, "status CREATING"):
                cluster.wait_active(eks, "aif-hub", FakeClock().clock(), 60)


class AccessTest(unittest.TestCase):
    def test_ingress_rule_is_idempotent(self) -> None:
        ec2 = offline_client("ec2")
        with Stubber(ec2) as stub:
            stub.add_client_error("authorize_security_group_ingress", "InvalidPermission.Duplicate")
            rep = Report()
            cluster.allow_api_from(ec2, "sg-c", "sg-ide", TAGS, rep)
        self.assertEqual(rep.lines, ["exists   ingress 443 sg-ide -> sg-c"])

    def test_admin_entry_created_and_policy_associated(self) -> None:
        eks = offline_client("eks")
        role = "arn:aws:iam::111122223333:role/admin"
        with Stubber(eks) as stub:
            stub.add_response(
                "create_access_entry",
                {},
                {"clusterName": "aif-hub", "principalArn": role, "type": "STANDARD", "tags": TAGS},
            )
            stub.add_response(
                "associate_access_policy",
                {},
                {
                    "clusterName": "aif-hub",
                    "principalArn": role,
                    "policyArn": CLUSTER_ADMIN_POLICY,
                    "accessScope": {"type": "cluster"},
                },
            )
            cluster.ensure_admin_entry(eks, "aif-hub", role, TAGS, Report())
            stub.assert_no_pending_responses()
