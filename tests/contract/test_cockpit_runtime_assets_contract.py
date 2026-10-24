"""Cockpit runtime assets contract: the exported manifest and assets by digest.

docs/plan/cockpit.md, the runtime. `FileCockpitRuntime` reads what a release
exports to `artifact_root/cockpit-runtime/`; nothing outside that directory,
and nothing that is not a content digest, is ever served.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from agent_core.adapters.cockpit.runtime import FileCockpitRuntime


def _export(directory: Path, name: str, content: bytes) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    (directory / f"{digest}.{name.rsplit('.', 1)[1]}").write_bytes(content)
    return digest


def test_nothing_is_exported_until_a_manifest_is(tmp_path: Path) -> None:
    runtime = FileCockpitRuntime(tmp_path / "cockpit-runtime")
    assert runtime.manifest() is None
    assert runtime.asset("0" * 64) is None


def test_the_manifest_names_the_entry_and_each_asset_by_digest(tmp_path: Path) -> None:
    directory = tmp_path / "cockpit-runtime"
    host = _export(directory, "host.html", b"<!doctype html>")
    realm = _export(directory, "realm.js", b"(()=>{})();")
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "kit_api": 1,
                "entry": f"{host}.html",
                "assets": {
                    "host.html": {"name": f"{host}.html", "sha256": host},
                    "realm.js": {"name": f"{realm}.js", "sha256": realm},
                },
            }
        ),
        encoding="utf-8",
    )
    runtime = FileCockpitRuntime(directory)
    manifest = runtime.manifest()
    assert manifest is not None
    assert (manifest.kit_api, manifest.entry) == (1, host)
    assert dict(manifest.assets) == {"host.html": host, "realm.js": realm}
    served = runtime.asset(realm)
    assert served is not None
    assert served.content == b"(()=>{})();"
    assert served.content_type.startswith("text/javascript")


def test_a_damaged_manifest_is_no_runtime(tmp_path: Path) -> None:
    directory = tmp_path / "cockpit-runtime"
    directory.mkdir()
    for damaged in ("{", json.dumps({"kit_api": "one"}), json.dumps([])):
        (directory / "manifest.json").write_text(damaged, encoding="utf-8")
        assert FileCockpitRuntime(directory).manifest() is None


def test_only_a_digest_inside_the_directory_is_ever_served(tmp_path: Path) -> None:
    directory = tmp_path / "cockpit-runtime"
    digest = _export(directory, "realm.js", b"x")
    (tmp_path / "secret.js").write_bytes(b"outside")
    (directory / f"{'1' * 64}.js").symlink_to(tmp_path / "secret.js")
    runtime = FileCockpitRuntime(directory)
    assert runtime.asset(digest) is not None
    for refused in ("1" * 64, "../secret", digest.upper(), digest[:-1], "manifest"):
        assert runtime.asset(refused) is None, refused
