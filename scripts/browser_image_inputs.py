"""What decides the hosted browser's own requests, for the CI image check (ADR-0152).

The `browser-image` job keys its record of a passed measurement on this
output and halts when it finds one. The inputs are the image, its production
limits, the Playwright version, which fixes the Chromium build, the runtime's
vendor-request launch switches, and the check's own code. The rest of the
runtime and of the lock file change often and decide none of these.

CI runs this module with the machine executor's Python before installing the
project, so it imports only the standard library.
"""

from __future__ import annotations

import ast
import hashlib
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "deploy/browser-profile-service.Dockerfile",
    "deploy/docker-compose.production.yml",
    "scripts/browser_image_inputs.py",
    "tests/browser_image_probe.py",
    "tests/real_browser_support.py",
    "tests/security/test_browser_image_traffic.py",
)
RUNTIME = "src/agent_core/adapters/browser/playwright.py"
LAUNCH_SWITCHES = (
    "_VENDOR_REQUEST_FEATURES",
    "_PLAYWRIGHT_DISABLED_FEATURES",
    "_VENDOR_REQUEST_ARGUMENTS",
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _locked_version(lock: Path, name: str) -> str:
    packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    return str(next(package["version"] for package in packages if package["name"] == name))


def _launch_switches(runtime: Path) -> str:
    source = runtime.read_text(encoding="utf-8")
    assignments = {
        target.id: ast.get_source_segment(source, node)
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    missing = [name for name in LAUNCH_SWITCHES if name not in assignments]
    if missing:
        raise LookupError(f"{RUNTIME} no longer assigns {', '.join(missing)}")
    return _digest("\n".join(str(assignments[name]) for name in LAUNCH_SWITCHES).encode())


def browser_image_inputs(root: Path = ROOT) -> dict[str, str]:
    """Each input of the browser image check, named, with its digest or version."""
    inputs = {name: _digest((root / name).read_bytes()) for name in FILES}
    inputs["playwright"] = _locked_version(root / "uv.lock", "playwright")
    inputs["launch switches"] = _launch_switches(root / RUNTIME)
    return inputs


def main() -> None:
    inputs = browser_image_inputs()
    for name in sorted(inputs):
        print(f"{name} {inputs[name]}")


if __name__ == "__main__":
    main()
