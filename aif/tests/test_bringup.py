import unittest

from fakes import FakeClock, FakeKube, app, hub_secret

from aif import bringup
from aif.argocd import APPLICATIONS
from aif.stages import Stage

S00 = Stage("00", "tenancy", "base", (), 8, ())
S03 = Stage("03", "gpu", "b", ("00",), 5, ())
S04 = Stage("04", "inference", "b", ("03",), 6, ())
S07 = Stage("07", "tools", "c", ("00",), 3, ())
S13 = Stage("13", "capstone", "capstone", ("00",), None, ())


class TimeoutTest(unittest.TestCase):
    def test_timeouts(self) -> None:
        self.assertEqual(bringup.stage_timeout(S00), 960)
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
