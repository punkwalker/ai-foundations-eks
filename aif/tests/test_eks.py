import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any

import yaml
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from fakes import FakeKube, hub_secret, obj, offline_client

from aif import eks
from aif.eks import ClusterInfo

REGION = "us-west-2"
ACCOUNT = "111122223333"


def info(key: str) -> ClusterInfo:
    name = f"aif-{key}"
    return ClusterInfo(
        key=key,
        name=name,
        arn=f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/{name}",
        endpoint=f"https://{key.upper()}.gr7.{REGION}.eks.amazonaws.com",
        ca_data="Q0EtREFUQQ==",
    )


def described(key: str, status: str = "ACTIVE") -> dict[str, Any]:
    i = info(key)
    return {
        "cluster": {
            "name": i.name,
            "arn": i.arn,
            "endpoint": i.endpoint,
            "status": status,
            "certificateAuthority": {"data": i.ca_data},
        }
    }


class DescribeTest(unittest.TestCase):
    def test_reads_all_three(self) -> None:
        client = offline_client("eks")
        with Stubber(client) as stub:
            for key in eks.CLUSTERS:
                stub.add_response("describe_cluster", described(key), {"name": f"aif-{key}"})
            found = eks.describe(client)
        self.assertEqual(found, [info(k) for k in eks.CLUSTERS])
        self.assertEqual(found[0].context, "aif-hub")

    def test_not_active(self) -> None:
        client = offline_client("eks")
        with Stubber(client) as stub:
            stub.add_response("describe_cluster", described("hub", "CREATING"))
            with self.assertRaisesRegex(eks.EksError, "aif-hub is CREATING"):
                eks.describe(client)


class KubeconfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self.path = self.home / ".kube" / "config"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_path(self) -> None:
        self.assertEqual(eks.kubeconfig_path({}, self.home), self.path)
        joined = os.pathsep.join(["/a/one", "/a/two"])
        self.assertEqual(eks.kubeconfig_path({"KUBECONFIG": joined}, self.home), Path("/a/one"))

    def test_entries_hold_no_token(self) -> None:
        cluster, user, context = eks.entries(info("dev"), REGION)
        self.assertEqual(
            context,
            {"name": "aif-dev", "context": {"cluster": info("dev").arn, "user": info("dev").arn}},
        )
        self.assertEqual(cluster["cluster"]["server"], info("dev").endpoint)
        exec_ = user["user"]["exec"]
        self.assertEqual(exec_["command"], "aws")
        self.assertEqual(
            exec_["args"], ["--region", REGION, "eks", "get-token", "--cluster-name", "aif-dev"]
        )
        self.assertEqual(set(user["user"]), {"exec"})

    def test_write_new_file_is_private(self) -> None:
        eks.write_kubeconfig(self.path, [info(k) for k in eks.CLUSTERS], REGION)
        data = yaml.safe_load(self.path.read_text())
        self.assertEqual([c["name"] for c in data["contexts"]], ["aif-hub", "aif-dev", "aif-prod"])
        self.assertEqual(data["current-context"], "aif-hub")
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn("token", self.path.read_text().replace("get-token", ""))

    def test_merge_keeps_other_entries_and_is_idempotent(self) -> None:
        self.path.parent.mkdir(parents=True)
        other = {
            "apiVersion": "v1",
            "kind": "Config",
            "current-context": "mine",
            "clusters": [{"name": "mine", "cluster": {"server": "https://x"}}],
            "users": [{"name": "mine", "user": {}}],
            "contexts": [{"name": "mine", "context": {"cluster": "mine", "user": "mine"}}],
        }
        self.path.write_text(yaml.safe_dump(other))
        eks.write_kubeconfig(self.path, [info("hub")], REGION)
        first = self.path.read_text()
        eks.write_kubeconfig(self.path, [info("hub")], REGION)
        self.assertEqual(self.path.read_text(), first)
        data = yaml.safe_load(first)
        self.assertEqual(data["current-context"], "mine")
        self.assertEqual([c["name"] for c in data["contexts"]], ["mine", "aif-hub"])

    def test_refuses_odd_file(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_text("- a list\n")
        with self.assertRaises(eks.EksError):
            eks.write_kubeconfig(self.path, [info("hub")], REGION)
        self.assertEqual(self.path.read_text(), "- a list\n")

    def test_failed_write_leaves_no_temp_file(self) -> None:
        self.path.parent.mkdir(parents=True)
        bad = ClusterInfo("hub", "aif-hub", object(), "e", "c")  # type: ignore[arg-type]
        with self.assertRaises(yaml.representer.RepresenterError):
            eks.write_kubeconfig(self.path, [bad], REGION)
        self.assertEqual(list(self.path.parent.iterdir()), [])


class ClusterSecretTest(unittest.TestCase):
    def test_capability_uses_arn(self) -> None:
        body = eks.cluster_secret(info("dev"), "capability")
        self.assertEqual(body["stringData"], {"name": "aif-dev", "server": info("dev").arn})
        self.assertEqual(body["metadata"]["labels"]["argocd.argoproj.io/secret-type"], "cluster")

    def test_helm_uses_endpoint_and_iam(self) -> None:
        data = eks.cluster_secret(info("dev"), "helm")["stringData"]
        self.assertEqual(data["server"], info("dev").endpoint)
        config = yaml.safe_load(data["config"])
        self.assertEqual(config["awsAuthConfig"], {"clusterName": "aif-dev"})
        self.assertNotIn("bearerToken", config)

    def test_apply_creates_then_updates_and_keeps_labels(self) -> None:
        hub = FakeKube()
        hub.secrets[("argocd", "hub")] = hub_secret({"platform.aws/stage-00": "on"})
        names = {"hub": "hub", "dev": "dev", "prod": "prod"}
        clusters = [info(k) for k in eks.CLUSTERS]
        done = eks.apply_cluster_secrets(hub, "argocd", clusters, "capability", names)
        self.assertEqual(done, ["argocd/hub", "argocd/dev", "argocd/prod"])
        hub_labels = hub.secrets[("argocd", "hub")]["metadata"]["labels"]
        self.assertEqual(hub_labels["platform.aws/stage-00"], "on")
        self.assertEqual(hub.secrets[("argocd", "dev")]["metadata"]["name"], "dev")
        eks.apply_cluster_secrets(hub, "argocd", clusters, "capability", names)
        creates = [c for c in hub.calls if c[0] == "create-secret"]
        self.assertEqual(len(creates), 2)


class StateHelpersTest(unittest.TestCase):
    def test_from_state(self) -> None:
        from aif.hub_api import ClusterState

        active = ClusterState("aif-hub", "arn:h", "ACTIVE", "https://h", "Q0E=")
        self.assertEqual(eks.from_state("hub", active).arn, "arn:h")
        with self.assertRaisesRegex(eks.EksError, "CREATING"):
            eks.from_state("hub", ClusterState("aif-hub", "a", "CREATING", "", ""))

    def test_status(self) -> None:
        client = offline_client("eks")
        with Stubber(client) as stub:
            stub.add_response("describe_cluster", described("dev", "CREATING"))
            stub.add_client_error("describe_cluster", "ResourceNotFoundException")
            stub.add_client_error("describe_cluster", "AccessDeniedException")
            self.assertEqual(eks.status(client, "dev"), "CREATING")
            self.assertEqual(eks.status(client, "dev"), "MISSING")
            with self.assertRaises(ClientError):
                eks.status(client, "dev")

    def test_secret_clusters(self) -> None:
        hub = FakeKube()
        kind = {"argocd.argoproj.io/secret-type": "cluster"}
        hub.secrets[("argocd", "dev")] = obj(
            "dev", "argocd", labels={**kind, "platform.aws/cluster": "dev"}
        )
        hub.secrets[("argocd", "other")] = obj("other", "argocd", labels=kind)
        hub.secrets[("argocd", "repo")] = obj("repo", "argocd", labels={"x": "y"})
        self.assertEqual(eks.secret_clusters(hub, "argocd"), {"dev"})
