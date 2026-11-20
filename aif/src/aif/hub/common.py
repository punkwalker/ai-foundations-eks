"""Helpers shared by the Hub modules: tags, error codes, step results."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from botocore.exceptions import ClientError

from aif.hub_api import HubError as HubApiError

Tags = Mapping[str, str]


HubError = HubApiError
"""A Hub step cannot go on. The message says what to do."""


def code(err: ClientError) -> str:
    return str(err.response.get("Error", {}).get("Code", ""))


def tag_list(tags: Tags, key: str = "Key", value: str = "Value") -> list[Any]:
    """`{"a": "b"}` gives `[{"Key": "a", "Value": "b"}]`, the shape most AWS APIs take."""
    return [{key: k, value: v} for k, v in sorted(tags.items())]


def has_tags(found: Mapping[str, str], wanted: Tags) -> bool:
    return all(found.get(k) == v for k, v in wanted.items() if k == "project")


@dataclass
class Report:
    """What a run did, one line per resource. Printed by the CLI."""

    lines: list[str] = field(default_factory=list)

    def created(self, what: str) -> None:
        self.lines.append(f"created  {what}")

    def updated(self, what: str) -> None:
        self.lines.append(f"updated  {what}")

    def kept(self, what: str) -> None:
        self.lines.append(f"exists   {what}")

    def deleted(self, what: str) -> None:
        self.lines.append(f"deleted  {what}")


Log = Callable[[str], None]
