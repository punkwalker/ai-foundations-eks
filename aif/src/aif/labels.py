"""Turn stages and groups on or off with labels on the Hub cluster secret.

The stage ApplicationSets select clusters by these labels, for example
`platform.aws/stage-03: "on"` and `platform.aws/group-b: "on"`.
Turning a label off removes it. Every function here is safe to run again.
"""

from collections.abc import Mapping

from aif.kube import KubeApi, KubeError
from aif.stages import Stage, with_dependents, with_needs

PREFIX = "platform.aws/"
ON = "on"


def group_label(group: str) -> str:
    return f"{PREFIX}group-{group}"


def read_labels(api: KubeApi, namespace: str, secret: str) -> dict[str, str]:
    """The `platform.aws/` labels on the Hub cluster secret."""
    obj = api.get_secret(namespace, secret)
    if obj is None:
        raise KubeError(404, "NotFound", f"cluster secret {namespace}/{secret} does not exist")
    labels = obj.get("metadata", {}).get("labels") or {}
    return {k: v for k, v in labels.items() if k.startswith(PREFIX)}


def set_labels(
    api: KubeApi, namespace: str, secret: str, wanted: Mapping[str, bool]
) -> dict[str, bool]:
    """Set each label on (True) or off (False). Returns only the labels that changed."""
    current = read_labels(api, namespace, secret)
    changes = {k: on for k, on in wanted.items() if (current.get(k) == ON) != on}
    if changes:
        patch = {k: (ON if on else None) for k, on in changes.items()}
        api.patch_secret(namespace, secret, {"metadata": {"labels": patch}})
    return changes


def on_labels(stages: Mapping[str, Stage], stage_id: str) -> dict[str, bool]:
    """Labels to turn on for a stage: the stage, what it needs, and their groups."""
    wanted: dict[str, bool] = {}
    for stage in with_needs(stages, stage_id):
        wanted[stage.label] = True
        wanted[group_label(stage.group)] = True
    return wanted


def off_labels(
    stages: Mapping[str, Stage], stage_id: str, current: Mapping[str, str]
) -> dict[str, bool]:
    """Labels to turn off for a stage: the stage, every stage that needs it, and any
    group that has no stage left on."""
    going = {s.id for s in with_dependents(stages, stage_id)}
    wanted = {stages[i].label: False for i in going}
    still_on = {
        s.group for s in stages.values() if s.id not in going and current.get(s.label) == ON
    }
    for group in {stages[i].group for i in going} - still_on:
        wanted[group_label(group)] = False
    return wanted


def stage_state(stages: Mapping[str, Stage], current: Mapping[str, str]) -> dict[str, bool]:
    return {s.id: current.get(s.label) == ON for s in stages.values()}
