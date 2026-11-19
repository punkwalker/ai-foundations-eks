"""The shared KMS key, the control-plane log group, and the Hub EKS Auto Mode cluster."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from botocore.exceptions import ClientError

from aif.hub.common import HubError, Report, Tags, code, tag_list
from aif.hub.names import CLUSTER_ADMIN_POLICY, KEY_ALIAS, control_plane_log_group
from aif.waiting import Clock, until

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_eks import EKSClient
    from mypy_boto3_kms import KMSClient
    from mypy_boto3_logs import CloudWatchLogsClient

LOG_RETENTION_DAYS = 7
LOG_TYPES = ("api", "audit", "authenticator", "controllerManager", "scheduler")
BUILT_IN_NODE_POOLS = ("general-purpose", "system")
CLUSTER_TIMEOUT = 25 * 60.0


@dataclass(frozen=True)
class ClusterSpec:
    name: str
    version: str
    role_arn: str
    node_role_arn: str
    key_arn: str
    subnets: Sequence[str]
    public_cidrs: Sequence[str] = ()
    """Empty keeps the API private to the VPC."""


def ensure_key(kms: "KMSClient", tags: Tags, report: Report) -> str:
    """The aif key encrypts Kubernetes secrets on every aif cluster. Found by alias."""
    try:
        key_id = kms.describe_key(KeyId=KEY_ALIAS)["KeyMetadata"]["Arn"]
        report.kept(f"kms key {KEY_ALIAS}")
        return key_id
    except ClientError as err:
        if code(err) != "NotFoundException":
            raise
    meta = kms.create_key(
        Description="aif: encrypts Kubernetes secrets on the aif clusters",
        KeyUsage="ENCRYPT_DECRYPT",
        KeySpec="SYMMETRIC_DEFAULT",
        Tags=tag_list(tags, "TagKey", "TagValue"),
    )["KeyMetadata"]
    kms.enable_key_rotation(KeyId=meta["KeyId"])
    kms.create_alias(AliasName=KEY_ALIAS, TargetKeyId=meta["KeyId"])
    report.created(f"kms key {KEY_ALIAS}")
    return meta["Arn"]


def ensure_log_group(
    logs: "CloudWatchLogsClient", cluster: str, tags: Tags, report: Report
) -> None:
    """Create the log group EKS writes to, first, so it has a short retention and our tags."""
    name = control_plane_log_group(cluster)
    try:
        logs.create_log_group(logGroupName=name, tags=dict(tags))
        report.created(f"log group {name}")
    except ClientError as err:
        if code(err) != "ResourceAlreadyExistsException":
            raise
        report.kept(f"log group {name}")
    logs.put_retention_policy(logGroupName=name, retentionInDays=LOG_RETENTION_DAYS)


def _vpc_config(spec: ClusterSpec) -> Any:
    config: dict[str, Any] = {
        "subnetIds": list(spec.subnets),
        "endpointPrivateAccess": True,
        "endpointPublicAccess": bool(spec.public_cidrs),
    }
    if spec.public_cidrs:
        config["publicAccessCidrs"] = list(spec.public_cidrs)
    return config


def _describe(eks: "EKSClient", name: str) -> dict[str, Any] | None:
    try:
        return dict(eks.describe_cluster(name=name)["cluster"])
    except ClientError as err:
        if code(err) == "ResourceNotFoundException":
            return None
        raise


def wait_active(eks: "EKSClient", name: str, clock: Clock | None, timeout: float) -> dict[str, Any]:
    def check() -> tuple[bool, dict[str, Any]]:
        found = _describe(eks, name) or {"status": "MISSING"}
        if found["status"] == "FAILED":
            raise HubError(f"cluster {name} is FAILED; see the EKS console health tab")
        return found["status"] == "ACTIVE", found

    return until(
        check,
        timeout=timeout,
        what=f"cluster {name} to be ACTIVE",
        describe=lambda c: f"status {c.get('status')}",
        interval=15.0,
        max_interval=60.0,
        clock=clock,
    )


def ensure_cluster(
    eks: "EKSClient",
    spec: ClusterSpec,
    tags: Tags,
    report: Report,
    *,
    clock: Clock | None = None,
    timeout: float = CLUSTER_TIMEOUT,
) -> dict[str, Any]:
    """Create the Auto Mode cluster or check the existing one, then wait until ACTIVE."""
    found = _describe(eks, spec.name)
    if found is None:
        eks.create_cluster(
            name=spec.name,
            version=spec.version,
            roleArn=spec.role_arn,
            resourcesVpcConfig=_vpc_config(spec),
            kubernetesNetworkConfig={"ipFamily": "ipv4", "elasticLoadBalancing": {"enabled": True}},
            logging=cast(Any, {"clusterLogging": [{"types": list(LOG_TYPES), "enabled": True}]}),
            accessConfig={
                "authenticationMode": "API",
                "bootstrapClusterCreatorAdminPermissions": False,
            },
            encryptionConfig=[{"resources": ["secrets"], "provider": {"keyArn": spec.key_arn}}],
            computeConfig={
                "enabled": True,
                "nodePools": list(BUILT_IN_NODE_POOLS),
                "nodeRoleArn": spec.node_role_arn,
            },
            storageConfig={"blockStorage": {"enabled": True}},
            bootstrapSelfManagedAddons=False,
            upgradePolicy={"supportType": "STANDARD"},
            tags=dict(tags),
        )
        report.created(f"eks cluster {spec.name} ({spec.version})")
        return wait_active(eks, spec.name, clock, timeout)
    if found.get("tags", {}).get("project") != tags.get("project"):
        raise HubError(
            f"cluster {spec.name} exists without the project tag; aif will not change it"
        )
    found = wait_active(eks, spec.name, clock, timeout)
    vpc = found.get("resourcesVpcConfig", {})
    want = _vpc_config(spec)
    have_cidrs = sorted(vpc.get("publicAccessCidrs", [])) if vpc.get("endpointPublicAccess") else []
    if bool(vpc.get("endpointPublicAccess")) != want[
        "endpointPublicAccess"
    ] or have_cidrs != sorted(spec.public_cidrs):
        update: Any = {k: v for k, v in want.items() if k != "subnetIds"}
        update_id = eks.update_cluster_config(name=spec.name, resourcesVpcConfig=update)["update"][
            "id"
        ]
        _wait_update(eks, spec.name, update_id, clock)
        report.updated(f"eks cluster {spec.name} endpoint access")
    else:
        report.kept(f"eks cluster {spec.name}")
    return found


def _wait_update(eks: "EKSClient", name: str, update_id: str, clock: Clock | None) -> None:
    def check() -> tuple[bool, str]:
        status = eks.describe_update(name=name, updateId=update_id)["update"]["status"]
        if status in ("Failed", "Cancelled"):
            raise HubError(f"update {update_id} on {name} ended {status}")
        return status == "Successful", status

    until(
        check,
        timeout=15 * 60.0,
        what=f"update {update_id} on {name}",
        describe=lambda s: f"status {s}",
        interval=15.0,
        max_interval=60.0,
        clock=clock,
    )


def allow_api_from(
    ec2: "EC2Client", cluster_sg: str, source_sg: str, tags: Tags, report: Report
) -> None:
    """Only the IDE security group may reach the private API, on 443."""
    try:
        ec2.authorize_security_group_ingress(
            GroupId=cluster_sg,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 443,
                    "ToPort": 443,
                    "UserIdGroupPairs": [
                        {"GroupId": source_sg, "Description": "Kubernetes API from the aif IDE"}
                    ],
                }
            ],
            TagSpecifications=cast(
                Any, [{"ResourceType": "security-group-rule", "Tags": tag_list(tags)}]
            ),
        )
        report.created(f"ingress 443 {source_sg} -> {cluster_sg}")
    except ClientError as err:
        if code(err) != "InvalidPermission.Duplicate":
            raise
        report.kept(f"ingress 443 {source_sg} -> {cluster_sg}")


def ensure_admin_entry(
    eks: "EKSClient", cluster: str, principal: str, tags: Tags, report: Report
) -> None:
    """Access entry with cluster admin for one role."""
    try:
        eks.create_access_entry(
            clusterName=cluster, principalArn=principal, type="STANDARD", tags=dict(tags)
        )
        report.created(f"access entry {principal.rsplit('/', 1)[-1]} on {cluster}")
    except ClientError as err:
        if code(err) != "ResourceInUseException":
            raise
        report.kept(f"access entry {principal.rsplit('/', 1)[-1]} on {cluster}")
    eks.associate_access_policy(
        clusterName=cluster,
        principalArn=principal,
        policyArn=CLUSTER_ADMIN_POLICY,
        accessScope={"type": "cluster"},
    )
