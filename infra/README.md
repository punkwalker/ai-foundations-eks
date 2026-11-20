# Infra

This folder is empty on purpose. The infra repository has no CDK app (decision D13).

- The launch template in the content repository creates the IDE and its VPC.
- `aif hub up` creates the Hub with boto3: IAM roles, the KMS key, the EKS Auto Mode cluster, the ACK, kro and Argo CD capabilities, the model bucket, ECR repositories and secrets.
- The Hub builds Dev and Prod from the kro RGD in `platform/rgds/platform-cluster/`, with instances in `platform/clusters/`.

Do not depend on internal-only packages here. This repository must stay ready to become public. The launch template built from the internal workshop CDK constructs lives in the content repository.
