"""Model bucket, ECR repositories, empty Secrets Manager secrets, and SSM outputs."""

import json
from collections.abc import Mapping
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from aif.hub.common import HubError, Report, Tags, code, tag_list
from aif.hub.policies import Scope, bucket_tls_only, dumps

if TYPE_CHECKING:
    from mypy_boto3_ecr import ECRClient
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_secretsmanager import SecretsManagerClient
    from mypy_boto3_ssm import SSMClient

COMPILE_CACHE_PREFIX = "compile-cache/"
COMPILE_CACHE_DAYS = 14
UNTAGGED_IMAGE_DAYS = 1


def ensure_bucket(s3: "S3Client", name: str, scope: Scope, tags: Tags, report: Report) -> None:
    """Private, TLS-only, SSE-S3, unversioned bucket with lifecycle rules."""
    try:
        s3.head_bucket(Bucket=name, ExpectedBucketOwner=scope.account)
        report.kept(f"s3 bucket {name}")
    except ClientError as err:
        if code(err) in ("403", "AccessDenied"):
            raise HubError(f"bucket {name} exists but is not in this account") from err
        if code(err) not in ("404", "NoSuchBucket", "NotFound"):
            raise
        s3.create_bucket(
            Bucket=name,
            CreateBucketConfiguration={"LocationConstraint": scope.region},  # type: ignore[typeddict-item]
            ObjectOwnership="BucketOwnerEnforced",
        )
        report.created(f"s3 bucket {name}")
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    s3.put_bucket_encryption(
        Bucket=name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        },
    )
    s3.put_bucket_policy(
        Bucket=name, Policy=dumps(bucket_tls_only(f"arn:{scope.partition}:s3:::{name}"))
    )
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "abort-incomplete-uploads",
                    "Status": "Enabled",
                    "Filter": {"Prefix": ""},
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 1},
                },
                {
                    "ID": "expire-compile-cache",
                    "Status": "Enabled",
                    "Filter": {"Prefix": COMPILE_CACHE_PREFIX},
                    "Expiration": {"Days": COMPILE_CACHE_DAYS},
                },
            ]
        },
    )
    s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": tag_list(tags)})


def ensure_repository(ecr: "ECRClient", name: str, tags: Tags, report: Report) -> None:
    """Scan on push, immutable tags, untagged images expire after a day."""
    try:
        ecr.describe_repositories(repositoryNames=[name])
        report.kept(f"ecr repository {name}")
    except ClientError as err:
        if code(err) != "RepositoryNotFoundException":
            raise
        ecr.create_repository(
            repositoryName=name,
            imageTagMutability="IMMUTABLE",
            imageScanningConfiguration={"scanOnPush": True},
            encryptionConfiguration={"encryptionType": "AES256"},
            tags=tag_list(tags),
        )
        report.created(f"ecr repository {name}")
    ecr.put_lifecycle_policy(
        repositoryName=name,
        lifecyclePolicyText=json.dumps(
            {
                "rules": [
                    {
                        "rulePriority": 1,
                        "description": "Drop untagged images",
                        "selection": {
                            "tagStatus": "untagged",
                            "countType": "sinceImagePushed",
                            "countUnit": "days",
                            "countNumber": UNTAGGED_IMAGE_DAYS,
                        },
                        "action": {"type": "expire"},
                    }
                ]
            }
        ),
    )


def ensure_secret(sm: "SecretsManagerClient", name: str, tags: Tags, report: Report) -> None:
    """Create the secret with no value. The stage that owns it writes the value."""
    try:
        sm.describe_secret(SecretId=name)
        report.kept(f"secret {name}")
    except ClientError as err:
        if code(err) != "ResourceNotFoundException":
            raise
        sm.create_secret(
            Name=name,
            Description="aif lab secret; the value is written by the stage that uses it",
            Tags=tag_list(tags),
        )
        report.created(f"secret {name}")


def publish(ssm: "SSMClient", values: Mapping[str, str], tags: Tags) -> None:
    """Write output parameters, then tag them (PutParameter cannot tag on overwrite)."""
    for name, value in sorted(values.items()):
        ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
        ssm.add_tags_to_resource(ResourceType="Parameter", ResourceId=name, Tags=tag_list(tags))
