import unittest

from fakes import FakeClock, FakeKube, app, obj

from aif import argocd
from aif.argocd import APPLICATIONS, APPLICATIONSETS
from aif.kube import KubeError


class ArgoCdTest(unittest.TestCase):
    def setUp(self) -> None:
        self.kube = FakeKube()
        self.clock = FakeClock()

    def add(self, *apps: dict[str, object]) -> None:
        for a in apps:
            self.kube.add(APPLICATIONS, a)

    def test_list_and_appsets(self) -> None:
        self.add(app("a", "00"), app("b", "01"))
        self.kube.add(APPLICATIONSETS, obj("stage-00", "argocd"))
        names = [a["metadata"]["name"] for a in argocd.list_applications(self.kube, "argocd")]
        self.assertEqual(names, ["a", "b"])
        sets = argocd.list_applicationsets(self.kube, "argocd")
        self.assertEqual(sets[0]["metadata"]["name"], "stage-00")
        only = argocd.list_applications(self.kube, "argocd", "platform.aws/stage=01")
        self.assertEqual([a["metadata"]["name"] for a in only], ["b"])

    def test_hard_refresh_sets_annotation(self) -> None:
        self.add(app("a", "00"), app("b", "00"))
        self.assertEqual(argocd.refresh_all(self.kube, "argocd"), ["a", "b"])
        stored = self.kube.get_object(APPLICATIONS, "a", "argocd") or {}
        self.assertEqual(stored["metadata"]["annotations"], {argocd.REFRESH_ANNOTATION: "hard"})

    def test_report_fields(self) -> None:
        bad = app(
            "a",
            "00",
            sync="OutOfSync",
            health="Degraded",
            conditions=[{"type": "SyncError", "message": "boom"}],
            operationState={"message": "one or more objects failed"},
        )
        r = argocd.report(bad)
        self.assertEqual(
            (r.app, r.sync, r.health, r.condition, r.last_operation),
            ("a", "OutOfSync", "Degraded", "SyncError: boom", "one or more objects failed"),
        )
        self.assertIn("health=Degraded", str(r))
        empty = argocd.report({"metadata": {"name": "x"}, "status": "odd"})
        self.assertEqual((empty.sync, empty.health), ("Unknown", "Unknown"))

    def test_missing_cascade_finalizer(self) -> None:
        apps = [app("keep", finalizer=False), app("good")]
        self.assertEqual(argocd.missing_cascade_finalizer(apps), ["keep"])

    def test_wait_ready(self) -> None:
        self.add(app("a", "00"))
        result = argocd.wait_for_apps(self.kube, "argocd", "", 60, clock=self.clock.clock())
        self.assertTrue(result.ok)
        self.assertEqual(result.ready, ("a",))
        self.assertIn("1 Applications", result.summary())

    def test_wait_retries_transient_then_succeeds(self) -> None:
        self.add(app("a", "00"))
        self.kube.fail_next = [
            KubeError(404, "NotFound", "the server could not find the requested resource"),
            KubeError(500, "Internal", "failed calling webhook x"),
        ]
        result = argocd.wait_for_apps(self.kube, "argocd", "", 60, clock=self.clock.clock())
        self.assertTrue(result.ok)
        self.assertEqual(self.clock.sleeps, [5.0, 10.0])

    def test_wait_stops_on_permanent_error(self) -> None:
        self.kube.fail_next = [KubeError(403, "Forbidden", "rbac")]
        result = argocd.wait_for_apps(self.kube, "argocd", "", 60, clock=self.clock.clock())
        self.assertFalse(result.ok)
        self.assertIn("Forbidden", result.error)

    def test_wait_timeout_reports_each_bad_app(self) -> None:
        self.add(app("a", "00"), app("b", "00", health="Progressing"))
        result = argocd.wait_for_apps(self.kube, "argocd", "", 30, clock=self.clock.clock())
        self.assertFalse(result.ok)
        self.assertEqual([r.app for r in result.reports], ["b"])
        self.assertIn("timed out", result.summary())
        self.assertIn("health=Progressing", result.summary())

    def test_wait_timeout_with_no_apps(self) -> None:
        self.kube.store(APPLICATIONS)
        result = argocd.wait_for_apps(self.kube, "argocd", "x=y", 10, clock=self.clock.clock())
        self.assertFalse(result.ok)
        self.assertIn("no Applications match", result.error)

    def test_wait_timeout_when_last_read_fails(self) -> None:
        self.kube.fail_next = [KubeError(503, "Unavailable")] * 3
        result = argocd.wait_for_apps(self.kube, "argocd", "", 10, clock=self.clock.clock())
        self.assertFalse(result.ok)
        self.assertEqual(result.reports, ())
