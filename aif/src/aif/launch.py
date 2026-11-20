"""Read the launch inputs that the launch stack publishes in SSM under /aif/."""

from typing import TYPE_CHECKING

from aif.hub_api import LaunchInputs

if TYPE_CHECKING:
    from mypy_boto3_ssm import SSMClient

PARAMS = {
    "region": "/aif/region",
    "vpc_id": "/aif/vpc-id",
    "public_subnet_ids": "/aif/public-subnet-ids",
    "private_subnet_ids": "/aif/private-subnet-ids",
    "admin_role_arn": "/aif/admin-role-arn",
    "ide_role_arn": "/aif/ide-role-arn",
    "ide_security_group_id": "/aif/ide-security-group-id",
    "launch_stack_name": "/aif/launch-stack-name",
    "idc_instance_arn": "/aif/idc-instance-arn",
}
OPTIONAL = frozenset({"idc_instance_arn"})
"""Needed only in capability mode. `read_inputs` checks that case."""


class LaunchError(RuntimeError):
    """An input is missing or wrong, so aif must not build anything."""


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def read_inputs(ssm: "SSMClient", region: str, argocd_mode: str) -> LaunchInputs:
    """Read every input. Raises LaunchError that names each missing parameter."""
    out = ssm.get_parameters(Names=list(PARAMS.values()))
    values = {p["Name"]: p.get("Value", "").strip() for p in out.get("Parameters", [])}
    missing = [
        name
        for key, name in PARAMS.items()
        if not values.get(name) and (key not in OPTIONAL or argocd_mode == "capability")
    ]
    if missing:
        raise LaunchError(
            f"missing SSM parameters {missing}. Deploy the launch stack, "
            "or run `aif dev network up` in a development account"
        )
    got = {key: values.get(name, "") for key, name in PARAMS.items()}
    if got["region"] != region:
        raise LaunchError(f"SSM /aif/region is {got['region']}, but aif runs in {region}")
    subnets = {k: _split(got[k]) for k in ("public_subnet_ids", "private_subnet_ids")}
    return LaunchInputs(
        region=got["region"],
        vpc_id=got["vpc_id"],
        public_subnet_ids=subnets["public_subnet_ids"],
        private_subnet_ids=subnets["private_subnet_ids"],
        admin_role_arn=got["admin_role_arn"],
        ide_role_arn=got["ide_role_arn"],
        ide_security_group_id=got["ide_security_group_id"],
        launch_stack_name=got["launch_stack_name"],
        idc_instance_arn=got["idc_instance_arn"] or None,
    )
