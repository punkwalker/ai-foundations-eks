"""Bring up the Hub with boto3: IAM, KMS, the EKS Auto Mode cluster, capabilities, storage.

Every step is create-or-update, so running it again is safe. Inputs come only from
SSM parameters under `/aif/` (see `inputs`). Dev and Prod are not created here: the
Hub builds them from the kro RGD `PlatformCluster` (decision D13).
"""
