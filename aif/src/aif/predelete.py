"""Steps that clear the way before AWS resources can be deleted.

Each step is a small function that takes a boto3 client. Every step is safe to run
again: a resource that is already gone counts as done.

A resource without the project tag is touched only when `include_untagged` is True.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_ecr import ECRClient
    from mypy_boto3_elbv2 import ElasticLoadBalancingv2Client
    from mypy_boto3_iam import IAMClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef
    from mypy_boto3_secretsmanager import SecretsManagerClient

AGENT_ROLE_PATH = "/aif/agents/"
GONE_CODES = frozenset(
    {
        "NoSuchBucket",
        "RepositoryNotFoundException",
        "ResourceNotFoundException",
        "NoSuchEntity",
        "InvalidNetworkInterfaceID.NotFound",
        "LoadBalancerNotFound",
    }
)


@dataclass
class StepResult:
    """What one step did. `skipped` lists resources left alone, with the reason."""

    step: str
    done: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def _gone(err: ClientError) -> bool:
    return err.response.get("Error", {}).get("Code") in GONE_CODES


def has_project_tag(tags: Iterable[Mapping[str, Any]], project: str) -> bool:
    """True if a boto3 tag list has `project=<project>`. Handles Key/Value and key/value."""
    for tag in tags:
        key = tag.get("Key", tag.get("key"))
        value = tag.get("Value", tag.get("value"))
        if key == "project" and value == project:
            return True
    return False


def empty_bucket(s3: "S3Client", bucket: str) -> StepResult:
    """Delete every object version and delete marker, so the bucket can be deleted."""
    result = StepResult("empty-s3-bucket")
    try:
        pages = s3.get_paginator("list_object_versions").paginate(Bucket=bucket)
        for page in pages:
            keys: list[ObjectIdentifierTypeDef] = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for v in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]
            ]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys, "Quiet": True})
    except ClientError as err:
        if not _gone(err):
            raise
    result.done.append(bucket)
    return result


def force_delete_ecr_repository(ecr: "ECRClient", repository: str) -> StepResult:
    """Delete a repository and all its images."""
    result = StepResult("force-delete-ecr-repository")
    try:
        ecr.delete_repository(repositoryName=repository, force=True)
    except ClientError as err:
        if not _gone(err):
            raise
    result.done.append(repository)
    return result


def delete_secret_now(sm: "SecretsManagerClient", secret_id: str) -> StepResult:
    """Delete a secret with no recovery window, so a new lab can reuse the name."""
    result = StepResult("delete-secret-no-recovery")
    try:
        sm.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
    except ClientError as err:
        if not _gone(err):
            raise
    result.done.append(secret_id)
    return result


@dataclass(frozen=True)
class Blocker:
    """Something that keeps a VPC from being deleted."""

    kind: str
    id: str
    tagged: bool
    detail: str


def vpc_blockers(
    ec2: "EC2Client", elbv2: "ElasticLoadBalancingv2Client", vpc_id: str, project: str
) -> list[Blocker]:
    """Load balancers and network interfaces still in the VPC."""
    blockers: list[Blocker] = []
    lbs = [
        lb
        for page in elbv2.get_paginator("describe_load_balancers").paginate()
        for lb in page.get("LoadBalancers", [])
        if lb.get("VpcId") == vpc_id
    ]
    if lbs:
        arns = [lb["LoadBalancerArn"] for lb in lbs]
        tags = {
            d["ResourceArn"]: d.get("Tags", [])
            for i in range(0, len(arns), 20)
            for d in elbv2.describe_tags(ResourceArns=arns[i : i + 20])["TagDescriptions"]
        }
        for lb in lbs:
            arn = lb["LoadBalancerArn"]
            tagged = has_project_tag(tags.get(arn, []), project)
            blockers.append(Blocker("load-balancer", arn, tagged, lb.get("LoadBalancerName", "")))
    enis = ec2.get_paginator("describe_network_interfaces").paginate(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
    )
    for page in enis:
        for eni in page.get("NetworkInterfaces", []):
            blockers.append(
                Blocker(
                    "network-interface",
                    eni["NetworkInterfaceId"],
                    has_project_tag(eni.get("TagSet", []), project),
                    f"status={eni.get('Status')} {eni.get('Description', '')}".strip(),
                )
            )
    return blockers


def clear_vpc_blockers(
    ec2: "EC2Client",
    elbv2: "ElasticLoadBalancingv2Client",
    blockers: Iterable[Blocker],
    include_untagged: bool,
) -> StepResult:
    """Delete load balancers, and network interfaces that are no longer attached.

    An attached interface belongs to something else (a node, an endpoint) and is
    reported, not deleted: it goes away when its owner is deleted.
    """
    result = StepResult("clear-vpc-blockers")
    for b in blockers:
        if not b.tagged and not include_untagged:
            result.skipped.append(f"{b.kind} {b.id}: no project tag (use --include-untagged)")
            continue
        if b.kind == "network-interface" and not b.detail.startswith("status=available"):
            result.skipped.append(f"{b.kind} {b.id}: still attached ({b.detail})")
            continue
        try:
            if b.kind == "load-balancer":
                elbv2.delete_load_balancer(LoadBalancerArn=b.id)
            else:
                ec2.delete_network_interface(NetworkInterfaceId=b.id)
        except ClientError as err:
            if not _gone(err):
                raise
        result.done.append(f"{b.kind} {b.id}")
    return result


def _delete_role(iam: "IAMClient", name: str) -> None:
    for attached in iam.get_paginator("list_attached_role_policies").paginate(RoleName=name):
        for policy in attached.get("AttachedPolicies", []):
            iam.detach_role_policy(RoleName=name, PolicyArn=policy["PolicyArn"])
    for inline in iam.get_paginator("list_role_policies").paginate(RoleName=name):
        for policy_name in inline.get("PolicyNames", []):
            iam.delete_role_policy(RoleName=name, PolicyName=policy_name)
    profiles = iam.get_paginator("list_instance_profiles_for_role").paginate(RoleName=name)
    for listing in profiles:
        for profile in listing.get("InstanceProfiles", []):
            iam.remove_role_from_instance_profile(
                InstanceProfileName=profile["InstanceProfileName"], RoleName=name
            )
    iam.delete_role(RoleName=name)


def delete_agent_roles(
    iam: "IAMClient", project: str, include_untagged: bool, path: str = AGENT_ROLE_PATH
) -> StepResult:
    """Delete the IAM roles that labs create for agents, under `path`."""
    result = StepResult("delete-agent-roles")
    for page in iam.get_paginator("list_roles").paginate(PathPrefix=path):
        for role in page.get("Roles", []):
            name = role["RoleName"]
            try:
                tags = iam.list_role_tags(RoleName=name).get("Tags", [])
                if not has_project_tag(tags, project) and not include_untagged:
                    result.skipped.append(f"role {name}: no project tag (use --include-untagged)")
                    continue
                _delete_role(iam, name)
            except ClientError as err:
                if not _gone(err):
                    raise
            result.done.append(f"role {name}")
    return result
