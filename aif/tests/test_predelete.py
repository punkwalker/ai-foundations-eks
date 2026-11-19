import unittest
from typing import Any

from botocore.exceptions import ClientError
from botocore.stub import Stubber
from fakes import offline_client

from aif import predelete

PROJECT = "ai-foundations-eks"
TAG = [{"Key": "project", "Value": PROJECT}]
LB_ARN = "arn:aws:elasticloadbalancing:us-west-2:111122223333:loadbalancer/net/lb/1"
NAME = "aif/x"
LB_ARN2 = "arn:aws:elasticloadbalancing:us-west-2:111122223333:loadbalancer/net/lb2/2"


class TagTest(unittest.TestCase):
    def test_has_project_tag_both_shapes(self) -> None:
        self.assertTrue(predelete.has_project_tag(TAG, PROJECT))
        self.assertTrue(predelete.has_project_tag([{"key": "project", "value": PROJECT}], PROJECT))
        self.assertFalse(predelete.has_project_tag([{"Key": "project", "Value": "x"}], PROJECT))


class S3Test(unittest.TestCase):
    def test_empty_bucket_deletes_versions_and_markers(self) -> None:
        s3 = offline_client("s3")
        with Stubber(s3) as stub:
            stub.add_response(
                "list_object_versions",
                {
                    "Versions": [{"Key": "a", "VersionId": "1"}],
                    "DeleteMarkers": [{"Key": "a", "VersionId": "2"}],
                    "IsTruncated": False,
                },
                {"Bucket": "weights"},
            )
            stub.add_response(
                "delete_objects",
                {},
                {
                    "Bucket": "weights",
                    "Delete": {
                        "Objects": [{"Key": "a", "VersionId": "1"}, {"Key": "a", "VersionId": "2"}],
                        "Quiet": True,
                    },
                },
            )
            result = predelete.empty_bucket(s3, "weights")
            stub.assert_no_pending_responses()
        self.assertEqual(result.done, ["weights"])

    def test_empty_bucket_that_is_gone(self) -> None:
        s3 = offline_client("s3")
        with Stubber(s3) as stub:
            stub.add_client_error("list_object_versions", "NoSuchBucket")
            self.assertEqual(predelete.empty_bucket(s3, "gone").done, ["gone"])

    def test_empty_bucket_other_error_raises(self) -> None:
        s3 = offline_client("s3")
        with Stubber(s3) as stub:
            stub.add_client_error("list_object_versions", "AccessDenied")
            with self.assertRaises(ClientError):
                predelete.empty_bucket(s3, "b")

    def test_empty_bucket_with_no_objects(self) -> None:
        s3 = offline_client("s3")
        with Stubber(s3) as stub:
            stub.add_response("list_object_versions", {"IsTruncated": False})
            predelete.empty_bucket(s3, "b")
            stub.assert_no_pending_responses()


class EcrSecretsTest(unittest.TestCase):
    def test_force_delete_repository(self) -> None:
        ecr = offline_client("ecr")
        with Stubber(ecr) as stub:
            stub.add_response("delete_repository", {}, {"repositoryName": "a/b", "force": True})
            stub.add_client_error("delete_repository", "RepositoryNotFoundException")
            stub.add_client_error("delete_repository", "AccessDeniedException")
            predelete.force_delete_ecr_repository(ecr, "a/b")
            predelete.force_delete_ecr_repository(ecr, "a/b")
            with self.assertRaises(ClientError):
                predelete.force_delete_ecr_repository(ecr, "a/b")

    def test_delete_secret_without_recovery(self) -> None:
        sm = offline_client("secretsmanager")
        with Stubber(sm) as stub:
            stub.add_response(
                "delete_secret", {}, {"SecretId": NAME, "ForceDeleteWithoutRecovery": True}
            )
            stub.add_client_error("delete_secret", "ResourceNotFoundException")
            stub.add_client_error("delete_secret", "AccessDeniedException")
            predelete.delete_secret_now(sm, NAME)
            predelete.delete_secret_now(sm, NAME)
            with self.assertRaises(ClientError):
                predelete.delete_secret_now(sm, NAME)


class VpcTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ec2 = offline_client("ec2")
        self.elb = offline_client("elbv2")

    def test_finds_load_balancers_and_enis(self) -> None:
        with Stubber(self.ec2) as ec2, Stubber(self.elb) as elb:
            elb.add_response(
                "describe_load_balancers",
                {
                    "LoadBalancers": [
                        {"LoadBalancerArn": LB_ARN, "VpcId": "vpc-1", "LoadBalancerName": "lb"},
                        {"LoadBalancerArn": LB_ARN2, "VpcId": "vpc-2"},
                    ]
                },
            )
            elb.add_response(
                "describe_tags",
                {"TagDescriptions": [{"ResourceArn": LB_ARN, "Tags": TAG}]},
                {"ResourceArns": [LB_ARN]},
            )
            ec2.add_response(
                "describe_network_interfaces",
                {
                    "NetworkInterfaces": [
                        {"NetworkInterfaceId": "eni-1", "Status": "available", "TagSet": []},
                        {"NetworkInterfaceId": "eni-2", "Status": "in-use", "Description": "node"},
                    ]
                },
                {"Filters": [{"Name": "vpc-id", "Values": ["vpc-1"]}]},
            )
            blockers = predelete.vpc_blockers(self.ec2, self.elb, "vpc-1", PROJECT)
        self.assertEqual(
            [(b.kind, b.id, b.tagged) for b in blockers],
            [
                ("load-balancer", LB_ARN, True),
                ("network-interface", "eni-1", False),
                ("network-interface", "eni-2", False),
            ],
        )

    def test_no_load_balancers(self) -> None:
        with Stubber(self.ec2) as ec2, Stubber(self.elb) as elb:
            elb.add_response("describe_load_balancers", {"LoadBalancers": []})
            ec2.add_response("describe_network_interfaces", {"NetworkInterfaces": []})
            self.assertEqual(predelete.vpc_blockers(self.ec2, self.elb, "vpc-1", PROJECT), [])

    def blockers(self) -> list[predelete.Blocker]:
        return [
            predelete.Blocker("load-balancer", LB_ARN, True, "lb"),
            predelete.Blocker("network-interface", "eni-1", False, "status=available"),
            predelete.Blocker("network-interface", "eni-2", True, "status=in-use node"),
            predelete.Blocker("network-interface", "eni-3", True, "status=available"),
        ]

    def test_clear_skips_untagged_and_attached(self) -> None:
        with Stubber(self.ec2) as ec2, Stubber(self.elb) as elb:
            elb.add_response("delete_load_balancer", {}, {"LoadBalancerArn": LB_ARN})
            ec2.add_client_error("delete_network_interface", "InvalidNetworkInterfaceID.NotFound")
            result = predelete.clear_vpc_blockers(self.ec2, self.elb, self.blockers(), False)
        self.assertEqual(result.done, [f"load-balancer {LB_ARN}", "network-interface eni-3"])
        self.assertEqual(len(result.skipped), 2)
        self.assertIn("--include-untagged", result.skipped[0])
        self.assertIn("still attached", result.skipped[1])

    def test_clear_includes_untagged_with_flag(self) -> None:
        with Stubber(self.ec2) as ec2, Stubber(self.elb) as elb:
            elb.add_response("delete_load_balancer", {})
            ec2.add_response("delete_network_interface", {}, {"NetworkInterfaceId": "eni-1"})
            ec2.add_client_error("delete_network_interface", "UnauthorizedOperation")
            with self.assertRaises(ClientError):
                predelete.clear_vpc_blockers(self.ec2, self.elb, self.blockers(), True)


class AgentRolesTest(unittest.TestCase):
    def role(self, name: str) -> dict[str, Any]:
        return {
            "Path": "/aif/agents/",
            "RoleName": name,
            "RoleId": "AROAEXAMPLEEXAMPLE1",
            "Arn": f"arn:aws:iam::111122223333:role/aif/agents/{name}",
            "CreateDate": "2026-11-19T00:00:00Z",
        }

    def test_deletes_tagged_roles_and_skips_untagged(self) -> None:
        iam = offline_client("iam")
        with Stubber(iam) as stub:
            stub.add_response(
                "list_roles",
                {"Roles": [self.role("agent-a"), self.role("other")]},
                {"PathPrefix": "/aif/agents/"},
            )
            stub.add_response("list_role_tags", {"Tags": TAG}, {"RoleName": "agent-a"})
            stub.add_response(
                "list_attached_role_policies",
                {"AttachedPolicies": [{"PolicyArn": "arn:aws:iam::111122223333:policy/p"}]},
            )
            stub.add_response("detach_role_policy", {})
            stub.add_response("list_role_policies", {"PolicyNames": ["inline"]})
            stub.add_response("delete_role_policy", {})
            stub.add_response(
                "list_instance_profiles_for_role",
                {
                    "InstanceProfiles": [
                        {
                            "Path": "/",
                            "InstanceProfileName": "ip",
                            "InstanceProfileId": "AIPAEXAMPLEEXAMPLE1",
                            "Arn": "arn:aws:iam::111122223333:instance-profile/ip",
                            "CreateDate": "2026-11-19T00:00:00Z",
                            "Roles": [],
                        }
                    ]
                },
            )
            stub.add_response("remove_role_from_instance_profile", {})
            stub.add_response("delete_role", {}, {"RoleName": "agent-a"})
            stub.add_response("list_role_tags", {"Tags": []}, {"RoleName": "other"})
            result = predelete.delete_agent_roles(iam, PROJECT, include_untagged=False)
            stub.assert_no_pending_responses()
        self.assertEqual(result.done, ["role agent-a"])
        self.assertIn("role other", result.skipped[0])

    def test_role_already_gone(self) -> None:
        iam = offline_client("iam")
        with Stubber(iam) as stub:
            stub.add_response("list_roles", {"Roles": [self.role("agent-a")]})
            stub.add_client_error("list_role_tags", "NoSuchEntity")
            result = predelete.delete_agent_roles(iam, PROJECT, include_untagged=True)
        self.assertEqual(result.done, ["role agent-a"])

    def test_role_error_raises(self) -> None:
        iam = offline_client("iam")
        with Stubber(iam) as stub:
            stub.add_response("list_roles", {"Roles": [self.role("agent-a")]})
            stub.add_client_error("list_role_tags", "AccessDenied")
            with self.assertRaises(ClientError):
                predelete.delete_agent_roles(iam, PROJECT, include_untagged=True)
