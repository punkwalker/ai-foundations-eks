"""IAM roles and the permission boundary, created or brought up to date.

aif only touches roles under `/aif/`. An existing role with the same name on another
path is refused, so aif never takes over a role it did not create.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from botocore.exceptions import ClientError

from aif.hub.common import HubError, Report, Tags, code, tag_list
from aif.hub.names import BOUNDARY_NAME, IAM_PATH
from aif.hub.policies import Doc, Scope, boundary, dumps

if TYPE_CHECKING:
    from mypy_boto3_iam import IAMClient

MAX_POLICY_VERSIONS = 5


@dataclass(frozen=True)
class RoleSpec:
    name: str
    trust: Doc
    description: str
    path: str = IAM_PATH
    managed: Sequence[str] = ()
    """AWS managed policy names, for example AmazonEKSClusterPolicy."""
    inline: dict[str, Doc] = field(default_factory=dict)


def _same(a: Any, b: Doc) -> bool:
    parsed = json.loads(unquote(a)) if isinstance(a, str) else a
    return dumps(parsed) == dumps(b)


def ensure_boundary(iam: "IAMClient", scope: Scope, tags: Tags, report: Report) -> str:
    """Create aif-infra-boundary, or add a new default version if its document changed."""
    arn = scope.boundary_arn
    wanted = boundary(scope)
    try:
        policy = iam.get_policy(PolicyArn=arn)["Policy"]
    except ClientError as err:
        if code(err) != "NoSuchEntity":
            raise
        iam.create_policy(
            PolicyName=BOUNDARY_NAME,
            Path=IAM_PATH,
            PolicyDocument=dumps(wanted),
            Description="Permission boundary for roles created by aif",
            Tags=tag_list(tags),
        )
        report.created(f"iam policy {BOUNDARY_NAME}")
        return arn
    current = iam.get_policy_version(PolicyArn=arn, VersionId=policy["DefaultVersionId"])
    if _same(current["PolicyVersion"]["Document"], wanted):
        report.kept(f"iam policy {BOUNDARY_NAME}")
        return arn
    versions = iam.list_policy_versions(PolicyArn=arn)["Versions"]
    old = sorted((v for v in versions if not v["IsDefaultVersion"]), key=lambda v: v["CreateDate"])
    for version in old[: max(0, len(versions) - (MAX_POLICY_VERSIONS - 1))]:
        iam.delete_policy_version(PolicyArn=arn, VersionId=version["VersionId"])
    iam.create_policy_version(PolicyArn=arn, PolicyDocument=dumps(wanted), SetAsDefault=True)
    report.updated(f"iam policy {BOUNDARY_NAME}")
    return arn


def ensure_role(iam: "IAMClient", spec: RoleSpec, scope: Scope, tags: Tags, report: Report) -> str:
    """Create the role, or bring its trust, boundary, policies and tags up to date."""
    boundary_arn = scope.boundary_arn
    try:
        role = iam.get_role(RoleName=spec.name)["Role"]
    except ClientError as err:
        if code(err) != "NoSuchEntity":
            raise
        role = iam.create_role(
            RoleName=spec.name,
            Path=spec.path,
            AssumeRolePolicyDocument=dumps(spec.trust),
            Description=spec.description,
            PermissionsBoundary=boundary_arn,
            Tags=tag_list(tags),
        )["Role"]
        report.created(f"iam role {spec.path}{spec.name}")
    else:
        if role["Path"] != spec.path:
            raise HubError(
                f"role {spec.name} exists on path {role['Path']}, not {spec.path}; "
                "aif does not take over roles it did not create. Rename or remove it."
            )
        changed = False
        if not _same(role.get("AssumeRolePolicyDocument", "{}"), spec.trust):
            iam.update_assume_role_policy(RoleName=spec.name, PolicyDocument=dumps(spec.trust))
            changed = True
        if role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn") != boundary_arn:
            iam.put_role_permissions_boundary(RoleName=spec.name, PermissionsBoundary=boundary_arn)
            changed = True
        iam.tag_role(RoleName=spec.name, Tags=tag_list(tags))
        if changed:
            report.updated(f"iam role {spec.path}{spec.name}")
        else:
            report.kept(f"iam role {spec.path}{spec.name}")
    _sync_managed(iam, spec, scope)
    for name, doc in spec.inline.items():
        iam.put_role_policy(RoleName=spec.name, PolicyName=name, PolicyDocument=dumps(doc))
    return str(role["Arn"])


def _sync_managed(iam: "IAMClient", spec: RoleSpec, scope: Scope) -> None:
    wanted = {f"arn:{scope.partition}:iam::aws:policy/{name}" for name in spec.managed}
    attached = {
        p["PolicyArn"]
        for p in iam.list_attached_role_policies(RoleName=spec.name)["AttachedPolicies"]
    }
    for arn in sorted(wanted - attached):
        iam.attach_role_policy(RoleName=spec.name, PolicyArn=arn)
    for arn in sorted(attached - wanted):
        iam.detach_role_policy(RoleName=spec.name, PolicyArn=arn)
