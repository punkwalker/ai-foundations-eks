"""Load, check and order the workshop stages.

Each `stages/NN-<name>/stage.yaml` declares one stage. The group rules are:

- Base stages need only Base stages.
- Stages in groups a to e need only Base stages or stages in their own group,
  so a failure in one group (for example a GPU shortage in group b) breaks only that group.
- Capstone stages may need any stage, and always run after every enabled group.
"""

import graphlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from aif.config import GROUPS

STAGE_FILE = "stage.yaml"
FIELDS = ("id", "name", "group", "needs", "budget_minutes", "health_checks")


class StageError(ValueError):
    """A stage.yaml file is wrong, or the stages break a group rule."""


@dataclass(frozen=True)
class Stage:
    id: str
    name: str
    group: str
    needs: tuple[str, ...]
    budget_minutes: int | None
    health_checks: tuple[str, ...]

    @property
    def label(self) -> str:
        """The Hub cluster secret label that turns this stage on."""
        return f"platform.aws/stage-{self.id}"


def _check_fields(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise StageError(f"{path}: expected a mapping")
    missing = [f for f in FIELDS if f not in data]
    extra = sorted(set(data) - set(FIELDS))
    if missing or extra:
        raise StageError(f"{path}: missing fields {missing}, unknown fields {extra}")
    return data


def _strings(value: Any, name: str, path: Path) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise StageError(f"{path}: {name} must be a list of strings")
    return tuple(value)


def parse_stage(data: Any, path: Path) -> Stage:
    """Build a Stage from one parsed stage.yaml document."""
    fields = _check_fields(data, path)
    stage_id = fields["id"]
    if not isinstance(stage_id, str) or len(stage_id) != 2 or not stage_id.isdigit():
        raise StageError(f"{path}: id must be a two-digit string, got {stage_id!r}")
    if not isinstance(fields["name"], str) or not fields["name"]:
        raise StageError(f"{path}: name must be a non-empty string")
    if fields["group"] not in GROUPS:
        raise StageError(f"{path}: unknown group {fields['group']!r}; known: {list(GROUPS)}")
    budget = fields["budget_minutes"]
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int)):
        raise StageError(f"{path}: budget_minutes must be a whole number or null")
    if budget is not None and budget < 0:
        raise StageError(f"{path}: budget_minutes must not be negative")
    return Stage(
        id=stage_id,
        name=fields["name"],
        group=fields["group"],
        needs=_strings(fields["needs"], "needs", path),
        budget_minutes=budget,
        health_checks=_strings(fields["health_checks"], "health_checks", path),
    )


def load_stages(stages_dir: Path) -> dict[str, Stage]:
    """Read every `NN-*/stage.yaml` under `stages_dir`, then validate them together."""
    stages: dict[str, Stage] = {}
    for path in sorted(stages_dir.glob(f"[0-9][0-9]-*/{STAGE_FILE}")):
        stage = parse_stage(yaml.safe_load(path.read_text(encoding="utf-8")), path)
        if not path.parent.name.startswith(f"{stage.id}-"):
            raise StageError(f"{path}: id {stage.id} does not match folder {path.parent.name}")
        if stage.id in stages:
            raise StageError(f"{path}: duplicate stage id {stage.id}")
        stages[stage.id] = stage
    if not stages:
        raise StageError(f"no {STAGE_FILE} files under {stages_dir}")
    validate(stages)
    return stages


def _group_rule_error(stage: Stage, need: Stage) -> str | None:
    if stage.group == "capstone" or need.group == "base":
        return None
    if stage.group == "base":
        return f"base stage {stage.id} must not need {need.id} (group {need.group})"
    if need.group != stage.group:
        return (
            f"stage {stage.id} (group {stage.group}) must not need {need.id} "
            f"(group {need.group}); groups a to e need only base or their own group"
        )
    return None


def validate(stages: Mapping[str, Stage]) -> None:
    """Check that needs exist, follow the group rules, and form a DAG."""
    problems: list[str] = []
    for stage in stages.values():
        for need in stage.needs:
            if need == stage.id:
                problems.append(f"stage {stage.id} needs itself")
            elif need not in stages:
                problems.append(f"stage {stage.id} needs unknown stage {need}")
            elif error := _group_rule_error(stage, stages[need]):
                problems.append(error)
    if problems:
        raise StageError("; ".join(problems))
    try:
        graphlib.TopologicalSorter({s.id: s.needs for s in stages.values()}).prepare()
    except graphlib.CycleError as err:
        raise StageError(f"stage needs form a cycle: {' -> '.join(err.args[1])}") from err


def _topological(stages: Mapping[str, Stage], ids: Iterable[str]) -> list[str]:
    wanted = set(ids)
    sorter = graphlib.TopologicalSorter(
        {i: [n for n in stages[i].needs if n in wanted] for i in sorted(wanted)}
    )
    sorter.prepare()
    ordered: list[str] = []
    while sorter.is_active():
        ready = sorted(sorter.get_ready())
        ordered.extend(ready)
        sorter.done(*ready)
    return ordered


def order(stages: Mapping[str, Stage], groups: Sequence[str]) -> list[Stage]:
    """Bring-up order for the enabled groups: needs first, then by id, capstone last."""
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise StageError(f"unknown groups {unknown}; known: {list(GROUPS)}")
    enabled = {"base", *groups}
    chosen = [s.id for s in stages.values() if s.group in enabled]
    ordered = _topological(stages, chosen)
    ordered.sort(key=lambda i: stages[i].group == "capstone")
    return [stages[i] for i in ordered]


def with_needs(stages: Mapping[str, Stage], stage_id: str) -> list[Stage]:
    """The stage and everything it needs, in bring-up order."""
    found: set[str] = set()
    todo = [stage_id]
    while todo:
        current = todo.pop()
        if current not in found:
            found.add(current)
            todo.extend(stages[current].needs)
    return [stages[i] for i in _topological(stages, found)]


def with_dependents(stages: Mapping[str, Stage], stage_id: str) -> list[Stage]:
    """The stage and every stage that needs it, in tear-down order (dependents first)."""
    found = {stage_id}
    changed = True
    while changed:
        changed = False
        for stage in stages.values():
            if stage.id not in found and found.intersection(stage.needs):
                found.add(stage.id)
                changed = True
    return [stages[i] for i in reversed(_topological(stages, found))]


def format_list(stages: Iterable[Stage]) -> str:
    rows = [f"{'ID':<4}{'GROUP':<10}{'BUDGET':<8}{'NAME'}"]
    for s in stages:
        budget = "-" if s.budget_minutes is None else f"{s.budget_minutes}m"
        rows.append(f"{s.id:<4}{s.group:<10}{budget:<8}{s.name}")
    return "\n".join(rows)


def format_graph(stages: Mapping[str, Stage]) -> str:
    """One line per stage: the stage, then the stages it needs."""
    lines = []
    for s in stages.values():
        needs = ", ".join(s.needs) if s.needs else "(nothing)"
        lines.append(f"{s.id} [{s.group}] needs {needs}")
    return "\n".join(lines)
