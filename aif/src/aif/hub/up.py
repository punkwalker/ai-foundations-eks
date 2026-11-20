"""`aif hub up`: bring up the Hub in order. Each step is safe to run again.

1. aif-infra-boundary, then the roles under /aif/ (cluster, node, capabilities, spokes).
2. The aif KMS key and the control-plane log group.
3. The Hub cluster, ACTIVE; the API rule for the IDE; access entries for admin and IDE.
4. aif IdC groups (capability mode only), then the ACK, kro and Argo CD capabilities.
5. Model bucket, ECR repositories, empty secrets.
6. Outputs to SSM under /aif/.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import boto3

from aif.config import ArgoCdMode
from aif.hub import capabilities, cluster, iam, inputs, storage
from aif.hub.common import HubError, Report
from aif.hub.names import (
    ACK_ROLE,
    ACK_ROLE_PATH,
    ARGOCD_ROLE,
    CLUSTER_ROLE,
    CLUSTER_ROLE_POLICIES,
    ECR_REPOSITORIES,
    HUB_CLUSTER,
    KRO_ROLE,
    NODE_ROLE,
    NODE_ROLE_POLICIES,
    PLATFORM_CLUSTERS_ROLE,
    SECRETS,
    STAGE_TAG,
    Param,
    model_bucket,
)
from aif.hub.policies import (
    Scope,
    ack_capability,
    argocd_capability,
    platform_clusters,
    trust,
    trust_role,
)
from aif.waiting import Clock

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ecr import ECRClient
    from mypy_boto3_eks import EKSClient
    from mypy_boto3_iam import IAMClient
    from mypy_boto3_identitystore import IdentityStoreClient
    from mypy_boto3_kms import KMSClient
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_secretsmanager import SecretsManagerClient
    from mypy_boto3_ssm import SSMClient
    from mypy_boto3_sts import STSClient

DEFAULT_VERSION = "1.37"
"""Newest EKS version on Nov 19, 2026. Nitro Sandboxes need 1.36 or later."""
MIN_PUBLIC_PREFIX = 16


@dataclass(frozen=True)
class HubOptions:
    """Hub settings that are not SSM inputs. Defaults keep the API private."""

    kubernetes_version: str = DEFAULT_VERSION
    public_cidrs: tuple[str, ...] = ()
    argocd_mode: ArgoCdMode = "capability"

    @staticmethod
    def from_env(
        env: Mapping[str, str] | None = None, argocd_mode: ArgoCdMode = "capability"
    ) -> HubOptions:
        """`AIF_HUB_K8S_VERSION` and `AIF_HUB_PUBLIC_CIDRS` (comma separated, empty by default)."""
        env = os.environ if env is None else env
        cidrs = tuple(
            c.strip() for c in env.get("AIF_HUB_PUBLIC_CIDRS", "").split(",") if c.strip()
        )
        for cidr in cidrs:
            check_cidr(cidr)
        return HubOptions(
            kubernetes_version=env.get("AIF_HUB_K8S_VERSION", DEFAULT_VERSION),
            public_cidrs=cidrs,
            argocd_mode=argocd_mode,
        )


def check_cidr(cidr: str) -> str:
    """Refuse 0.0.0.0/0 and anything broader than /16."""
    try:
        net = ipaddress.IPv4Network(cidr, strict=True)
    except ValueError as err:
        raise HubError(f"not an IPv4 CIDR: {cidr!r}") from err
    if net.prefixlen < MIN_PUBLIC_PREFIX:
        raise HubError(f"CIDR {cidr} is broader than /{MIN_PUBLIC_PREFIX}; name your own address")
    return cidr


@dataclass
class HubClients:
    sts: STSClient
    ssm: SSMClient
    iam: IAMClient
    kms: KMSClient
    logs: CloudWatchLogsClient
    eks: EKSClient
    ec2: EC2Client
    identitystore: IdentityStoreClient
    s3: S3Client
    ecr: ECRClient
    secretsmanager: SecretsManagerClient

    @staticmethod
    def create(region: str) -> HubClients:
        s = boto3.Session(region_name=region)
        return HubClients(
            sts=s.client("sts"),
            ssm=s.client("ssm"),
            iam=s.client("iam"),
            kms=s.client("kms"),
            logs=s.client("logs"),
            eks=s.client("eks"),
            ec2=s.client("ec2"),
            identitystore=s.client("identitystore"),
            s3=s.client("s3"),
            ecr=s.client("ecr"),
            secretsmanager=s.client("secretsmanager"),
        )


@dataclass
class HubResult:
    report: Report = field(default_factory=Report)
    outputs: dict[str, str] = field(default_factory=dict)


def scope_of(sts: STSClient, region: str) -> Scope:
    arn = sts.get_caller_identity()["Arn"]
    return Scope(partition=arn.split(":", 2)[1], account=arn.split(":")[4], region=region)


def role_specs(scope: Scope, key_arn: str, argocd: bool) -> list[iam.RoleSpec]:
    cap_trust = trust("capabilities.eks.amazonaws.com")
    specs = [
        iam.RoleSpec(
            CLUSTER_ROLE,
            trust("eks.amazonaws.com"),
            "EKS Auto Mode cluster role for aif-hub",
            managed=CLUSTER_ROLE_POLICIES,
        ),
        iam.RoleSpec(
            NODE_ROLE,
            trust("ec2.amazonaws.com", tag_session=False),
            "EKS Auto Mode node role for aif clusters",
            managed=NODE_ROLE_POLICIES,
        ),
        iam.RoleSpec(
            ACK_ROLE,
            cap_trust,
            "ACK capability; only assumes roles under /aif/ack/",
            inline={"assume-ack-selector-roles": ack_capability(scope)},
        ),
        iam.RoleSpec(KRO_ROLE, cap_trust, "kro capability; needs no AWS permissions"),
        iam.RoleSpec(
            PLATFORM_CLUSTERS_ROLE,
            trust_role(scope.iam(f"role/aif/{ACK_ROLE}")),
            "ACK builds Dev and Prod from the PlatformCluster RGD with this role",
            path=ACK_ROLE_PATH,
            inline={"platform-clusters": platform_clusters(scope, key_arn)},
        ),
    ]
    if argocd:
        specs.append(
            iam.RoleSpec(
                ARGOCD_ROLE,
                cap_trust,
                "Argo CD capability; reads aif/argocd/* secrets",
                inline={"read-argocd-secrets": argocd_capability(scope)},
            )
        )
    return specs


class HubBuilder:
    """The Hub steps, each safe to run again. `up()` runs them in order; `BotoHub` runs them
    one at a time for `aif up`."""

    def __init__(
        self,
        c: HubClients,
        region: str,
        project_tags: Mapping[str, str],
        options: HubOptions,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.c = c
        self.region = region
        self.options = options
        self.clock = clock
        self.tags = {**project_tags, "stage": STAGE_TAG}
        self.report = Report()
        self.scope = scope_of(c.sts, region)
        self.argocd = options.argocd_mode == "capability"
        self.boundary_arn = ""
        self.key_arn = ""
        self.roles: dict[str, str] = {}
        self.hub: dict[str, Any] = {}
        self.cap_arns: dict[str, str] = {}
        self.groups: dict[str, str] = {}

    def check(self, found: inputs.HubInputs) -> None:
        if self.argocd and found.idc is None:
            raise HubError(
                f"argocd mode is capability but {Param.IDC_INSTANCE} is not set. The launch "
                "template creates IAM Identity Center; or set argocd_mode = helm."
            )

    def platform(self) -> None:
        """Boundary, KMS key, bucket, repositories and secrets."""
        c, tags, rep = self.c, self.tags, self.report
        self.boundary_arn = iam.ensure_boundary(c.iam, self.scope, tags, rep)
        self.key_arn = cluster.ensure_key(c.kms, tags, rep)
        bucket = model_bucket(self.scope.account, self.region)
        storage.ensure_bucket(c.s3, bucket, self.scope, tags, rep)
        for repo in ECR_REPOSITORIES:
            storage.ensure_repository(c.ecr, repo, tags, rep)
        for secret in SECRETS:
            storage.ensure_secret(c.secretsmanager, secret, tags, rep)

    def cluster(self, found: inputs.HubInputs) -> None:
        """Roles, log group, the Hub cluster (waits until ACTIVE) and its access entries."""
        c, tags, rep = self.c, self.tags, self.report
        self.platform_ready()
        self.roles = {
            s.name: iam.ensure_role(c.iam, s, self.scope, tags, rep)
            for s in role_specs(self.scope, self.key_arn, self.argocd)
        }
        cluster.ensure_log_group(c.logs, HUB_CLUSTER, tags, rep)
        spec = cluster.ClusterSpec(
            name=HUB_CLUSTER,
            version=self.options.kubernetes_version,
            role_arn=self.roles[CLUSTER_ROLE],
            node_role_arn=self.roles[NODE_ROLE],
            key_arn=self.key_arn,
            subnets=found.private_subnets,
            public_cidrs=self.options.public_cidrs,
        )
        self.hub = cluster.ensure_cluster(c.eks, spec, tags, rep, clock=self.clock)
        sg = self.hub["resourcesVpcConfig"]["clusterSecurityGroupId"]
        cluster.allow_api_from(c.ec2, sg, found.ide_security_group, tags, rep)
        for principal in dict.fromkeys((found.admin_role_arn, found.ide_role_arn)):
            cluster.ensure_admin_entry(c.eks, HUB_CLUSTER, principal, tags, rep)

    def platform_ready(self) -> None:
        if not self.key_arn:
            self.key_arn = cluster.ensure_key(self.c.kms, self.tags, self.report)
        if not self.boundary_arn:
            self.boundary_arn = iam.ensure_boundary(self.c.iam, self.scope, self.tags, self.report)

    def capabilities(self, found: inputs.HubInputs) -> None:
        """ACK and kro, plus Argo CD in capability mode. Waits until each is ACTIVE."""
        c, tags, rep = self.c, self.tags, self.report
        if not self.roles:
            self.cluster(found)
        caps: list[capabilities.CapabilitySpec] = [
            capabilities.CapabilitySpec("ack", "ACK", self.roles[ACK_ROLE]),
            capabilities.CapabilitySpec("kro", "KRO", self.roles[KRO_ROLE]),
        ]
        if self.argocd and found.idc is not None:
            self.groups = capabilities.ensure_groups(
                c.identitystore, found.idc.identity_store_id, rep
            )
            caps.append(
                capabilities.CapabilitySpec(
                    "argocd",
                    "ARGOCD",
                    self.roles[ARGOCD_ROLE],
                    capabilities.argocd_configuration(
                        found.idc.instance_arn, self.region, self.groups
                    ),
                )
            )
        self.cap_arns = {
            s.name: capabilities.ensure_capability(
                c.eks, HUB_CLUSTER, s, tags, rep, clock=self.clock
            )
            for s in caps
        }

    def publish(self) -> dict[str, str]:
        """Write the outputs to SSM under /aif/."""
        hub = self.hub
        issuer = str(hub.get("identity", {}).get("oidc", {}).get("issuer", ""))
        outputs = {
            Param.BOUNDARY_ARN: self.boundary_arn,
            Param.KEY_ARN: self.key_arn,
            Param.MODEL_BUCKET: model_bucket(self.scope.account, self.region),
            Param.ARGOCD_MODE: self.options.argocd_mode,
            Param.cluster("hub", "name"): HUB_CLUSTER,
            Param.cluster("hub", "arn"): str(hub["arn"]),
            Param.cluster("hub", "endpoint"): str(hub["endpoint"]),
            Param.cluster("hub", "oidc-issuer-id"): issuer.rsplit("/", 1)[-1],
            Param.cluster("hub", "security-group-id"): hub["resourcesVpcConfig"][
                "clusterSecurityGroupId"
            ],
            Param.cluster("hub", "node-role-arn"): self.roles[NODE_ROLE],
            Param.cluster("hub", "platform-clusters-role-arn"): self.roles[PLATFORM_CLUSTERS_ROLE],
            **{Param.capability(name): arn for name, arn in self.cap_arns.items()},
            **{Param.argocd_group(role): gid for role, gid in self.groups.items()},
        }
        if self.argocd:
            outputs[Param.cluster("hub", "argocd-role-arn")] = self.roles[ARGOCD_ROLE]
        storage.publish(self.c.ssm, outputs, self.tags)
        return outputs


def up(
    c: HubClients,
    region: str,
    project_tags: Mapping[str, str],
    options: HubOptions,
    *,
    clock: Clock | None = None,
    log: Any = print,
) -> HubResult:
    """Bring up the Hub. Raises HubError or inputs.InputError with what to fix."""
    found = inputs.read(c.ssm)
    build = HubBuilder(c, region, project_tags, options, clock=clock)
    build.check(found)
    log("step 1/4: IAM boundary, KMS key, bucket, repositories, secrets")
    build.platform()
    log("step 2/4: roles and the Hub cluster (up to 25 minutes on first run)")
    build.cluster(found)
    log("step 3/4: capabilities")
    build.capabilities(found)
    log("step 4/4: outputs to SSM")
    return HubResult(report=build.report, outputs=build.publish())


def needed_inputs() -> Sequence[str]:
    return inputs.REQUIRED + inputs.OPTIONAL
