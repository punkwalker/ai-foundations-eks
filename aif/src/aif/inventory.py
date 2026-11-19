"""Build the teardown inventory from four sources.

1. Argo CD Applications on the Hub.
2. kro instances on each cluster. aif finds the instance kinds from the RGDs, so new
   RGDs need no code change.
3. ACK resources on each cluster: every CRD whose group ends in `.services.k8s.aws`.
4. An AWS tag sweep with the Resource Groups Tagging API, by the `project` tag.

The plan groups items by stage, in teardown order: the last stage to come up goes first.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from aif.argocd import APPLICATIONS, STAGE_LABEL, list_applications
from aif.keep import is_kept
from aif.kube import KubeApi, KubeError, Obj, Resource, list_crds
from aif.stages import Stage

if TYPE_CHECKING:
    from mypy_boto3_resourcegroupstaggingapi import ResourceGroupsTaggingAPIClient

Source = Literal["argocd", "kro", "ack", "aws"]
SOURCES: tuple[Source, ...] = ("argocd", "kro", "ack", "aws")
UNASSIGNED = "unassigned"
RGDS = Resource("kro.run", "v1alpha1", "resourcegraphdefinitions", "ResourceGraphDefinition", False)
ACK_GROUP_SUFFIX = ".services.k8s.aws"


@dataclass(frozen=True)
class Item:
    source: Source
    where: str
    """Cluster name (hub, dev, prod), or `aws` for the tag sweep."""
    kind: str
    name: str
    stage: str
    namespace: str | None = None
    resource: Resource | None = None
    arn: str = ""

    def __str__(self) -> str:
        ns = f"{self.namespace}/" if self.namespace else ""
        return f"{self.source:<7}{self.where:<6}{self.kind} {ns}{self.name}"


@dataclass
class Inventory:
    items: list[Item] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Sources that could not be read, and why."""

    def by_stage(self, stage_order: Sequence[str]) -> list[tuple[str, list[Item]]]:
        """Items per stage. Stages follow `stage_order`, unassigned items come last.
        Inside a stage: Applications, then kro, then ACK, then AWS."""
        known = [*stage_order, UNASSIGNED]
        extra = sorted({i.stage for i in self.items} - set(known))
        groups: list[tuple[str, list[Item]]] = []
        for stage in [*known, *extra]:
            items = [i for i in self.items if i.stage == stage]
            if items:
                items.sort(key=lambda i: (SOURCES.index(i.source), i.where, i.kind, i.name))
                groups.append((stage, items))
        return groups


def teardown_order(bring_up: Sequence[Stage]) -> list[str]:
    """Stage ids in reverse bring-up order."""
    return [s.id for s in reversed(bring_up)]


def _stage_of(obj: Obj, fallback: str = UNASSIGNED) -> str:
    labels = obj.get("metadata", {}).get("labels") or {}
    return str(labels.get(STAGE_LABEL, fallback))


def _k8s_item(source: Source, where: str, res: Resource, obj: Obj, fallback: str) -> Item:
    meta = obj.get("metadata", {})
    arn = obj.get("status", {}).get("ackResourceMetadata", {}).get("arn", "")
    return Item(
        source=source,
        where=where,
        kind=res.kind or res.plural,
        name=str(meta.get("name")),
        namespace=meta.get("namespace"),
        stage=_stage_of(obj, fallback),
        resource=res,
        arn=str(arn),
    )


def applications(api: KubeApi, namespace: str) -> list[Item]:
    return [
        _k8s_item("argocd", "hub", APPLICATIONS, app, UNASSIGNED)
        for app in list_applications(api, namespace)
    ]


def kro_instances(api: KubeApi, where: str, crds: Iterable[Resource]) -> list[Item]:
    """Every instance of every kind that an RGD on this cluster defines."""
    by_kind = {(r.group, r.kind): r for r in crds}
    items: list[Item] = []
    for rgd in api.list_objects(RGDS):
        schema = rgd.get("spec", {}).get("schema", {})
        res = by_kind.get((schema.get("group") or "kro.run", schema.get("kind")))
        if res is None:
            continue  # kro has not created the CRD yet, so there are no instances
        rgd_stage = _stage_of(rgd)
        items.extend(_k8s_item("kro", where, res, o, rgd_stage) for o in api.list_objects(res))
    return items


def ack_resources(api: KubeApi, where: str, crds: Iterable[Resource]) -> list[Item]:
    items: list[Item] = []
    for res in crds:
        if res.group.endswith(ACK_GROUP_SUFFIX):
            items.extend(_k8s_item("ack", where, res, o, UNASSIGNED) for o in api.list_objects(res))
    return items


def tag_sweep(
    tagging: "ResourceGroupsTaggingAPIClient", project: str, keep_stacks: Iterable[str] = ()
) -> list[Item]:
    """Every AWS resource in this Region tagged `project=<project>`, minus kept ones.

    A resource is kept when it is a kept stack, belongs to one, or is tagged aif:keep=true.
    """
    keep = tuple(keep_stacks)
    items: list[Item] = []
    pages = tagging.get_paginator("get_resources").paginate(
        TagFilters=[{"Key": "project", "Values": [project]}]
    )
    for page in pages:
        for res in page.get("ResourceTagMappingList", []):
            tags = {t["Key"]: t["Value"] for t in res.get("Tags", [])}
            arn = res.get("ResourceARN", "")
            if is_kept(arn, tags, keep):
                continue
            items.append(
                Item(
                    source="aws",
                    where="aws",
                    kind=_arn_kind(arn),
                    name=arn.rsplit(":", 1)[-1],
                    stage=tags.get("stage", UNASSIGNED),
                    arn=arn,
                )
            )
    return items


def _arn_kind(arn: str) -> str:
    """`arn:aws:s3:::bucket` gives `s3`; `arn:aws:ec2:r:a:vpc/vpc-1` gives `ec2/vpc`."""
    parts = arn.split(":", 5)
    if len(parts) < 6:
        return "unknown"
    resource = parts[5]
    sub = resource.split("/", 1)[0] if "/" in resource else ""
    return f"{parts[2]}/{sub}" if sub else parts[2]


def build(
    clusters: Mapping[str, KubeApi],
    argocd_namespace: str,
    tagging: "ResourceGroupsTaggingAPIClient | None",
    project: str,
    keep_stacks: Iterable[str] = (),
) -> Inventory:
    """Read all four sources. A cluster that cannot be read becomes a note, not a crash."""
    inv = Inventory()
    hub = clusters.get("hub")
    if hub is not None:
        try:
            inv.items.extend(applications(hub, argocd_namespace))
        except KubeError as err:
            inv.notes.append(f"hub: cannot list Argo CD Applications: {err}")
    for where, api in clusters.items():
        try:
            crds = list_crds(api)
        except KubeError as err:
            inv.notes.append(f"{where}: cannot list CRDs: {err}")
            continue
        try:
            inv.items.extend(kro_instances(api, where, crds))
        except KubeError as err:
            inv.notes.append(f"{where}: cannot list kro instances: {err}")
        try:
            inv.items.extend(ack_resources(api, where, crds))
        except KubeError as err:
            inv.notes.append(f"{where}: cannot list ACK resources: {err}")
    if tagging is not None:
        inv.items.extend(tag_sweep(tagging, project, keep_stacks))
    return inv


def format_plan(inv: Inventory, stage_order: Sequence[str]) -> str:
    lines: list[str] = []
    for stage, items in inv.by_stage(stage_order):
        lines.append(f"stage {stage}: {len(items)} items")
        lines.extend(f"  {item}" for item in items)
    if not lines:
        lines.append("nothing to remove")
    lines.extend(f"note: {n}" for n in inv.notes)
    return "\n".join(lines)
