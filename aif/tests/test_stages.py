import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml

from aif import stages
from aif.stages import Stage, StageError

REPO_STAGES = Path(__file__).resolve().parents[2] / "stages"


def st(sid: str, group: str, needs: tuple[str, ...] = (), budget: int | None = 1) -> Stage:
    return Stage(sid, f"stage {sid}", group, needs, budget, ())


def as_map(*items: Stage) -> dict[str, Stage]:
    return {s.id: s for s in items}


class RepoStagesTest(unittest.TestCase):
    """The 14 real stage.yaml files load and follow the rules."""

    def setUp(self) -> None:
        self.stages = stages.load_stages(REPO_STAGES)

    def test_all_fourteen_load(self) -> None:
        self.assertEqual(list(self.stages), [f"{i:02d}" for i in range(14)])

    def test_every_stage_except_capstone_has_budget_and_checks(self) -> None:
        for s in self.stages.values():
            with self.subTest(stage=s.id):
                self.assertTrue(s.health_checks)
                if s.group != "capstone":
                    self.assertIsNotNone(s.budget_minutes)

    def test_groups_follow_group_rules(self) -> None:
        groups = {s.id: s.group for s in self.stages.values()}
        self.assertEqual(
            groups,
            {
                "00": "base",
                "01": "base",
                "02": "a",
                "03": "b",
                "04": "b",
                "05": "b",
                "06": "b",
                "07": "c",
                "08": "c",
                "09": "d",
                "10": "d",
                "11": "e",
                "12": "e",
                "13": "capstone",
            },
        )

    def test_order_for_group_b_only(self) -> None:
        ids = [s.id for s in stages.order(self.stages, ["b"])]
        self.assertEqual(ids, ["00", "01", "03", "04", "05", "06"])

    def test_order_all_groups_puts_capstone_last(self) -> None:
        ids = [s.id for s in stages.order(self.stages, ["a", "b", "c", "d", "e", "capstone"])]
        self.assertEqual(ids[-1], "13")
        self.assertEqual(ids[:2], ["00", "01"])
        self.assertEqual(len(ids), 14)


class ParseTest(unittest.TestCase):
    def good(self, **change: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "id": "03",
            "name": "GPU",
            "group": "b",
            "needs": ["00"],
            "budget_minutes": 5,
            "health_checks": ["x"],
        }
        data.update(change)
        return data

    def test_parse_good(self) -> None:
        s = stages.parse_stage(self.good(), Path("x"))
        self.assertEqual(s.label, "platform.aws/stage-03")
        self.assertEqual(s.needs, ("00",))

    def test_parse_rejects_bad_fields(self) -> None:
        bad = [
            "not a mapping",
            {"id": "03"},
            self.good(extra=1),
            self.good(id=3),
            self.good(id="3"),
            self.good(name=""),
            self.good(group="z"),
            self.good(budget_minutes="5"),
            self.good(budget_minutes=True),
            self.good(budget_minutes=-1),
            self.good(needs="00"),
            self.good(health_checks=[1]),
        ]
        for data in bad:
            with self.subTest(data=data), self.assertRaises(StageError):
                stages.parse_stage(data, Path("x"))


class LoadTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(
        self, folder: str, sid: str, group: str = "base", needs: list[str] | None = None
    ) -> None:
        path = self.dir / folder
        path.mkdir()
        data = {
            "id": sid,
            "name": "n",
            "group": group,
            "needs": needs or [],
            "budget_minutes": 1,
            "health_checks": [],
        }
        (path / "stage.yaml").write_text(yaml.safe_dump(data))

    def test_empty_folder(self) -> None:
        with self.assertRaises(StageError):
            stages.load_stages(self.dir)

    def test_id_must_match_folder(self) -> None:
        self.write("00-a", "01")
        with self.assertRaisesRegex(StageError, "does not match"):
            stages.load_stages(self.dir)

    def test_duplicate_id(self) -> None:
        self.write("00-a", "00")
        self.write("00-b", "00")
        with self.assertRaisesRegex(StageError, "duplicate"):
            stages.load_stages(self.dir)


class ValidateTest(unittest.TestCase):
    def test_unknown_and_self_needs(self) -> None:
        with self.assertRaisesRegex(StageError, "unknown stage 09"):
            stages.validate(as_map(st("00", "base", ("09",))))
        with self.assertRaisesRegex(StageError, "needs itself"):
            stages.validate(as_map(st("00", "base", ("00",))))

    def test_group_rules(self) -> None:
        base_needs_b = as_map(st("00", "base", ("03",)), st("03", "b"))
        with self.assertRaisesRegex(StageError, "base stage 00"):
            stages.validate(base_needs_b)
        d_needs_c = as_map(st("00", "base"), st("08", "c", ("00",)), st("09", "d", ("08",)))
        with self.assertRaisesRegex(StageError, "groups a to e"):
            stages.validate(d_needs_c)
        capstone_needs_any = as_map(st("00", "base"), st("03", "b"), st("13", "capstone", ("03",)))
        stages.validate(capstone_needs_any)

    def test_cycle(self) -> None:
        cyclic = as_map(st("03", "b", ("04",)), st("04", "b", ("03",)))
        with self.assertRaisesRegex(StageError, "cycle"):
            stages.validate(cyclic)


class OrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stages = as_map(
            st("00", "base"),
            st("01", "base", ("00",)),
            st("03", "b", ("01",)),
            st("04", "b", ("03",)),
            st("07", "c", ("01",)),
            st("13", "capstone", ("00",)),
        )

    def test_unknown_group(self) -> None:
        with self.assertRaises(StageError):
            stages.order(self.stages, ["z"])

    def test_base_is_always_included(self) -> None:
        self.assertEqual([s.id for s in stages.order(self.stages, [])], ["00", "01"])

    def test_with_needs(self) -> None:
        self.assertEqual(
            [s.id for s in stages.with_needs(self.stages, "04")], ["00", "01", "03", "04"]
        )

    def test_with_dependents_is_reverse(self) -> None:
        ids = [s.id for s in stages.with_dependents(self.stages, "01")]
        self.assertEqual(ids[-1], "01")
        self.assertLess(ids.index("04"), ids.index("03"))
        self.assertNotIn("00", ids)

    def test_formatting(self) -> None:
        text = stages.format_list(self.stages.values())
        self.assertIn("04  b         1m      stage 04", text)
        graph = stages.format_graph(self.stages)
        self.assertIn("00 [base] needs (nothing)", graph)
        self.assertIn("04 [b] needs 03", graph)
        no_budget = stages.format_list([st("13", "capstone", budget=None)])
        self.assertIn(" -  ", no_budget)
