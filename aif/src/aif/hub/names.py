"""Names shared by the Hub modules, the dev network stand-in, the RGDs and the launch template."""

from typing import Final

IAM_PATH: Final = "/aif/"
ACK_ROLE_PATH: Final = "/aif/ack/"
BOUNDARY_NAME: Final = "aif-infra-boundary"
"""Ceiling for every role aif creates. The launch template defines its own, aif-role-boundary."""

HUB_CLUSTER: Final = "aif-hub"
KEY_ALIAS: Final = "alias/aif-platform"
ARGOCD_NAMESPACE: Final = "argocd"
STAGE_TAG: Final = "platform"

CLUSTER_ROLE: Final = "aif-hub-cluster"
NODE_ROLE: Final = "aif-hub-node"
ACK_ROLE: Final = "aif-ack-capability"
KRO_ROLE: Final = "aif-kro-capability"
ARGOCD_ROLE: Final = "aif-argocd-capability"
PLATFORM_CLUSTERS_ROLE: Final = "aif-ack-platform-clusters"
"""Assumed by ACK, through an IAMRoleSelector, for the `platform-clusters` namespace."""
PLATFORM_CLUSTERS_NAMESPACE: Final = "platform-clusters"

ARGOCD_GROUPS: Final = {"ADMIN": "aif-admins", "EDITOR": "aif-editors", "VIEWER": "aif-viewers"}
ECR_REPOSITORIES: Final = ("aif/sample-agent", "aif/sample-mcp-server")
SECRETS: Final = ("aif/keycloak/admin", "aif/litellm/master-key")

CLUSTER_ADMIN_POLICY: Final = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
CLUSTER_ROLE_POLICIES: Final = (
    "AmazonEKSClusterPolicy",
    "AmazonEKSComputePolicy",
    "AmazonEKSBlockStoragePolicy",
    "AmazonEKSLoadBalancingPolicy",
    "AmazonEKSNetworkingPolicy",
)
NODE_ROLE_POLICIES: Final = (
    "AmazonEKSWorkerNodeMinimalPolicy",
    "AmazonEC2ContainerRegistryPullOnly",
)


def model_bucket(account: str, region: str) -> str:
    return f"aif-models-{account}-{region}"


def control_plane_log_group(cluster: str) -> str:
    return f"/aws/eks/{cluster}/cluster"


class Param:
    """SSM parameter names. INPUT ones come from the launch template or the dev stand-in."""

    VPC_ID: Final = "/aif/vpc-id"
    PUBLIC_SUBNETS: Final = "/aif/public-subnet-ids"
    PRIVATE_SUBNETS: Final = "/aif/private-subnet-ids"
    REGION: Final = "/aif/region"
    ADMIN_ROLE: Final = "/aif/admin-role-arn"
    IDE_ROLE: Final = "/aif/ide-role-arn"
    IDE_SECURITY_GROUP: Final = "/aif/ide-security-group-id"
    LAUNCH_STACK: Final = "/aif/launch-stack-name"
    IDC_INSTANCE: Final = "/aif/idc-instance-arn"
    IDENTITY_STORE: Final = "/aif/identity-store-id"

    # Outputs aif publishes.
    BOUNDARY_ARN: Final = "/aif/platform/boundary-arn"
    KEY_ARN: Final = "/aif/platform/key-arn"
    MODEL_BUCKET: Final = "/aif/platform/model-bucket"
    ARGOCD_MODE: Final = "/aif/argocd/mode"

    @staticmethod
    def cluster(name: str, field: str) -> str:
        return f"/aif/clusters/{name}/{field}"

    @staticmethod
    def capability(kind: str) -> str:
        return f"/aif/capabilities/{kind}-arn"

    @staticmethod
    def argocd_group(role: str) -> str:
        return f"/aif/argocd/groups/{role.lower()}-id"
