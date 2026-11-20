import unittest
from typing import Any

from botocore.stub import ANY, Stubber
from fakes import FakeClock, offline_client

from aif import devnet
from aif.hub.common import HubError, Report

TAGS = {"project": "ai-foundations-eks", "owner": "someone"}
ADMIN = "arn:aws:iam::111122223333:role/admin"
ROLE = {
    "Path": "/aif/",
    "RoleName": "r",
    "RoleId": "AROAEXAMPLE000000001",
    "Arn": "arn:aws:iam::111122223333:role/aif/r",
    "CreateDate": "2026-11-19T00:00:00Z",
}
OURS = [
    {"Name": "tag:aif:dev-standin", "Values": ["true"]},
    {"Name": "tag:project", "Values": ["ai-foundations-eks"]},
]


class Clients:
    def __init__(self) -> None:
        names = {
            "sts": "sts",
            "ec2": "ec2",
            "iam": "iam",
            "logs": "logs",
            "ssm": "ssm",
            "sso": "sso-admin",
            "eks": "eks",
        }
        self.raw = {k: offline_client(v) for k, v in names.items()}
        self.stub = {k: Stubber(c) for k, c in self.raw.items()}
        self.c = devnet.DevNetClients(**self.raw)

    def __enter__(self) -> "Clients":
        for s in self.stub.values():
            s.activate()
        return self

    def __exit__(self, *_: Any) -> None:
        for s in self.stub.values():
            s.deactivate()

    def done(self, case: unittest.TestCase) -> None:
        for name, s in self.stub.items():
            with case.subTest(client=name):
                s.assert_no_pending_responses()


class RefuseTest(unittest.TestCase):
    def test_up_and_down_need_the_flag(self) -> None:
        with Clients() as c:
            with self.assertRaisesRegex(devnet.StandInRefusedError, "--dev-standin"):
                devnet.up(c.c, "us-west-2", TAGS, ADMIN, dev_standin=False)
            with self.assertRaisesRegex(devnet.StandInRefusedError, "--dev-standin"):
                devnet.down(c.c, TAGS, dev_standin=False)
            c.done(self)

    def test_down_refuses_while_the_hub_exists(self) -> None:
        with Clients() as c:
            c.stub["eks"].add_response(
                "describe_cluster", {"cluster": {"name": "aif-hub"}}, {"name": "aif-hub"}
            )
            with self.assertRaisesRegex(HubError, "aif down"):
                devnet.down(c.c, TAGS, dev_standin=True)


def _stub_fresh_up(c: Clients) -> None:
    sts, ec2, iam, logs, sso = (c.stub[k] for k in ("sts", "ec2", "iam", "logs", "sso"))
    sts.add_response(
        "get_caller_identity",
        {"Account": "111122223333", "Arn": "arn:aws:sts::111122223333:assumed-role/admin/s"},
    )
    ec2.add_response("describe_vpcs", {"Vpcs": []}, {"Filters": OURS})
    ec2.add_response(
        "create_vpc",
        {"Vpc": {"VpcId": "vpc-1"}},
        {"CidrBlock": "10.64.0.0/16", "TagSpecifications": ANY},
    )
    ec2.add_response(
        "modify_vpc_attribute", {}, {"VpcId": "vpc-1", "EnableDnsHostnames": {"Value": True}}
    )
    ec2.add_response(
        "describe_availability_zones",
        {
            "AvailabilityZones": [
                {"ZoneName": z} for z in ("us-west-2d", "us-west-2a", "us-west-2c", "us-west-2b")
            ]
        },
    )
    cidrs = list(devnet.PUBLIC_CIDRS) + list(devnet.PRIVATE_CIDRS)
    for i, cidr in enumerate(cidrs):
        ec2.add_response("describe_subnets", {"Subnets": []})
        ec2.add_response(
            "create_subnet",
            {"Subnet": {"SubnetId": f"subnet-{i}"}},
            {
                "VpcId": "vpc-1",
                "CidrBlock": cidr,
                "AvailabilityZone": f"us-west-2{'abc'[i % 3]}",
                "TagSpecifications": ANY,
            },
        )
    ec2.add_response("describe_internet_gateways", {"InternetGateways": []})
    ec2.add_response("create_internet_gateway", {"InternetGateway": {"InternetGatewayId": "igw-1"}})
    ec2.add_response(
        "attach_internet_gateway", {}, {"InternetGatewayId": "igw-1", "VpcId": "vpc-1"}
    )
    ec2.add_response("describe_route_tables", {"RouteTables": []})
    ec2.add_response("create_route_table", {"RouteTable": {"RouteTableId": "rtb-pub"}})
    ec2.add_response(
        "create_route",
        {},
        {"RouteTableId": "rtb-pub", "DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-1"},
    )
    for i in range(3):
        ec2.add_response(
            "associate_route_table", {}, {"RouteTableId": "rtb-pub", "SubnetId": f"subnet-{i}"}
        )
    ec2.add_response("describe_nat_gateways", {"NatGateways": []})
    ec2.add_response("allocate_address", {"AllocationId": "eipalloc-1"})
    ec2.add_response(
        "create_nat_gateway",
        {"NatGateway": {"NatGatewayId": "nat-1"}},
        {
            "SubnetId": "subnet-0",
            "AllocationId": "eipalloc-1",
            "TagSpecifications": ANY,
        },
    )
    ec2.add_response(
        "describe_nat_gateways", {"NatGateways": [{"NatGatewayId": "nat-1", "State": "pending"}]}
    )
    ec2.add_response(
        "describe_nat_gateways", {"NatGateways": [{"NatGatewayId": "nat-1", "State": "available"}]}
    )
    ec2.add_response("describe_route_tables", {"RouteTables": []})
    ec2.add_response("create_route_table", {"RouteTable": {"RouteTableId": "rtb-priv"}})
    ec2.add_response(
        "create_route",
        {},
        {"RouteTableId": "rtb-priv", "DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-1"},
    )
    for i in range(3, 6):
        ec2.add_response(
            "associate_route_table", {}, {"RouteTableId": "rtb-priv", "SubnetId": f"subnet-{i}"}
        )
    logs.add_response("create_log_group", {}, {"logGroupName": "/aif/vpc-flow-logs", "tags": ANY})
    logs.add_response(
        "put_retention_policy", {}, {"logGroupName": "/aif/vpc-flow-logs", "retentionInDays": 7}
    )
    iam.add_client_error("get_policy", "NoSuchEntity")
    iam.add_response(
        "create_policy",
        {"Policy": {"Arn": "arn:aws:iam::111122223333:policy/aif/aif-infra-boundary"}},
    )
    iam.add_client_error("get_role", "NoSuchEntity")
    iam.add_response(
        "create_role",
        {
            "Role": {
                **ROLE,
                "RoleName": "aif-vpc-flow-logs",
                "Arn": "arn:aws:iam::111122223333:role/aif/aif-vpc-flow-logs",
            }
        },
    )
    iam.add_response("list_attached_role_policies", {"AttachedPolicies": []})
    iam.add_response("put_role_policy", {})
    ec2.add_response("describe_flow_logs", {"FlowLogs": []})
    ec2.add_response(
        "create_flow_logs",
        {},
        {
            "ResourceIds": ["vpc-1"],
            "ResourceType": "VPC",
            "TrafficType": "ALL",
            "LogDestinationType": "cloud-watch-logs",
            "LogGroupName": "/aif/vpc-flow-logs",
            "DeliverLogsPermissionArn": "arn:aws:iam::111122223333:role/aif/aif-vpc-flow-logs",
            "TagSpecifications": ANY,
        },
    )
    iam.add_client_error("get_role", "NoSuchEntity")
    iam.add_response(
        "create_role",
        {
            "Role": {
                **ROLE,
                "RoleName": "aif-ide-stand-in",
                "Arn": "arn:aws:iam::111122223333:role/aif/aif-ide-stand-in",
            }
        },
    )
    iam.add_response("list_attached_role_policies", {"AttachedPolicies": []})
    ec2.add_response("describe_security_groups", {"SecurityGroups": []})
    ec2.add_response("create_security_group", {"GroupId": "sg-ide"})
    sso.add_response(
        "list_instances",
        {
            "Instances": [
                {
                    "InstanceArn": "arn:aws:sso:::instance/ssoins-0123456789abcdef",
                    "IdentityStoreId": "d-0123456789",
                }
            ]
        },
    )


class UpTest(unittest.TestCase):
    def test_fresh_up_builds_the_network_and_publishes_inputs(self) -> None:
        with Clients() as c:
            _stub_fresh_up(c)
            ssm = c.stub["ssm"]
            expected = {
                "/aif/admin-role-arn": ADMIN,
                "/aif/ide-role-arn": "arn:aws:iam::111122223333:role/aif/aif-ide-stand-in",
                "/aif/ide-security-group-id": "sg-ide",
                "/aif/idc-instance-arn": "arn:aws:sso:::instance/ssoins-0123456789abcdef",
                "/aif/identity-store-id": "d-0123456789",
                "/aif/launch-stack-name": "aif-dev-standin",
                "/aif/region": "us-west-2",
                "/aif/vpc-id": "vpc-1",
            }
            for name, value in sorted(expected.items()):
                ssm.add_response(
                    "put_parameter",
                    {},
                    {"Name": name, "Value": value, "Type": "String", "Overwrite": True},
                )
            ssm.add_response(
                "put_parameter",
                {},
                {
                    "Name": "/aif/public-subnet-ids",
                    "Value": "subnet-0,subnet-1,subnet-2",
                    "Type": "StringList",
                    "Overwrite": True,
                },
            )
            ssm.add_response(
                "put_parameter",
                {},
                {
                    "Name": "/aif/private-subnet-ids",
                    "Value": "subnet-3,subnet-4,subnet-5",
                    "Type": "StringList",
                    "Overwrite": True,
                },
            )
            for _ in range(len(expected) + 2):
                ssm.add_response("add_tags_to_resource", {})
            report = devnet.up(
                c.c, "us-west-2", TAGS, ADMIN, dev_standin=True, clock=FakeClock().clock()
            )
            c.done(self)
        self.assertIn("created  vpc 10.64.0.0/16", report.lines)
        self.assertIn("created  nat gateway and its elastic IP", report.lines)
        self.assertIn("created  vpc flow log", report.lines)

    def test_subnets_carry_the_load_balancer_role_tags(self) -> None:
        spec = devnet._spec("subnet", {**devnet._tags(TAGS, "n"), "kubernetes.io/role/elb": "1"})
        keys = {t["Key"]: t["Value"] for t in spec[0]["Tags"]}
        self.assertEqual(keys["kubernetes.io/role/elb"], "1")
        self.assertEqual(keys["aif:dev-standin"], "true")
        self.assertEqual(keys["project"], "ai-foundations-eks")
        self.assertEqual(keys["stage"], "platform")


class DownTest(unittest.TestCase):
    def test_down_deletes_in_reverse_order(self) -> None:
        with Clients() as c:
            eks, ec2, iam, logs, ssm = (c.stub[k] for k in ("eks", "ec2", "iam", "logs", "ssm"))
            eks.add_client_error("describe_cluster", "ResourceNotFoundException")
            ec2.add_response("describe_vpcs", {"Vpcs": [{"VpcId": "vpc-1"}]}, {"Filters": OURS})
            ec2.add_response("describe_flow_logs", {"FlowLogs": [{"FlowLogId": "fl-1"}]})
            ec2.add_response("delete_flow_logs", {}, {"FlowLogIds": ["fl-1"]})
            ec2.add_response(
                "describe_nat_gateways",
                {"NatGateways": [{"NatGatewayId": "nat-1", "State": "available"}]},
            )
            ec2.add_response(
                "delete_nat_gateway", {"NatGatewayId": "nat-1"}, {"NatGatewayId": "nat-1"}
            )
            ec2.add_response(
                "describe_nat_gateways",
                {"NatGateways": [{"NatGatewayId": "nat-1", "State": "deleted"}]},
            )
            ec2.add_response(
                "describe_addresses",
                {"Addresses": [{"AllocationId": "eipalloc-1"}]},
                {"Filters": OURS},
            )
            ec2.add_response("release_address", {}, {"AllocationId": "eipalloc-1"})
            ec2.add_response(
                "describe_internet_gateways", {"InternetGateways": [{"InternetGatewayId": "igw-1"}]}
            )
            ec2.add_response(
                "detach_internet_gateway", {}, {"InternetGatewayId": "igw-1", "VpcId": "vpc-1"}
            )
            ec2.add_response("delete_internet_gateway", {}, {"InternetGatewayId": "igw-1"})
            ec2.add_response("describe_subnets", {"Subnets": [{"SubnetId": "subnet-1"}]})
            ec2.add_response("delete_subnet", {}, {"SubnetId": "subnet-1"})
            ec2.add_response(
                "describe_route_tables",
                {
                    "RouteTables": [
                        {"RouteTableId": "rtb-main", "Associations": [{"Main": True}]},
                        {"RouteTableId": "rtb-pub", "Associations": []},
                    ]
                },
            )
            ec2.add_response("delete_route_table", {}, {"RouteTableId": "rtb-pub"})
            ec2.add_response(
                "describe_security_groups",
                {
                    "SecurityGroups": [
                        {"GroupId": "sg-d", "GroupName": "default"},
                        {"GroupId": "sg-ide", "GroupName": "aif-dev-ide"},
                    ]
                },
            )
            ec2.add_response("delete_security_group", {}, {"GroupId": "sg-ide"})
            ec2.add_response("delete_vpc", {}, {"VpcId": "vpc-1"})
            for name in ("aif-vpc-flow-logs", "aif-ide-stand-in"):
                iam.add_response(
                    "get_role", {"Role": {**ROLE, "RoleName": name}}, {"RoleName": name}
                )
                iam.add_response(
                    "list_role_policies",
                    {"PolicyNames": ["p"] if "flow" in name else []},
                    {"RoleName": name},
                )
                if "flow" in name:
                    iam.add_response(
                        "delete_role_policy", {}, {"RoleName": name, "PolicyName": "p"}
                    )
                iam.add_response("delete_role", {}, {"RoleName": name})
            logs.add_response("delete_log_group", {}, {"logGroupName": "/aif/vpc-flow-logs"})
            ssm.add_response("delete_parameters", {}, {"Names": list(devnet.PUBLISHED)})
            report = devnet.down(c.c, TAGS, dev_standin=True, clock=FakeClock().clock())
            c.done(self)
        self.assertEqual(report.lines[-1], "deleted  SSM parameters published by the stand-in")
        self.assertIn("deleted  vpc vpc-1", report.lines)

    def test_down_refuses_a_role_outside_aif(self) -> None:
        with Clients() as c:
            c.stub["iam"].add_response("get_role", {"Role": {**ROLE, "Path": "/"}})
            with self.assertRaisesRegex(HubError, "not deleting"):
                devnet._delete_role(c.raw["iam"], "x", Report())
