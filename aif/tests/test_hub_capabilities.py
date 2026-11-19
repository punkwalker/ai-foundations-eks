import unittest
from typing import Any

from botocore.stub import Stubber
from fakes import FakeClock, offline_client

from aif.hub import capabilities
from aif.hub.common import HubError, Report

TAGS = {"project": "ai-foundations-eks", "stage": "platform"}
ROLE = "arn:aws:iam::111122223333:role/aif/aif-ack-capability"
ARN = "arn:aws:eks:us-west-2:111122223333:capability/aif-hub/ack/1"
STORE = "d-0123456789"
GROUP_ARN = "arn:aws:identitystore::111122223333:identitystore/d-0123456789/group/"
IDC = "arn:aws:sso:::instance/ssoins-0123456789abcdef"


def _cap(status: str, kind: str = "ACK", config: dict[str, Any] | None = None) -> dict[str, Any]:
    cap: dict[str, Any] = {"capabilityName": "ack", "arn": ARN, "type": kind, "status": status}
    if config is not None:
        cap["configuration"] = config
    return {"capability": cap}


class GroupsTest(unittest.TestCase):
    def test_creates_only_missing_aif_groups(self) -> None:
        ids = offline_client("identitystore")
        with Stubber(ids) as stub:
            stub.add_response(
                "get_group_id",
                {
                    "GroupId": "g-admin",
                    "IdentityStoreId": STORE,
                    "GroupArn": GROUP_ARN + "g-admin-0000000",
                },
                {
                    "IdentityStoreId": STORE,
                    "AlternateIdentifier": {
                        "UniqueAttribute": {
                            "AttributePath": "displayName",
                            "AttributeValue": "aif-admins",
                        }
                    },
                },
            )
            stub.add_client_error("get_group_id", "ResourceNotFoundException")
            stub.add_response(
                "create_group",
                {
                    "GroupId": "g-editor",
                    "IdentityStoreId": STORE,
                    "GroupArn": GROUP_ARN + "g-editor-0000000",
                    "Revision": "1",
                },
                {
                    "IdentityStoreId": STORE,
                    "DisplayName": "aif-editors",
                    "Description": "ai-foundations-eks: Argo CD EDITOR on the Hub",
                },
            )
            stub.add_client_error("get_group_id", "ResourceNotFoundException")
            stub.add_response(
                "create_group",
                {
                    "GroupId": "g-viewer",
                    "IdentityStoreId": STORE,
                    "GroupArn": GROUP_ARN + "g-viewer-0000000",
                    "Revision": "1",
                },
                {
                    "IdentityStoreId": STORE,
                    "DisplayName": "aif-viewers",
                    "Description": "ai-foundations-eks: Argo CD VIEWER on the Hub",
                },
            )
            groups = capabilities.ensure_groups(ids, STORE, Report())
            stub.assert_no_pending_responses()
        self.assertEqual(groups, {"ADMIN": "g-admin", "EDITOR": "g-editor", "VIEWER": "g-viewer"})

    def test_argocd_configuration_maps_each_role(self) -> None:
        conf = capabilities.argocd_configuration(IDC, "us-west-2", {"VIEWER": "v", "ADMIN": "a"})
        self.assertEqual(
            conf["argoCd"]["awsIdc"], {"idcInstanceArn": IDC, "idcRegion": "us-west-2"}
        )
        self.assertEqual(
            conf["argoCd"]["rbacRoleMappings"],
            [
                {"role": "ADMIN", "identities": [{"id": "a", "type": "SSO_GROUP"}]},
                {"role": "VIEWER", "identities": [{"id": "v", "type": "SSO_GROUP"}]},
            ],
        )


class CapabilityTest(unittest.TestCase):
    def test_creates_with_retain_and_waits(self) -> None:
        eks = offline_client("eks")
        clock = FakeClock()
        with Stubber(eks) as stub:
            stub.add_client_error("describe_capability", "ResourceNotFoundException")
            stub.add_response(
                "create_capability",
                {},
                {
                    "clusterName": "aif-hub",
                    "capabilityName": "ack",
                    "type": "ACK",
                    "roleArn": ROLE,
                    "deletePropagationPolicy": "RETAIN",
                    "tags": TAGS,
                },
            )
            stub.add_response("describe_capability", _cap("CREATING"))
            stub.add_response("describe_capability", _cap("ACTIVE"))
            arn = capabilities.ensure_capability(
                eks,
                "aif-hub",
                capabilities.CapabilitySpec("ack", "ACK", ROLE),
                TAGS,
                Report(),
                clock=clock.clock(),
            )
            stub.assert_no_pending_responses()
        self.assertEqual(arn, ARN)

    def test_failed_capability_reports_health_issues(self) -> None:
        eks = offline_client("eks")
        failed = _cap("CREATE_FAILED")
        failed["capability"]["health"] = {"issues": [{"code": "AccessDenied", "message": "role"}]}
        with Stubber(eks) as stub:
            stub.add_response("describe_capability", failed)
            with self.assertRaisesRegex(HubError, "AccessDenied: role"):
                capabilities.wait_active(eks, "aif-hub", "ack", FakeClock().clock(), 60)

    def test_wrong_type_is_refused(self) -> None:
        eks = offline_client("eks")
        with Stubber(eks) as stub:
            stub.add_response("describe_capability", _cap("ACTIVE", kind="KRO"))
            with self.assertRaisesRegex(HubError, "is KRO, not ACK"):
                capabilities.ensure_capability(
                    eks, "aif-hub", capabilities.CapabilitySpec("ack", "ACK", ROLE), TAGS, Report()
                )

    def test_existing_argocd_gets_missing_mappings(self) -> None:
        eks = offline_client("eks")
        wanted = capabilities.argocd_configuration(IDC, "us-west-2", {"ADMIN": "a", "VIEWER": "v"})
        have = {
            "argoCd": {
                "rbacRoleMappings": [
                    {"role": "ADMIN", "identities": [{"id": "a", "type": "SSO_GROUP"}]}
                ]
            }
        }
        with Stubber(eks) as stub:
            stub.add_response("describe_capability", _cap("ACTIVE", "ARGOCD"))
            stub.add_response("describe_capability", _cap("ACTIVE", "ARGOCD", have))
            stub.add_response(
                "update_capability",
                {},
                {
                    "clusterName": "aif-hub",
                    "capabilityName": "argocd",
                    "configuration": {
                        "argoCd": {
                            "rbacRoleMappings": {
                                "addOrUpdateRoleMappings": [
                                    {
                                        "role": "VIEWER",
                                        "identities": [{"id": "v", "type": "SSO_GROUP"}],
                                    }
                                ]
                            }
                        }
                    },
                },
            )
            stub.add_response("describe_capability", _cap("ACTIVE", "ARGOCD", wanted))
            rep = Report()
            capabilities.ensure_capability(
                eks,
                "aif-hub",
                capabilities.CapabilitySpec("argocd", "ARGOCD", ROLE, wanted),
                TAGS,
                rep,
                clock=FakeClock().clock(),
            )
            stub.assert_no_pending_responses()
        self.assertEqual(rep.lines, ["updated  capability argocd role mappings"])
