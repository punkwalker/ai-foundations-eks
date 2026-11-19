"""Account-specific Kubernetes objects for the Hub, built from the `aif hub up` outputs.

These hold ARNs and subnet IDs, so they never go into Git. The seed step applies them
on the Hub before Argo CD syncs `platform/clusters/`:

1. Namespace `platform-clusters`, where PlatformCluster instances live.
2. ConfigMap `aif-platform`, read by the PlatformCluster RGD as an externalRef.
3. IAMRoleSelector, so ACK uses aif-ack-platform-clusters for that namespace only.
"""

from collections.abc import Mapping
from typing import Any

from aif.hub.inputs import HubInputs
from aif.hub.names import PLATFORM_CLUSTERS_NAMESPACE, Param

LABELS = {"app.kubernetes.io/part-of": "ai-foundations-eks"}


def manifests(outputs: Mapping[str, str], found: HubInputs) -> list[dict[str, Any]]:
    """The three objects, in apply order. Raises KeyError if an output is missing."""
    ns = PLATFORM_CLUSTERS_NAMESPACE
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": LABELS}},
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "aif-platform", "namespace": ns, "labels": LABELS},
            "data": {
                "boundaryARN": outputs[Param.BOUNDARY_ARN],
                "keyARN": outputs[Param.KEY_ARN],
                "privateSubnetIDs": ",".join(found.private_subnets),
                "adminRoleARN": found.admin_role_arn,
                "ideRoleARN": found.ide_role_arn,
                "argocdRoleARN": outputs.get(Param.cluster("hub", "argocd-role-arn"), ""),
            },
        },
        {
            "apiVersion": "services.k8s.aws/v1alpha1",
            "kind": "IAMRoleSelector",
            "metadata": {"name": "platform-clusters", "labels": LABELS},
            "spec": {
                "arn": outputs[Param.cluster("hub", "platform-clusters-role-arn")],
                "namespaceSelector": {"names": [ns]},
            },
        },
    ]
