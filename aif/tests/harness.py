"""Shared set-up for CLI tests. Every outside call goes to a fake or a botocore Stubber."""

import io
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path

from botocore.stub import Stubber
from fakes import (
    FakeClock,
    FakeDevNetwork,
    FakeHub,
    FakeKube,
    app,
    hub_secret,
    obj,
    offline_client,
)

from aif.argocd import APPLICATIONS, APPLICATIONSETS
from aif.cli import Deps, main
from aif.launch import PARAMS
from aif.teardown import AwsClients

ACCOUNT = "111122223333"
HERE = Path(__file__).resolve().parent
LAUNCH_PARAM = "/aif/launch-stack-name"


def aws_clients() -> AwsClients:
    return AwsClients(
        **{
            name: offline_client(service)
            for name, service in (
                ("s3", "s3"),
                ("ecr", "ecr"),
                ("secrets", "secretsmanager"),
                ("ec2", "ec2"),
                ("elbv2", "elbv2"),
                ("iam", "iam"),
                ("tagging", "resourcegroupstaggingapi"),
                ("cloudformation", "cloudformation"),
            )
        }
    )


class CliHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.hub = FakeKube()
        self.hub.secrets[("argocd", "hub")] = hub_secret()
        self.hub.add(APPLICATIONS, app("team", "00"))
        self.hub.add(APPLICATIONS, app("gpu", "03", health="Degraded"))
        self.sts = offline_client("sts")
        self.ssm = offline_client("ssm")
        self.eks = offline_client("eks")
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._home.cleanup)
        self.home = Path(self._home.name)
        self.aws = aws_clients()
        self.clock = FakeClock()
        self.env: dict[str, str] = {}
        self.fake_hub = FakeHub()
        self.dev_network = FakeDevNetwork()
        self.deps = Deps(
            kube=lambda _ctx: self.hub,
            sts=lambda _region: self.sts,
            ssm=lambda _region: self.ssm,
            aws_clients=lambda _region: self.aws,
            hub=lambda _region, _tags: self.fake_hub,
            dev_network=lambda _region, _tags: self.dev_network,
            eks=lambda _region: self.eks,
            home=self.home,
            clock=self.clock.clock(),
            cwd=HERE,
            env=self.env,
        )
        self.stack = ExitStack()
        self.stubs = {
            name: self.stack.enter_context(Stubber(client))
            for name, client in (
                ("sts", self.sts),
                ("ssm", self.ssm),
                ("eks", self.eks),
                ("tagging", self.aws.tagging),
                ("iam", self.aws.iam),
                ("cloudformation", self.aws.cloudformation),
            )
        }
        self.addCleanup(self.stack.close)

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(argv), self.deps)
        return code, out.getvalue(), err.getvalue()

    def caller(self, account: str = ACCOUNT) -> None:
        arn = f"arn:aws:sts::{account}:assumed-role/Admin/session"
        self.stubs["sts"].add_response(
            "get_caller_identity", {"Account": account, "Arn": arn, "UserId": "x"}
        )

    def launch_stack(self, name: str = "launch") -> None:
        self.stubs["ssm"].add_response(
            "get_parameter",
            {"Parameter": {"Name": LAUNCH_PARAM, "Type": "String", "Value": name}},
            {"Name": LAUNCH_PARAM},
        )

    def launch_inputs(self, **override: str | None) -> None:
        values: dict[str, str | None] = {n: f"value-{k}" for k, n in PARAMS.items()}
        values["/aif/region"] = "us-west-2"
        values["/aif/launch-stack-name"] = "launch"
        values["/aif/private-subnet-ids"] = "subnet-a, subnet-b"
        values.update(override)
        params = [
            {"Name": n, "Type": "String", "Value": v} for n, v in values.items() if v is not None
        ]
        self.stubs["ssm"].add_response("get_parameters", {"Parameters": params})

    def spokes_ready(self) -> None:
        """Argo CD API served, spoke Secrets present, both spokes ACTIVE."""
        self.hub.store(APPLICATIONSETS)
        for key in ("dev", "prod"):
            labels = {"argocd.argoproj.io/secret-type": "cluster", "platform.aws/cluster": key}
            self.hub.secrets[("argocd", key)] = obj(key, "argocd", labels=labels)
        self.clusters_active(("dev", "prod"))
        self.clusters_active(("dev", "prod"))

    def clusters_active(self, keys: tuple[str, ...] = ("hub", "dev", "prod")) -> None:
        for key in keys:
            name = f"aif-{key}"
            cluster = {
                "name": name,
                "arn": f"arn:aws:eks:us-west-2:{ACCOUNT}:cluster/{name}",
                "endpoint": f"https://{key}.example.eks.amazonaws.com",
                "status": "ACTIVE",
                "certificateAuthority": {"data": "Q0E="},
            }
            self.stubs["eks"].add_response("describe_cluster", {"cluster": cluster}, {"name": name})

    def no_tagged_resources(self) -> None:
        self.stubs["tagging"].add_response("get_resources", {"ResourceTagMappingList": []})
