import unittest

from botocore.stub import ANY, Stubber
from fakes import offline_client

from aif.hub import storage
from aif.hub.common import HubError, Report
from aif.hub.policies import Scope

SCOPE = Scope(partition="aws", account="111122223333", region="us-west-2")
TAGS = {"project": "ai-foundations-eks", "stage": "platform"}
BUCKET = "aif-models-111122223333-us-west-2"


def _bucket_settings(stub: Stubber) -> None:
    stub.add_response(
        "put_public_access_block",
        {},
        {
            "Bucket": BUCKET,
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        },
    )
    stub.add_response(
        "put_bucket_encryption", {}, {"Bucket": BUCKET, "ServerSideEncryptionConfiguration": ANY}
    )
    stub.add_response("put_bucket_policy", {}, {"Bucket": BUCKET, "Policy": ANY})
    stub.add_response(
        "put_bucket_lifecycle_configuration", {}, {"Bucket": BUCKET, "LifecycleConfiguration": ANY}
    )
    stub.add_response("put_bucket_tagging", {}, {"Bucket": BUCKET, "Tagging": ANY})


class BucketTest(unittest.TestCase):
    def test_creates_a_missing_bucket(self) -> None:
        s3 = offline_client("s3")
        with Stubber(s3) as stub:
            stub.add_client_error("head_bucket", "404")
            stub.add_response(
                "create_bucket",
                {},
                {
                    "Bucket": BUCKET,
                    "CreateBucketConfiguration": {"LocationConstraint": "us-west-2"},
                    "ObjectOwnership": "BucketOwnerEnforced",
                },
            )
            _bucket_settings(stub)
            storage.ensure_bucket(s3, BUCKET, SCOPE, TAGS, Report())
            stub.assert_no_pending_responses()

    def test_bucket_owned_elsewhere_is_refused(self) -> None:
        s3 = offline_client("s3")
        with Stubber(s3) as stub:
            stub.add_client_error("head_bucket", "403")
            with self.assertRaisesRegex(HubError, "not in this account"):
                storage.ensure_bucket(s3, BUCKET, SCOPE, TAGS, Report())


class RepositoryTest(unittest.TestCase):
    def test_creates_scanned_immutable_repository(self) -> None:
        ecr = offline_client("ecr")
        with Stubber(ecr) as stub:
            stub.add_client_error("describe_repositories", "RepositoryNotFoundException")
            stub.add_response(
                "create_repository",
                {},
                {
                    "repositoryName": "aif/x",
                    "imageTagMutability": "IMMUTABLE",
                    "imageScanningConfiguration": {"scanOnPush": True},
                    "encryptionConfiguration": {"encryptionType": "AES256"},
                    "tags": ANY,
                },
            )
            stub.add_response(
                "put_lifecycle_policy", {}, {"repositoryName": "aif/x", "lifecyclePolicyText": ANY}
            )
            storage.ensure_repository(ecr, "aif/x", TAGS, Report())
            stub.assert_no_pending_responses()


class SecretTest(unittest.TestCase):
    def test_creates_a_secret_with_no_value(self) -> None:
        sm = offline_client("secretsmanager")
        with Stubber(sm) as stub:
            stub.add_client_error("describe_secret", "ResourceNotFoundException")
            stub.add_response(
                "create_secret", {}, {"Name": "aif/x", "Description": ANY, "Tags": ANY}
            )
            storage.ensure_secret(sm, "aif/x", TAGS, Report())
            stub.assert_no_pending_responses()

    def test_keeps_an_existing_secret(self) -> None:
        sm = offline_client("secretsmanager")
        with Stubber(sm) as stub:
            stub.add_response("describe_secret", {"Name": "aif/x"})
            rep = Report()
            storage.ensure_secret(sm, "aif/x", TAGS, rep)
        self.assertEqual(rep.lines, ["exists   secret aif/x"])


class PublishTest(unittest.TestCase):
    def test_puts_then_tags(self) -> None:
        ssm = offline_client("ssm")
        with Stubber(ssm) as stub:
            stub.add_response(
                "put_parameter",
                {},
                {"Name": "/aif/a", "Value": "1", "Type": "String", "Overwrite": True},
            )
            stub.add_response(
                "add_tags_to_resource",
                {},
                {"ResourceType": "Parameter", "ResourceId": "/aif/a", "Tags": ANY},
            )
            storage.publish(ssm, {"/aif/a": "1"}, TAGS)
            stub.assert_no_pending_responses()
