import unittest

from botocore.stub import Stubber
from fakes import offline_client

from aif import keep
from aif.config import Settings

PARAM = "/aif/launch-stack-name"
STACK_ARN = "arn:aws:cloudformation:us-west-2:111122223333:stack/launch/1"


def parameter(value: str) -> dict[str, object]:
    return {"Parameter": {"Name": PARAM, "Type": "String", "Value": value}}


class KeepStacksTest(unittest.TestCase):
    def test_default_keeps_launch_and_bootstrap(self) -> None:
        ssm = offline_client("ssm")
        with Stubber(ssm) as stub:
            stub.add_response("get_parameter", parameter(" launch "), {"Name": PARAM})
            kept = keep.keep_stacks(ssm, Settings(keep_stacks=("extra", "launch")))
        self.assertEqual(kept, ("launch", "extra"))

    def test_missing_parameter_is_an_error(self) -> None:
        ssm = offline_client("ssm")
        with Stubber(ssm) as stub:
            stub.add_client_error("get_parameter", "ParameterNotFound")
            with self.assertRaisesRegex(keep.KeepError, "ParameterNotFound"):
                keep.keep_stacks(ssm, Settings())

    def test_empty_parameter_is_an_error(self) -> None:
        ssm = offline_client("ssm")
        with Stubber(ssm) as stub:
            stub.add_response("get_parameter", parameter("  "))
            with self.assertRaisesRegex(keep.KeepError, "empty"):
                keep.keep_stacks(ssm, Settings())


class IsKeptTest(unittest.TestCase):
    def test_cases(self) -> None:
        kept = ("launch", "bootstrap")
        cases = [
            (STACK_ARN, {}, True),
            ("arn:aws:s3:::b", {"aws:cloudformation:stack-name": "bootstrap"}, True),
            ("arn:aws:s3:::b", {"aif:keep": "TRUE"}, True),
            ("arn:aws:s3:::b", {"aif:keep": "no"}, False),
            ("arn:aws:cloudformation:us-west-2:111122223333:stack/hub/1", {}, False),
            ("arn:aws:s3:::b", {}, False),
        ]
        for arn, tags, want in cases:
            with self.subTest(arn=arn, tags=tags):
                self.assertEqual(keep.is_kept(arn, tags, kept), want)

    def test_stack_name_from_arn(self) -> None:
        self.assertEqual(keep.stack_name_from_arn(STACK_ARN), "launch")
        self.assertIsNone(keep.stack_name_from_arn("arn:aws:s3:::b"))
        self.assertIsNone(keep.stack_name_from_arn("bad"))
