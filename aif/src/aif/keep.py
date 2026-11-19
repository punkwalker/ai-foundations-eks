"""Stacks that `aif down` must never delete.

By default aif keeps:

1. The launch stack. Its name is in the SSM parameter `/aif/launch-stack-name`.
2. Any stack tagged `aif:keep=true`, and every resource that carries that tag.

Extra stack names come from the `keep_stacks` setting.
"""

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

from aif.config import Settings

if TYPE_CHECKING:
    from mypy_boto3_ssm import SSMClient

KEEP_TAG = "aif:keep"
KEEP_VALUE = "true"


class KeepError(RuntimeError):
    """aif cannot tell which stacks to keep, so it must not delete any."""


def launch_stack(ssm: "SSMClient", parameter: str) -> str:
    try:
        value = ssm.get_parameter(Name=parameter)["Parameter"].get("Value", "")
    except ClientError as err:
        code = err.response.get("Error", {}).get("Code", "")
        raise KeepError(
            f"cannot read SSM parameter {parameter} ({code}), so the launch stack is unknown"
        ) from err
    if not value.strip():
        raise KeepError(f"SSM parameter {parameter} is empty, so the launch stack is unknown")
    return value.strip()


def keep_stacks(ssm: "SSMClient", settings: Settings) -> tuple[str, ...]:
    """Names of the stacks to keep. Raises KeepError if the launch stack is unknown."""
    names = [launch_stack(ssm, settings.launch_stack_parameter), *settings.keep_stacks]
    return tuple(dict.fromkeys(names))


def has_keep_tag(tags: Mapping[str, str]) -> bool:
    return tags.get(KEEP_TAG, "").lower() == KEEP_VALUE


def stack_name_from_arn(arn: str) -> str | None:
    """`arn:aws:cloudformation:r:a:stack/NAME/id` gives NAME."""
    parts = arn.split(":", 5)
    if len(parts) == 6 and parts[2] == "cloudformation" and parts[5].startswith("stack/"):
        return parts[5].split("/")[1]
    return None


def is_kept(arn: str, tags: Mapping[str, str], kept: Iterable[str]) -> bool:
    """True if a resource is a kept stack, belongs to one, or has the keep tag."""
    names = set(kept)
    if has_keep_tag(tags):
        return True
    if tags.get("aws:cloudformation:stack-name") in names:
        return True
    return stack_name_from_arn(arn) in names
