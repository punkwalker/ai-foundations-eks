"""Tests for aif up, aif down, aif dev network and aif cleanup. No network calls."""

from fakes import FakeHub, app, launch_inputs
from harness import ACCOUNT, CliHarness

from aif.argocd import APPLICATIONS, APPLICATIONSETS
from aif.cli import EXIT_FAILED, EXIT_OK, EXIT_REFUSED
from aif.hub_api import HubError
from aif.kube import KubeError

OTHER = "444455556666"
LAUNCH_ARN = "arn:aws:cloudformation:us-west-2:111122223333:stack/launch/1"
CLEAN_LABELS = {"argocd.argoproj.io/secret-type": "cluster"}


class UpTest(CliHarness):
    def labels(self) -> dict[str, str]:
        return dict(self.hub.secrets[("argocd", "hub")]["metadata"]["labels"])

    def test_needs_an_account(self) -> None:
        code, _, err = self.run_cli("up", "--apply")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("--confirm-account or AIF_CONFIRM_ACCOUNT is required", err)

    def test_account_from_env(self) -> None:
        self.env["AIF_CONFIRM_ACCOUNT"] = ACCOUNT
        self.caller()
        self.launch_inputs()
        code, out, _ = self.run_cli("up")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("dry run", out)

    def test_env_and_flag_must_agree(self) -> None:
        self.env["AIF_CONFIRM_ACCOUNT"] = OTHER
        code, _, err = self.run_cli("up", "--confirm-account", ACCOUNT)
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("differ", err)

    def test_env_account_is_still_checked(self) -> None:
        self.env["AIF_CONFIRM_ACCOUNT"] = ACCOUNT
        self.caller(OTHER)
        code, out, err = self.run_cli("up", "--apply")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("Nothing was changed", err)
        self.assertNotIn("plan:", out)
        self.assertEqual(self.fake_hub.calls, [])
        self.assertEqual(self.labels(), CLEAN_LABELS)

    def test_missing_inputs_stop_before_anything(self) -> None:
        self.caller()
        self.launch_inputs(**{"/aif/vpc-id": None})
        code, _, err = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("/aif/vpc-id", err)
        self.assertEqual(self.fake_hub.calls, [])

    def test_dry_run_prints_plan_only(self) -> None:
        self.caller()
        self.launch_inputs()
        code, out, _ = self.run_cli("up", "--confirm-account", ACCOUNT, "--groups", "c")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("launch stack: launch", out)
        self.assertIn("plan: 3. create capabilities argocd, kro, ack", out)
        self.assertIn("plan: 6. turn on 4 stages in order", out)
        self.assertEqual(self.fake_hub.calls, [])
        self.assertNotIn(("patch-secret", "hub"), self.hub.calls)

    def test_apply_runs_every_step_in_order(self) -> None:
        self.caller()
        self.launch_inputs()
        self.spokes_ready()
        self.env["AIF_GROUPS"] = "base"
        self.hub.add(APPLICATIONS, app("model", "01"))
        code, out, err = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_OK, out + err)
        self.assertEqual(
            self.fake_hub.calls,
            ["ensure_platform", "ensure_hub", "ensure_capabilities:capability"],
        )
        self.assertIn("kubeconfig: aif-hub in", out)
        self.assertIn("kubeconfig: aif-dev, aif-prod in", out)
        hub_data = self.hub.secrets[("argocd", "hub")]["stringData"]
        self.assertEqual(hub_data["server"], self.fake_hub.cluster.arn)  # type: ignore[union-attr]
        self.assertEqual(self.labels()["platform.aws/group-base"], "on")
        self.assertIn("stage 01: ready", out)

    def test_apply_stops_when_hub_fails(self) -> None:
        self.caller()
        self.launch_inputs()
        self.fake_hub.fail_cluster = True
        code, _, err = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("aif-hub is FAILED", err)
        self.assertFalse((self.home / ".kube" / "config").exists())

    def test_apply_hub_timeout(self) -> None:
        self.caller()
        self.launch_inputs()
        self.fake_hub.stuck = True
        code, _, err = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("timed out after 1800s waiting for the Hub cluster", err)

    def test_apply_stops_when_capability_fails(self) -> None:
        self.caller()
        self.launch_inputs()
        self.fake_hub.fail_capability = "kro"
        code, _, err = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("capability kro is CREATE_FAILED: quota", err)

    def test_apply_stops_when_argocd_never_ready(self) -> None:
        self.caller()
        self.launch_inputs()
        code, _, err = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("Argo CD API not ready", err)

    def test_apply_spoke_timeout_names_what_is_missing(self) -> None:
        self.caller()
        self.launch_inputs()
        self.hub.store(APPLICATIONSETS)
        for _ in range(200):
            self.stubs["eks"].add_client_error("describe_cluster", "ResourceNotFoundException")
        code, _, err = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("aif-dev=MISSING", err)
        self.assertIn("Secrets missing for ['dev', 'prod']", err)
        self.assertIn("gpu: sync=Synced health=Degraded", err)

    def test_apply_reports_failed_stage(self) -> None:
        self.caller()
        self.launch_inputs()
        self.spokes_ready()
        code, _, _ = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply", "--groups", "b")
        self.assertEqual(code, EXIT_FAILED)

    def test_helm_mode_skips_argocd_capability(self) -> None:
        self.caller()
        self.env["AIF_ARGOCD_MODE"] = "helm"
        self.launch_inputs(**{"/aif/idc-instance-arn": None})
        code, out, _ = self.run_cli("up", "--confirm-account", ACCOUNT)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("plan: 3. create capabilities kro, ack", out)

    def test_hub_code_not_built(self) -> None:
        self.caller()
        self.launch_inputs()

        def missing(_region: str, _tags: object) -> FakeHub:
            raise HubError("aif.hub is not built yet, so aif cannot create the Hub")

        self.deps.hub = missing
        code, _, err = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("aif.hub is not built yet", err)


class DownTest(CliHarness):
    def test_does_not_read_account_from_env(self) -> None:
        self.env["AIF_CONFIRM_ACCOUNT"] = ACCOUNT
        code, _, err = self.run_cli("down", "--apply")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("--confirm-account is required", err)

    def test_refuses_wrong_account(self) -> None:
        self.caller(OTHER)
        code, _, _ = self.run_cli("down", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_REFUSED)

    def test_refuses_when_launch_stack_unknown(self) -> None:
        self.caller()
        self.stubs["ssm"].add_client_error("get_parameter", "ParameterNotFound")
        code, _, err = self.run_cli("down", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("launch stack is unknown", err)
        self.assertEqual(len(self.hub.store(APPLICATIONS)), 2)
        self.assertEqual(self.fake_hub.calls, [])

    def test_dry_run_shows_kept_stacks(self) -> None:
        self.caller()
        self.launch_stack()
        self.stubs["tagging"].add_response(
            "get_resources",
            {
                "ResourceTagMappingList": [
                    {"ResourceARN": LAUNCH_ARN, "Tags": []},
                    {
                        "ResourceARN": "arn:aws:s3:::kept",
                        "Tags": [{"Key": "aif:keep", "Value": "true"}],
                    },
                    {"ResourceARN": "arn:aws:s3:::lab", "Tags": [{"Key": "stage", "Value": "01"}]},
                ]
            },
        )
        code, out, _ = self.run_cli("down", "--confirm-account", ACCOUNT)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("keep: stacks launch, and anything tagged aif:keep=true", out)
        self.assertIn("s3 lab", out)
        self.assertNotIn("kept", out.replace("keep:", ""))
        self.assertNotIn("stack/launch", out)
        self.assertIn("then: agent roles, VPC blockers, Hub capabilities, Hub cluster", out)
        self.assertIn("dry run", out)
        self.assertEqual(self.fake_hub.calls, [])

    def apply_stubs(self) -> None:
        self.caller()
        self.launch_stack()
        self.no_tagged_resources()
        self.stubs["iam"].add_response("list_roles", {"Roles": []})

    def hub_up(self) -> None:
        self.fake_hub.ensure_hub(launch_inputs())
        self.fake_hub.hub_status()
        self.fake_hub.ensure_capabilities(launch_inputs(), "capability")
        self.fake_hub.capability_status()
        self.fake_hub.calls.clear()

    def test_apply_removes_hub_in_order_and_sweeps(self) -> None:
        self.hub_up()
        self.apply_stubs()
        self.no_tagged_resources()
        code, _, err = self.run_cli(
            "down", "--confirm-account", ACCOUNT, "--apply", "--force-finalizers"
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("WARNING: --force-finalizers", err)
        self.assertEqual(
            self.fake_hub.calls, ["delete_capabilities", "delete_hub", "delete_platform"]
        )
        self.assertIsNone(self.fake_hub.cluster)
        self.assertEqual(self.hub.store(APPLICATIONS), {})

    def test_apply_stops_when_hub_removal_hangs(self) -> None:
        self.hub_up()
        self.fake_hub.stuck = True
        self.apply_stubs()
        code, _, err = self.run_cli("down", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("Hub removal stopped", err)
        self.assertEqual(self.fake_hub.calls, ["delete_capabilities"])

    def test_apply_reports_leftovers(self) -> None:
        self.apply_stubs()
        self.stubs["tagging"].add_response(
            "get_resources",
            {"ResourceTagMappingList": [{"ResourceARN": "arn:aws:s3:::left", "Tags": []}]},
        )
        code, _, err = self.run_cli("down", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("left over:", err)


class DevNetworkTest(CliHarness):
    def test_needs_account(self) -> None:
        code, _, err = self.run_cli("dev", "network", "up", "--dev-standin", "--apply")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("--confirm-account is required", err)

    def test_dry_run(self) -> None:
        self.caller()
        code, out, _ = self.run_cli(
            "dev", "network", "up", "--dev-standin", "--confirm-account", ACCOUNT
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("plan: create the development network", out)
        self.assertEqual(self.dev_network.calls, [])

    def test_up_and_down(self) -> None:
        self.caller()
        self.caller()
        code, out, _ = self.run_cli(
            "dev", "network", "up", "--dev-standin", "--confirm-account", ACCOUNT, "--apply"
        )
        self.assertEqual(code, EXIT_OK)
        self.assertIn("published inputs for VPC vpc-1", out)
        code, out, _ = self.run_cli(
            "dev", "network", "down", "--dev-standin", "--confirm-account", ACCOUNT, "--apply"
        )
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.dev_network.calls, ["ensure", "delete"])


class CleanupTest(CliHarness):
    def test_report(self) -> None:
        self.launch_stack()
        self.no_tagged_resources()
        code, out, _ = self.run_cli("cleanup", "--report")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("stage 00: 1 items", out)

    def test_report_without_launch_stack_notes_it(self) -> None:
        self.stubs["ssm"].add_client_error("get_parameter", "ParameterNotFound")
        self.no_tagged_resources()
        code, out, _ = self.run_cli("cleanup", "--report")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("note: cannot read SSM parameter", out)

    def test_hub_error(self) -> None:
        self.launch_stack()
        self.hub.fail_next = [KubeError(403, "Forbidden", "rbac")]
        self.no_tagged_resources()
        code, out, _ = self.run_cli("cleanup", "--report")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("note: hub: cannot list Argo CD Applications", out)
