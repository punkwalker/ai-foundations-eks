"""The boto3 implementation of `HubApi` and `DevNetworkApi` that `aif up` and `aif down` use.

`aif.hub_api` defines the interfaces. The step functions live in the other modules of this
package. This module only joins them, one `HubApi` call at a time.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping

from botocore.exceptions import ClientError

from aif import devnet, launch
from aif.config import ArgoCdMode
from aif.hub import remove
from aif.hub.common import Report
from aif.hub.inputs import HubInputs, Idc
from aif.hub.names import (
    ACK_ROLE,
    ARGOCD_ROLE,
    CLUSTER_ROLE,
    HUB_CLUSTER,
    KRO_ROLE,
    NODE_ROLE,
    PLATFORM_CLUSTERS_ROLE,
    Param,
)
from aif.hub.up import HubBuilder, HubClients, HubOptions
from aif.hub_api import (
    CapabilityState,
    CapabilityType,
    ClusterState,
    HubError,
    LaunchInputs,
)
from aif.waiting import Clock

OUTPUT_PATHS = ("/aif/platform/", "/aif/clusters/", "/aif/capabilities/", "/aif/argocd/")
"""Where aif writes its outputs. The inputs sit directly under /aif/, so they are not here."""
ROLE_NAMES = (
    ACK_ROLE,
    KRO_ROLE,
    ARGOCD_ROLE,
    PLATFORM_CLUSTERS_ROLE,
    CLUSTER_ROLE,
    NODE_ROLE,
)
CAPABILITY_NAMES: Mapping[str, CapabilityType] = {"argocd": "argocd", "kro": "kro", "ack": "ack"}


def _hub_inputs(inputs: LaunchInputs, identity_store_id: str) -> HubInputs:
    idc = Idc(inputs.idc_instance_arn, identity_store_id) if inputs.idc_instance_arn else None
    return HubInputs(
        vpc_id=inputs.vpc_id,
        private_subnets=inputs.private_subnet_ids,
        admin_role_arn=inputs.admin_role_arn,
        ide_role_arn=inputs.ide_role_arn,
        ide_security_group=inputs.ide_security_group_id,
        idc=idc,
    )


class BotoHub:
    """`HubApi` over boto3. Waits inside `ensure_hub` and `ensure_capabilities` (up to 25 and
    30 minutes); the caller's own waits then return at once."""

    def __init__(
        self,
        clients: HubClients,
        region: str,
        tags: Mapping[str, str],
        options: HubOptions,
        clock: Clock | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.c = clients
        self.region = region
        self.tags = tags
        self.options = options
        self.clock = clock
        self.log = log
        self._builder: HubBuilder | None = None

    def _build(self, argocd_mode: str | None = None) -> HubBuilder:
        if self._builder is None or (
            argocd_mode is not None and argocd_mode != self.options.argocd_mode
        ):
            if argocd_mode is not None:
                self.options = HubOptions(
                    self.options.kubernetes_version,
                    self.options.public_cidrs,
                    _mode(argocd_mode),
                )
            self._builder = HubBuilder(
                self.c, self.region, self.tags, self.options, clock=self.clock
            )
        return self._builder

    def _identity_store(self) -> str:
        try:
            return self.c.ssm.get_parameter(Name=Param.IDENTITY_STORE)["Parameter"]["Value"]
        except ClientError:
            return ""

    def _found(self, inputs: LaunchInputs) -> HubInputs:
        found = _hub_inputs(inputs, self._identity_store())
        self._build().check(found)
        return found

    def ensure_platform(self, inputs: LaunchInputs) -> None:
        self._found(inputs)
        self.log("platform: boundary, KMS key, bucket, repositories, secrets")
        self._build().platform()

    def ensure_hub(self, inputs: LaunchInputs) -> None:
        self.log("hub: roles and the aif-hub cluster")
        self._build().cluster(self._found(inputs))

    def ensure_capabilities(self, inputs: LaunchInputs, argocd_mode: str) -> None:
        build = self._build(argocd_mode)
        found = self._found(inputs)
        self.log("hub: capabilities")
        build.capabilities(found)
        build.publish()

    def hub_status(self) -> ClusterState | None:
        try:
            found = self.c.eks.describe_cluster(name=HUB_CLUSTER)["cluster"]
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise
        return ClusterState(
            name=found["name"],
            arn=found["arn"],
            status=found["status"],
            endpoint=found.get("endpoint", ""),
            ca_data=found.get("certificateAuthority", {}).get("data", ""),
        )

    def capability_status(self) -> Mapping[CapabilityType, CapabilityState]:
        out: dict[CapabilityType, CapabilityState] = {}
        for name in self.c.eks.list_capabilities(clusterName=HUB_CLUSTER).get("capabilities", []):
            kind = CAPABILITY_NAMES.get(name["capabilityName"])
            if kind is None:
                continue
            found = self.c.eks.describe_capability(
                clusterName=HUB_CLUSTER, capabilityName=name["capabilityName"]
            )["capability"]
            issues = found.get("health", {}).get("issues", [])
            out[kind] = CapabilityState(
                type=kind,
                arn=found["arn"],
                status=found["status"],
                reason="; ".join(i.get("message", "") for i in issues),
            )
        return out

    def delete_capabilities(self) -> None:
        for kind in self.capability_status():
            self.c.eks.delete_capability(clusterName=HUB_CLUSTER, capabilityName=kind)

    def delete_hub(self) -> None:
        self.c.eks.delete_cluster(name=HUB_CLUSTER)

    def delete_platform(self) -> None:
        """Roles, boundary, KMS key, bucket, repositories, secrets, log group, groups, outputs."""
        c, tags, rep = self.c, self.tags, Report()
        scope = self._build().scope
        for name in ROLE_NAMES:
            remove.delete_role(c.iam, name, tags, rep)
        remove.delete_boundary(c.iam, scope, tags, rep)
        remove.delete_key(c.kms, tags, rep)
        remove.delete_bucket(c.s3, scope.account, self.region, tags, rep)
        remove.delete_repositories(c.ecr, tags, rep)
        remove.delete_secrets(c.secretsmanager, tags, rep)
        remove.delete_log_group(c.logs, rep)
        store = self._identity_store()
        if store:
            remove.delete_groups(c.identitystore, store, rep)
        names = [
            p["Name"]
            for path in OUTPUT_PATHS
            for page in c.ssm.get_paginator("get_parameters_by_path").paginate(
                Path=path, Recursive=True
            )
            for p in page["Parameters"]
        ]
        remove.delete_outputs(c.ssm, names, rep)
        for line in rep.lines:
            self.log(line)


def _mode(value: str) -> ArgoCdMode:
    if value == "helm":
        return "helm"
    if value == "capability":
        return "capability"
    raise HubError(f"argocd mode must be capability or helm, got {value!r}")


class BotoDevNetwork:
    """`DevNetworkApi` over boto3. The caller's own role gets cluster admin."""

    def __init__(
        self,
        clients: devnet.DevNetClients,
        region: str,
        tags: Mapping[str, str],
        log: Callable[[str], None] = print,
    ) -> None:
        self.c = clients
        self.region = region
        self.tags = tags
        self.log = log

    def _admin_role(self) -> str:
        """The role of the current credentials: assumed-role/NAME/session -> role/NAME."""
        arn = self.c.sts.get_caller_identity()["Arn"]
        parts = arn.split(":")
        kind, _, rest = parts[5].partition("/")
        if kind != "assumed-role":
            raise HubError(f"run as an assumed IAM role, not {arn!r}")
        return f"arn:{parts[1]}:iam::{parts[4]}:role/{rest.split('/')[0]}"

    def ensure(self) -> LaunchInputs:
        report = devnet.up(self.c, self.region, self.tags, self._admin_role(), dev_standin=True)
        for line in report.lines:
            self.log(line)
        return launch.read_inputs(self.c.ssm, self.region, "helm")

    def delete(self) -> None:
        for line in devnet.down(self.c, self.tags, dev_standin=True).lines:
            self.log(line)


def make_hub(region: str, tags: Mapping[str, str]) -> BotoHub:
    options = HubOptions.from_env(
        os.environ, _mode(os.environ.get("AIF_ARGOCD_MODE", "capability"))
    )
    return BotoHub(HubClients.create(region), region, tags, options)


def make_dev_network(region: str, tags: Mapping[str, str]) -> BotoDevNetwork:
    return BotoDevNetwork(devnet.DevNetClients.create(region), region, tags)


__all__ = ["BotoDevNetwork", "BotoHub", "make_dev_network", "make_hub"]
