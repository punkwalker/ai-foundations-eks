"""`aif cost`: sum the cost sheet per stage.

Rows whose `est_cost_usd` is not a number (for example `verify`) are counted as
unpriced and left out of the sums, so the total is a lower bound until every row
is priced.
"""

import csv
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

REQUIRED_COLUMNS = ("resource", "stage", "est_cost_usd")


class CostSheetError(ValueError):
    pass


@dataclass
class CostSummary:
    per_stage: dict[str, Decimal] = field(default_factory=dict)
    unpriced: dict[str, list[str]] = field(default_factory=dict)

    @property
    def total(self) -> Decimal:
        return sum(self.per_stage.values(), Decimal(0))

    @property
    def unpriced_count(self) -> int:
        return sum(len(v) for v in self.unpriced.values())


def _price(text: str) -> Decimal | None:
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def summarize(path: Path) -> CostSummary:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise CostSheetError(f"{path}: missing columns {missing}")
        summary = CostSummary()
        for row in reader:
            stage = row["stage"].strip()
            price = _price(row["est_cost_usd"])
            summary.per_stage.setdefault(stage, Decimal(0))
            if price is None:
                summary.unpriced.setdefault(stage, []).append(row["resource"])
            else:
                summary.per_stage[stage] += price
    return summary


def format_summary(summary: CostSummary) -> str:
    lines = [f"{'STAGE':<8}{'EST USD':>10}  UNPRICED ROWS"]
    for stage in sorted(summary.per_stage):
        unpriced = len(summary.unpriced.get(stage, []))
        lines.append(f"{stage:<8}{summary.per_stage[stage]:>10.2f}  {unpriced}")
    lines.append(f"{'total':<8}{summary.total:>10.2f}  {summary.unpriced_count}")
    if summary.unpriced_count:
        lines.append("The total is a lower bound: unpriced rows are not counted.")
    return "\n".join(lines)
