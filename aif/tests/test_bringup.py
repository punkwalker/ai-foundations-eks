import unittest

from fakes import FakeClock, FakeKube, app, hub_secret

from aif import bringup
from aif.argocd import APPLICATIONS, APPLICATIONSETS
from aif.kube import KubeError
from aif.stages import Stage

S00 = Stage("00", "tenancy", "base", (), 8, ())
S03 = Stage("03", "gpu", "b", ("00",), 5, ())
S04 = Stage("04", "inference", "b", ("03",), 6, ())
S07 = Stage("07", "tools", "c", ("00",), 3, ())
S13 = Stage("13", "capstone", "capstone", ("00",), None, ())


class TimeoutTest(unittest.TestCase):
    def test_timeouts(self) -> None:
        self.assertEqual(bringup.stage_timeout(S00), 960)  # 8 min budget in this test
        self.assertEqual(bringup.stage_timeout(Stage("02", "r", "a", (), 1, ())), 300)
        self.assertEqual(bringup.stage_timeout(S13), 600)
        self.assertIn("1. stage 00 [base] tenancy: timeout 960s", bringup.format_plan([S00]))


class BringUpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = FakeKube()
        self.hub.secrets[("argocd", "hub")] = hub_secret()
        self.clock = FakeClock()
        self.log: list[str] = []

    def run_up(self, *order: Stage) -> bringup.UpResult:
        return bringup.bring_up(
            order, self.hub, "argocd", "hub", self.log.append, self.clock.clock()
        )

    def test_all_ready(self) -> None:
        for sid in ("00", "07"):
            self.hub.add(APPLICATIONS, app(f"app-{sid}", sid))
        result = self.run_up(S00, S07)
        self.assertTrue(result.ok)
        self.assertEqual(result.ready, ["00", "07"])
        labels = self.hub.secrets[("argocd", "hub")]["metadata"]["labels"]
        self.assertEqual(labels["platform.aws/group-c"], "on")
        self.assertEqual(labels["platform.aws/stage-07"], "on")

    def test_failure_breaks_only_its_group(self) -> None:
        self.hub.add(APPLICATIONS, app("app-00", "00"))
        self.hub.add(APPLICATIONS, app("gpu", "03", health="Progressing"))
        self.hub.add(APPLICATIONS, app("app-07", "07"))
        result = self.run_up(S00, S03, S04, S07)
        self.assertFalse(result.ok)
        self.assertEqual(result.ready, ["00", "07"])
        self.assertIn("health=Progressing", result.failed["03"])
        self.assertEqual(result.skipped, ["04"])

    def test_base_failure_stops_everything(self) -> None:
        self.hub.add(APPLICATIONS, app("app-00", "00", sync="OutOfSync"))
        self.hub.add(APPLICATIONS, app("app-07", "07"))
        result = self.run_up(S00, S07)
        self.assertEqual(list(result.failed), ["00"])
        self.assertEqual(result.skipped, ["07"])


class WaitForArgoCdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = FakeKube()
        self.hub.store(APPLICATIONSETS)
        self.clock = FakeClock()

    def wait(self) -> str | None:
        return bringup.wait_for_argocd(self.hub, "argocd", 60, self.clock.clock())

    def test_ready(self) -> None:
        self.hub.secrets[("argocd", "hub")] = hub_secret()
        self.assertIsNone(self.wait())

    def test_retries_missing_crd(self) -> None:
        self.hub.secrets[("argocd", "hub")] = hub_secret()
        self.hub.fail_next = [KubeError(503, "Unavailable")]
        self.assertIsNone(self.wait())
        self.assertEqual(self.clock.sleeps, [5.0])

    def test_timeout_says_why(self) -> None:
        self.hub.fail_next = [KubeError(503, "Unavailable")] * 10
        problem = self.wait() or ""
        self.assertIn("timed out", problem)
        self.assertIn("Argo CD API not ready", problem)

    def test_permanent_error(self) -> None:
        self.hub.fail_next = [KubeError(403, "Forbidden", "rbac")]
        self.assertIn("Forbidden", self.wait() or "")

    def test_seed_group_labels(self) -> None:
        self.hub.secrets[("argocd", "hub")] = hub_secret()
        changed = bringup.seed_group_labels(self.hub, "argocd", "hub", ["base", "c"])
        self.assertEqual(changed, {"platform.aws/group-base": True, "platform.aws/group-c": True})
        self.assertEqual(bringup.seed_group_labels(self.hub, "argocd", "hub", ["base"]), {})
