"""Cockpit runtime assets from their files, and the JSON Schema check (docs/plan/cockpit.md)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

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


def _unresolved_references(node: object, resolver: Any) -> list[str]:
    """The internal references that name nothing in the schema, in document order."""

    missing: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("$id"), str):
            resolver = resolver.in_subresource(DRAFT202012.create_resource(node))
        for key, value in node.items():
            if key in _REFERENCE_KEYWORDS and isinstance(value, str):
                try:
                    resolver.lookup(value)
                except Unresolvable:
                    missing.append(value)
            missing.extend(_unresolved_references(value, resolver))
    elif isinstance(node, list):
        for item in node:
            missing.extend(_unresolved_references(item, resolver))
    return missing


class Draft202012SchemaCheck:
    """JSON Schema draft 2020-12, confined to the schema itself: nothing is ever retrieved."""

    def schema_violations(self, schema: Mapping[str, Any]) -> list[str]:
        if _outside_references(dict(schema)):
            return ["a schema refers only to locations inside itself"]
        problems = sorted(
            error.message
            for error in Draft202012Validator(Draft202012Validator.META_SCHEMA).iter_errors(
                dict(schema)
            )
        )
        if problems:
            return problems
        resolver = Registry().resolver_with_root(DRAFT202012.create_resource(dict(schema)))
        return [
            f"the reference {reference} names nothing in the schema"
            for reference in _unresolved_references(dict(schema), resolver)
        ]

    def violations(self, schema: Mapping[str, Any], instance: object) -> list[str]:
        problems = self.schema_violations(schema)
        if problems:
            return [f"the schema is invalid: {problems[0]}"]
        try:
            return sorted(
                error.message for error in Draft202012Validator(dict(schema)).iter_errors(instance)
            )
        except Unresolvable as error:
            # A reference the schema check could not follow statically still fails as a
            # violation, never as an exception out of a publish or a settings write.
            return [f"the schema is invalid: {error}"]
