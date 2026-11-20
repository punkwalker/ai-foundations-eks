import unittest

from botocore.stub import ANY, Stubber
from fakes import offline_client

from aif.hub import iam
from aif.hub.common import HubError, Report
from aif.hub.policies import Scope, boundary, dumps, trust

SCOPE = Scope(partition="aws", account="111122223333", region="us-west-2")
TAGS = {"project": "ai-foundations-eks", "stage": "platform"}
BOUNDARY = SCOPE.boundary_arn
ROLE_ARN = "arn:aws:iam::111122223333:role/aif/r"
SPEC = iam.RoleSpec(
    "r",
    trust("eks.amazonaws.com"),
    "test",
    managed=("AmazonEKSClusterPolicy",),
    inline={"p": {"a": 1}},
)


def _role(
    path: str = "/aif/", doc: dict[str, object] | None = None, boundary_arn: str = BOUNDARY
) -> dict[str, object]:
    return {
        "Path": path,
        "RoleName": "r",
        "RoleId": "AROAEXAMPLE000000001",
        "Arn": ROLE_ARN,
        "CreateDate": "2026-11-19T00:00:00Z",
        "AssumeRolePolicyDocument": dumps(doc if doc is not None else trust("eks.amazonaws.com")),
        "PermissionsBoundary": {
            "PermissionsBoundaryType": "Policy",
            "PermissionsBoundaryArn": boundary_arn,
        },
    }


class RoleTest(unittest.TestCase):
    def test_creates_a_missing_role_with_boundary_tags_and_policies(self) -> None:
        client = offline_client("iam")
        with Stubber(client) as stub:
            stub.add_client_error("get_role", "NoSuchEntity")
            stub.add_response(
                "create_role",
                {"Role": _role()},
                {
                    "RoleName": "r",
                    "Path": "/aif/",
                    "AssumeRolePolicyDocument": dumps(trust("eks.amazonaws.com")),
                    "Description": "test",
                    "PermissionsBoundary": BOUNDARY,
                    "Tags": [
                        {"Key": "project", "Value": "ai-foundations-eks"},
                        {"Key": "stage", "Value": "platform"},
                    ],
                },
            )
            stub.add_response(
                "list_attached_role_policies", {"AttachedPolicies": []}, {"RoleName": "r"}
            )
            stub.add_response(
                "attach_role_policy",
                {},
                {"RoleName": "r", "PolicyArn": "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"},
            )
            stub.add_response(
                "put_role_policy",
                {},
                {"RoleName": "r", "PolicyName": "p", "PolicyDocument": '{"a":1}'},
            )
            rep = Report()
            self.assertEqual(iam.ensure_role(client, SPEC, SCOPE, TAGS, rep), ROLE_ARN)
            stub.assert_no_pending_responses()
        self.assertEqual(rep.lines, ["created  iam role /aif/r"])

    def test_existing_role_is_brought_up_to_date(self) -> None:
        client = offline_client("iam")
        with Stubber(client) as stub:
            stub.add_response(
                "get_role",
                {"Role": _role(doc={"old": True}, boundary_arn="arn:aws:iam::1:policy/x")},
            )
            stub.add_response(
                "update_assume_role_policy", {}, {"RoleName": "r", "PolicyDocument": ANY}
            )
            stub.add_response(
                "put_role_permissions_boundary",
                {},
                {"RoleName": "r", "PermissionsBoundary": BOUNDARY},
            )
            stub.add_response("tag_role", {}, {"RoleName": "r", "Tags": ANY})
            stub.add_response(
                "list_attached_role_policies",
                {
                    "AttachedPolicies": [
                        {"PolicyArn": "arn:aws:iam::aws:policy/Extra"},
                        {"PolicyArn": "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"},
                    ]
                },
                {"RoleName": "r"},
            )
            stub.add_response(
                "detach_role_policy",
                {},
                {"RoleName": "r", "PolicyArn": "arn:aws:iam::aws:policy/Extra"},
            )
            stub.add_response(
                "put_role_policy", {}, {"RoleName": "r", "PolicyName": "p", "PolicyDocument": ANY}
            )
            rep = Report()
            iam.ensure_role(client, SPEC, SCOPE, TAGS, rep)
            stub.assert_no_pending_responses()
        self.assertEqual(rep.lines, ["updated  iam role /aif/r"])

    def test_unchanged_role_is_kept(self) -> None:
        client = offline_client("iam")
        with Stubber(client) as stub:
            stub.add_response("get_role", {"Role": _role()})
            stub.add_response("tag_role", {}, {"RoleName": "r", "Tags": ANY})
            stub.add_response(
                "list_attached_role_policies",
                {
                    "AttachedPolicies": [
                        {"PolicyArn": "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"}
                    ]
                },
                {"RoleName": "r"},
            )
            stub.add_response(
                "put_role_policy", {}, {"RoleName": "r", "PolicyName": "p", "PolicyDocument": ANY}
            )
            rep = Report()
            iam.ensure_role(client, SPEC, SCOPE, TAGS, rep)
        self.assertEqual(rep.lines, ["exists   iam role /aif/r"])

    def test_refuses_a_role_on_another_path(self) -> None:
        client = offline_client("iam")
        with Stubber(client) as stub:
            stub.add_response("get_role", {"Role": _role(path="/")})
            with self.assertRaisesRegex(HubError, "does not take over"):
                iam.ensure_role(client, SPEC, SCOPE, TAGS, Report())


class BoundaryTest(unittest.TestCase):
    def test_creates_the_boundary(self) -> None:
        client = offline_client("iam")
        with Stubber(client) as stub:
            stub.add_client_error("get_policy", "NoSuchEntity")
            stub.add_response(
                "create_policy",
                {"Policy": {"Arn": BOUNDARY}},
                {
                    "PolicyName": "aif-infra-boundary",
                    "Path": "/aif/",
                    "PolicyDocument": dumps(boundary(SCOPE)),
                    "Description": ANY,
                    "Tags": ANY,
                },
            )
            self.assertEqual(iam.ensure_boundary(client, SCOPE, TAGS, Report()), BOUNDARY)
            stub.assert_no_pending_responses()

    def test_keeps_an_unchanged_boundary(self) -> None:
        client = offline_client("iam")
        with Stubber(client) as stub:
            stub.add_response("get_policy", {"Policy": {"Arn": BOUNDARY, "DefaultVersionId": "v1"}})
            stub.add_response(
                "get_policy_version",
                {"PolicyVersion": {"Document": dumps(boundary(SCOPE)), "VersionId": "v1"}},
            )
            rep = Report()
            iam.ensure_boundary(client, SCOPE, TAGS, rep)
        self.assertEqual(rep.lines, ["exists   iam policy aif-infra-boundary"])

    def test_new_version_prunes_the_oldest(self) -> None:
        client = offline_client("iam")
        versions = [
            {
                "VersionId": f"v{i}",
                "IsDefaultVersion": i == 5,
                "CreateDate": f"2026-11-0{i}T00:00:00Z",
            }
            for i in range(1, 6)
        ]
        with Stubber(client) as stub:
            stub.add_response("get_policy", {"Policy": {"Arn": BOUNDARY, "DefaultVersionId": "v5"}})
            stub.add_response(
                "get_policy_version", {"PolicyVersion": {"Document": "{}", "VersionId": "v5"}}
            )
            stub.add_response("list_policy_versions", {"Versions": versions})
            stub.add_response(
                "delete_policy_version", {}, {"PolicyArn": BOUNDARY, "VersionId": "v1"}
            )
            stub.add_response(
                "create_policy_version",
                {},
                {"PolicyArn": BOUNDARY, "PolicyDocument": ANY, "SetAsDefault": True},
            )
            iam.ensure_boundary(client, SCOPE, TAGS, Report())
            stub.assert_no_pending_responses()
