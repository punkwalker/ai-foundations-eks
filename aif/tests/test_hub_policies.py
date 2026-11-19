"""IAM guard for every document aif creates: no admin, no `*` action, no bare `*` resource."""

import json
import unittest
from typing import Any

from aif.hub import policies
from aif.hub.policies import Scope

SCOPE = Scope(partition="aws", account="111122223333", region="us-west-2")
KEY = "arn:aws:kms:us-west-2:111122223333:key/abc"

DOCS: dict[str, Any] = {
    "boundary": policies.boundary(SCOPE),
    "ack": policies.ack_capability(SCOPE),
    "argocd": policies.argocd_capability(SCOPE),
    "platform_clusters": policies.platform_clusters(SCOPE, KEY),
    "flow_logs": policies.flow_logs("arn:aws:logs:us-west-2:111122223333:log-group:/aif/x"),
}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


class GuardTest(unittest.TestCase):
    def test_no_admin_and_no_star_action(self) -> None:
        for name, doc in DOCS.items():
            self.assertNotIn("AdministratorAccess", json.dumps(doc), name)
            for st in doc["Statement"]:
                if st["Effect"] != "Allow":
                    continue
                self.assertNotIn("NotAction", st, name)
                for action in _list(st["Action"]):
                    self.assertNotIn(action, ("*", "*:*"), f"{name} {st.get('Sid')}")

    def test_star_resource_has_a_condition(self) -> None:
        for name, doc in DOCS.items():
            for st in doc["Statement"]:
                if st["Effect"] == "Allow" and "*" in _list(st["Resource"]):
                    self.assertIn("Condition", st, f"{name} {st.get('Sid')}")

    def test_every_statement_has_a_sid(self) -> None:
        for name, doc in DOCS.items():
            for st in doc["Statement"]:
                self.assertTrue(st.get("Sid"), name)

    def test_spoke_role_needs_the_boundary_and_cannot_touch_the_hub(self) -> None:
        doc = DOCS["platform_clusters"]
        by_sid = {s["Sid"]: s for s in doc["Statement"]}
        cond = by_sid["SpokeRolesWithBoundary"]["Condition"]["StringEquals"]
        self.assertEqual(cond["iam:PermissionsBoundary"], SCOPE.boundary_arn)
        self.assertIn("iam:CreateRole", by_sid["SpokeRolesWithBoundary"]["Action"])
        self.assertNotIn("iam:CreateRole", by_sid["SpokeRolesLifecycle"]["Action"])
        self.assertEqual(by_sid["NotTheHub"]["Effect"], "Deny")
        self.assertIn("cluster/aif-hub", json.dumps(by_sid["NotTheHub"]["Resource"]))

    def test_trust_lists_only_services(self) -> None:
        doc = policies.trust("eks.amazonaws.com")
        self.assertEqual(list(doc["Statement"][0]["Principal"]), ["Service"])
        self.assertEqual(
            policies.trust("ec2.amazonaws.com", tag_session=False)["Statement"][0]["Action"],
            ["sts:AssumeRole"],
        )

    def test_bucket_policy_denies_plain_http(self) -> None:
        st = policies.bucket_tls_only("arn:aws:s3:::b")["Statement"][0]
        self.assertEqual(st["Effect"], "Deny")
        self.assertEqual(st["Condition"], {"Bool": {"aws:SecureTransport": "false"}})

    def test_documents_fit_the_inline_limit(self) -> None:
        for name, doc in DOCS.items():
            self.assertLess(len(policies.dumps(doc)), 6144, name)
