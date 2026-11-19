"""`aif dev network up|down`: a dev-account stand-in for the launch template's network.

At an event the launch template creates the VPC and the IDE, and publishes the SSM
parameters under `/aif/`. In the owner's dev account this module creates the same
VPC and publishes the same parameters, so `aif hub up` works the same in both places.

It refuses to run without `dev_standin=True` (the `--dev-standin` flag). It only finds
and deletes resources tagged `aif:dev-standin=true` and `project=<project>`.

Network: 3 AZs, public /24 and private /19 subnets, one NAT gateway, VPC flow logs to
a CloudWatch log group with 7-day retention (CloudWatch Logs encrypts at rest).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import boto3
from botocore.exceptions import ClientError

from aif.hub import iam as hub_iam
from aif.hub.common import HubError, Report, Tags, code, tag_list
from aif.hub.names import HUB_CLUSTER, IAM_PATH, Param
from aif.hub.policies import Scope, flow_logs, trust
from aif.waiting import Clock, until

if TYPE_CHECKING:
    from mypy_boto3_ec2 import EC2Client
    from mypy_boto3_eks import EKSClient
    from mypy_boto3_iam import IAMClient
    from mypy_boto3_logs import CloudWatchLogsClient
    from mypy_boto3_ssm import SSMClient
    from mypy_boto3_sso_admin import SSOAdminClient
    from mypy_boto3_sts import STSClient

STANDIN_TAG = "aif:dev-standin"
VPC_CIDR = "10.64.0.0/16"
PUBLIC_CIDRS = ("10.64.0.0/24", "10.64.1.0/24", "10.64.2.0/24")
PRIVATE_CIDRS = ("10.64.32.0/19", "10.64.64.0/19", "10.64.96.0/19")
FLOW_LOG_GROUP = "/aif/vpc-flow-logs"
FLOW_LOG_ROLE = "aif-vpc-flow-logs"
IDE_ROLE = "aif-ide-stand-in"
IDE_SG = "aif-dev-ide"
LAUNCH_STACK_VALUE = "aif-dev-standin"
"""No launch stack exists in the dev account. aif down keeps a stack by this name; none exists."""
NAT_TIMEOUT = 10 * 60.0
PUBLISHED = (
    Param.VPC_ID,
    Param.PUBLIC_SUBNETS,
    Param.PRIVATE_SUBNETS,
    Param.REGION,
    Param.ADMIN_ROLE,
    Param.IDE_ROLE,
    Param.IDE_SECURITY_GROUP,
    Param.LAUNCH_STACK,
    Param.IDC_INSTANCE,
    Param.IDENTITY_STORE,
)


class StandInRefusedError(RuntimeError):
    """The stand-in runs only with --dev-standin, in the owner's dev account."""


@dataclass
class DevNetClients:
    sts: "STSClient"
    ec2: "EC2Client"
    iam: "IAMClient"
    logs: "CloudWatchLogsClient"
    ssm: "SSMClient"
    sso: "SSOAdminClient"
    eks: "EKSClient"

    @staticmethod
    def create(region: str) -> "DevNetClients":
        s = boto3.Session(region_name=region)
        return DevNetClients(
            sts=s.client("sts"),
            ec2=s.client("ec2"),
            iam=s.client("iam"),
            logs=s.client("logs"),
            ssm=s.client("ssm"),
            sso=s.client("sso-admin"),
            eks=s.client("eks"),
        )


@dataclass
class Network:
    vpc_id: str = ""
    public: list[str] = field(default_factory=list)
    private: list[str] = field(default_factory=list)


def _refuse_unless(dev_standin: bool) -> None:
    if not dev_standin:
        raise StandInRefusedError(
            "the network stand-in is for the owner's dev account only; pass --dev-standin. "
            "At an event the launch template provides the network."
        )


def _tags(project_tags: Tags, name: str) -> dict[str, str]:
    return {**project_tags, "stage": "platform", STANDIN_TAG: "true", "Name": name}


def _spec(kind: str, tags: Mapping[str, str]) -> list[Any]:
    return [{"ResourceType": kind, "Tags": tag_list(tags)}]


def _ours(project: str) -> list[Any]:
    return [
        {"Name": f"tag:{STANDIN_TAG}", "Values": ["true"]},
        {"Name": "tag:project", "Values": [project]},
    ]


def _find_vpc(ec2: "EC2Client", project: str) -> str | None:
    vpcs = ec2.describe_vpcs(Filters=_ours(project))["Vpcs"]
    if len(vpcs) > 1:
        raise HubError(f"found {len(vpcs)} stand-in VPCs; expected at most one")
    return vpcs[0]["VpcId"] if vpcs else None


def _azs(ec2: "EC2Client") -> list[str]:
    zones = ec2.describe_availability_zones(
        Filters=[
            {"Name": "state", "Values": ["available"]},
            {"Name": "zone-type", "Values": ["availability-zone"]},
        ]
    )["AvailabilityZones"]
    names = sorted(z["ZoneName"] for z in zones)
    if len(names) < len(PUBLIC_CIDRS):
        raise HubError(f"need {len(PUBLIC_CIDRS)} availability zones, found {len(names)}")
    return names[: len(PUBLIC_CIDRS)]


def _subnet(
    ec2: "EC2Client",
    vpc: str,
    cidr: str,
    az: str,
    name: str,
    extra: Mapping[str, str],
    tags: Tags,
    rep: Report,
) -> str:
    found = ec2.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "cidr-block", "Values": [cidr]}]
    )["Subnets"]
    if found:
        rep.kept(f"subnet {name}")
        return found[0]["SubnetId"]
    subnet = ec2.create_subnet(
        VpcId=vpc,
        CidrBlock=cidr,
        AvailabilityZone=az,
        TagSpecifications=_spec("subnet", {**_tags(tags, name), **extra}),
    )["Subnet"]["SubnetId"]
    rep.created(f"subnet {name} {cidr} {az}")
    return subnet


def _route_table(
    ec2: "EC2Client",
    vpc: str,
    name: str,
    subnets: Sequence[str],
    target: Mapping[str, str],
    tags: Tags,
    rep: Report,
) -> None:
    found = ec2.describe_route_tables(
        Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "tag:Name", "Values": [name]}]
    )["RouteTables"]
    if found:
        rep.kept(f"route table {name}")
        return
    table = ec2.create_route_table(
        VpcId=vpc, TagSpecifications=_spec("route-table", _tags(tags, name))
    )
    table_id = table["RouteTable"]["RouteTableId"]
    ec2.create_route(RouteTableId=table_id, DestinationCidrBlock="0.0.0.0/0", **target)  # type: ignore[arg-type]
    for subnet in subnets:
        ec2.associate_route_table(RouteTableId=table_id, SubnetId=subnet)
    rep.created(f"route table {name}")


def _gateway(ec2: "EC2Client", vpc: str, tags: Tags, rep: Report) -> str:
    found = ec2.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc]}]
    )["InternetGateways"]
    if found:
        rep.kept("internet gateway")
        return found[0]["InternetGatewayId"]
    igw = ec2.create_internet_gateway(
        TagSpecifications=_spec("internet-gateway", _tags(tags, "aif-dev"))
    )["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    rep.created("internet gateway")
    return igw


def _nat(
    ec2: "EC2Client", vpc: str, subnet: str, tags: Tags, rep: Report, clock: Clock | None
) -> str:
    live = ["pending", "available"]
    found = ec2.describe_nat_gateways(
        Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "state", "Values": live}]
    )["NatGateways"]
    if found:
        nat = found[0]["NatGatewayId"]
        rep.kept("nat gateway")
    else:
        alloc = ec2.allocate_address(
            Domain="vpc", TagSpecifications=_spec("elastic-ip", _tags(tags, "aif-dev-nat"))
        )["AllocationId"]
        nat = ec2.create_nat_gateway(
            SubnetId=subnet,
            AllocationId=alloc,
            TagSpecifications=_spec("natgateway", _tags(tags, "aif-dev-nat")),
        )["NatGateway"]["NatGatewayId"]
        rep.created("nat gateway and its elastic IP")

    def check() -> tuple[bool, str]:
        state = ec2.describe_nat_gateways(NatGatewayIds=[nat])["NatGateways"][0]["State"]
        if state == "failed":
            raise HubError(f"nat gateway {nat} failed")
        return state == "available", state

    until(
        check,
        timeout=NAT_TIMEOUT,
        what=f"nat gateway {nat}",
        describe=lambda s: f"state {s}",
        clock=clock,
    )
    return nat


def _security_group(ec2: "EC2Client", vpc: str, tags: Tags, rep: Report) -> str:
    found = ec2.describe_security_groups(
        Filters=[{"Name": "vpc-id", "Values": [vpc]}, {"Name": "group-name", "Values": [IDE_SG]}]
    )["SecurityGroups"]
    if found:
        rep.kept(f"security group {IDE_SG}")
        return found[0]["GroupId"]
    sg = ec2.create_security_group(
        GroupName=IDE_SG,
        Description="Stand-in for the IDE security group. No ingress.",
        VpcId=vpc,
        TagSpecifications=_spec("security-group", _tags(tags, IDE_SG)),
    )["GroupId"]
    rep.created(f"security group {IDE_SG}")
    return sg


def _flow_logs(c: DevNetClients, vpc: str, scope: Scope, tags: Tags, rep: Report) -> None:
    try:
        c.logs.create_log_group(logGroupName=FLOW_LOG_GROUP, tags=dict(tags))
        rep.created(f"log group {FLOW_LOG_GROUP}")
    except ClientError as err:
        if code(err) != "ResourceAlreadyExistsException":
            raise
    c.logs.put_retention_policy(logGroupName=FLOW_LOG_GROUP, retentionInDays=7)
    group_arn = scope.arn("logs", f"log-group:{FLOW_LOG_GROUP}")
    hub_iam.ensure_boundary(c.iam, scope, tags, rep)
    role_arn = hub_iam.ensure_role(
        c.iam,
        hub_iam.RoleSpec(
            FLOW_LOG_ROLE,
            trust("vpc-flow-logs.amazonaws.com", tag_session=False),
            "VPC Flow Logs writes the aif dev stand-in log group",
            inline={"write-flow-log-group": flow_logs(group_arn)},
        ),
        scope,
        tags,
        rep,
    )
    found = c.ec2.describe_flow_logs(Filters=[{"Name": "resource-id", "Values": [vpc]}])["FlowLogs"]
    if found:
        rep.kept("vpc flow log")
        return
    c.ec2.create_flow_logs(
        ResourceIds=[vpc],
        ResourceType="VPC",
        TrafficType="ALL",
        LogDestinationType="cloud-watch-logs",
        LogGroupName=FLOW_LOG_GROUP,
        DeliverLogsPermissionArn=role_arn,
        TagSpecifications=_spec("vpc-flow-log", _tags(tags, "aif-dev")),
    )
    rep.created("vpc flow log")


def _idc(sso: "SSOAdminClient") -> tuple[str, str] | None:
    """The account's existing IdC instance, read only. None if there is none."""
    instances = sso.list_instances()["Instances"]
    if len(instances) != 1:
        return None
    return instances[0]["InstanceArn"], instances[0]["IdentityStoreId"]


def _scope(sts: "STSClient", region: str) -> Scope:
    arn = sts.get_caller_identity()["Arn"]
    return Scope(partition=arn.split(":", 2)[1], account=arn.split(":")[4], region=region)


def up(
    c: DevNetClients,
    region: str,
    project_tags: Tags,
    admin_role_arn: str,
    *,
    dev_standin: bool,
    clock: Clock | None = None,
) -> Report:
    """Create the stand-in network and publish the launch template's SSM parameters."""
    _refuse_unless(dev_standin)
    rep = Report()
    scope = _scope(c.sts, region)
    project = project_tags["project"]
    tags = {**project_tags, "stage": "platform"}
    vpc = _find_vpc(c.ec2, project)
    if vpc is None:
        vpc = c.ec2.create_vpc(
            CidrBlock=VPC_CIDR, TagSpecifications=_spec("vpc", _tags(tags, "aif-dev"))
        )["Vpc"]["VpcId"]
        rep.created(f"vpc {VPC_CIDR}")
    else:
        rep.kept(f"vpc {vpc}")
    c.ec2.modify_vpc_attribute(VpcId=vpc, EnableDnsHostnames={"Value": True})
    net = Network(vpc_id=vpc)
    zones = _azs(c.ec2)
    for i, (az, cidr) in enumerate(zip(zones, PUBLIC_CIDRS, strict=True)):
        net.public.append(
            _subnet(
                c.ec2,
                vpc,
                cidr,
                az,
                f"aif-dev-public-{i + 1}",
                {"kubernetes.io/role/elb": "1"},
                tags,
                rep,
            )
        )
    for i, (az, cidr) in enumerate(zip(zones, PRIVATE_CIDRS, strict=True)):
        net.private.append(
            _subnet(
                c.ec2,
                vpc,
                cidr,
                az,
                f"aif-dev-private-{i + 1}",
                {"kubernetes.io/role/internal-elb": "1"},
                tags,
                rep,
            )
        )
    igw = _gateway(c.ec2, vpc, tags, rep)
    _route_table(c.ec2, vpc, "aif-dev-public", net.public, {"GatewayId": igw}, tags, rep)
    nat = _nat(c.ec2, vpc, net.public[0], tags, rep, clock)
    _route_table(c.ec2, vpc, "aif-dev-private", net.private, {"NatGatewayId": nat}, tags, rep)
    _flow_logs(c, vpc, scope, tags, rep)
    ide_role = hub_iam.ensure_role(
        c.iam,
        hub_iam.RoleSpec(
            IDE_ROLE,
            trust("ec2.amazonaws.com", tag_session=False),
            "Stand-in for the IDE role. No permissions.",
        ),
        scope,
        tags,
        rep,
    )
    sg = _security_group(c.ec2, vpc, tags, rep)
    values = {
        Param.VPC_ID: vpc,
        Param.REGION: region,
        Param.ADMIN_ROLE: admin_role_arn,
        Param.IDE_ROLE: ide_role,
        Param.IDE_SECURITY_GROUP: sg,
        Param.LAUNCH_STACK: LAUNCH_STACK_VALUE,
    }
    idc = _idc(c.sso)
    if idc is not None:
        values[Param.IDC_INSTANCE], values[Param.IDENTITY_STORE] = idc
    for name, value in sorted(values.items()):
        c.ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
    for name, ids in ((Param.PUBLIC_SUBNETS, net.public), (Param.PRIVATE_SUBNETS, net.private)):
        c.ssm.put_parameter(Name=name, Value=",".join(ids), Type="StringList", Overwrite=True)
    for name in [*values, Param.PUBLIC_SUBNETS, Param.PRIVATE_SUBNETS]:
        c.ssm.add_tags_to_resource(ResourceType="Parameter", ResourceId=name, Tags=tag_list(tags))
    rep.created(f"{len(values) + 2} SSM parameters under /aif/")
    return rep


def _hub_exists(eks: "EKSClient") -> bool:
    try:
        eks.describe_cluster(name=HUB_CLUSTER)
        return True
    except ClientError as err:
        if code(err) == "ResourceNotFoundException":
            return False
        raise


def _delete_role(iam: "IAMClient", name: str, rep: Report) -> None:
    try:
        role = iam.get_role(RoleName=name)["Role"]
    except ClientError as err:
        if code(err) == "NoSuchEntity":
            return
        raise
    if role["Path"] != IAM_PATH:
        raise HubError(f"role {name} is not under {IAM_PATH}; not deleting it")
    for policy in iam.list_role_policies(RoleName=name)["PolicyNames"]:
        iam.delete_role_policy(RoleName=name, PolicyName=policy)
    iam.delete_role(RoleName=name)
    rep.deleted(f"iam role {name}")


def down(
    c: DevNetClients, project_tags: Tags, *, dev_standin: bool, clock: Clock | None = None
) -> Report:
    """Delete the stand-in network, roles, log group and SSM inputs. The Hub must be gone."""
    _refuse_unless(dev_standin)
    rep = Report()
    if _hub_exists(c.eks):
        raise HubError(f"cluster {HUB_CLUSTER} still uses this network; run `aif down` first")
    project = project_tags["project"]
    vpc = _find_vpc(c.ec2, project)
    if vpc is not None:
        _delete_network(c.ec2, vpc, project, rep, clock)
    for role in (FLOW_LOG_ROLE, IDE_ROLE):
        _delete_role(c.iam, role, rep)
    try:
        c.logs.delete_log_group(logGroupName=FLOW_LOG_GROUP)
        rep.deleted(f"log group {FLOW_LOG_GROUP}")
    except ClientError as err:
        if code(err) != "ResourceNotFoundException":
            raise
    c.ssm.delete_parameters(Names=list(PUBLISHED))
    rep.deleted("SSM parameters published by the stand-in")
    return rep


def _delete_network(
    ec2: "EC2Client", vpc: str, project: str, rep: Report, clock: Clock | None
) -> None:
    by_vpc: list[Any] = [{"Name": "vpc-id", "Values": [vpc]}]
    for flow in ec2.describe_flow_logs(Filters=[{"Name": "resource-id", "Values": [vpc]}])[
        "FlowLogs"
    ]:
        ec2.delete_flow_logs(FlowLogIds=[flow["FlowLogId"]])
        rep.deleted("vpc flow log")
    nats = ec2.describe_nat_gateways(
        Filters=[*by_vpc, {"Name": "state", "Values": ["pending", "available", "deleting"]}]
    )["NatGateways"]
    for nat in nats:
        ec2.delete_nat_gateway(NatGatewayId=nat["NatGatewayId"])
    if nats:
        ids = [n["NatGatewayId"] for n in nats]

        def gone() -> tuple[bool, list[str]]:
            left = [
                n["NatGatewayId"]
                for n in ec2.describe_nat_gateways(NatGatewayIds=ids)["NatGateways"]
                if n["State"] != "deleted"
            ]
            return not left, left

        until(gone, timeout=NAT_TIMEOUT, what="nat gateways to delete", describe=str, clock=clock)
        rep.deleted("nat gateway")
    for address in ec2.describe_addresses(Filters=_ours(project))["Addresses"]:
        ec2.release_address(AllocationId=address["AllocationId"])
        rep.deleted("elastic IP")
    for igw in ec2.describe_internet_gateways(
        Filters=[{"Name": "attachment.vpc-id", "Values": [vpc]}]
    )["InternetGateways"]:
        ec2.detach_internet_gateway(InternetGatewayId=igw["InternetGatewayId"], VpcId=vpc)
        ec2.delete_internet_gateway(InternetGatewayId=igw["InternetGatewayId"])
        rep.deleted("internet gateway")
    for subnet in ec2.describe_subnets(Filters=by_vpc)["Subnets"]:
        ec2.delete_subnet(SubnetId=subnet["SubnetId"])
    rep.deleted("subnets")
    for table in ec2.describe_route_tables(Filters=by_vpc)["RouteTables"]:
        if any(a.get("Main") for a in table.get("Associations", [])):
            continue
        ec2.delete_route_table(RouteTableId=table["RouteTableId"])
    rep.deleted("route tables")
    for group in ec2.describe_security_groups(Filters=by_vpc)["SecurityGroups"]:
        if group["GroupName"] != "default":
            ec2.delete_security_group(GroupId=group["GroupId"])
    ec2.delete_vpc(VpcId=vpc)
    rep.deleted(f"vpc {vpc}")
