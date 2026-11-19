"""`aif doctor`: what mode the lab runs in, and what is unhealthy."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar

from aif.argocd import AppReport, is_ready, list_applications, report
from aif.aws import Identity
from aif.config import GROUPS, ArgoCdMode, Settings
from aif.kube import KubeApi, KubeError
from aif.labels import ON, group_label, read_labels, stage_state
from aif.stages import Stage

T = TypeVar("T")

SIGN_IN: Mapping[ArgoCdMode, str] = {
    "capability": "IAM Identity Center (Keycloak as external IdP)",
    "helm": "Argo CD admin account, facilitators only (fallback)",
}
UNKNOWN = "unknown"
IDENTITY_CHECKS = ("keycloak", "saml-trust", "scim-sync", "sign-in-test")
"""Identity checks. They report `unknown` until the identity module exists."""


@dataclass
class Report:
    argocd_mode: ArgoCdMode
    sign_in: str
    aws: Identity | None = None
    stages: dict[str, bool] = field(default_factory=dict)
    groups: dict[str, bool] = field(default_factory=dict)
    unhealthy: list[AppReport] = field(default_factory=list)
    identity: dict[str, str] = field(
        default_factory=lambda: dict.fromkeys(IDENTITY_CHECKS, UNKNOWN)
    )
    errors: list[str] = field(default_factory=list)


def _try[T](errors: list[str], what: str, fn: Callable[[], T]) -> T | None:
    try:
        return fn()
    except (KubeError, OSError) as err:
        errors.append(f"{what}: {err}")
        return None


def collect(
    settings: Settings,
    stages: Mapping[str, Stage],
    hub: KubeApi | None,
    whoami: Callable[[], Identity] | None,
) -> Report:
    """Gather the report. Each failed check becomes an error line; doctor never crashes."""
    rep = Report(argocd_mode=settings.argocd_mode, sign_in=SIGN_IN[settings.argocd_mode])
    if whoami is not None:
        rep.aws = _try(rep.errors, "AWS identity", whoami)
    if hub is None:
        rep.errors.append("Hub cluster: no kubeconfig context")
        return rep
    ns = settings.argocd_namespace
    labels = _try(rep.errors, "Hub labels", lambda: read_labels(hub, ns, settings.hub_cluster))
    if labels is not None:
        rep.stages = stage_state(stages, labels)
        rep.groups = {g: labels.get(group_label(g)) == ON for g in GROUPS}
    apps = _try(rep.errors, "Argo CD Applications", lambda: list_applications(hub, ns))
    if apps is not None:
        rep.unhealthy = [report(a) for a in apps if not is_ready(a)]
    return rep


def _on(state: Mapping[str, bool]) -> str:
    return ", ".join(k for k, v in state.items() if v) or "none"


def format_report(rep: Report) -> str:
    who = f"account {rep.aws.account}, role {rep.aws.role}" if rep.aws else UNKNOWN
    lines = [
        f"Argo CD mode:  {rep.argocd_mode}",
        f"Sign-in:       {rep.sign_in}",
        f"AWS identity:  {who}",
        f"Groups on:     {_on(rep.groups) if rep.groups else UNKNOWN}",
        f"Stages on:     {_on(rep.stages) if rep.stages else UNKNOWN}",
        *(f"Identity {k + ':':<14}{v}" for k, v in rep.identity.items()),
        f"Unhealthy Applications: {len(rep.unhealthy)}",
        *(f"  {r}" for r in rep.unhealthy),
        *(f"error: {e}" for e in rep.errors),
    ]
    return "\n".join(lines)
