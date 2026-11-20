"""Tests for the aif command line. No network: every outside call goes to a fake."""

import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from harness import ACCOUNT, CliHarness

from aif import __version__
from aif.cli import EXIT_FAILED, EXIT_OK, EXIT_USAGE, main
from aif.kube import KubeError


class CliTest(CliHarness):
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

    # up, down and cleanup tests live in test_cli_lifecycle.py

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
        self.assertIn("cannot load context aif-hub", out)

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
        self.stubs["sts"].add_client_error("get_caller_identity", "ExpiredToken")
        code, _, err = self.run_cli("up", "--confirm-account", ACCOUNT)
        self.assertEqual(code, EXIT_FAILED)
        self.assertIn("AWS error", err)
