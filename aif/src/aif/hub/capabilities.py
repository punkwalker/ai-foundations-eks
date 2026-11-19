"""EKS managed capabilities on the Hub, and the aif IAM Identity Center groups for Argo CD.

ACK and kro always. Argo CD only in `capability` mode, which needs an IdC instance from
the launch template. aif never creates an IdC instance and never changes groups it did
not create: it looks groups up by display name and creates only the missing aif ones.

`deletePropagationPolicy` is RETAIN, the only value EKS takes.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from botocore.exceptions import ClientError

from aif.hub.common import HubError, Report, Tags, code
from aif.hub.names import ARGOCD_GROUPS, ARGOCD_NAMESPACE
from aif.waiting import Clock, until

if TYPE_CHECKING:
    from mypy_boto3_eks import EKSClient
    from mypy_boto3_identitystore import IdentityStoreClient

CAPABILITY_TIMEOUT = 20 * 60.0


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    type: str
    role_arn: str
    configuration: Mapping[str, Any] | None = None


def ensure_groups(ids: "IdentityStoreClient", store_id: str, report: Report) -> dict[str, str]:
    """Return the group ID for each Argo CD role, creating the aif group if it is missing."""
    found: dict[str, str] = {}
    for role, name in ARGOCD_GROUPS.items():
        try:
            found[role] = ids.get_group_id(
                IdentityStoreId=store_id,
                AlternateIdentifier={
                    "UniqueAttribute": {
                        "AttributePath": "displayName",
                        "AttributeValue": cast(Any, name),
                    }
                },
            )["GroupId"]
            report.kept(f"idc group {name}")
        except ClientError as err:
            if code(err) != "ResourceNotFoundException":
                raise
            # Identity store groups take no tags, so the description names the project.
            found[role] = ids.create_group(
                IdentityStoreId=store_id,
                DisplayName=name,
                Description=f"ai-foundations-eks: Argo CD {role} on the Hub",
            )["GroupId"]
            report.created(f"idc group {name}")
    return found


def argocd_configuration(
    instance_arn: str, region: str, groups: Mapping[str, str]
) -> dict[str, Any]:
    return {
        "argoCd": {
            "namespace": ARGOCD_NAMESPACE,
            "awsIdc": {"idcInstanceArn": instance_arn, "idcRegion": region},
            "rbacRoleMappings": [
                {"role": role, "identities": [{"id": gid, "type": "SSO_GROUP"}]}
                for role, gid in sorted(groups.items())
            ],
        }
    }


def _describe(eks: "EKSClient", cluster: str, name: str) -> dict[str, Any] | None:
    try:
        return dict(eks.describe_capability(clusterName=cluster, capabilityName=name)["capability"])
    except ClientError as err:
        if code(err) == "ResourceNotFoundException":
            return None
        raise


def _issues(found: Mapping[str, Any]) -> str:
    issues = found.get("health", {}).get("issues", [])
    return "; ".join(f"{i.get('code')}: {i.get('message')}" for i in issues) or "no issues reported"


def wait_active(
    eks: "EKSClient", cluster: str, name: str, clock: Clock | None, timeout: float
) -> dict[str, Any]:
    def check() -> tuple[bool, dict[str, Any]]:
        found = _describe(eks, cluster, name) or {"status": "MISSING"}
        if found["status"] in ("CREATE_FAILED", "UPDATE_FAILED"):
            raise HubError(f"capability {name} on {cluster} is {found['status']}: {_issues(found)}")
        return found["status"] == "ACTIVE", found

    return until(
        check,
        timeout=timeout,
        what=f"capability {name} on {cluster} to be ACTIVE",
        describe=lambda c: f"status {c.get('status')}, {_issues(c)}",
        interval=10.0,
        max_interval=30.0,
        clock=clock,
    )


def _missing_mappings(found: Mapping[str, Any], wanted: Mapping[str, Any]) -> list[Any]:
    have = found.get("configuration", {}).get("argoCd", {}).get("rbacRoleMappings", [])
    pairs = {(m["role"], i["id"]) for m in have for i in m.get("identities", [])}
    return [
        m
        for m in wanted["argoCd"]["rbacRoleMappings"]
        if any((m["role"], i["id"]) not in pairs for i in m["identities"])
    ]


def ensure_capability(
    eks: "EKSClient",
    cluster: str,
    spec: CapabilitySpec,
    tags: Tags,
    report: Report,
    *,
    clock: Clock | None = None,
    timeout: float = CAPABILITY_TIMEOUT,
) -> str:
    """Create the capability or check it; add missing Argo CD role mappings. Returns its ARN."""
    found = _describe(eks, cluster, spec.name)
    if found is None:
        args: dict[str, Any] = {
            "clusterName": cluster,
            "capabilityName": spec.name,
            "type": spec.type,
            "roleArn": spec.role_arn,
            "deletePropagationPolicy": "RETAIN",
            "tags": dict(tags),
        }
        if spec.configuration is not None:
            args["configuration"] = dict(spec.configuration)
        eks.create_capability(**args)
        report.created(f"capability {spec.name} ({spec.type}) on {cluster}")
        return str(wait_active(eks, cluster, spec.name, clock, timeout)["arn"])
    if found.get("type") != spec.type:
        raise HubError(
            f"capability {spec.name} on {cluster} is {found.get('type')}, not {spec.type}"
        )
    found = wait_active(eks, cluster, spec.name, clock, timeout)
    if spec.type == "ARGOCD" and spec.configuration is not None:
        missing = _missing_mappings(found, spec.configuration)
        if missing:
            eks.update_capability(
                clusterName=cluster,
                capabilityName=spec.name,
                configuration={
                    "argoCd": {"rbacRoleMappings": {"addOrUpdateRoleMappings": missing}}
                },
            )
            report.updated(f"capability {spec.name} role mappings")
            found = wait_active(eks, cluster, spec.name, clock, timeout)
            return str(found["arn"])
    report.kept(f"capability {spec.name} on {cluster}")
    return str(found["arn"])
