"""Explicit one-segment entry point; never retry, self-resume or activate recall.

Use the frozen runtime's Python interpreter. Protocol files are immutable across
segments; each separately approved segment pins fresh workers and creation flags.
"""

import argparse
import asyncio
import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

MODULES = (
    "general_recall_checkpoint",
    "general_recall_checkpoint_runtime",
    "general_recall_continuation_budget",
    "general_recall_checkpoint_parent",
    "general_recall_continuation_collector",
    "general_recall_checkpoint_recovery",
    "general_recall_formation_repair",
    "general_recall_checkpoint_runner",
)


def _read(path: Path) -> bytes:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("An operator input has an unsafe path")
    with path.open("rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 16 * 1024 * 1024:
            raise ValueError("Invalid operator input")
        raw = source.read(info.st_size + 1)
    if len(raw) != info.st_size:
        raise ValueError("Operator input changed during reading")
    return raw


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate operator field")
        result[key] = value
    return result


def _load(path: Path, digest: str) -> dict[str, Any]:
    raw = _read(path)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Operator input digest changed")
    value = json.loads(raw, object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise ValueError("Operator input must be an object")
    return value


def _tree(root: Path, commit: str) -> None:
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None or root.resolve() != root:
        raise ValueError("Invalid committed source root")

    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL)

    if git("rev-parse", "HEAD").decode().strip() != commit or git("status", "--porcelain"):
        raise ValueError("The source root is not the clean admitted commit")


def verify_invocation(
    protocol_path: Path,
    protocol_sha256: str,
    segment_path: Path,
    segment_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify immutable protocol and separately admitted segment before credentials."""
    protocol = _load(protocol_path, protocol_sha256)
    segment = _load(segment_path, segment_sha256)
    if (
        protocol["version"]
        not in {
            "recall-checkpoint-operator@1",
            "recall-checkpoint-operator@2",
            "recall-checkpoint-operator@3",
        }
        or segment["version"] != "recall-checkpoint-paid-admission@1"
        or segment["paid_launch_authorized"] is not True
        or segment["protocol_sha256"] != protocol_sha256
        or segment["plan_sha256"] != protocol["plan_sha256"]
        or any(type(segment[k]) is not bool for k in ("initialize_store", "initialize_deadline"))
    ):
        raise ValueError("Unadmitted checkpoint protocol or segment")
    if protocol["version"] == "recall-checkpoint-operator@2":
        verify_successor(protocol, segment)
    if protocol["version"] == "recall-checkpoint-operator@3":
        verify_formation_successor(protocol, segment)
    captured = datetime.fromisoformat(segment["checked_at"])
    if captured.tzinfo is None or not 0 <= (datetime.now(UTC) - captured).total_seconds() <= 3600:
        raise ValueError("Fresh configuration and budget admission are required")
    for prefix in ("collector", "frozen"):
        _tree(Path(protocol[f"{prefix}_root"]), protocol[f"{prefix}_commit"])
    files = protocol["files"]
    if not isinstance(files, dict) or str(Path(__file__).resolve()) not in files:
        raise ValueError("The invoked operator is not pinned")
    for name, digest in files.items():
        if hashlib.sha256(_read(Path(name))).hexdigest() != digest:
            raise ValueError("Frozen operator input changed")
    databases = segment["databases"]
    if (
        not isinstance(databases, list)
        or len(databases) != 2
        or len(set(databases)) != 2
        or any(re.fullmatch(r"veetbot_m33_eval_[a-z0-9_]{1,32}", v) is None for v in databases)
    ):
        raise ValueError("Exactly two distinct guarded scratch databases are required")
    directory = Path(protocol["state_directory"])
    output = Path(segment["output"])
    protected = [
        Path(protocol[p])
        for p in ("frozen_root", "collector_root", "parent_directory")
        if p in protocol
    ]
    if (
        not directory.is_absolute()
        or not output.is_absolute()
        or any(p.is_symlink() for p in (directory, output, *directory.parents, *output.parents))
        or any(directory.is_relative_to(p) or output.is_relative_to(p) for p in protected)
        or directory.exists() == segment["initialize_store"]
        or (directory.exists() and not directory.is_dir())
        or output.exists()
    ):
        raise ValueError("Unsafe or reused continuation state/output identity")
    deadline = directory / "deadline.json"
    if deadline.exists() == segment["initialize_deadline"]:
        raise ValueError("Explicit initialization cannot renew an existing absolute deadline")
    if segment["initialize_deadline"] and any((directory / "budget").glob("*.jsonl")):
        raise ValueError("A paid segment cannot establish a replacement deadline")
    return protocol, segment


def verify_successor(protocol: dict[str, Any], segment: dict[str, Any]) -> None:
    """Only collector identity and explicit recovery inputs may change."""
    predecessor = _load(
        Path(protocol["predecessor_protocol_file"]), protocol["predecessor_protocol_sha256"]
    )
    additions = {
        "predecessor_protocol_file",
        "predecessor_protocol_sha256",
        "recovery_manifest_file",
        "recovery_owner_file",
        "recovery_assessment_file",
    }
    changed = {"version", "files", "collector_root", "collector_commit"}
    if (
        predecessor["version"] != "recall-checkpoint-operator@1"
        or set(protocol) != set(predecessor) | additions
        or {k: v for k, v in protocol.items() if k not in additions | changed}
        != {k: v for k, v in predecessor.items() if k not in changed}
        or segment["initialize_store"]
        or segment["initialize_deadline"]
    ):
        raise ValueError("Recovery cannot change runtime, allocation, state or deadline")
    files = protocol["files"]
    for name, digest in predecessor["files"].items():
        # Retain all original operator inputs, even the old collector files.
        if files.get(name) != digest or hashlib.sha256(_read(Path(name))).hexdigest() != digest:
            raise ValueError("Original protocol input changed or was omitted")
    for name in additions - {"predecessor_protocol_sha256"}:
        if protocol[name] not in files:
            raise ValueError("Recovery authorization input is not pinned")
    if files[protocol["predecessor_protocol_file"]] != protocol["predecessor_protocol_sha256"]:
        raise ValueError("Original protocol is not pinned")
    manifest = _load(
        Path(protocol["recovery_manifest_file"]), files[protocol["recovery_manifest_file"]]
    )
    if (
        manifest["predecessor_protocol_sha256"] != protocol["predecessor_protocol_sha256"]
        or segment.get("recovery_manifest_sha256") != files[protocol["recovery_manifest_file"]]
    ):
        raise ValueError("Paid admission does not bind the exact bounded recovery")


def verify_formation_successor(protocol: dict[str, Any], segment: dict[str, Any]) -> None:
    """Admit only the owner-approved fixed source patch, keeping all prior inputs."""
    predecessor = _load(
        Path(protocol["repair_predecessor_file"]), protocol["repair_predecessor_sha256"]
    )
    additions = {
        "repair_predecessor_file",
        "repair_predecessor_sha256",
        "repair_manifest_file",
        "repair_owner_file",
        "repair_assessment_file",
        "repair_original_source",
        "repair_runtime_source",
    }
    changed = {"version", "files", "collector_root", "collector_commit"}
    if (
        predecessor["version"] != "recall-checkpoint-operator@2"
        or set(protocol) != set(predecessor) | additions
        or {k: v for k, v in protocol.items() if k not in additions | changed}
        != {k: v for k, v in predecessor.items() if k not in changed}
        or segment["initialize_store"]
        or segment["initialize_deadline"]
    ):
        raise ValueError(
            "Formation repair cannot change frozen inputs, allocation, state or deadline"
        )
    files = protocol["files"]
    if any(files.get(name) != digest for name, digest in predecessor["files"].items()):
        raise ValueError("Formation repair omitted or changed an original input")
    for key in additions - {"repair_predecessor_sha256"}:
        if protocol[key] not in files:
            raise ValueError("Formation repair input is not pinned")
    if files[protocol["repair_predecessor_file"]] != protocol["repair_predecessor_sha256"]:
        raise ValueError("Formation repair predecessor changed")
    manifest = _load(
        Path(protocol["repair_manifest_file"]), files[protocol["repair_manifest_file"]]
    )
    if (
        manifest["predecessor_protocol_sha256"] != protocol["repair_predecessor_sha256"]
        or segment.get("repair_manifest_sha256") != files[protocol["repair_manifest_file"]]
    ):
        raise ValueError("Formation repair admission mismatch")
    verify_formation_sources(protocol)


def verify_formation_sources(protocol: dict[str, Any]) -> None:
    # Stdlib-only duplicate of the evidence guard: verify BEFORE importing runtime.
    # A regression enforces these exact two source hashes in both implementations.
    for key, root_key, expected in (
        (
            "repair_original_source",
            "frozen_root",
            "e7ce0e0f0e93f2279126ba8a7d6f9e554222c22f2c9fd074836ed48c2bad6823",
        ),
        (
            "repair_runtime_source",
            "collector_root",
            "110141418041c582f635dcb25ae22bb3088d748e00227424060c4da1b3e6a1a7",
        ),
    ):
        path = Path(protocol[root_key]) / "src/agent_core/memory/distillation.py"
        if str(path) != protocol[key] or hashlib.sha256(_read(path)).hexdigest() != expected:
            raise ValueError("Unverified formation deadline repair source")


def _bootstrap(protocol: dict[str, Any]) -> None:
    if any(name == "agent_core" or name.startswith("agent_core.") for name in sys.modules):
        raise ValueError("Use a fresh interpreter with no previously imported runtime")
    root = Path(protocol["frozen_root"])
    if Path(sys.executable).parent != root / ".venv/bin":
        raise ValueError("Use the frozen runtime's interpreter")
    sys.path.insert(0, str(root / "src"))
    importlib.import_module("agent_core.evals")
    if protocol["version"] == "recall-checkpoint-operator@3":
        verify_formation_sources(protocol)
        name = "agent_core.memory.distillation"
        if name in sys.modules:
            raise ValueError("Formation was imported before its explicit repair")

        class RepairFinder(importlib.abc.MetaPathFinder):
            def find_spec(
                self,
                fullname: str,
                path: Sequence[str] | None,
                target: ModuleType | None = None,
            ) -> importlib.machinery.ModuleSpec | None:
                if fullname == name:
                    return importlib.util.spec_from_file_location(
                        fullname, Path(protocol["repair_runtime_source"])
                    )
                return None

        finder = RepairFinder()
        sys.meta_path.insert(0, finder)
        try:
            # memory.__init__ builds its extractor census during import; intercept
            # the first load so that it cannot retain an old class reference.
            importlib.import_module(name)
        finally:
            sys.meta_path.remove(finder)
    for short in MODULES:
        path = Path(protocol["collector_root"]) / f"src/agent_core/evals/{short}.py"
        if str(path) not in protocol["files"]:
            raise ValueError("An external collector module is not pinned")
        name = "agent_core.evals." + short
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ValueError("Cannot load the admitted collector module")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules[name] = loaded
        spec.loader.exec_module(loaded)


async def _run(
    protocol: dict[str, Any], segment: dict[str, Any], protocol_sha256: str
) -> dict[str, Any]:
    from dataclasses import replace
    from decimal import Decimal

    from pydantic import SecretStr, TypeAdapter
    from sqlalchemy import text
    from sqlalchemy.engine import make_url

    from agent_core.adapters.persistence.database import create_engine
    from agent_core.adapters.persistence.sqlalchemy_models import Base
    from agent_core.config import DeploymentMode, Settings
    from agent_core.evals.general_recall_checkpoint import (
        CheckpointStore,
        ContinuationPlan,
        _digest,
    )
    from agent_core.evals.general_recall_checkpoint_parent import CheckpointParent
    from agent_core.evals.general_recall_checkpoint_runner import (
        run_checkpoint_segment,
        verify_frozen_module,
    )
    from agent_core.evals.general_recall_checkpoint_runtime import VerifiedParentArchive
    from agent_core.evals.general_recall_confirmatory import ConfirmationOptions
    from agent_core.evals.general_recall_continuation_budget import ParentLedger
    from agent_core.evals.general_recall_database import confirmation_databases

    def pinned(name: str) -> bytes:
        path = protocol[name]
        raw = _read(Path(path))
        if _digest(raw) != protocol["files"][path]:
            raise ValueError("A required operator input is not frozen")
        return raw

    root = Path(protocol["frozen_root"])
    runtime_patch = (
        Path(protocol["repair_runtime_source"])
        if protocol["version"] == "recall-checkpoint-operator@3"
        else None
    )
    verify_frozen_module(root, runtime_patch)
    plan_data = json.loads(pinned("plan_file"), object_pairs_hook=_unique)
    plan_data.pop("version")
    plan_data["scenarios"] = tuple(plan_data["scenarios"])
    for name in ("allowance_usd", "prior_exposure_usd"):
        plan_data[name] = Decimal(plan_data[name])
    plan = ContinuationPlan(**plan_data)
    if _digest(plan.encoded()) != protocol["plan_sha256"]:
        raise ValueError("Continuation allocation or matrix changed")
    settings = TypeAdapter(Settings).validate_json(pinned("settings_file"))
    settings = replace(settings, memory_semantic_index_enabled=True)
    scratch = TypeAdapter(Settings).validate_json(pinned("scratch_settings_file"))
    scratch = replace(
        scratch,
        deployment_mode=DeploymentMode.DEVELOPMENT,
        database_url=make_url(scratch.database_url)
        .set(database=segment["databases"][0])
        .render_as_string(hide_password=False),
    )
    options = ConfirmationOptions.model_validate_json(pinned("options_file")).model_copy(
        update={
            "output": Path(segment["output"]),
            "parallel_scratch_database": segment["databases"][1],
        }
    )
    workers = confirmation_databases(
        scratch,
        additional=options.parallel_scratch_database,
        authorized=options.disposable_database,
    )
    for worker in workers:
        if make_url(worker.database_url).database in protocol["forbidden_databases"]:
            raise ValueError("A failed or previous scratch database cannot be reused")
        engine = create_engine(worker.database_url)
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
                for table in Base.metadata.sorted_tables:
                    if await connection.scalar(
                        text(f'SELECT EXISTS (SELECT 1 FROM "{table.name}" LIMIT 1)')
                    ):
                        raise ValueError("Fresh checkpoint worker databases must be empty")
        finally:
            await engine.dispose()
    archive = VerifiedParentArchive(
        Path(protocol["parent_directory"]),
        Path(protocol["parent_archive"]),
        protocol["parent_archive_sha256"],
    )
    parent = CheckpointParent(archive, settings)
    parents = tuple(
        ParentLedger(v["name"], v["sha256"], _read(Path(v["path"])), Decimal(v["maximum"]))
        for v in protocol["parent_ledgers"]
    )
    approval = (
        (
            pinned("preparation_approval_file"),
            protocol["files"][protocol["preparation_approval_file"]],
            pinned("preparation_diagnostic_file"),
        )
        if protocol.get("preparation_approval_file")
        and protocol["version"] == "recall-checkpoint-operator@1"
        else None
    )
    # Credentials are read only after offline identity and fresh-database checks.
    settings = replace(
        settings,
        credentials={**settings.credentials, "openai": SecretStr(os.environ["VEETBOT_OPENAI_KEY"])},
    )
    stamps = {
        name: (
            Path(name).stat().st_ino,
            Path(name).stat().st_size,
            Path(name).stat().st_mtime_ns,
            Path(name).stat().st_ctime_ns,
        )
        for name in protocol["files"]
    }

    def verify_protocol() -> None:
        for name, stamp in stamps.items():
            path = Path(name)
            info = path.stat()
            if (
                path.is_symlink()
                or (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != stamp
            ):
                raise ValueError("A pinned operator input changed during collection")

    with CheckpointStore(
        Path(protocol["state_directory"]), plan, create=segment["initialize_store"]
    ) as store:
        return await run_checkpoint_segment(
            root=root,
            store=store,
            parent=parent,
            parents=parents,
            options=options,
            settings=settings,
            scratch=scratch,
            protocol_sha256=protocol_sha256,
            verify_protocol=verify_protocol,
            forbidden_databases=tuple(protocol["forbidden_databases"]),
            create_deadline=segment["initialize_deadline"],
            preparation_approval=approval,
            runtime_patch=runtime_patch,
            formation_repair=(
                tuple(
                    pinned(name)
                    for name in (
                        "repair_manifest_file",
                        "repair_owner_file",
                        "repair_assessment_file",
                        "repair_original_source",
                        "repair_runtime_source",
                    )
                )
                if protocol["version"] == "recall-checkpoint-operator@3"
                else None
            ),
            bounded_recovery=(
                (
                    pinned("recovery_manifest_file"),
                    pinned("recovery_owner_file"),
                    pinned("recovery_assessment_file"),
                )
                if protocol["version"] == "recall-checkpoint-operator@2"
                else None
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--protocol-sha256", required=True)
    parser.add_argument("--segment", type=Path, required=True)
    parser.add_argument("--segment-sha256", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        protocol, segment = verify_invocation(
            args.protocol, args.protocol_sha256, args.segment, args.segment_sha256
        )
        if args.check:
            print(json.dumps({"input_guards_passed": True, "provider_calls": 0}))
            return
        if os.environ.get("RUN_LIVE_MODEL_TESTS") != "1":
            raise ValueError("Explicit live invocation is required")
        _bootstrap(protocol)
        result = asyncio.run(_run(protocol, segment, args.protocol_sha256))
        print(json.dumps({k: result[k] for k in ("state", "completed_slots", "required_slots")}))
        if result["state"] != "collected":
            raise SystemExit(1)
    except (Exception, KeyboardInterrupt, asyncio.CancelledError) as error:
        print(json.dumps({"state": "stopped", "error_class": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
