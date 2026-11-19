"""Typed settings for aif.

Settings come from three places. Later ones win:

1. The defaults in `Settings`.
2. An optional TOML file, `aif.toml` in the repository root, or the path in `AIF_CONFIG`.
3. Environment variables that start with `AIF_`.
"""

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast

ArgoCdMode = Literal["capability", "helm"]
ARGOCD_MODES: tuple[ArgoCdMode, ...] = ("capability", "helm")

GROUPS: tuple[str, ...] = ("base", "a", "b", "c", "d", "e", "capstone")
"""Stage groups, in bring-up order."""

DEFAULT_TAGS: Mapping[str, str] = {"project": "ai-foundations-eks", "owner": "punkwalker"}
DEFAULT_CONFIG_NAME = "aif.toml"


class ConfigError(ValueError):
    """A setting is missing or has a bad value."""


@dataclass(frozen=True)
class KubeContexts:
    """kubeconfig context names for the three clusters."""

    hub: str = "hub"
    dev: str = "dev"
    prod: str = "prod"

    def as_dict(self) -> dict[str, str]:
        return {"hub": self.hub, "dev": self.dev, "prod": self.prod}


@dataclass(frozen=True)
class Settings:
    """Every setting aif reads. Defaults match the plan."""

    region: str = "us-west-2"
    project_tags: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_TAGS))
    groups: tuple[str, ...] = GROUPS
    argocd_mode: ArgoCdMode = "capability"
    argocd_namespace: str = "argocd"
    hub_cluster: str = "hub"
    contexts: KubeContexts = field(default_factory=KubeContexts)
    keep_stacks: tuple[str, ...] = ()
    """CloudFormation stacks that `aif down` must never delete, such as the launch stack."""

    @property
    def project(self) -> str:
        return self.project_tags["project"]


def _str(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string, got {value!r}")
    return value


def _str_list(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        raise ConfigError(f"{name} must be a list of strings, got {value!r}")
    return tuple(_str(item, name) for item in value)


def _groups(value: Any) -> tuple[str, ...]:
    groups = _str_list(value, "groups")
    unknown = [g for g in groups if g not in GROUPS]
    if unknown:
        raise ConfigError(f"unknown groups {unknown}; known groups are {list(GROUPS)}")
    if "base" not in groups:
        groups = ("base", *groups)
    return tuple(g for g in GROUPS if g in groups)


def _argocd_mode(value: Any) -> ArgoCdMode:
    if value not in ARGOCD_MODES:
        raise ConfigError(f"argocd_mode must be one of {list(ARGOCD_MODES)}, got {value!r}")
    return cast(ArgoCdMode, value)


def _tags(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ConfigError(f"project_tags must be a table of strings, got {value!r}")
    tags = {_str(k, "project_tags key"): _str(v, f"project_tags.{k}") for k, v in value.items()}
    if "project" not in tags:
        raise ConfigError("project_tags must include a 'project' tag")
    return tags


def _contexts(value: Any, current: KubeContexts) -> KubeContexts:
    if not isinstance(value, dict):
        raise ConfigError(f"contexts must be a table, got {value!r}")
    unknown = set(value) - {"hub", "dev", "prod"}
    if unknown:
        raise ConfigError(f"unknown contexts {sorted(unknown)}; use hub, dev, prod")
    named = {k: _str(v, f"contexts.{k}") for k, v in value.items()}
    return replace(current, **named)


def apply_values(settings: Settings, values: Mapping[str, Any]) -> Settings:
    """Return `settings` with `values` applied. Rejects unknown keys."""
    known = {
        "region",
        "project_tags",
        "groups",
        "argocd_mode",
        "argocd_namespace",
        "hub_cluster",
        "contexts",
        "keep_stacks",
    }
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"unknown settings {sorted(unknown)}")
    changes: dict[str, Any] = {}
    for key, value in values.items():
        if key == "project_tags":
            changes[key] = _tags(value)
        elif key == "groups":
            changes[key] = _groups(value)
        elif key == "argocd_mode":
            changes[key] = _argocd_mode(value)
        elif key == "contexts":
            changes[key] = _contexts(value, settings.contexts)
        elif key == "keep_stacks":
            changes[key] = _str_list(value, key)
        else:
            changes[key] = _str(value, key)
    return replace(settings, **changes)


def values_from_env(env: Mapping[str, str]) -> dict[str, Any]:
    """Read `AIF_*` variables. Lists are comma separated."""
    values: dict[str, Any] = {}
    simple = {
        "AIF_REGION": "region",
        "AIF_GROUPS": "groups",
        "AIF_ARGOCD_MODE": "argocd_mode",
        "AIF_ARGOCD_NAMESPACE": "argocd_namespace",
        "AIF_HUB_CLUSTER": "hub_cluster",
        "AIF_KEEP_STACKS": "keep_stacks",
    }
    for var, key in simple.items():
        if var in env:
            values[key] = env[var]
    contexts = {
        name: env[f"AIF_{name.upper()}_CONTEXT"]
        for name in ("hub", "dev", "prod")
        if f"AIF_{name.upper()}_CONTEXT" in env
    }
    if contexts:
        values["contexts"] = contexts
    return values


def find_repo_root(start: Path) -> Path:
    """Walk up from `start` to the folder that holds `Taskfile.yaml` and `stages/`."""
    for folder in (start, *start.parents):
        if (folder / "Taskfile.yaml").is_file() and (folder / "stages").is_dir():
            return folder
    raise ConfigError(f"no repository root (Taskfile.yaml and stages/) above {start}")


def load(
    repo_root: Path, env: Mapping[str, str] | None = None, path: Path | None = None
) -> Settings:
    """Load settings from the defaults, the TOML file, then the environment."""
    env = os.environ if env is None else env
    if path is None:
        path = Path(env["AIF_CONFIG"]) if "AIF_CONFIG" in env else repo_root / DEFAULT_CONFIG_NAME
    settings = Settings()
    if path.is_file():
        with path.open("rb") as handle:
            try:
                settings = apply_values(settings, tomllib.load(handle))
            except tomllib.TOMLDecodeError as err:
                raise ConfigError(f"{path}: {err}") from err
    elif "AIF_CONFIG" in env:
        raise ConfigError(f"AIF_CONFIG points to {path}, which does not exist")
    return apply_values(settings, values_from_env(env))
