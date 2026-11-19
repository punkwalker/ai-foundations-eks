import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from aif import cost

REPO_SHEET = Path(__file__).resolve().parents[2] / "cost" / "cost-sheet.csv"
HEADER = (
    "resource,stage,qty,unit_price_usd_per_hour,hours,est_cost_usd,price_source,created_by,notes\n"
)


class CostTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "cost-sheet.csv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_sums_per_stage_and_total(self) -> None:
        self.path.write_text(
            HEADER
            + "EKS,00,3,0.10,8,2.40,api,W2,\n"
            + "NAT,00,1,0.045,8,0.36,api,W2,\n"
            + "GPU,03-06,1,0.80,4,3.20,api,W4,\n"
            + "Sandbox,09-10,1,verify,verify,verify,verify,W5,\n"
            + "Bad,09-10,1,x,x,NaN,x,W5,\n"
        )
        summary = cost.summarize(self.path)
        self.assertEqual(summary.per_stage["00"], Decimal("2.76"))
        self.assertEqual(summary.per_stage["03-06"], Decimal("3.20"))
        self.assertEqual(summary.per_stage["09-10"], Decimal(0))
        self.assertEqual(summary.total, Decimal("5.96"))
        self.assertEqual(summary.unpriced, {"09-10": ["Sandbox", "Bad"]})
        text = cost.format_summary(summary)
        self.assertIn("total         5.96  2", text)
        self.assertIn("lower bound", text)

    def test_missing_column(self) -> None:
        self.path.write_text("resource,stage\nx,00\n")
        with self.assertRaises(cost.CostSheetError):
            cost.summarize(self.path)

    def test_repo_sheet_loads(self) -> None:
        summary = cost.summarize(REPO_SHEET)
        self.assertIn("00", summary.per_stage)
        self.assertNotIn("lower bound", cost.format_summary(cost.CostSummary()))
