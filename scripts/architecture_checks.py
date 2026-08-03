"""Static architecture, migration, transaction, and secret checks."""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class Finding:
    """A safe static-check finding that never contains matched secret text."""

    path: str
    line: int
    rule: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.rule}"


SECRET_RULES: dict[str, re.Pattern[str]] = {
    "provider_key": re.compile(r"\b(?:sk-ant-|sk-)[A-Za-z0-9_-]{12,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "bearer_literal": re.compile(r"Authorization\s*:\s*Bearer\s+[^\s<>{}\[\]]+", re.IGNORECASE),
    "dsn_password": re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/]+:[^\s@/]+@", re.IGNORECASE),
    "assigned_secret": re.compile(
        r"(?i)\b(?:[A-Z0-9_]*(?:secret|token|password|api_?key)[A-Z0-9_]*)\s*=\s*"
        r"[\"'][^\"'\n]{13,}[\"']"
    ),
}
PROVIDER_SDK_ROOTS = frozenset({"anthropic", "openai"})
MODULE_SCOPE_RESOURCE_FACTORIES = frozenset(
    {
        "AsyncSession",
        "Session",
        "async_sessionmaker",
        "create_async_engine",
        "create_engine",
        "sessionmaker",
    }
)


def _candidate_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    candidates: list[Path] = []
    for relative in result.stdout.splitlines():
        path = root / relative
        if not path.is_file():
            continue
        if relative == ".env.example" or relative.startswith(
            ("src/", "tests/", "evals/", "migrations/", "docs/")
        ):
            candidates.append(path)
    return sorted(candidates)


def _secret_allowlist(root: Path) -> tuple[set[tuple[str, int, str]], list[str]]:
    path = root / "security" / "secret-allowlist.yaml"
    loaded: object = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    rows = loaded.get("allow", []) if isinstance(loaded, dict) else []
    allowed: set[tuple[str, int, str]] = set()
    errors: list[str] = []
    if not isinstance(rows, list):
        return allowed, ["security/secret-allowlist.yaml allow must be a list"]
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            errors.append(f"secret allowlist entry {index} must be a mapping")
            continue
        reason = str(row.get("reason", "")).strip()
        if not reason:
            errors.append(f"secret allowlist entry {index} requires a prose reason")
            continue
        try:
            key = (str(row["path"]), int(row["line"]), str(row["rule"]))
        except (KeyError, TypeError, ValueError):
            errors.append(f"secret allowlist entry {index} requires path, line, and rule")
            continue
        allowed.add(key)
    return allowed, errors


def secret_findings(root: Path) -> tuple[list[Finding], list[str]]:
    """Scan committed text and return findings without ever returning matches."""

    allowlist, errors = _secret_allowlist(root)
    findings: list[Finding] = []
    observed_allowlist: set[tuple[str, int, str]] = set()
    for path in _candidate_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, 1):
            for rule, pattern in SECRET_RULES.items():
                if pattern.search(line) is None:
                    continue
                key = (relative, line_number, rule)
                if key in allowlist:
                    observed_allowlist.add(key)
                else:
                    findings.append(Finding(relative, line_number, rule))
    for stale in sorted(allowlist - observed_allowlist):
        errors.append(f"stale secret allowlist entry: {stale[0]}:{stale[1]}:{stale[2]}")
    return findings, errors


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root / "src").with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(module: str, path: Path, tree: ast.AST) -> set[str]:
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                keep = max(0, len(parts) - node.level + 1)
                prefix = ".".join(parts[:keep])
                target = ".".join(part for part in (prefix, node.module or "") if part)
                if node.module is None:
                    found.update(
                        ".".join(part for part in (prefix, alias.name) if part)
                        for alias in node.names
                    )
            else:
                target = node.module or ""
            if target:
                found.add(target)
    return found


def _import_bindings(module: str, path: Path, tree: ast.AST) -> dict[str, str]:
    """Map each imported local name to the module-qualified name it denotes."""

    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    bindings: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".", 1)[0]
                bindings[local] = alias.name if alias.asname else local
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".") if package else []
                keep = max(0, len(parts) - node.level + 1)
                prefix = ".".join(parts[:keep])
                target = ".".join(part for part in (prefix, node.module or "") if part)
            else:
                target = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                bindings[alias.asname or alias.name] = ".".join(
                    part for part in (target, alias.name) if part
                )
    return bindings


def _resolve_name(name: str, bindings: dict[str, str]) -> str:
    first, separator, remainder = name.partition(".")
    resolved = bindings.get(first, first)
    return f"{resolved}.{remainder}" if separator else resolved


def _annotation_names(annotation: ast.AST | None) -> set[str]:
    if annotation is None:
        return set()
    names: set[str] = set()
    for node in ast.walk(annotation):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            name = _call_name(node)
            if name:
                names.add(name)
    return names


def _function_annotations(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    annotations: list[ast.AST] = [
        argument.annotation
        for argument in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
        if argument.annotation is not None
    ]
    if node.args.vararg is not None and node.args.vararg.annotation is not None:
        annotations.append(node.args.vararg.annotation)
    if node.args.kwarg is not None and node.args.kwarg.annotation is not None:
        annotations.append(node.args.kwarg.annotation)
    if node.returns is not None:
        annotations.append(node.returns)
    return annotations


def architecture_errors(root: Path) -> list[str]:
    """Walk the import graph and the additional static architecture rules."""

    errors: list[str] = []
    modules: dict[str, tuple[Path, ast.Module, set[str]]] = {}
    for path in sorted((root / "src" / "agent_core").rglob("*.py")):
        module = _module_name(root, path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        modules[module] = (path, tree, _imports(module, path, tree))

    local_names = set(modules)

    def local_target(name: str) -> str | None:
        matches = [
            candidate
            for candidate in local_names
            if name == candidate or name.startswith(candidate + ".")
        ]
        return max(matches, key=len) if matches else None

    graph: dict[str, set[str]] = defaultdict(set)
    for module, (_, _, imports) in modules.items():
        for imported in imports:
            target = local_target(imported)
            if target is not None:
                graph[module].add(target)

    def reachable(start: str, *, stop_at: frozenset[str] = frozenset()) -> set[str]:
        seen: set[str] = set()
        todo = list(graph[start])
        while todo:
            item = todo.pop()
            if item in seen:
                continue
            seen.add(item)
            if item not in stop_at:
                todo.extend(graph[item] - seen)
        return seen

    for module, (path, tree, imports) in modules.items():
        relative = path.relative_to(root)
        bindings = _import_bindings(module, path, tree)
        imported_roots = {imported.split(".", 1)[0] for imported in imports}
        if module.startswith("agent_core.domain"):
            for imported in imports:
                top = imported.split(".")[0]
                if top not in sys.stdlib_module_names | {"pydantic", "agent_core"}:
                    errors.append(f"{relative}: domain imports forbidden dependency {imported}")
                if imported.startswith("agent_core.") and not imported.startswith(
                    "agent_core.domain"
                ):
                    errors.append(f"{relative}: domain imports {imported}")
        if module.startswith("agent_core.ports"):
            for imported in imports:
                top = imported.split(".", 1)[0]
                if top not in sys.stdlib_module_names | {"agent_core"}:
                    errors.append(f"{relative}: ports imports forbidden dependency {imported}")
            for target in reachable(module):
                if not target.startswith(("agent_core.domain", "agent_core.ports")):
                    errors.append(f"{relative}: ports reaches {target}")
            protocol_classes = [
                declaration
                for declaration in tree.body
                if isinstance(declaration, ast.ClassDef)
                and any(_call_name(base).endswith("Protocol") for base in declaration.bases)
            ]
            for protocol in protocol_classes:
                for member in protocol.body:
                    annotations: list[ast.AST] = []
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        annotations.extend(_function_annotations(member))
                    elif isinstance(member, ast.AnnAssign):
                        annotations.append(member.annotation)
                    for annotation in annotations:
                        for name in _annotation_names(annotation):
                            resolved = _resolve_name(name, bindings)
                            if resolved.startswith("agent_core.") and not resolved.startswith(
                                ("agent_core.domain", "agent_core.ports")
                            ):
                                errors.append(
                                    f"{relative}:{member.lineno}: port signature exposes {resolved}"
                                )
        if module.startswith(("agent_core.runtime", "agent_core.application")):
            for imported in imports:
                top = imported.split(".", 1)[0]
                if top not in sys.stdlib_module_names | {"agent_core"}:
                    errors.append(
                        f"{relative}: runtime/application imports forbidden dependency {imported}"
                    )
            for target in reachable(module):
                if not target.startswith(
                    (
                        "agent_core.domain",
                        "agent_core.ports",
                        module.split(".", 2)[0] + "." + module.split(".", 2)[1],
                    )
                ):
                    errors.append(f"{relative}: runtime/application reaches {target}")
            if module.startswith("agent_core.application"):
                reached_modules = {module, *reachable(module)}
                if any(
                    imported.split(".", 1)[0] == "fastapi"
                    for reached in reached_modules
                    for imported in modules[reached][2]
                ):
                    errors.append(f"{relative}: application reaches FastAPI")
        if module.startswith(("agent_core.api", "agent_core.cli")):
            surface_targets = reachable(module, stop_at=frozenset({"agent_core.bootstrap"}))
            for target in surface_targets:
                if target.startswith(
                    ("agent_core.adapters", "agent_core.ports.repositories", "agent_core.runtime")
                ):
                    errors.append(f"{relative}: entry point reaches forbidden module {target}")
        if module.startswith("agent_core.tools"):
            for target in reachable(module):
                if target.startswith("agent_core.models"):
                    errors.append(f"{relative}: tool reaches model gateway {target}")
        if module.startswith("agent_core.models"):
            for target in reachable(module):
                if target.startswith("agent_core.tools"):
                    errors.append(f"{relative}: model gateway reaches tool {target}")
        if module.startswith("agent_core.policy"):
            for target in reachable(module):
                if target.startswith(("agent_core.context", "agent_core.models")):
                    errors.append(f"{relative}: policy reaches model or prompt module {target}")
        if module != "agent_core.bootstrap" and "agent_core.bootstrap" in imports:
            allowed = {"agent_core.api.main", "agent_core.cli.main", "agent_core.runtime.worker"}
            if module not in allowed:
                errors.append(f"{relative}: bootstrap imported outside an entry point")
        if not module.startswith("agent_core.evals"):
            for target in reachable(module):
                if target.startswith("agent_core.evals"):
                    errors.append(f"{relative}: production module reaches agent_core.evals")

        if not module.startswith("agent_core.adapters.models"):
            for provider_sdk in sorted(imported_roots & PROVIDER_SDK_ROOTS):
                errors.append(f"{relative}: provider SDK {provider_sdk} crosses adapter boundary")

        if module != "agent_core.bootstrap":
            persistence_module = module == "agent_core.adapters.persistence" or module.startswith(
                "agent_core.adapters.persistence."
            )
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                called = _resolve_name(_call_name(node.func), bindings)
                if called.startswith("agent_core.adapters."):
                    persistence_target = (
                        called == "agent_core.adapters.persistence"
                        or called.startswith("agent_core.adapters.persistence.")
                    )
                    if persistence_module and persistence_target:
                        continue
                    errors.append(
                        f"{relative}:{node.lineno}: adapter constructed outside bootstrap"
                    )

        if module != "agent_core.adapters.determinism":
            forbidden_calls = {
                "datetime.datetime.now",
                "datetime.datetime.utcnow",
                "datetime.now",
                "datetime.utcnow",
                "time.monotonic",
                "time.time",
                "uuid.uuid1",
                "uuid.uuid4",
            }
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = _resolve_name(_call_name(node.func), bindings)
                if name in forbidden_calls or name == "random" or name.startswith("random."):
                    errors.append(f"{relative}:{node.lineno}: ambient nondeterminism call {name}")

        for node in tree.body:
            value: ast.AST | None = None
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
            if value is not None:
                for child in ast.walk(value):
                    if not isinstance(child, ast.Call):
                        continue
                    called = _resolve_name(_call_name(child.func), bindings)
                    if called.rsplit(".", 1)[-1] in MODULE_SCOPE_RESOURCE_FACTORIES:
                        errors.append(
                            f"{relative}:{node.lineno}: module-scope database resource {called}"
                        )

        if not module.startswith("agent_core.adapters.persistence"):
            for node in ast.walk(tree):
                annotations = (
                    _function_annotations(node)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    else []
                )
                for annotation in annotations:
                    for name in _annotation_names(annotation):
                        resolved = _resolve_name(name, bindings)
                        if resolved.startswith(
                            "agent_core.adapters.persistence.sqlalchemy_models."
                        ):
                            errors.append(
                                f"{relative}:{getattr(node, 'lineno', 0)}: "
                                "ORM type crosses adapter signature"
                            )

    dependency_text = (root / "pyproject.toml").read_text(encoding="utf-8").lower()
    for denied in ("dependency-injector", "injector", "punq", "lagom"):
        if re.search(rf"[\"']{re.escape(denied)}(?:[<>=~!\"'])", dependency_text):
            errors.append(
                f"pyproject.toml includes forbidden dependency-injection package {denied}"
            )
    return sorted(set(errors))


def _call_name(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def transaction_hygiene_errors(root: Path) -> list[str]:
    """Reject external awaits lexically nested inside transaction contexts."""

    errors: list[str] = []
    for path in sorted((root / "src" / "agent_core").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module = _module_name(root, path)
        bindings = _import_bindings(module, path, tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.With, ast.AsyncWith)):
                continue
            names = [
                _resolve_name(
                    _call_name(item.context_expr.func)
                    if isinstance(item.context_expr, ast.Call)
                    else _call_name(item.context_expr),
                    bindings,
                )
                for item in node.items
            ]
            if not any(
                "transaction" in name.lower()
                or name.endswith(".begin")
                or name.rsplit(".", 1)[-1].lower() in {"session", "unit_of_work", "uow"}
                for name in names
            ):
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Await):
                    continue
                call = child.value
                called = (
                    _resolve_name(_call_name(call.func), bindings)
                    if isinstance(call, ast.Call)
                    else ""
                )
                words = set(re.split(r"[._]", called.lower()))
                repository_call = bool(words & {"repo", "repository", "store"})
                external_call = bool(
                    words
                    & {
                        "client",
                        "environment",
                        "execution",
                        "http",
                        "mcp",
                        "model",
                        "provider",
                        "sandbox",
                        "tool",
                    }
                )
                external_call = external_call or called.startswith(
                    (
                        "agent_core.adapters.execution.",
                        "agent_core.adapters.mcp.",
                        "agent_core.adapters.models.",
                    )
                )
                if external_call and not repository_call:
                    errors.append(
                        f"{path.relative_to(root)}:{child.lineno}: "
                        f"external I/O awaited in transaction via {called}"
                    )
    return errors


def contract_coverage_errors(root: Path) -> list[str]:
    """Require one contract module for every Protocol declared by a port module."""

    errors: list[str] = []
    ports_root = root / "src" / "agent_core" / "ports"
    if not ports_root.exists():
        return errors
    for path in sorted(ports_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            if not any(_call_name(base).endswith("Protocol") for base in node.bases):
                continue
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", node.name).lower()
            contract = root / "tests" / "contract" / f"test_{snake}_contract.py"
            if not contract.is_file():
                errors.append(
                    f"{path.relative_to(root)}:{node.lineno}: {node.name} has no "
                    f"{contract.relative_to(root)}"
                )
    return errors


def migration_graph_errors(root: Path) -> list[str]:
    """Assert a linear Alembic graph and a matching EXPECTED_REVISION."""

    revisions: dict[str, str | None] = {}
    errors: list[str] = []
    for path in sorted((root / "migrations" / "versions").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values: dict[str, Any] = {}
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                    if value is None:
                        errors.append(
                            f"{path.relative_to(root)}:{node.lineno}: {target.id} has no value"
                        )
                        continue
                    try:
                        values[target.id] = ast.literal_eval(value)
                    except (ValueError, TypeError):
                        errors.append(
                            f"{path.relative_to(root)}:{node.lineno}: {target.id} must be literal"
                        )
        revision = values.get("revision")
        parent = values.get("down_revision")
        if not isinstance(revision, str):
            errors.append(f"{path.relative_to(root)}: missing string revision")
            continue
        if isinstance(parent, tuple):
            errors.append(f"{path.relative_to(root)}: merge revisions are forbidden")
            continue
        if parent is not None and not isinstance(parent, str):
            errors.append(f"{path.relative_to(root)}: down_revision must be a string or None")
            continue
        if revision in revisions:
            errors.append(f"duplicate Alembic revision {revision}")
        revisions[revision] = parent
    if not revisions:
        return ["migrations/versions contains no revisions"]
    for revision, parent in revisions.items():
        if parent is not None and parent not in revisions:
            errors.append(f"revision {revision} has missing parent {parent}")
    parents = {parent for parent in revisions.values() if parent is not None}
    heads = set(revisions) - parents
    if len(heads) != 1:
        errors.append(f"migration graph has {len(heads)} heads: {', '.join(sorted(heads))}")
    revision_path = root / "src" / "agent_core" / "adapters" / "persistence" / "revision.py"
    revision_tree = ast.parse(
        revision_path.read_text(encoding="utf-8"), filename=str(revision_path)
    )
    expected: str | None = None
    for node in revision_tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "EXPECTED_REVISION"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            expected = value if isinstance(value, str) else None
    if len(heads) == 1 and expected != next(iter(heads)):
        errors.append(
            f"EXPECTED_REVISION {expected!r} does not match migration head {next(iter(heads))!r}"
        )
    return errors
