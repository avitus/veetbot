"""Deployment settings and validation for the composition boundary."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import SecretStr


class ConfigurationError(ValueError):
    """Raised when deployment configuration is incomplete or unsafe."""


class DeploymentMode(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class AuthMode(StrEnum):
    DEV = "dev"
    TOKEN = "token"


class SandboxMechanism(StrEnum):
    MICROVM = "microvm"
    GVISOR = "gvisor"
    DOCKER = "docker"
    FAKE = "fake"


@dataclass(frozen=True, slots=True)
class Settings:
    """Environment-layer settings; tuning values remain in versioned YAML."""

    database_url: str
    deployment_mode: DeploymentMode
    auth_mode: AuthMode
    auth_token: SecretStr | None
    sandbox: SandboxMechanism
    config_dir: Path | None
    credentials: Mapping[str, SecretStr]
    interpolation: Mapping[str, str]


PACKAGE_ROOT = Path(__file__).resolve().parent
SHIPPED_CONFIGS = (
    "policy/hardline.yaml",
    "policy/default.yaml",
    "models/policies.yaml",
    "models/catalog.yaml",
    "context/plan.yaml",
    "tools/limits.yaml",
    "runtime/limits.yaml",
    "memory/profiles.yaml",
)
FROZEN_CONFIG = "policy/hardline.yaml"
INTERPOLATION = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def _environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    if environ is not None:
        return dict(environ)
    from_file = {key: value or "" for key, value in dotenv_values(".env").items()}
    from_file.update(os.environ)
    return from_file


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise ConfigurationError(f"required environment variable {name} is missing")
    return value


def _parse_enum[T: StrEnum](enum_type: type[T], value: str, name: str) -> T:
    try:
        return enum_type(value)
    except ValueError as exc:
        choices = ", ".join(member.value for member in enum_type)
        raise ConfigurationError(f"{name} must be one of: {choices}") from exc


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded: object = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot load configuration file {path}") from exc
    if not isinstance(loaded, dict):
        raise ConfigurationError(f"configuration file {path} must contain a mapping")
    return {str(key): value for key, value in loaded.items()}


def _validate_documents(config_dir: Path | None, interpolation: Mapping[str, str]) -> None:
    overlay_files: dict[str, Path] = {}
    if config_dir is not None:
        if not config_dir.is_dir():
            raise ConfigurationError(f"AGENT_CONFIG_DIR is not a directory: {config_dir}")
        for path in config_dir.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(config_dir).as_posix()
            if relative not in SHIPPED_CONFIGS:
                raise ConfigurationError(f"overlay has no shipped counterpart: {relative}")
            if relative == FROZEN_CONFIG:
                raise ConfigurationError("policy/hardline.yaml cannot be overlaid")
            overlay_files[relative] = path

    for relative in SHIPPED_CONFIGS:
        shipped = _read_yaml(PACKAGE_ROOT / relative)
        overlay_path = overlay_files.get(relative)
        merged = shipped
        if overlay_path is not None:
            merged = {**shipped, **_read_yaml(overlay_path)}
        serialized = yaml.safe_dump(merged, sort_keys=True)
        missing = sorted(set(INTERPOLATION.findall(serialized)) - interpolation.keys())
        if missing:
            names = ", ".join(missing)
            raise ConfigurationError(f"{relative} references unavailable interpolation: {names}")


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    """Load and validate the environment layer before constructing resources."""

    values = _environment(environ)
    database_url = _required(values, "DATABASE_URL")
    deployment_mode = _parse_enum(
        DeploymentMode, _required(values, "DEPLOYMENT_MODE"), "DEPLOYMENT_MODE"
    )
    auth_mode = _parse_enum(AuthMode, values.get("AUTH_MODE", "dev"), "AUTH_MODE")
    sandbox = _parse_enum(
        SandboxMechanism, values.get("SANDBOX_MECHANISM", "docker"), "SANDBOX_MECHANISM"
    )
    raw_token = values.get("AUTH_TOKEN", "").strip()
    auth_token = SecretStr(raw_token) if raw_token else None

    if auth_mode is AuthMode.TOKEN and auth_token is None:
        raise ConfigurationError("AUTH_TOKEN is required when AUTH_MODE=token")
    if deployment_mode is DeploymentMode.PRODUCTION and auth_mode is AuthMode.DEV:
        raise ConfigurationError("production refuses AUTH_MODE=dev")
    if deployment_mode is DeploymentMode.PRODUCTION and sandbox in {
        SandboxMechanism.DOCKER,
        SandboxMechanism.FAKE,
    }:
        raise ConfigurationError("production refuses docker and fake sandbox mechanisms")

    raw_dir = values.get("AGENT_CONFIG_DIR", "").strip()
    config_dir = Path(raw_dir).expanduser().resolve() if raw_dir else None
    credentials = {
        name.removesuffix("_API_KEY").lower(): SecretStr(value)
        for name, value in values.items()
        if name.endswith("_API_KEY") and value.strip()
    }
    interpolation = {"OPENAI_MODEL": values.get("OPENAI_MODEL", "")}
    _validate_documents(config_dir, interpolation)

    return Settings(
        database_url=database_url,
        deployment_mode=deployment_mode,
        auth_mode=auth_mode,
        auth_token=auth_token,
        sandbox=sandbox,
        config_dir=config_dir,
        credentials=MappingProxyType(credentials),
        interpolation=MappingProxyType(interpolation),
    )
