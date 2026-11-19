import unittest

from fakes import FakeKube, hub_secret

from aif import labels
from aif.kube import KubeError
from aif.stages import Stage

STAGES = {
    "00": Stage("00", "t", "base", (), 1, ()),
    "01": Stage("01", "m", "base", ("00",), 1, ()),
    "03": Stage("03", "g", "b", ("00", "01"), 1, ()),
    "04": Stage("04", "i", "b", ("03",), 1, ()),
    "07": Stage("07", "t", "c", ("01",), 1, ()),
}


class LabelsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.kube = FakeKube()
        self.kube.secrets[("argocd", "hub")] = hub_secret()

    def labels(self) -> dict[str, str]:
        return labels.read_labels(self.kube, "argocd", "hub")

    def test_read_only_platform_labels(self) -> None:
        self.assertEqual(self.labels(), {})

    def test_missing_secret(self) -> None:
        with self.assertRaises(KubeError):
            labels.read_labels(self.kube, "argocd", "nope")

    def test_set_is_idempotent(self) -> None:
        wanted = {"platform.aws/stage-00": True}
        self.assertEqual(labels.set_labels(self.kube, "argocd", "hub", wanted), wanted)
        patches = [c for c in self.kube.calls if c[0] == "patch-secret"]
        self.assertEqual(labels.set_labels(self.kube, "argocd", "hub", wanted), {})
        self.assertEqual(len([c for c in self.kube.calls if c[0] == "patch-secret"]), len(patches))
        self.assertEqual(self.labels(), {"platform.aws/stage-00": "on"})

    def test_off_removes_label(self) -> None:
        labels.set_labels(self.kube, "argocd", "hub", {"platform.aws/stage-00": True})
        labels.set_labels(self.kube, "argocd", "hub", {"platform.aws/stage-00": False})
        self.assertEqual(self.labels(), {})
        stored = self.kube.secrets[("argocd", "hub")]["metadata"]["labels"]
        self.assertIn("argocd.argoproj.io/secret-type", stored)

    def test_on_labels_include_needs_and_groups(self) -> None:
        wanted = labels.on_labels(STAGES, "04")
        self.assertEqual(
            sorted(wanted),
            sorted(
                [
                    "platform.aws/stage-00",
                    "platform.aws/stage-01",
                    "platform.aws/stage-03",
                    "platform.aws/stage-04",
                    "platform.aws/group-base",
                    "platform.aws/group-b",
                ]
            ),
        )

    def test_off_labels_take_dependents_and_empty_groups(self) -> None:
        current = {s.label: "on" for s in STAGES.values()}
        wanted = labels.off_labels(STAGES, "03", current)
        self.assertEqual(
            wanted,
            {
                "platform.aws/stage-03": False,
                "platform.aws/stage-04": False,
                "platform.aws/group-b": False,
            },
        )

    def test_off_keeps_group_with_other_stage_on(self) -> None:
        current = {s.label: "on" for s in STAGES.values()}
        wanted = labels.off_labels(STAGES, "04", current)
        self.assertEqual(wanted, {"platform.aws/stage-04": False})

    def test_stage_state(self) -> None:
        state = labels.stage_state(STAGES, {"platform.aws/stage-00": "on"})
        self.assertTrue(state["00"])
        self.assertFalse(state["01"])
