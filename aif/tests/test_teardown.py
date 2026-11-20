import unittest
from contextlib import ExitStack
from dataclasses import replace
from typing import Any

from botocore.stub import Stubber
from fakes import FakeClock, FakeHub, FakeKube, app, hub_secret, launch_inputs, obj, offline_client

from aif.argocd import APPLICATIONS
from aif.inventory import Inventory, Item
from aif.kube import Resource
from aif.stages import Stage
from aif.teardown import AwsClients, Options, Teardown

PROJECT = "ai-foundations-eks"
STAGES = {
    "00": Stage("00", "tenancy", "base", (), 8, ()),
    "03": Stage("03", "gpu", "b", ("00",), 5, ()),
}
STACK = "arn:aws:cloudformation:us-west-2:111122223333:stack/hub/1"
VPC = "arn:aws:ec2:us-west-2:111122223333:vpc/vpc-1"


def clients() -> AwsClients:
    return AwsClients(
        s3=offline_client("s3"),
        ecr=offline_client("ecr"),
        secrets=offline_client("secretsmanager"),
        ec2=offline_client("ec2"),
        elbv2=offline_client("elbv2"),
        iam=offline_client("iam"),
        tagging=offline_client("resourcegroupstaggingapi"),
        cloudformation=offline_client("cloudformation"),
    )


class TeardownTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = FakeKube()
        self.hub.secrets[("argocd", "hub")] = hub_secret(
            {
                "platform.aws/stage-00": "on",
                "platform.aws/stage-03": "on",
                "platform.aws/group-base": "on",
                "platform.aws/group-b": "on",
            }
        )
        self.team = Resource("kro.run", "v1alpha1", "teams", "Team")
        self.bucket = Resource("s3.services.k8s.aws", "v1alpha1", "buckets", "Bucket")
        self.hub.add(APPLICATIONS, app("gpu", "03"))
        self.hub.add(APPLICATIONS, app("no-finalizer", "03", finalizer=False))
        self.hub.add(self.team, obj("alpha", "teams"))
        self.hub.add(self.bucket, obj("weights", "aif", finalizers=["ack"]))
        self.aws = clients()
        self.log: list[str] = []
        self.clock = FakeClock()

    def inventory(self, *extra: Item) -> Inventory:
        return Inventory(
            items=[
                Item("argocd", "hub", "Application", "gpu", "03", "argocd", APPLICATIONS),
                Item("argocd", "hub", "Application", "no-finalizer", "03", "argocd", APPLICATIONS),
                Item("ack", "hub", "Bucket", "weights", "03", "aif", self.bucket),
                Item("aws", "aws", "s3", "weights", "03", arn="arn:aws:s3:::weights"),
                Item("kro", "hub", "Team", "alpha", "00", "teams", self.team),
                *extra,
            ]
        )

    def teardown(self, **opts: Any) -> Teardown:
        options = Options(PROJECT, "argocd", "hub", stage_timeout=60, stack_timeout=60, **opts)
        return Teardown(
            STAGES, {"hub": self.hub}, self.aws, options, self.log.append, self.clock.clock()
        )

    def stub_all(self, stack: ExitStack) -> dict[str, Stubber]:
        stubs = {
            name: stack.enter_context(Stubber(getattr(self.aws, name)))
            for name in ("s3", "iam", "ec2", "elbv2", "cloudformation", "tagging")
        }
        stubs["s3"].add_response("list_object_versions", {}, {"Bucket": "weights"})
        stubs["iam"].add_response("list_roles", {"Roles": []}, {"PathPrefix": "/aif/agents/"})
        return stubs

    def test_happy_path_removes_everything(self) -> None:
        with ExitStack() as stack:
            stubs = self.stub_all(stack)
            stubs["elbv2"].add_response("describe_load_balancers", {"LoadBalancers": []})
            stubs["ec2"].add_response("describe_network_interfaces", {"NetworkInterfaces": []})
            stubs["cloudformation"].add_response("delete_stack", {}, {"StackName": STACK})
            stubs["cloudformation"].add_response(
                "describe_stacks",
                {
                    "Stacks": [
                        {
                            "StackName": "hub",
                            "CreationTime": "2026-11-19T00:00:00Z",
                            "StackStatus": "DELETE_COMPLETE",
                        }
                    ]
                },
            )
            stubs["tagging"].add_response("get_resources", {"ResourceTagMappingList": []})
            inv = self.inventory(
                Item("aws", "aws", "ec2/vpc", "vpc/vpc-1", "unassigned", arn=VPC),
                Item("aws", "aws", "cloudformation/stack", "stack/hub/1", "unassigned", arn=STACK),
            )
            result = self.teardown().run(inv, ["03", "00"])
            for stub in stubs.values():
                stub.assert_no_pending_responses()
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(
            self.hub.secrets[("argocd", "hub")]["metadata"]["labels"],
            {"argocd.argoproj.io/secret-type": "cluster"},
        )
        self.assertEqual(self.hub.store(APPLICATIONS), {})
        self.assertEqual(self.hub.store(self.bucket), {})
        self.assertEqual(self.hub.store(self.team), {})
        self.assertTrue(any("no-finalizer has no cascade finalizer" in m for m in self.log))
        self.assertIn("final tag sweep: zero resources tagged project", self.log)

    def test_stuck_object_fails_without_force(self) -> None:
        self.hub.stuck.add("weights")
        with ExitStack() as stack:
            self.stub_all(stack)
            result = self.teardown().run(self.inventory(), ["03", "00"])
        self.assertFalse(result.ok)
        self.assertIn("--force-finalizers", result.problems[0])
        self.assertIn("weights", result.problems[0])
        # Teardown stopped at stage 03, so stage 00's Team is still there.
        self.assertIn(("teams", "alpha"), self.hub.store(self.team))

    def test_force_finalizers_is_loud(self) -> None:
        self.hub.stuck.add("weights")
        with ExitStack() as stack:
            stubs = self.stub_all(stack)
            stubs["tagging"].add_response("get_resources", {"ResourceTagMappingList": []})
            result = self.teardown(force_finalizers=True).run(self.inventory(), ["03", "00"])
        self.assertTrue(result.ok, result.problems)
        self.assertTrue(any("FORCING FINALIZERS OFF" in m for m in self.log))
        self.assertEqual(self.hub.store(self.bucket), {})

    def test_final_sweep_must_be_empty(self) -> None:
        with ExitStack() as stack:
            stubs = self.stub_all(stack)
            stubs["tagging"].add_response(
                "get_resources",
                {
                    "ResourceTagMappingList": [
                        {
                            "ResourceARN": "arn:aws:s3:::left",
                            "Tags": [{"Key": "project", "Value": PROJECT}],
                        }
                    ]
                },
            )
            result = self.teardown().run(self.inventory(), ["03", "00"])
        self.assertFalse(result.ok)
        self.assertEqual([i.name for i in result.leftovers], ["left"])

    def test_failed_stack_is_reported(self) -> None:
        with ExitStack() as stack:
            stubs = self.stub_all(stack)
            stubs["cloudformation"].add_response("delete_stack", {})
            stubs["cloudformation"].add_response(
                "describe_stacks",
                {
                    "Stacks": [
                        {
                            "StackName": "hub",
                            "CreationTime": "2026-11-19T00:00:00Z",
                            "StackStatus": "DELETE_FAILED",
                            "StackStatusReason": "VPC in use",
                        }
                    ]
                },
            )
            inv = self.inventory(
                Item("aws", "aws", "cloudformation/stack", "stack/hub/1", "unassigned", arn=STACK)
            )
            result = self.teardown().run(inv, ["03", "00"])
        self.assertFalse(result.ok)
        self.assertIn("VPC in use", result.problems[0])

    def test_stack_delete_timeout(self) -> None:
        in_progress = {
            "Stacks": [
                {
                    "StackName": "hub",
                    "CreationTime": "2026-11-19T00:00:00Z",
                    "StackStatus": "DELETE_IN_PROGRESS",
                }
            ]
        }
        with ExitStack() as stack:
            stubs = self.stub_all(stack)
            stubs["cloudformation"].add_response("delete_stack", {})
            for _ in range(10):
                stubs["cloudformation"].add_response("describe_stacks", in_progress)
            inv = self.inventory(
                Item("aws", "aws", "cloudformation/stack", "stack/hub/1", "unassigned", arn=STACK)
            )
            result = self.teardown().run(inv, ["03", "00"])
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.problems[0])

    def test_prepare_ecr_and_secret(self) -> None:
        ecr_arn = "arn:aws:ecr:us-west-2:111122223333:repository/aif/agent"
        sm_arn = "arn:aws:secretsmanager:us-west-2:111122223333:secret:aif/key-AbCdEf"
        inv = Inventory(
            items=[
                Item("aws", "aws", "ecr/repository", "repository/aif/agent", "03", arn=ecr_arn),
                Item("aws", "aws", "secretsmanager", "aif/key", "03", arn=sm_arn),
                Item("aws", "aws", "ec2/instance", "instance/i-1", "03", arn="x"),
            ]
        )
        with ExitStack() as stack:
            ecr = stack.enter_context(Stubber(self.aws.ecr))
            sm = stack.enter_context(Stubber(self.aws.secrets))
            iam = stack.enter_context(Stubber(self.aws.iam))
            tagging = stack.enter_context(Stubber(self.aws.tagging))
            ecr.add_response(
                "delete_repository", {}, {"repositoryName": "aif/agent", "force": True}
            )
            sm.add_response(
                "delete_secret", {}, {"SecretId": sm_arn, "ForceDeleteWithoutRecovery": True}
            )
            iam.add_response("list_roles", {"Roles": []})
            tagging.add_response("get_resources", {"ResourceTagMappingList": []})
            result = self.teardown().run(inv, ["03", "00"])
            ecr.assert_no_pending_responses()
            sm.assert_no_pending_responses()
        self.assertTrue(result.ok, result.problems)

    def test_item_without_resource_type(self) -> None:
        inv = Inventory(items=[Item("kro", "hub", "Team", "x", "unassigned")])
        with self.assertRaises(ValueError):
            self.teardown().run(inv, [])


class HubStepTest(TeardownTest):
    """The Hub goes after the pre-delete steps and before the final sweep."""

    def fake_hub(self) -> FakeHub:
        hub = FakeHub()
        hub.ensure_hub(launch_inputs())
        hub.hub_status()
        hub.ensure_capabilities(launch_inputs(), "capability")
        hub.capability_status()
        hub.calls.clear()
        return hub

    def test_hub_removed_in_order_then_sweep(self) -> None:
        hub = self.fake_hub()
        with ExitStack() as stack:
            stubs = self.stub_all(stack)
            stubs["tagging"].add_response("get_resources", {"ResourceTagMappingList": []})
            teardown = self.teardown()
            teardown.hub_api = hub
            result = teardown.run(self.inventory(), ["03", "00"])
        self.assertTrue(result.ok, result.problems)
        self.assertEqual(hub.calls, ["delete_capabilities", "delete_hub", "delete_platform"])
        self.assertIn("Hub platform removed", self.log)

    def test_failed_capability_delete_stops(self) -> None:
        hub = self.fake_hub()
        hub.stuck = True
        hub.caps["ack"] = replace(hub.caps["ack"], status="DELETE_FAILED")
        hub.delete_capabilities = lambda: hub.calls.append("delete_capabilities")  # type: ignore[method-assign]
        with ExitStack() as stack:
            self.stub_all(stack)
            teardown = self.teardown()
            teardown.hub_api = hub
            result = teardown.run(self.inventory(), ["03", "00"])
        self.assertFalse(result.ok)
        self.assertIn("capabilities ['ack'] failed to delete", result.problems[0])
        self.assertEqual(hub.calls, ["delete_capabilities"])

    def test_failed_cluster_delete_stops(self) -> None:
        hub = self.fake_hub()
        hub.caps.clear()
        hub.delete_hub = lambda: setattr(  # type: ignore[method-assign]
            hub,
            "cluster",
            replace(hub.cluster, status="FAILED"),  # type: ignore[type-var]
        )
        with ExitStack() as stack:
            self.stub_all(stack)
            teardown = self.teardown()
            teardown.hub_api = hub
            result = teardown.run(self.inventory(), ["03", "00"])
        self.assertFalse(result.ok)
        self.assertIn("aif-hub is FAILED", result.problems[0])

    def test_forced_finalizers_that_still_hang(self) -> None:
        self.hub.stuck.add("weights")
        real_patch = self.hub.patch_object

        def keep_finalizers(*args: Any, **kwargs: Any) -> Any:
            return {}

        self.hub.patch_object = keep_finalizers  # type: ignore[method-assign]
        with ExitStack() as stack:
            self.stub_all(stack)
            result = self.teardown(force_finalizers=True).run(self.inventory(), ["03", "00"])
        self.hub.patch_object = real_patch  # type: ignore[method-assign]
        self.assertFalse(result.ok)
        self.assertIn("after force", result.problems[0])
