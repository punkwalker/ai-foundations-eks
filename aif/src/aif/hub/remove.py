"""Remove what the Hub steps created. Each function touches only resources that carry our
`project` tag, and treats "already gone" as done. Nothing here waits."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, cast

from botocore.exceptions import ClientError

from aif.hub.common import Report, Tags, code
from aif.hub.names import (
    ARGOCD_GROUPS,
    BOUNDARY_NAME,
    ECR_REPOSITORIES,
    HUB_CLUSTER,
    KEY_ALIAS,
    SECRETS,
    control_plane_log_group,
    model_bucket,
)
from aif.hub.policies import Scope

if TYPE_CHECKING:
    from mypy_boto3_ecr import ECRClient
    from mypy_boto3_iam import IAMClient
    from mypy_boto3_identitystore import IdentityStoreClient
    from mypy_boto3_kms import KMSClient
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_secretsmanager import SecretsManagerClient
    from mypy_boto3_ssm import SSMClient

KEY_DELETE_DAYS = 7
"""The shortest waiting period KMS allows."""


def _ours(found: Mapping[str, str], tags: Tags) -> bool:
    return found.get("project") == tags["project"]


def _missing(err: ClientError, *codes: str) -> None:
    """Return when the error says the resource is already gone; raise otherwise."""
    if code(err) not in codes:
        raise err


def delete_role(iam: IAMClient, name: str, tags: Tags, report: Report) -> None:
    try:
        found = {t["Key"]: t["Value"] for t in iam.list_role_tags(RoleName=name)["Tags"]}
    except ClientError as err:
        _missing(err, "NoSuchEntity")
        return
    if not _ours(found, tags):
        report.kept(f"iam role {name} (not tagged project={tags['project']}; left alone)")
        return
    for policy in iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]:
        iam.detach_role_policy(RoleName=name, PolicyArn=policy["PolicyArn"])
    for inline in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=name, PolicyName=inline)
    iam.delete_role(RoleName=name)
    report.deleted(f"iam role {name}")


def delete_boundary(iam: IAMClient, scope: Scope, tags: Tags, report: Report) -> None:
    arn = scope.boundary_arn
    try:
        found = {t["Key"]: t["Value"] for t in iam.list_policy_tags(PolicyArn=arn)["Tags"]}
    except ClientError as err:
        _missing(err, "NoSuchEntity")
        return
    if not _ours(found, tags):
        report.kept(f"iam policy {BOUNDARY_NAME} (not ours; left alone)")
        return
    for version in iam.list_policy_versions(PolicyArn=arn)["Versions"]:
        if not version["IsDefaultVersion"]:
            iam.delete_policy_version(PolicyArn=arn, VersionId=version["VersionId"])
    iam.delete_policy(PolicyArn=arn)
    report.deleted(f"iam policy {BOUNDARY_NAME}")


def delete_key(kms: KMSClient, tags: Tags, report: Report) -> None:
    try:
        meta = kms.describe_key(KeyId=KEY_ALIAS)["KeyMetadata"]
    except ClientError as err:
        _missing(err, "NotFoundException")
        return
    listed = kms.list_resource_tags(KeyId=meta["KeyId"])["Tags"]
    if not _ours({t["TagKey"]: t["TagValue"] for t in listed}, tags):
        report.kept(f"kms key {KEY_ALIAS} (not ours; left alone)")
        return
    kms.delete_alias(AliasName=KEY_ALIAS)
    if meta["KeyState"] != "PendingDeletion":
        kms.schedule_key_deletion(KeyId=meta["KeyId"], PendingWindowInDays=KEY_DELETE_DAYS)
    report.deleted(f"kms key {KEY_ALIAS} (deleted after {KEY_DELETE_DAYS} days)")


def delete_bucket(s3: S3Client, account: str, region: str, tags: Tags, report: Report) -> None:
    name = model_bucket(account, region)
    try:
        found = {t["Key"]: t["Value"] for t in s3.get_bucket_tagging(Bucket=name)["TagSet"]}
    except ClientError as err:
        _missing(err, "NoSuchBucket", "NoSuchTagSet")
        if code(err) == "NoSuchTagSet":
            report.kept(f"s3 bucket {name} (no project tag; left alone)")
        return
    if not _ours(found, tags):
        report.kept(f"s3 bucket {name} (not ours; left alone)")
        return
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=name):
        objects: list[Any] = [
            {"Key": v["Key"], "VersionId": v["VersionId"]}
            for v in (*page.get("Versions", []), *page.get("DeleteMarkers", []))
        ]
        if objects:
            s3.delete_objects(Bucket=name, Delete={"Objects": objects, "Quiet": True})
    s3.delete_bucket(Bucket=name)
    report.deleted(f"s3 bucket {name}")


def delete_repositories(ecr: ECRClient, tags: Tags, report: Report) -> None:
    for name in ECR_REPOSITORIES:
        try:
            repo = ecr.describe_repositories(repositoryNames=[name])["repositories"][0]
            listed = ecr.list_tags_for_resource(resourceArn=repo["repositoryArn"])["tags"]
        except ClientError as err:
            _missing(err, "RepositoryNotFoundException")
            continue
        if not _ours({t["Key"]: t["Value"] for t in listed}, tags):
            report.kept(f"ecr repository {name} (not ours; left alone)")
            continue
        ecr.delete_repository(repositoryName=name, force=True)
        report.deleted(f"ecr repository {name}")


def delete_secrets(sm: SecretsManagerClient, tags: Tags, report: Report) -> None:
    for name in SECRETS:
        try:
            listed = sm.describe_secret(SecretId=name).get("Tags", [])
        except ClientError as err:
            _missing(err, "ResourceNotFoundException")
            continue
        if not _ours({t["Key"]: t["Value"] for t in listed}, tags):
            report.kept(f"secret {name} (not ours; left alone)")
            continue
        sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)
        report.deleted(f"secret {name}")


def delete_log_group(logs: CloudWatchLogsClient, report: Report) -> None:
    name = control_plane_log_group(HUB_CLUSTER)
    try:
        logs.delete_log_group(logGroupName=name)
    except ClientError as err:
        _missing(err, "ResourceNotFoundException")
        return
    report.deleted(f"log group {name}")


def delete_groups(ids: IdentityStoreClient, store_id: str, report: Report) -> None:
    """Only the three aif groups, found by exact name. Other groups and users stay."""
    for name in ARGOCD_GROUPS.values():
        try:
            gid = ids.get_group_id(
                IdentityStoreId=store_id,
                AlternateIdentifier={
                    "UniqueAttribute": {
                        "AttributePath": "displayName",
                        "AttributeValue": cast(Any, name),
                    }
                },
            )["GroupId"]
        except ClientError as err:
            _missing(err, "ResourceNotFoundException")
            continue
        ids.delete_group(IdentityStoreId=store_id, GroupId=gid)
        report.deleted(f"identity center group {name}")


def delete_outputs(ssm: SSMClient, names: list[str], report: Report) -> None:
    for i in range(0, len(names), 10):
        ssm.delete_parameters(Names=names[i : i + 10])
    report.deleted("SSM parameters aif published")
