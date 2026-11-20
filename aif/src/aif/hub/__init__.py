"""Bring up the Hub with boto3: IAM, KMS, the EKS Auto Mode cluster, capabilities, storage.

Every step is create-or-update, so running it again is safe. Inputs come only from
SSM parameters under `/aif/` (see `inputs`). Dev and Prod are not created here: the
Hub builds them from the kro RGD `PlatformCluster` (decision D13).
"""

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aif.hub.api import BotoDevNetwork, BotoHub


def make_hub(region: str, tags: Mapping[str, str]) -> "BotoHub":
    """Factory that `aif.hub_api.load_hub` finds. Imported late to avoid import cycles."""
    from aif.hub import api

    return api.make_hub(region, tags)


def make_dev_network(region: str, tags: Mapping[str, str]) -> "BotoDevNetwork":
    from aif.hub import api

    return api.make_dev_network(region, tags)
