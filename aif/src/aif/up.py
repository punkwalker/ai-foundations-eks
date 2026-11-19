"""The `aif up` sequence, from the launch inputs to ready stages.

1. Create the shared platform and start the Hub cluster (HubApi).
2. Wait for the Hub, then write its kubeconfig context.
3. Start the capabilities and wait until each is ACTIVE.
4. Wait for the Argo CD API, then write the Hub cluster Secret and the group labels.
5. Argo CD syncs the platform. Its kro PlatformCluster RGD creates Dev and Prod
   through ACK. Wait until both clusters are ACTIVE and their Argo CD cluster
   Secrets exist, then write their kubeconfig contexts.
6. Turn the stages on in order and wait for each (bringup.bring_up).

Every wait has a timeout and raises UpError that says what was not ready.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from aif import bringup, eks
from aif.argocd import is_ready, list_applications, report
from aif.config import Settings
from aif.hub_api import (
    CAPABILITY_TYPES,
    CapabilityState,
    CapabilityType,
    ClusterState,
    HubApi,
    HubError,
    LaunchInputs,
)
from aif.kube import KubeApi, KubeError
from aif.stages import Stage
from aif.waiting import Clock, WaitTimeoutError, until

if TYPE_CHECKING:
    from mypy_boto3_eks import EKSClient

HUB_TIMEOUT = 1800.0
CAPABILITY_TIMEOUT = 1800.0
ARGOCD_TIMEOUT = 900.0
SPOKE_TIMEOUT = 2700.0
FAILED_STATES = frozenset({"FAILED", "CREATE_FAILED", "DELETE_FAILED"})
SPOKES = ("dev", "prod")


class UpError(RuntimeError):
    """A bring-up step did not finish. The message says which and why."""


def wanted_capabilities(argocd_mode: str) -> tuple[CapabilityType, ...]:
    return CAPABILITY_TYPES if argocd_mode == "capability" else ("kro", "ack")


def plan_lines(settings: Settings, groups: Sequence[str], order: Sequence[Stage]) -> list[str]:
    caps = ", ".join(wanted_capabilities(settings.argocd_mode))
    return [
        "plan: 1. create the platform (boundary, bucket, ECR, KMS, secrets) and the Hub cluster",
        f"plan: 2. wait up to {HUB_TIMEOUT:.0f}s for aif-hub, write kubeconfig context aif-hub",
        f"plan: 3. create capabilities {caps}, wait up to {CAPABILITY_TIMEOUT:.0f}s",
        f"plan: 4. write the Hub cluster Secret and groups {', '.join(groups)}",
        f"plan: 5. wait up to {SPOKE_TIMEOUT:.0f}s for aif-dev and aif-prod from the platform",
        f"plan: 6. turn on {len(order)} stages in order",
        bringup.format_plan(order),
    ]


@dataclass
class Up:
    settings: Settings
    hub_api: HubApi
    eks_client: "EKSClient"
    kube: Callable[[str], KubeApi]
    kubeconfig: Path
    clock: Clock = field(default_factory=Clock)
    log: Callable[[str], None] = print

    def run(self, inputs: LaunchInputs, groups: Sequence[str], order: Sequence[Stage]) -> bool:
        s = self.settings
        self.hub_api.ensure_platform(inputs)
        self.hub_api.ensure_hub(inputs)
        hub_state = self.wait_for_hub()
        self._write_kubeconfig([eks.from_state("hub", hub_state)])
        self.hub_api.ensure_capabilities(inputs, s.argocd_mode)
        self.wait_for_capabilities()
        hub = self.kube(s.contexts.hub)
        problem = bringup.wait_for_argocd(hub, s.argocd_namespace, ARGOCD_TIMEOUT, self.clock)
        if problem:
            raise UpError(problem)
        names = {"hub": s.hub_cluster}
        hub_info = eks.from_state("hub", hub_state)
        eks.apply_cluster_secrets(hub, s.argocd_namespace, [hub_info], s.argocd_mode, names)
        bringup.seed_group_labels(hub, s.argocd_namespace, s.hub_cluster, groups)
        self.log(f"Hub cluster Secret {s.argocd_namespace}/{s.hub_cluster} and group labels set")
        self.wait_for_spokes(hub)
        self._write_kubeconfig(eks.describe(self.eks_client, SPOKES))
        result = bringup.bring_up(
            order, hub, s.argocd_namespace, s.hub_cluster, self.log, self.clock
        )
        return result.ok

    def _write_kubeconfig(self, clusters: Sequence[eks.ClusterInfo]) -> None:
        path = eks.write_kubeconfig(self.kubeconfig, clusters, self.settings.region)
        self.log(f"kubeconfig: {', '.join(c.context for c in clusters)} in {path}")

    def _wait(self, check: Callable[[], tuple[bool, str]], what: str, timeout: float) -> None:
        try:
            until(check, timeout=timeout, what=what, describe=str, interval=15.0, clock=self.clock)
        except WaitTimeoutError as err:
            raise UpError(str(err)) from err

    def wait_for_hub(self) -> ClusterState:
        seen: list[ClusterState] = []

        def check() -> tuple[bool, str]:
            state = self.hub_api.hub_status()
            if state is None:
                return False, "aif-hub does not exist yet"
            if state.status in FAILED_STATES:
                raise HubError(f"aif-hub is {state.status}")
            seen[:] = [state]
            return state.status == "ACTIVE", f"aif-hub is {state.status}"

        self._wait(check, "the Hub cluster", HUB_TIMEOUT)
        return seen[0]

    def wait_for_capabilities(self) -> Mapping[CapabilityType, CapabilityState]:
        wanted = wanted_capabilities(self.settings.argocd_mode)
        seen: dict[CapabilityType, CapabilityState] = {}

        def check() -> tuple[bool, str]:
            states = self.hub_api.capability_status()
            seen.clear()
            seen.update(states)
            failed = [st for t in wanted if (st := states.get(t)) and st.status in FAILED_STATES]
            if failed:
                raise HubError(
                    "; ".join(f"capability {f.type} is {f.status}: {f.reason}" for f in failed)
                )
            waiting = [
                f"{t}={states[t].status if t in states else 'MISSING'}"
                for t in wanted
                if t not in states or states[t].status != "ACTIVE"
            ]
            return not waiting, "not ACTIVE: " + ", ".join(waiting)

        self._wait(check, "the Hub capabilities", CAPABILITY_TIMEOUT)
        return seen

    def wait_for_spokes(self, hub: KubeApi) -> None:
        ns = self.settings.argocd_namespace

        def check() -> tuple[bool, str]:
            states = {k: eks.status(self.eks_client, k) for k in SPOKES}
            try:
                secrets = eks.secret_clusters(hub, ns)
            except KubeError as err:
                return False, f"cannot list cluster Secrets: {err}"
            missing = [k for k in SPOKES if k not in secrets]
            not_active = [f"aif-{k}={v}" for k, v in states.items() if v != "ACTIVE"]
            done = not missing and not not_active
            why = f"clusters {not_active or 'ACTIVE'}, Secrets missing for {missing or 'none'}"
            return done, f"{why}; {self._unhealthy(hub)}"

        self._wait(check, "the Dev and Prod clusters", SPOKE_TIMEOUT)

    def _unhealthy(self, hub: KubeApi) -> str:
        try:
            apps = list_applications(hub, self.settings.argocd_namespace)
        except KubeError as err:
            return f"cannot list Applications: {err}"
        bad = [str(report(a)) for a in apps if not is_ready(a)]
        return "unhealthy Applications: " + ("; ".join(bad) if bad else "none")
