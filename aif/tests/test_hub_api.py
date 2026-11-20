import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from botocore.stub import Stubber
from fakes import offline_client

from aif import hub_api, launch
from aif.hub_api import HubError


class LoaderTest(unittest.TestCase):
    def test_missing_module(self) -> None:
        with (
            mock.patch.dict(sys.modules, {"aif.hub": None}),
            self.assertRaisesRegex(HubError, "not built yet"),
        ):
            hub_api.load_hub("us-west-2", {})

    def test_missing_factory(self) -> None:
        with (
            mock.patch.dict(sys.modules, {"aif.hub": types.ModuleType("aif.hub")}),
            self.assertRaisesRegex(HubError, "no make_dev_network"),
        ):
            hub_api.load_dev_network("us-west-2", {})

    def test_factories_get_region_and_tags(self) -> None:
        module = types.ModuleType("aif.hub")
        module.make_hub = lambda region, tags: ("hub", region, dict(tags))  # type: ignore[attr-defined]
        module.make_dev_network = lambda region, tags: ("net", region)  # type: ignore[attr-defined]
        with mock.patch.dict(sys.modules, {"aif.hub": module}):
            hub = hub_api.load_hub("us-west-2", {"project": "p"})
            net = hub_api.load_dev_network("us-west-2", {})
        self.assertEqual(hub, ("hub", "us-west-2", {"project": "p"}))
        self.assertEqual(net, ("net", "us-west-2"))

    def test_other_import_errors_are_not_hidden(self) -> None:
        err = ModuleNotFoundError("No module named 'kubernetes_x'", name="kubernetes_x")
        with (
            mock.patch("importlib.import_module", side_effect=err),
            self.assertRaises(ModuleNotFoundError),
        ):
            hub_api.load_hub("us-west-2", {})


class LaunchInputsTest(unittest.TestCase):
    def params(self, **values: str) -> dict[str, object]:
        base = {name: f"v-{key}" for key, name in launch.PARAMS.items()}
        base.update({"/aif/region": "us-west-2", "/aif/public-subnet-ids": "a,b"})
        base.update(values)
        return {"Parameters": [{"Name": n, "Type": "String", "Value": v} for n, v in base.items()]}

    def test_reads_all(self) -> None:
        ssm = offline_client("ssm")
        with Stubber(ssm) as stub:
            stub.add_response("get_parameters", self.params())
            inputs = launch.read_inputs(ssm, "us-west-2", "capability")
        self.assertEqual(inputs.public_subnet_ids, ("a", "b"))
        self.assertEqual(inputs.idc_instance_arn, "v-idc_instance_arn")

    def test_idc_optional_in_helm_mode(self) -> None:
        ssm = offline_client("ssm")
        with Stubber(ssm) as stub:
            stub.add_response("get_parameters", self.params(**{"/aif/idc-instance-arn": ""}))
            inputs = launch.read_inputs(ssm, "us-west-2", "helm")
        self.assertIsNone(inputs.idc_instance_arn)

    def test_idc_required_in_capability_mode(self) -> None:
        ssm = offline_client("ssm")
        with Stubber(ssm) as stub:
            stub.add_response("get_parameters", self.params(**{"/aif/idc-instance-arn": ""}))
            with self.assertRaisesRegex(launch.LaunchError, "/aif/idc-instance-arn"):
                launch.read_inputs(ssm, "us-west-2", "capability")

    def test_region_must_match(self) -> None:
        ssm = offline_client("ssm")
        with Stubber(ssm) as stub:
            stub.add_response("get_parameters", self.params(**{"/aif/region": "eu-west-1"}))
            with self.assertRaisesRegex(launch.LaunchError, "eu-west-1"):
                launch.read_inputs(ssm, "us-west-2", "capability")


class NoSubprocessTest(unittest.TestCase):
    """aif starts no other programs. No module may import subprocess or os.system."""

    def test_no_module_starts_programs(self) -> None:
        root = Path(__file__).resolve().parents[1]
        banned = ("import subprocess", "from subprocess", "os.system(", "os.popen(", "pty.spawn(")
        offenders = [
            f"{path.relative_to(root)}: {line.strip()}"
            for path in sorted([*root.glob("src/**/*.py"), *root.glob("tests/**/*.py")])
            if path.name != Path(__file__).name
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#")[0].strip().startswith(banned) or "os.system(" in line
        ]
        self.assertEqual(offenders, [])
