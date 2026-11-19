"""IAM policy documents for the Hub. Each statement says why it exists.

No statement allows `*` actions or AdministratorAccess. `Resource: "*"` appears only
where the API has no resource ARN, and then with a Region condition.
"""

import json
from dataclasses import dataclass
from typing import Any

from aif.hub.names import ACK_ROLE_PATH, BOUNDARY_NAME, HUB_CLUSTER, IAM_PATH

Doc = dict[str, Any]


@dataclass(frozen=True)
class Scope:
    partition: str
    account: str
    region: str

    def arn(self, service: str, resource: str, *, region: bool = True, account: bool = True) -> str:
        reg = self.region if region else ""
        acct = self.account if account else ""
        return f"arn:{self.partition}:{service}:{reg}:{acct}:{resource}"

    def iam(self, resource: str) -> str:
        return self.arn("iam", resource, region=False)

    @property
    def boundary_arn(self) -> str:
        return self.iam(f"policy{IAM_PATH}{BOUNDARY_NAME}")


def document(*statements: Doc) -> Doc:
    return {"Version": "2012-10-17", "Statement": list(statements)}


def dumps(doc: Doc) -> str:
    return json.dumps(doc, separators=(",", ":"), sort_keys=True)


def in_region(scope: Scope) -> Doc:
    return {"StringEquals": {"aws:RequestedRegion": scope.region}}


def trust(*services: str, tag_session: bool = True) -> Doc:
    """Trust only the named AWS services."""
    actions = ["sts:AssumeRole", "sts:TagSession"] if tag_session else ["sts:AssumeRole"]
    return document(
        {"Effect": "Allow", "Principal": {"Service": list(services)}, "Action": actions}
    )


def trust_role(role_arn: str) -> Doc:
    """Trust one role (the ACK capability role, for IAM role selectors)."""
    return document(
        {
            "Effect": "Allow",
            "Principal": {"AWS": role_arn},
            "Action": ["sts:AssumeRole", "sts:TagSession"],
        }
    )


def boundary(scope: Scope) -> Doc:
    """aif-infra-boundary: the ceiling for every role aif creates. It grants nothing alone."""
    aif_roles = scope.iam(f"role{IAM_PATH}*")
    return document(
        # Services the AWS managed EKS Auto Mode policies use (cluster and node roles),
        # plus EKS itself, ECR, logs, KMS, Secrets Manager and STS for the capability and
        # ACK roles. Pinned to this Region.
        {
            "Sid": "RegionalServices",
            "Effect": "Allow",
            "Action": [
                "acm:*",
                "autoscaling:*",
                "cognito-idp:DescribeUserPoolClient",
                "ec2:*",
                "ecr:*",
                "eks:*",
                "eks-auth:*",
                "elasticloadbalancing:*",
                "kms:*",
                "logs:*",
                "secretsmanager:*",
                "sts:*",
                "wafv2:*",
            ],
            "Resource": "*",
            "Condition": in_region(scope),
        },
        # Global endpoints the AWS managed EKS policies call: Shield for load balancers and
        # the ECR bearer token for image pulls. A Region condition cannot pin them.
        {
            "Sid": "GlobalEndpointsUsedByEks",
            "Effect": "Allow",
            "Action": [
                "shield:CreateProtection",
                "shield:DeleteProtection",
                "shield:DescribeProtection",
                "shield:GetSubscriptionState",
                "shield:TagResource",
                "signin:CreateBearerToken",
            ],
            "Resource": "*",
            "Condition": {"Bool": {"aws:SecureTransport": "true"}},
        },
        # EKS and load balancing create service-linked roles on first use.
        {
            "Sid": "ServiceLinkedRoles",
            "Effect": "Allow",
            "Action": "iam:CreateServiceLinkedRole",
            "Resource": scope.iam("role/aws-service-role/*"),
            "Condition": {
                "StringEquals": {
                    "iam:AWSServiceName": [
                        "eks.amazonaws.com",
                        "elasticloadbalancing.amazonaws.com",
                        "autoscaling.amazonaws.com",
                        "spot.amazonaws.com",
                    ]
                }
            },
        },
        # Auto Mode adds the node role to its instance profiles and passes it to EC2. The
        # spoke role also manages roles under /aif/ (see platform_clusters below).
        {
            "Sid": "AifRolesAndInstanceProfiles",
            "Effect": "Allow",
            "Action": [
                "iam:AddRoleToInstanceProfile",
                "iam:AttachRolePolicy",
                "iam:CreateRole",
                "iam:DeleteRole",
                "iam:DeleteRolePolicy",
                "iam:DetachRolePolicy",
                "iam:GetRole",
                "iam:ListAttachedRolePolicies",
                "iam:ListRolePolicies",
                "iam:PassRole",
                "iam:PutRolePermissionsBoundary",
                "iam:PutRolePolicy",
                "iam:TagRole",
                "iam:UntagRole",
                "iam:UpdateAssumeRolePolicy",
            ],
            "Resource": [scope.iam("instance-profile/eks*"), aif_roles],
        },
    )


def ack_capability(scope: Scope) -> Doc:
    """The ACK capability uses IAM role selectors: it only assumes roles under /aif/ack/."""
    return document(
        {
            "Sid": "AssumeAckSelectorRoles",
            "Effect": "Allow",
            "Action": ["sts:AssumeRole", "sts:TagSession"],
            "Resource": scope.iam(f"role{ACK_ROLE_PATH}*"),
        }
    )


def argocd_capability(scope: Scope) -> Doc:
    """Argo CD reads Git repository credentials stored under aif/argocd/."""
    return document(
        {
            "Sid": "ReadArgoCdRepositorySecrets",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
            "Resource": scope.arn("secretsmanager", "secret:aif/argocd/*"),
        }
    )


def platform_clusters(scope: Scope, key_arn: str) -> Doc:
    """What ACK needs to build spoke clusters from the PlatformCluster RGD."""
    aif_roles = scope.iam(f"role{IAM_PATH}*")
    return document(
        # Spoke clusters, their access entries and capabilities. Only names aif-*, never the Hub.
        {
            "Sid": "SpokeClusters",
            "Effect": "Allow",
            "Action": [
                "eks:AssociateAccessPolicy",
                "eks:CreateAccessEntry",
                "eks:CreateCapability",
                "eks:CreateCluster",
                "eks:DeleteAccessEntry",
                "eks:DeleteCapability",
                "eks:DeleteCluster",
                "eks:DescribeAccessEntry",
                "eks:DescribeCapability",
                "eks:DescribeCluster",
                "eks:DescribeUpdate",
                "eks:DisassociateAccessPolicy",
                "eks:ListAccessEntries",
                "eks:ListAssociatedAccessPolicies",
                "eks:ListTagsForResource",
                "eks:TagResource",
                "eks:UntagResource",
                "eks:UpdateAccessEntry",
                "eks:UpdateClusterConfig",
                "eks:UpdateClusterVersion",
            ],
            "Resource": [
                scope.arn("eks", "cluster/aif-*"),
                scope.arn("eks", "access-entry/aif-*/*"),
                scope.arn("eks", "capability/aif-*/*"),
                f"arn:{scope.partition}:eks::aws:cluster-access-policy/*",
            ],
        },
        # Never touch the Hub, which aif owns.
        {
            "Sid": "NotTheHub",
            "Effect": "Deny",
            "Action": "eks:*",
            "Resource": [
                scope.arn("eks", f"cluster/{HUB_CLUSTER}"),
                scope.arn("eks", f"access-entry/{HUB_CLUSTER}/*"),
                scope.arn("eks", f"capability/{HUB_CLUSTER}/*"),
            ],
        },
        # Spoke roles: only under /aif/ and only with aif-infra-boundary attached.
        {
            "Sid": "SpokeRolesWithBoundary",
            "Effect": "Allow",
            "Action": [
                "iam:AttachRolePolicy",
                "iam:CreateRole",
                "iam:DetachRolePolicy",
                "iam:PutRolePermissionsBoundary",
                "iam:PutRolePolicy",
                "iam:DeleteRolePolicy",
            ],
            "Resource": aif_roles,
            "Condition": {"StringEquals": {"iam:PermissionsBoundary": scope.boundary_arn}},
        },
        # Reads, tags and deletes on /aif/ roles, which IAM cannot condition on the boundary.
        {
            "Sid": "SpokeRolesLifecycle",
            "Effect": "Allow",
            "Action": [
                "iam:DeleteRole",
                "iam:GetRole",
                "iam:GetRolePolicy",
                "iam:ListAttachedRolePolicies",
                "iam:ListInstanceProfilesForRole",
                "iam:ListRolePolicies",
                "iam:ListRoleTags",
                "iam:TagRole",
                "iam:UntagRole",
                "iam:UpdateAssumeRolePolicy",
            ],
            "Resource": aif_roles,
        },
        # Pass /aif/ roles only to the services that use them.
        {
            "Sid": "PassSpokeRoles",
            "Effect": "Allow",
            "Action": "iam:PassRole",
            "Resource": aif_roles,
            "Condition": {
                "StringEquals": {
                    "iam:PassedToService": [
                        "eks.amazonaws.com",
                        "ec2.amazonaws.com",
                        "capabilities.eks.amazonaws.com",
                    ]
                }
            },
        },
        # Spokes encrypt secrets with the shared aif key; EKS needs a grant from the caller.
        {
            "Sid": "UseSharedKey",
            "Effect": "Allow",
            "Action": ["kms:CreateGrant", "kms:DescribeKey"],
            "Resource": key_arn,
        },
        # EKS validates subnets and security groups at CreateCluster. EC2 Describe has no
        # resource ARN, so it is pinned to the Region.
        {
            "Sid": "DescribeNetwork",
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeSecurityGroups",
                "ec2:DescribeSubnets",
                "ec2:DescribeVpcs",
            ],
            "Resource": "*",
            "Condition": in_region(scope),
        },
    )


def bucket_tls_only(bucket_arn: str) -> Doc:
    """Deny every request to the model bucket that does not use TLS."""
    return document(
        {
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [bucket_arn, f"{bucket_arn}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }
    )


def flow_logs(log_group_arn: str) -> Doc:
    """VPC Flow Logs writes the dev stand-in flow log group, and nothing else."""
    return document(
        {
            "Sid": "WriteFlowLogGroup",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogStream",
                "logs:DescribeLogStreams",
                "logs:PutLogEvents",
            ],
            "Resource": [log_group_arn, f"{log_group_arn}:*"],
        }
    )
