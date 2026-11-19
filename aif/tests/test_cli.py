"""Tests for the aif command line. No network: every outside call goes to a fake."""

import io
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

from botocore.stub import Stubber
from fakes import FakeClock, FakeKube, app, hub_secret, offline_client

from aif import __version__
from aif.argocd import APPLICATIONS
from aif.cli import EXIT_FAILED, EXIT_OK, EXIT_REFUSED, EXIT_USAGE, Deps, main
from aif.kube import KubeError
from aif.teardown import AwsClients

ACCOUNT = "111122223333"
HERE = Path(__file__).resolve().parent


def aws_clients() -> AwsClients:
    return AwsClients(
        **{
            field: offline_client(service)
            for field, service in (
                ("s3", "s3"),
                ("ecr", "ecr"),
                ("secrets", "secretsmanager"),
                ("ec2", "ec2"),
                ("elbv2", "elbv2"),
                ("iam", "iam"),
                ("tagging", "resourcegroupstaggingapi"),
                ("cloudformation", "cloudformation"),
            )
        }
    )


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = FakeKube()
        self.hub.secrets[("argocd", "hub")] = hub_secret()
        self.hub.add(APPLICATIONS, app("team", "00"))
        self.hub.add(APPLICATIONS, app("gpu", "03", health="Degraded"))
        self.sts = offline_client("sts")
        self.aws = aws_clients()
        self.clock = FakeClock()
        self.env: dict[str, str] = {}
        self.deps = Deps(
            kube=lambda _ctx: self.hub,
            sts=lambda _region: self.sts,
            aws_clients=lambda _region: self.aws,
            clock=self.clock.clock(),
            cwd=HERE,
            env=self.env,
        )
        self.stack = ExitStack()
        self.stubs = {
            name: self.stack.enter_context(Stubber(client))
            for name, client in (
                ("sts", self.sts),
                ("tagging", self.aws.tagging),
                ("iam", self.aws.iam),
            )
        }
        self.addCleanup(self.stack.close)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv), self.deps)
        return code, out.getvalue(), err.getvalue()

    def caller(self, account: str = ACCOUNT) -> None:
        arn = f"arn:aws:sts::{account}:assumed-role/Admin/session"
        self.stubs["sts"].add_response(
            "get_caller_identity", {"Account": account, "Arn": arn, "UserId": "x"}
        )

    def no_tagged_resources(self) -> None:
        self.stubs["tagging"].add_response("get_resources", {"ResourceTagMappingList": []})

    def test_version(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out), self.assertRaises(SystemExit) as exc:
            main(["--version"])
        self.assertEqual(exc.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"aif {__version__}")

    def test_stage_list_and_graph(self) -> None:
        code, out, _ = self.run_cli("stage", "list")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("13  capstone", out)
        code, out, _ = self.run_cli("stage", "graph")
        self.assertIn("05 [b] needs 00, 01, 03, 04", out)

    def test_stage_rejects_bad_id(self) -> None:
        for bad in ("3", "14", "ab"):
            with self.subTest(stage=bad):
                with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
                    main(["stage", "on", bad], self.deps)
                self.assertEqual(exc.exception.code, EXIT_USAGE)

    def test_stage_on_then_off(self) -> None:
        code, out, _ = self.run_cli("stage", "on", "00")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("platform.aws/stage-00", out)
        code, out, _ = self.run_cli("stage", "on", "00")
        self.assertIn("nothing, already set", out)
        code, out, _ = self.run_cli("stage", "off", "00")
        self.assertEqual(code, EXIT_OK)
        labels = self.hub.secrets[("argocd", "hub")]["metadata"]["labels"]
        self.assertNotIn("platform.aws/stage-00", labels)

    def test_stage_on_reports_unhealthy(self) -> None:
        code, out, _ = self.run_cli("stage", "on", "03")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("health=Degraded", out)

    def test_up_needs_confirm_account(self) -> None:
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exc:
            main(["up"], self.deps)
        self.assertEqual(exc.exception.code, EXIT_USAGE)

    def test_up_refuses_wrong_account(self) -> None:
        self.caller("444455556666")
        code, out, err = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_REFUSED)
        self.assertIn("Nothing was changed", err)
        self.assertNotIn("plan:", out)
        self.assertEqual(
            self.hub.secrets[("argocd", "hub")]["metadata"]["labels"],
            {"argocd.argoproj.io/secret-type": "cluster"},
        )

    def test_up_dry_run_prints_plan_only(self) -> None:
        self.caller()
        code, out, _ = self.run_cli("up", "--confirm-account", ACCOUNT, "--groups", "c")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("plan: bring up 4 stages, groups base, c", out)
        self.assertIn("dry run", out)
        self.assertNotIn(("patch-secret", "hub"), self.hub.calls)

    def test_up_apply(self) -> None:
        self.caller()
        self.env["AIF_GROUPS"] = "base"
        self.hub.add(APPLICATIONS, app("model", "01"))
        code, out, _ = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_OK, out)
        self.assertIn("stage 01: ready", out)

    def test_up_apply_failure_exit_code(self) -> None:
        self.caller()
        code, _, _ = self.run_cli("up", "--confirm-account", ACCOUNT, "--apply", "--groups", "b")
        self.assertEqual(code, EXIT_FAILED)

    def test_down_dry_run(self) -> None:
        self.caller()
        self.no_tagged_resources()
        code, out, _ = self.run_cli("down", "--confirm-account", ACCOUNT)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("stage 03: 1 items", out)
        self.assertIn("dry run", out)
        self.assertEqual(len(self.hub.store(APPLICATIONS)), 2)

    def test_down_refuses_wrong_account(self) -> None:
        self.caller("444455556666")
        code, _, _ = self.run_cli("down", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_REFUSED)

    def test_down_apply(self) -> None:
        self.caller()
        self.no_tagged_resources()
        self.stubs["iam"].add_response("list_roles", {"Roles": []})
        self.no_tagged_resources()
        code, _, err = self.run_cli(
            "down", "--confirm-account", ACCOUNT, "--apply", "--force-finalizers"
        )
        self.assertEqual(code, EXIT_OK, err)
        self.assertIn("WARNING: --force-finalizers", err)
        self.assertEqual(self.hub.store(APPLICATIONS), {})

    def test_down_apply_reports_leftovers(self) -> None:
        self.caller()
        self.no_tagged_resources()
        self.stubs["iam"].add_response("list_roles", {"Roles": []})
        self.stubs["tagging"].add_response(
            "get_resources",
            {"ResourceTagMappingList": [{"ResourceARN": "arn:aws:s3:::left", "Tags": []}]},
        )
        code, _, err = self.run_cli("down", "--confirm-account", ACCOUNT, "--apply")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("left over:", err)

    def test_status(self) -> None:
        code, out, _ = self.run_cli("status")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("03  b         off   1     gpu", out)

    def test_doctor(self) -> None:
        self.caller()
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("Argo CD mode:  capability", out)
        self.assertIn("role Admin", out)
        self.assertIn("gpu: sync=Synced health=Degraded", out)

    def test_doctor_without_hub_context(self) -> None:
        def no_context(_ctx: str) -> Any:
            raise ValueError("context hub not found")

        self.deps.kube = no_context
        self.caller()
        code, out, _ = self.run_cli("doctor")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("cannot load context hub", out)

    def test_cleanup_report(self) -> None:
        self.no_tagged_resources()
        code, out, _ = self.run_cli("cleanup", "--report")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("stage 00: 1 items", out)

    def test_cost(self) -> None:
        code, out, _ = self.run_cli("cost")
        self.assertEqual(code, EXIT_OK)
        self.assertIn("total", out)

    def test_config_error_is_usage(self) -> None:
        self.env["AIF_ARGOCD_MODE"] = "local"
        code, _, err = self.run_cli("stage", "list")
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("argocd_mode", err)

    def test_kube_error(self) -> None:
        self.hub.fail_next = [KubeError(403, "Forbidden", "rbac")]
        code, _, err = self.run_cli("status")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("Kubernetes API error", err)

    def test_aws_error(self) -> None:
        self.stubs["tagging"].add_client_error("get_resources", "AccessDeniedException")
        code, _, err = self.run_cli("cleanup", "--report")
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("AWS error", err)
