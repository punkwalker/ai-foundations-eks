"""Connect aif and Argo CD to the three EKS clusters.

aif reads each cluster with `eks:DescribeCluster` (or takes the Hub state from
`HubApi.hub_status`) and then:

1. Writes a kubeconfig entry per cluster, merged into the existing file. Each user
   entry runs `aws eks get-token` when kubectl or aif needs a token, so the file
   holds no token or key.
2. Creates or updates the Argo CD cluster Secret for the Hub. The platform creates
   the Dev and Prod Secrets; aif waits for them by the label `platform.aws/cluster`.
3. Leaves every other kubeconfig entry, and every label on the Secrets, as it was.
"""

import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from botocore.exceptions import ClientError

from aif.config import ArgoCdMode
from aif.hub_api import ClusterState
from aif.kube import KubeApi, Obj

if TYPE_CHECKING:
    from mypy_boto3_eks import EKSClient

CLUSTERS = ("hub", "dev", "prod")
CLUSTER_PREFIX = "aif-"
"""EKS cluster names and kubeconfig context names are aif-hub, aif-dev, aif-prod."""
EXEC_API = "client.authentication.k8s.io/v1beta1"
ARGOCD_TYPE_LABEL = "argocd.argoproj.io/secret-type"
CLUSTER_LABEL = "platform.aws/cluster"


class EksError(RuntimeError):
    """A cluster is missing or not ready."""


@dataclass(frozen=True)
class ClusterInfo:
    key: str
    """hub, dev or prod."""
    name: str
    arn: str
    endpoint: str
    ca_data: str

    @property
    def context(self) -> str:
        return self.name


def cluster_name(key: str) -> str:
    return f"{CLUSTER_PREFIX}{key}"


def from_state(key: str, state: ClusterState) -> ClusterInfo:
    """A ClusterInfo from the HubApi view of a cluster."""
    if state.status != "ACTIVE":
        raise EksError(f"cluster {state.name} is {state.status}, not ACTIVE")
    return ClusterInfo(key, state.name, state.arn, state.endpoint, state.ca_data)


def status(eks: "EKSClient", key: str) -> str:
    """The cluster status, or MISSING when it does not exist yet."""
    try:
        return eks.describe_cluster(name=cluster_name(key))["cluster"].get("status", "UNKNOWN")
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return "MISSING"
        raise


def secret_clusters(hub: KubeApi, namespace: str) -> set[str]:
    """Values of `platform.aws/cluster` on the Argo CD cluster Secrets on the Hub."""
    secrets = hub.list_secrets(namespace, f"{ARGOCD_TYPE_LABEL}=cluster")
    return {
        str(s["metadata"]["labels"][CLUSTER_LABEL])
        for s in secrets
        if CLUSTER_LABEL in (s.get("metadata", {}).get("labels") or {})
    }


def describe(eks: "EKSClient", keys: Iterable[str] = CLUSTERS) -> list[ClusterInfo]:
    """Read each cluster. Raises EksError if one is not ACTIVE."""
    found: list[ClusterInfo] = []
    for key in keys:
        name = cluster_name(key)
        cluster = eks.describe_cluster(name=name)["cluster"]
        status = cluster.get("status", "UNKNOWN")
        if status != "ACTIVE":
            raise EksError(f"cluster {name} is {status}, not ACTIVE")
        found.append(
            ClusterInfo(
                key=key,
                name=name,
                arn=cluster["arn"],
                endpoint=cluster["endpoint"],
                ca_data=cluster["certificateAuthority"]["data"],
            )
        )
    return found


# kubeconfig


def kubeconfig_path(env: Mapping[str, str], home: Path) -> Path:
    """The first file in KUBECONFIG, else ~/.kube/config."""
    first = env.get("KUBECONFIG", "").split(os.pathsep)[0]
    return Path(first) if first else home / ".kube" / "config"


def entries(info: ClusterInfo, region: str) -> tuple[Obj, Obj, Obj]:
    """The cluster, user and context entries for one cluster. No token is stored."""
    cluster = {
        "name": info.arn,
        "cluster": {"server": info.endpoint, "certificate-authority-data": info.ca_data},
    }
    user = {
        "name": info.arn,
        "user": {
            "exec": {
                "apiVersion": EXEC_API,
                "command": "aws",
                "args": ["--region", region, "eks", "get-token", "--cluster-name", info.name],
                "interactiveMode": "Never",
            }
        },
    }
    context = {"name": info.context, "context": {"cluster": info.arn, "user": info.arn}}
    return cluster, user, context


def _upsert(items: list[Obj], entry: Obj) -> list[Obj]:
    return [i for i in items if i.get("name") != entry["name"]] + [entry]


def merge(current: Mapping[str, Any] | None, clusters: Iterable[ClusterInfo], region: str) -> Obj:
    """Add or replace the aif entries. Every other entry stays as it was."""
    config: Obj = dict(current or {})
    config.setdefault("apiVersion", "v1")
    config.setdefault("kind", "Config")
    lists = {k: list(config.get(k) or []) for k in ("clusters", "users", "contexts")}
    for info in clusters:
        cluster, user, context = entries(info, region)
        lists["clusters"] = _upsert(lists["clusters"], cluster)
        lists["users"] = _upsert(lists["users"], user)
        lists["contexts"] = _upsert(lists["contexts"], context)
    config.update(lists)
    config.setdefault("current-context", cluster_name("hub"))
    return config


def write_kubeconfig(path: Path, clusters: Iterable[ClusterInfo], region: str) -> Path:
    """Merge the aif entries into `path`, readable by the owner only, replaced in one step."""
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else None
    if current is not None and not isinstance(current, dict):
        raise EksError(f"{path} is not a kubeconfig mapping; not changing it")
    merged = merge(current, clusters, region)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(merged, handle, sort_keys=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


# Argo CD cluster Secrets


def cluster_secret(info: ClusterInfo, mode: ArgoCdMode) -> Obj:
    """The Argo CD cluster Secret for one cluster. It holds no token or key.

    In capability mode the managed Argo CD reaches a cluster by its ARN.
    In helm mode Argo CD uses the endpoint and signs in with its own IAM role.
    """
    data: dict[str, str] = {"name": info.name}
    if mode == "capability":
        data["server"] = info.arn
    else:
        data["server"] = info.endpoint
        config = {
            "awsAuthConfig": {"clusterName": info.name},
            "tlsClientConfig": {"caData": info.ca_data},
        }
        data["config"] = yaml.safe_dump(config, default_flow_style=True).strip()
    return {
        "metadata": {
            "labels": {ARGOCD_TYPE_LABEL: "cluster", CLUSTER_LABEL: info.key},
        },
        "stringData": data,
    }


def apply_cluster_secrets(
    hub: KubeApi,
    namespace: str,
    clusters: Iterable[ClusterInfo],
    mode: ArgoCdMode,
    names: Mapping[str, str],
) -> list[str]:
    """Create or update one Secret per cluster. `names` maps hub/dev/prod to Secret names.

    An update merges labels, so the stage and group labels on the Hub Secret stay.
    """
    done: list[str] = []
    for info in clusters:
        name = names[info.key]
        body = cluster_secret(info, mode)
        if hub.get_secret(namespace, name) is None:
            hub.create_secret(namespace, {**body, "metadata": {**body["metadata"], "name": name}})
        else:
            hub.patch_secret(namespace, name, body)
        done.append(f"{namespace}/{name}")
    return done
