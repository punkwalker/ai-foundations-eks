"""The interface between the aif command line and the Hub provisioning code.

Two sides use this file:

- `aif.cli`, `aif.bringup` and `aif.teardown` call these methods, own every wait,
  and decide the order.
- `aif.hub` implements them with boto3 only. It must provide two factories:

      def make_hub(region: str, tags: Mapping[str, str]) -> HubApi: ...
      def make_dev_network(region: str, tags: Mapping[str, str]) -> DevNetworkApi: ...

Rules for every method:

1. boto3 only. No subprocess, no shell, no AWS CLI.
2. Safe to call again. A resource that already exists, or is already gone, counts as done.
3. Never block on a long AWS operation. Start it and return. The caller polls the
   `*_status` methods with its own timeout.
4. Tag everything it creates with `tags` (at least `project`, plus `stage=00`).
5. Never touch the launch stack or anything tagged `aif:keep=true`.
6. Raise `HubError` with a short reason a person can act on. Never put secret values,
   tokens or keys in an error, a log line or a return value.
"""

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, cast

CapabilityType = Literal["argocd", "kro", "ack"]
CAPABILITY_TYPES: tuple[CapabilityType, ...] = ("argocd", "kro", "ack")
HUB_CLUSTER_NAME = "aif-hub"
SPOKE_CLUSTER_NAMES = ("aif-dev", "aif-prod")


class HubError(RuntimeError):
    """A Hub step failed. The message says why and what to do."""


@dataclass(frozen=True)
class LaunchInputs:
    """Values the launch stack (or `aif dev network`) publishes in SSM under /aif/."""

    region: str
    vpc_id: str
    public_subnet_ids: tuple[str, ...]
    private_subnet_ids: tuple[str, ...]
    admin_role_arn: str
    ide_role_arn: str
    ide_security_group_id: str
    launch_stack_name: str
    idc_instance_arn: str | None
    """Needed only in `capability` mode."""


@dataclass(frozen=True)
class ClusterState:
    """One EKS cluster as `eks:DescribeCluster` reports it."""

    name: str
    arn: str
    status: str
    """CREATING, ACTIVE, DELETING, FAILED, UPDATING or PENDING."""
    endpoint: str
    ca_data: str


@dataclass(frozen=True)
class CapabilityState:
    type: CapabilityType
    arn: str
    status: str
    """CREATING, ACTIVE, UPDATING, DELETING or CREATE_FAILED, as EKS reports it."""
    reason: str = ""
    """Why it is not ACTIVE, when EKS says. Empty otherwise."""


class HubApi(Protocol):
    """Create, read and remove the Hub cluster, its capabilities and the shared platform."""

    def ensure_platform(self, inputs: LaunchInputs) -> None:
        """Create the permission boundary, model bucket, ECR repositories, KMS key and
        generated secrets, if missing. Returns when the calls are made."""
        ...

    def ensure_hub(self, inputs: LaunchInputs) -> None:
        """Start creating the Hub EKS Auto Mode cluster and its roles, if missing.
        Access entries for `admin_role_arn` and `ide_role_arn`. Does not wait."""
        ...

    def hub_status(self) -> ClusterState | None:
        """The Hub cluster, or None if it does not exist."""
        ...

    def ensure_capabilities(self, inputs: LaunchInputs, argocd_mode: str) -> None:
        """Start creating the kro and ACK capabilities, plus Argo CD when
        `argocd_mode == "capability"`, if missing. Needs an ACTIVE Hub. Does not wait."""
        ...

    def capability_status(self) -> Mapping[CapabilityType, CapabilityState]:
        """Every capability that exists on the Hub. Missing types are left out."""
        ...

    def delete_capabilities(self) -> None:
        """Start deleting every capability on the Hub. Does not wait."""
        ...

    def delete_hub(self) -> None:
        """Start deleting the Hub cluster. Needs no capabilities left. Does not wait."""
        ...

    def delete_platform(self) -> None:
        """Delete what `ensure_platform` and `ensure_hub` created besides the cluster:
        roles, KMS key (scheduled deletion), bucket (emptied first, all versions),
        ECR repositories (with images) and secrets (no recovery window)."""
        ...


class DevNetworkApi(Protocol):
    """`aif dev network`: a stand-in for the launch stack in a development account."""

    def ensure(self) -> LaunchInputs:
        """Create the VPC, subnets, IDE role and security group, and publish the
        /aif/ SSM inputs, if missing. Returns the inputs it published."""
        ...

    def delete(self) -> None:
        """Remove everything `ensure` created, including the SSM inputs."""
        ...


def _factory(name: str) -> Callable[..., Any]:
    try:
        module = importlib.import_module("aif.hub")
    except ModuleNotFoundError as err:
        if err.name != "aif.hub":
            raise
        raise HubError("aif.hub is not built yet, so aif cannot create the Hub") from err
    factory = getattr(module, name, None)
    if not callable(factory):
        raise HubError(f"aif.hub has no {name}() factory")
    return cast(Callable[..., Any], factory)


def load_hub(region: str, tags: Mapping[str, str]) -> HubApi:
    """Build the HubApi from aif.hub.make_hub."""
    return cast(HubApi, _factory("make_hub")(region, tags))


def load_dev_network(region: str, tags: Mapping[str, str]) -> DevNetworkApi:
    """Build the DevNetworkApi from aif.hub.make_dev_network."""
    return cast(DevNetworkApi, _factory("make_dev_network")(region, tags))
