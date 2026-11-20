"""AWS identity checks and boto3 clients."""

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import boto3

from aif.teardown import AwsClients

if TYPE_CHECKING:
    from mypy_boto3_eks import EKSClient
    from mypy_boto3_ssm import SSMClient
    from mypy_boto3_sts import STSClient

ACCOUNT_ID = re.compile(r"^\d{12}$")


class AccountMismatchError(RuntimeError):
    """The credentials point to a different account than the one confirmed."""


@dataclass(frozen=True)
class Identity:
    account: str
    role: str
    """Role name only. The session name and full ARN are not kept, so they are never printed."""


def role_name(arn: str) -> str:
    """`arn:aws:sts::1:assumed-role/Admin/me` gives `Admin`; an IAM user gives `user/<name>`."""
    resource = arn.split(":", 5)[-1]
    kind, _, rest = resource.partition("/")
    if kind == "assumed-role":
        return rest.split("/", 1)[0]
    if kind == "role":
        return rest.rsplit("/", 1)[-1]
    return f"{kind}/{rest.rsplit('/', 1)[-1]}" if rest else kind


def identity(sts: "STSClient") -> Identity:
    out = sts.get_caller_identity()
    return Identity(account=out["Account"], role=role_name(out["Arn"]))


def confirm_account(sts: "STSClient", expected: str) -> Identity:
    """Raise unless `expected` is a 12-digit id that equals the caller's account."""
    if not ACCOUNT_ID.match(expected):
        raise AccountMismatchError(
            f"--confirm-account must be a 12-digit account id, got {expected!r}"
        )
    who = identity(sts)
    if who.account != expected:
        raise AccountMismatchError(
            f"credentials are for account {who.account}, not {expected}. Nothing was changed."
        )
    return who


def sts_client(region: str) -> "STSClient":
    return boto3.client("sts", region_name=region)


def ssm_client(region: str) -> "SSMClient":
    return boto3.client("ssm", region_name=region)


def eks_client(region: str) -> "EKSClient":
    return boto3.client("eks", region_name=region)


def clients(region: str) -> AwsClients:
    session = boto3.Session(region_name=region)
    return AwsClients(
        s3=session.client("s3"),
        ecr=session.client("ecr"),
        secrets=session.client("secretsmanager"),
        ec2=session.client("ec2"),
        elbv2=session.client("elbv2"),
        iam=session.client("iam"),
        tagging=session.client("resourcegroupstaggingapi"),
        cloudformation=session.client("cloudformation"),
    )
