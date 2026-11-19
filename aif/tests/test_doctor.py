import unittest

from fakes import FakeKube, app, hub_secret

from aif import doctor
from aif.argocd import APPLICATIONS
from aif.aws import Identity
from aif.config import Settings
from aif.kube import KubeError
from aif.stages import Stage

STAGES = {"00": Stage("00", "t", "base", (), 8, ()), "03": Stage("03", "g", "b", ("00",), 5, ())}


class DoctorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = FakeKube()
        self.hub.secrets[("argocd", "hub")] = hub_secret(
            {"platform.aws/stage-00": "on", "platform.aws/group-base": "on"}
        )
        self.hub.add(APPLICATIONS, app("ok", "00"))
        self.hub.add(APPLICATIONS, app("bad", "00", health="Degraded"))

    def test_full_report(self) -> None:
        rep = doctor.collect(
            Settings(), STAGES, self.hub, lambda: Identity("111122223333", "Admin")
        )
        self.assertEqual(rep.argocd_mode, "capability")
        self.assertIn("IAM Identity Center", rep.sign_in)
        self.assertEqual(rep.stages, {"00": True, "03": False})
        self.assertTrue(rep.groups["base"])
        self.assertEqual([r.app for r in rep.unhealthy], ["bad"])
        self.assertEqual(set(rep.identity.values()), {"unknown"})
        text = doctor.format_report(rep)
        self.assertIn("account 111122223333, role Admin", text)
        self.assertIn("Stages on:     00", text)
        self.assertIn("Identity keycloak:", text)
        self.assertNotIn("arn:", text)

    def test_helm_mode_sign_in(self) -> None:
        rep = doctor.collect(Settings(argocd_mode="helm"), STAGES, None, None)
        self.assertIn("admin account", rep.sign_in)
        self.assertIn("Hub cluster: no kubeconfig context", rep.errors)
        self.assertIn("AWS identity:  unknown", doctor.format_report(rep))

    def test_errors_do_not_crash(self) -> None:
        def no_creds() -> Identity:
            raise OSError("no credentials")

        self.hub.fail_next = [KubeError(403, "Forbidden"), KubeError(503, "Unavailable")]
        rep = doctor.collect(Settings(), STAGES, self.hub, no_creds)
        self.assertEqual(len(rep.errors), 3)
        self.assertIn("Groups on:     unknown", doctor.format_report(rep))
