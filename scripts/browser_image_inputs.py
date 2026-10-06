"""What decides the hosted browser's own requests, for the CI image check (ADR-0152).

The `browser-image` job keys its record of a passed measurement on this
output and halts when it finds one. The inputs are the image, its production
limits, the Playwright version, which fixes the Chromium build, the runtime's
vendor-request launch switches, startup/context configuration, and the check's
own code. Unrelated runtime operations and locked packages are not inputs.

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
CONFIGURATION_METHODS = ("start", "_context_options")


def _digest(data: bytes) -> str:
    """Return the stable SHA-256 identity of one measurement input."""
    return hashlib.sha256(data).hexdigest()


def _locked_version(lock: Path, name: str) -> str:
    """Read the named package version without importing project dependencies."""
    packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    return str(next(package["version"] for package in packages if package["name"] == name))


def _launch_switches(runtime: Path) -> str:
    """Hash the required vendor-request assignments, failing on missing inputs."""
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


def _runtime_configuration(runtime: Path) -> str:
    """Hash startup and context settings, ignoring prose and unrelated operations."""
    tree = ast.parse(runtime.read_text(encoding="utf-8"))
    methods = {
        method.name: method
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "PythonPlaywrightRuntime"
        for method in node.body
        if isinstance(method, ast.FunctionDef | ast.AsyncFunctionDef)
        and method.name in CONFIGURATION_METHODS
    }
    missing = set(CONFIGURATION_METHODS) - methods.keys()
    if missing:
        raise LookupError(f"{RUNTIME} no longer defines configuration methods {sorted(missing)}")
    normalized = []
    for name in CONFIGURATION_METHODS:
        method = methods[name]
        if ast.get_docstring(method) is not None:
            method.body = method.body[1:]
        normalized.append(ast.dump(method, include_attributes=False))
    return _digest("\n".join(normalized).encode())


def browser_image_inputs(root: Path = ROOT) -> dict[str, str]:
    """Each input of the browser image check, named, with its digest or version."""
    inputs = {name: _digest((root / name).read_bytes()) for name in FILES}
    inputs["playwright"] = _locked_version(root / "uv.lock", "playwright")
    inputs["launch switches"] = _launch_switches(root / RUNTIME)
    inputs["runtime configuration"] = _runtime_configuration(root / RUNTIME)
    return inputs


def main() -> None:
    """Print sorted measurement inputs for the architecture-specific CI cache."""
    inputs = browser_image_inputs()
    for name in sorted(inputs):
        print(f"{name} {inputs[name]}")


if __name__ == "__main__":
    main()
