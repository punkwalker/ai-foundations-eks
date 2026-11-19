"""Read the Hub inputs from SSM. Nothing else is an input: no stack outputs, no lookups."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from aif.hub.names import Param

if TYPE_CHECKING:
    from mypy_boto3_ssm import SSMClient


class InputError(RuntimeError):
    """A required SSM parameter is missing or empty."""


@dataclass(frozen=True)
class Idc:
    instance_arn: str
    identity_store_id: str


@dataclass(frozen=True)
class HubInputs:
    vpc_id: str
    private_subnets: tuple[str, ...]
    admin_role_arn: str
    ide_role_arn: str
    ide_security_group: str
    idc: Idc | None


REQUIRED = (
    Param.VPC_ID,
    Param.PRIVATE_SUBNETS,
    Param.ADMIN_ROLE,
    Param.IDE_ROLE,
    Param.IDE_SECURITY_GROUP,
)
OPTIONAL = (Param.IDC_INSTANCE, Param.IDENTITY_STORE)


def _fetch(ssm: "SSMClient", names: tuple[str, ...]) -> dict[str, str]:
    try:
        out = ssm.get_parameters(Names=list(names))
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        raise InputError(f"cannot read SSM parameters under /aif/ ({code})") from err
    return {p["Name"]: p.get("Value", "").strip() for p in out.get("Parameters", [])}


def read(ssm: "SSMClient") -> HubInputs:
    """Read every input. Raises InputError naming each missing required parameter."""
    values = _fetch(ssm, REQUIRED + OPTIONAL)
    missing = [name for name in REQUIRED if not values.get(name)]
    if missing:
        raise InputError(
            f"missing SSM parameters {missing}. The launch template publishes them; "
            "in the dev account run `aif dev network up --dev-standin` first."
        )
    instance, store = values.get(Param.IDC_INSTANCE, ""), values.get(Param.IDENTITY_STORE, "")
    if bool(instance) != bool(store):
        raise InputError(f"set both {Param.IDC_INSTANCE} and {Param.IDENTITY_STORE}, or neither")
    return HubInputs(
        vpc_id=values[Param.VPC_ID],
        private_subnets=tuple(s.strip() for s in values[Param.PRIVATE_SUBNETS].split(",") if s),
        admin_role_arn=values[Param.ADMIN_ROLE],
        ide_role_arn=values[Param.IDE_ROLE],
        ide_security_group=values[Param.IDE_SECURITY_GROUP],
        idc=Idc(instance, store) if instance else None,
    )
