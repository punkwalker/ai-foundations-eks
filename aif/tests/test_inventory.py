import unittest
from typing import Any

from botocore.stub import Stubber
from fakes import FakeKube, app, obj, offline_client

from aif import inventory
from aif.argocd import APPLICATIONS
from aif.inventory import RGDS, Inventory, Item
from aif.kube import KubeError
from aif.stages import Stage

PROJECT = "ai-foundations-eks"


def tagging_client() -> Any:
    return offline_client("resourcegroupstaggingapi")


def mapping(arn: str, **tags: str) -> dict[str, Any]:
    return {"ResourceARN": arn, "Tags": [{"Key": k, "Value": v} for k, v in tags.items()]}


def rgd(name: str, kind: str, stage: str | None = None, group: str | None = None) -> dict[str, Any]:
    item = obj(name, labels={"platform.aws/stage": stage} if stage else {})
    schema: dict[str, Any] = {"kind": kind, "apiVersion": "v1alpha1"}
    if group:
        schema["group"] = group
    item["spec"] = {"schema": schema}
    return item


class KubeSourcesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = FakeKube()
        self.hub.add(APPLICATIONS, app("team-alpha", "00"))
        self.hub.add(APPLICATIONS, app("orphan"))
        self.team = self.hub.add_crd("kro.run", "Team", "teams")
        self.hub.add_crd("kro.run", "RGD", "resourcegraphdefinitions")
        self.hub.add(RGDS, rgd("team", "Team", stage="00"))
        self.hub.add(RGDS, rgd("pending", "NotYet"))
        self.hub.add(self.team, obj("alpha", "teams"))
        self.bucket = self.hub.add_crd("s3.services.k8s.aws", "Bucket", "buckets")
        bucket = obj("weights", "aif", labels={"platform.aws/stage": "04"})
        bucket["status"] = {"ackResourceMetadata": {"arn": "arn:aws:s3:::weights"}}
        self.hub.add(self.bucket, bucket)

    def test_build_reads_all_kube_sources(self) -> None:
        inv = inventory.build({"hub": self.hub}, "argocd", None, PROJECT)
        found = {(i.source, i.name, i.stage) for i in inv.items}
        self.assertEqual(
            found,
            {
                ("argocd", "team-alpha", "00"),
                ("argocd", "orphan", "unassigned"),
                ("kro", "alpha", "00"),
                ("ack", "weights", "04"),
            },
        )
        ack = next(i for i in inv.items if i.source == "ack")
        self.assertEqual(ack.arn, "arn:aws:s3:::weights")
        self.assertEqual(inv.notes, [])

    def test_unreadable_cluster_becomes_note(self) -> None:
        broken = FakeKube()
        broken.fail_next = [KubeError(0, "ConnectionError", "refused")]
        inv = inventory.build({"dev": broken}, "argocd", None, PROJECT)
        self.assertEqual(inv.items, [])
        self.assertIn("dev: cannot list CRDs", inv.notes[0])

    def test_cluster_without_kro_or_argocd(self) -> None:
        bare = FakeKube()
        inv = inventory.build({"hub": bare}, "argocd", None, PROJECT)
        self.assertTrue(any("Argo CD" in n for n in inv.notes))
        self.assertTrue(any("kro" in n for n in inv.notes))

    def test_ack_list_failure_is_a_note(self) -> None:
        dev = FakeKube()
        dev.add_crd("s3.services.k8s.aws", "Bucket", "buckets")
        dev.add_crd("kro.run", "RGD", "resourcegraphdefinitions")
        # No Bucket objects store exists, so listing buckets returns 404.
        inv = inventory.build({"dev": dev}, "argocd", None, PROJECT)
        self.assertTrue(any("ACK" in n for n in inv.notes))


class TagSweepTest(unittest.TestCase):
    def test_sweep_pages_and_skips_kept_stacks(self) -> None:
        client = tagging_client()
        filters = {"TagFilters": [{"Key": "project", "Values": [PROJECT]}]}
        with Stubber(client) as stub:
            stub.add_response(
                "get_resources",
                {
                    "ResourceTagMappingList": [
                        mapping("arn:aws:s3:::weights", project=PROJECT, stage="04"),
                        mapping(
                            "arn:aws:ec2:us-west-2:111122223333:vpc/vpc-1",
                            project=PROJECT,
                            **{"aws:cloudformation:stack-name": "launch"},
                        ),
                    ],
                    "PaginationToken": "next",
                },
                filters,
            )
            stub.add_response(
                "get_resources",
                {
                    "ResourceTagMappingList": [
                        mapping(
                            "arn:aws:ecr:us-west-2:111122223333:repository/a/b", project=PROJECT
                        )
                    ],
                    "PaginationToken": "",
                },
                {**filters, "PaginationToken": "next"},
            )
            items = inventory.tag_sweep(client, PROJECT, keep_stacks=["launch"])
        self.assertEqual(
            [(i.kind, i.name, i.stage) for i in items],
            [("s3", "weights", "04"), ("ecr/repository", "repository/a/b", "unassigned")],
        )

    def test_arn_kind(self) -> None:
        self.assertEqual(inventory._arn_kind("bad"), "unknown")
        self.assertEqual(
            inventory._arn_kind("arn:aws:cloudformation:r:1:stack/s/id"), "cloudformation/stack"
        )


class PlanTest(unittest.TestCase):
    def test_by_stage_orders_stages_and_sources(self) -> None:
        inv = Inventory(
            items=[
                Item("aws", "aws", "s3", "b", "03"),
                Item("argocd", "hub", "Application", "x", "03"),
                Item("kro", "hub", "Team", "t", "00"),
                Item("ack", "dev", "Bucket", "u", "unassigned"),
                Item("ack", "dev", "Bucket", "z", "99"),
            ],
            notes=["dev: something"],
        )
        groups = inv.by_stage(["03", "00"])
        self.assertEqual([s for s, _ in groups], ["03", "00", "unassigned", "99"])
        self.assertEqual([i.source for i in groups[0][1]], ["argocd", "aws"])
        text = inventory.format_plan(inv, ["03", "00"])
        self.assertIn("stage 03: 2 items", text)
        self.assertIn("note: dev: something", text)

    def test_empty_plan(self) -> None:
        self.assertIn("nothing to remove", inventory.format_plan(Inventory(), []))

    def test_teardown_order_reverses(self) -> None:
        bring_up = [Stage("00", "", "base", (), 1, ()), Stage("01", "", "base", (), 1, ())]
        self.assertEqual(inventory.teardown_order(bring_up), ["01", "00"])

    def test_item_str(self) -> None:
        self.assertEqual(str(Item("kro", "hub", "Team", "t", "00", "ns")), "kro    hub   Team ns/t")
