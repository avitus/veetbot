"""Cockpit runtime assets from their files, and the JSON Schema check (docs/plan/cockpit.md)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from agent_core.domain.cockpit import is_bundle_digest
from agent_core.ports.cockpit import CockpitAsset, CockpitRuntimeManifest

_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef"})
_CONTENT_TYPES = {
    "js": "text/javascript; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "html": "text/html; charset=utf-8",
}


class FileCockpitRuntime:
    """The runtime a release exported to `artifact_root/cockpit-runtime/`."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def manifest(self) -> CockpitRuntimeManifest | None:
        try:
            document = json.loads((self._directory / "manifest.json").read_text(encoding="utf-8"))
            return CockpitRuntimeManifest(
                kit_api=int(document["kit_api"]),
                entry=str(document["entry"]).split(".", 1)[0],
                assets={
                    str(name): str(asset["sha256"]) for name, asset in document["assets"].items()
                },
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def asset(self, sha256: str) -> CockpitAsset | None:
        # A digest is the only name served, so nothing outside the directory resolves.
        if not is_bundle_digest(sha256):
            return None
        for extension, content_type in _CONTENT_TYPES.items():
            path = self._directory / f"{sha256}.{extension}"
            if path.is_file() and not path.is_symlink():
                return CockpitAsset(path.read_bytes(), content_type)
        return None


def _outside_references(node: object) -> bool:
    """Whether a schema names anything but a location inside itself."""

    if isinstance(node, dict):
        for key, value in node.items():
            if key in _REFERENCE_KEYWORDS and not (
                isinstance(value, str) and value.startswith("#")
            ):
                return True
            if _outside_references(value):
                return True
    elif isinstance(node, list):
        return any(_outside_references(item) for item in node)
    return False


class Draft202012SchemaCheck:
    """JSON Schema draft 2020-12, confined to the schema itself: nothing is ever retrieved."""

    def schema_violations(self, schema: Mapping[str, Any]) -> list[str]:
        if _outside_references(dict(schema)):
            return ["a schema refers only to locations inside itself"]
        return sorted(
            error.message
            for error in Draft202012Validator(Draft202012Validator.META_SCHEMA).iter_errors(
                dict(schema)
            )
        )

    def violations(self, schema: Mapping[str, Any], instance: object) -> list[str]:
        problems = self.schema_violations(schema)
        if problems:
            return [f"the schema is invalid: {problems[0]}"]
        return sorted(
            error.message for error in Draft202012Validator(dict(schema)).iter_errors(instance)
        )
