import unittest
from unittest import mock

from botocore.stub import Stubber
from fakes import offline_client

from aif import aws

ARN = "arn:aws:sts::111122223333:assumed-role/Admin/session"


class RoleNameTest(unittest.TestCase):
    def test_role_name_shapes(self) -> None:
        cases = {
            ARN: "Admin",
            "arn:aws:iam::111122223333:role/aif/agents/agent-a": "agent-a",
            "arn:aws:iam::111122223333:user/dev/alice": "user/alice",
            "arn:aws:iam::111122223333:root": "root",
        }
        for arn, name in cases.items():
            with self.subTest(arn=arn):
                self.assertEqual(aws.role_name(arn), name)


class ConfirmTest(unittest.TestCase):
    def sts(self, stub: Stubber) -> None:
        stub.add_response(
            "get_caller_identity", {"Account": "111122223333", "Arn": ARN, "UserId": "AROA:x"}
        )

    def test_match(self) -> None:
        sts = offline_client("sts")
        with Stubber(sts) as stub:
            self.sts(stub)
            who = aws.confirm_account(sts, "111122223333")
        self.assertEqual(who, aws.Identity("111122223333", "Admin"))

    def test_mismatch(self) -> None:
        sts = offline_client("sts")
        with Stubber(sts) as stub:
            self.sts(stub)
            with self.assertRaisesRegex(aws.AccountMismatchError, "not 444455556666"):
                aws.confirm_account(sts, "444455556666")

    def test_bad_format_makes_no_call(self) -> None:
        sts = offline_client("sts")
        with Stubber(sts):
            for bad in ("123", "11112222333a", ""):
                with self.subTest(bad=bad), self.assertRaises(aws.AccountMismatchError):
                    aws.confirm_account(sts, bad)


class ClientsTest(unittest.TestCase):
    def test_clients_use_region(self) -> None:
        with mock.patch("boto3.Session") as session:
            built = aws.clients("us-west-2")
        session.assert_called_once_with(region_name="us-west-2")
        services = [c.args[0] for c in session.return_value.client.call_args_list]
        self.assertEqual(
            services,
            [
                "s3",
                "ecr",
                "secretsmanager",
                "ec2",
                "elbv2",
                "iam",
                "resourcegroupstaggingapi",
                "cloudformation",
            ],
        )
        self.assertIsNotNone(built.tagging)
        with mock.patch("boto3.client") as client:
            aws.sts_client("us-west-2")
            aws.ssm_client("us-west-2")
            aws.eks_client("us-west-2")
        self.assertEqual([c.args[0] for c in client.call_args_list], ["sts", "ssm", "eks"])
