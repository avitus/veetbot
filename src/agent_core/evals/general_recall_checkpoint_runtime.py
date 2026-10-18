"""Bind separately versioned collection to unchanged measured runtime inputs.

This module can run beside the frozen runtime without replacing its source files.
It grants neither answer-quality certification nor permission to activate recall.
"""

import hashlib
import io
import json
import os
import re
import stat
import tarfile
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import AwareDatetime, Field
from sqlalchemy.engine import make_url

from agent_core.domain.general_recall_release import (
    Digest,
    RecallPreregistration,
    RecallReleaseIdentity,
    ReleaseValue,
)
from agent_core.evals.general_recall_checkpoint import CheckpointStore, _digest, _read, _write
from agent_core.evals.general_recall_confirmation import ConfirmedExecution, RecallExecutionManifest
from agent_core.evals.general_recall_confirmatory import ConfirmationOptions, PreparedConfirmation
from agent_core.evals.general_recall_database import confirmation_databases
from agent_core.memory.general_recall_evidence import _unique_object


class VerifiedParentArchive:
    """Verify every archived original against a retained directory; never extract."""

    def __init__(self, directory: Path, archive: Path, expected_sha256: str) -> None:
        self.directory = directory
        descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            info = os.fstat(source.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 256 * 1024 * 1024:
                raise ValueError("Invalid parent archive")
            raw = source.read(info.st_size + 1)
        if len(raw) != info.st_size or _digest(raw) != expected_sha256:
            raise ValueError("Parent archive changed")
        self.sha256 = expected_sha256
        self.members: dict[str, str] = {}
        seen: set[str] = set()
        total = 0
        archive_root: str | None = None
        try:
            with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as retained:
                for entry in retained:
                    path = PurePosixPath(entry.name)
                    if (
                        path.is_absolute()
                        or not path.parts
                        or ".." in path.parts
                        or str(path) != entry.name
                        or entry.name in seen
                        or not (entry.isfile() or entry.isdir())
                        or len(seen) >= 20000
                    ):
                        raise ValueError("Unsafe or duplicate parent archive member")
                    seen.add(entry.name)
                    if archive_root is None:
                        archive_root = path.parts[0]
                    if path.parts[0] != archive_root:
                        raise ValueError("Parent archive has multiple roots")
                    if entry.isdir():
                        continue
                    total += entry.size
                    if (
                        len(path.parts) < 2
                        or not 0 < entry.size <= 16 * 1024 * 1024
                        or total > 1024**3
                    ):
                        raise ValueError("Invalid parent archive member size")
                    content = retained.extractfile(entry)
                    if content is None:
                        raise ValueError("Missing parent archive member")
                    data = content.read(entry.size + 1)
                    if len(data) != entry.size:
                        raise ValueError("Truncated parent archive member")
                    name = path.relative_to(archive_root).as_posix()
                    self.members[name] = _digest(data)
        except tarfile.TarError as error:
            raise ValueError("Invalid parent archive") from error
        if not self.members:
            raise ValueError("Empty parent archive")
        self.verify()

    def _path(self, name: str) -> Path:
        if name not in self.members:
            raise ValueError("Artifact is not in the original parent inventory")
        current = self.directory
        if current.is_symlink() or not current.is_dir():
            raise ValueError("Unsafe parent directory")
        for part in PurePosixPath(name).parts[:-1]:
            current /= part
            if current.is_symlink() or not current.is_dir():
                raise ValueError("Unsafe parent artifact directory")
        return self.directory / name

    def read(self, name: str) -> bytes:
        try:
            raw = _read(self._path(name))
        except OSError as error:
            raise ValueError("Missing or unsafe parent artifact") from error
        if _digest(raw) != self.members[name]:
            raise ValueError("Parent artifact changed")
        return raw

    def verify(self) -> None:
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError("Unsafe parent directory")
        actual: set[str] = set()
        for base, directories, files in os.walk(self.directory, followlinks=False):
            for name in (*directories, *files):
                path = Path(base) / name
                if path.is_symlink():
                    raise ValueError("Parent inventory contains a symlink")
            actual.update(
                (Path(base) / name).relative_to(self.directory).as_posix() for name in files
            )
        if actual != set(self.members):
            raise ValueError("Parent inventory changed or omitted an original")
        for name in self.members:
            self.read(name)


def bind_frozen_preflight(
    *,
    parent_identity: dict[str, Any],
    parent_manifest: dict[str, Any],
    parent_preregistration: dict[str, Any],
    parent_inputs: dict[str, Any],
    parent_options: dict[str, Any],
    prepared: PreparedConfirmation,
    options: ConfirmationOptions,
    root: Path,
    forbidden_databases: tuple[str, ...],
) -> PreparedConfirmation:
    """Keep the original measurement identity after exact frozen-runtime preflight.

    Only fresh capture timestamps/digests and operational output/database names may
    differ. The old manifest allowance remains provenance, not a new allocation:
    ContinuationBudget separately enforces the smaller reconciled allowance.
    """
    from agent_core.memory.recall_release_binding import capture_inputs

    identity = RecallReleaseIdentity.model_validate(parent_identity)
    manifest = RecallExecutionManifest.model_validate(parent_manifest)
    preregistration = RecallPreregistration.model_validate(parent_preregistration)
    original = ConfirmedExecution(identity, manifest)
    original.validate(manifest.variants[0].configuration)
    prepared.confirmation.validate(prepared.confirmation.manifest.variants[0].configuration)
    metadata = {"preregistration_sha256", "execution_manifest_sha256", "evaluated_inputs_sha256"}
    if (
        _digest(preregistration.model_dump_json().encode()) != identity.preregistration_sha256
        or identity.model_dump(exclude=metadata)
        != prepared.confirmation.identity.model_dump(exclude=metadata)
        or manifest.model_dump(exclude={"captured_at"})
        != prepared.confirmation.manifest.model_dump(exclude={"captured_at"})
        or preregistration.model_dump(exclude={"registered_at"})
        != prepared.preregistration.model_dump(exclude={"registered_at"})
        or {k: v for k, v in parent_inputs.items() if k != "evaluated_inputs"}
        != {k: v for k, v in prepared.snapshots.items() if k != "evaluated_inputs"}
        or capture_inputs(root, identity.model_dump(mode="json")).digest
        != identity.evaluated_inputs_sha256
    ):
        raise ValueError("Frozen measurement inputs or runtime changed")
    operational = {"output", "parallel_scratch_database"}
    previous_options = ConfirmationOptions.model_validate(parent_options)
    if (
        previous_options.model_dump(exclude=operational) != options.model_dump(exclude=operational)
        or options.output == previous_options.output
        or manifest.worker_count != 2
        or len(forbidden_databases) < 2
        or len(set(forbidden_databases)) != len(forbidden_databases)
    ):
        raise ValueError("Continuation options or original worker identity changed")
    workers = confirmation_databases(
        prepared.settings,
        additional=options.parallel_scratch_database,
        authorized=options.disposable_database,
    )
    names = tuple(make_url(worker.database_url).database for worker in workers)
    if (
        len(workers) != 2
        or prepared.worker_settings != workers
        or len(set(names)) != 2
        or any(name in forbidden_databases for name in names)
    ):
        raise ValueError("Continuation requires two new guarded scratch databases")
    return replace(
        prepared, confirmation=original, preregistration=preregistration, snapshots=parent_inputs
    )


class _Deadline(ReleaseValue):
    version: Literal["recall-continuation-deadline@1"]
    plan_sha256: Digest
    protocol_sha256: Digest
    started_at: AwareDatetime
    expires_at: AwareDatetime
    duration_seconds: int = Field(ge=1, le=604800, strict=True)


class ContinuationDeadline:
    """One immutable wall-clock expiry, with monotonic enforcement within a process."""

    def __init__(
        self,
        store: CheckpointStore,
        *,
        protocol_sha256: str,
        duration_seconds: int = 604800,
        create: bool = False,
        now: Callable[[], datetime],
        monotonic: Callable[[], float],
    ) -> None:
        store._owned()
        if re.fullmatch(r"[0-9a-f]{64}", protocol_sha256) is None:
            raise ValueError("Invalid collector protocol identity")
        self._now, self._monotonic = now, monotonic
        self._wall, self._tick = now(), monotonic()
        path = store.directory / "deadline.json"
        plan_sha256 = hashlib.sha256(store.plan.encoded()).hexdigest()
        if create:
            value = _Deadline(
                version="recall-continuation-deadline@1",
                plan_sha256=plan_sha256,
                protocol_sha256=protocol_sha256,
                started_at=self._wall,
                expires_at=self._wall + timedelta(seconds=duration_seconds),
                duration_seconds=duration_seconds,
            )
            _write(path, value.model_dump_json().encode() + b"\n")
        from agent_core.evals.general_recall_checkpoint_recovery import protocol_ancestry

        predecessor, _ = protocol_ancestry(store, protocol_sha256)
        self.value = _Deadline.model_validate(
            json.loads(_read(path), object_pairs_hook=_unique_object)
        )
        if (
            self._wall.tzinfo is None
            or self.value.plan_sha256 != plan_sha256
            or self.value.protocol_sha256 != predecessor
            or self.value.duration_seconds != duration_seconds
            or self.value.expires_at != self.value.started_at + timedelta(seconds=duration_seconds)
            or self._wall < self.value.started_at
        ):
            raise ValueError("Continuation deadline, protocol or clock changed")
        self.remaining()

    def remaining(self) -> float:
        elapsed = self._monotonic() - self._tick
        if elapsed < 0:
            raise ValueError("Monotonic clock moved backwards")
        current = max(self._now(), self._wall + timedelta(seconds=elapsed))
        remaining = (self.value.expires_at - current).total_seconds()
        if remaining <= 0:
            raise TimeoutError("The absolute continuation deadline expired")
        return remaining


class WorkerAdmission(ReleaseValue):
    version: Literal["recall-checkpoint-worker-admission@1"]
    plan_sha256: Digest
    protocol_sha256: Digest
    budget_admission_sha256: Digest
    databases: tuple[str, str]
    forbidden_databases: tuple[str, ...]
    options_sha256: Digest
    effective_inputs_sha256: Digest


def verify_worker_admissions(
    store: CheckpointStore, *, protocol_sha256: str, forbidden_databases: tuple[str, ...]
) -> tuple[str, ...]:
    """Require one immutable, fresh database pair for every provider segment."""
    store._owned()
    directory = store.directory / "operator-segments"
    journals = sorted((store.directory / "budget").glob("*.jsonl"))
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValueError("Unsafe worker admission directory")
    if journals and not directory.exists():
        raise ValueError("Missing worker admissions for existing provider segments")
    if directory.exists() and {p.name for p in directory.iterdir()} != {
        p.with_suffix(".json").name for p in journals
    }:
        raise ValueError("Incomplete or extra worker admission inventory")
    from agent_core.evals.general_recall_checkpoint_recovery import protocol_ancestry

    predecessor, first_successor = protocol_ancestry(store, protocol_sha256)
    used: tuple[str, ...] = ()
    for index, journal in enumerate(journals, 1):
        value = WorkerAdmission.model_validate_json(
            _read(directory / journal.with_suffix(".json").name)
        )
        if (
            journal.name != f"{index:06d}.jsonl"
            or value.plan_sha256 != _digest(store.plan.encoded())
            or value.protocol_sha256
            != (predecessor if index < first_successor else protocol_sha256)
            or value.budget_admission_sha256
            != _digest(_read(journal.with_suffix(".admission.json")))
            or value.forbidden_databases != forbidden_databases + used
            or len(set(value.databases)) != 2
            or any(
                re.fullmatch(r"veetbot_m33_eval_[a-z0-9_]{1,32}", name) is None
                or name in value.forbidden_databases
                for name in value.databases
            )
        ):
            raise ValueError("Changed worker admission or reused scratch database")
        used += value.databases
    return used
