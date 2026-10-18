"""Immutable M33 continuation records; never grant provider or release authority.

The coordinator must still verify the frozen runtime, compile every imported row,
validate its formation bank and reconcile all provider ledgers before sending work.
This store only makes scheduling decisions durable; it cannot certify answer quality.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

from pydantic import Field

from agent_core.domain.general_recall_release import Digest, ReleaseValue
from agent_core.evals.general_recall_composition import system_clock
from agent_core.memory.general_recall_evidence import _unique_object
from agent_core.ports.determinism import Clock

Slot = tuple[str, str, str, str]
Validator = Callable[[dict[str, Any]], None]
_MAX_BYTES = 16 * 1024 * 1024


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MAX_BYTES:
            raise ValueError("Invalid checkpoint artifact")
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) != info.st_size:
        raise ValueError("Checkpoint artifact changed while reading")
    return raw


def _sync(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, raw: bytes) -> None:
    """Publish complete bytes without replacing an existing artifact."""
    descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ContinuationPlan:
    """Fixed measurement identity, matrix and maximum additional allocation."""

    identity_sha256: str
    evaluated_inputs_sha256: str
    parent_archive_sha256: str
    scenarios: tuple[str, ...]
    prior_exposure_usd: Decimal
    allowance_usd: Decimal

    def encoded(self) -> bytes:
        for value in (
            self.identity_sha256,
            self.evaluated_inputs_sha256,
            self.parent_archive_sha256,
        ):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("Invalid continuation plan identity")
        required_slots(self.scenarios)
        if (
            not self.prior_exposure_usd.is_finite()
            or not self.allowance_usd.is_finite()
            or self.prior_exposure_usd < 0
            or self.allowance_usd <= 0
            or self.prior_exposure_usd + self.allowance_usd > 300
        ):
            raise ValueError("Continuation allowance exceeds the aggregate cap")
        return _json(
            {
                **asdict(self),
                "version": "recall-checkpoints@1",
                "prior_exposure_usd": str(self.prior_exposure_usd),
                "allowance_usd": str(self.allowance_usd),
            }
        )


def required_slots(scenarios: tuple[str, ...]) -> tuple[Slot, ...]:
    """Select the fixed formation comparison without inspecting any answer."""
    if (
        len(scenarios) != 400
        or len(set(scenarios)) != 400
        or any(not value or len(value) > 256 for value in scenarios)
    ):
        raise ValueError("Continuation requires 400 unique reviewed scenarios")
    return tuple(
        (scenario, path, "formation", channel)
        for scenario in scenarios
        for path in ("automatic", "deliberate")
        for channel in ("sparse", "hybrid")
    )


class _Event(ReleaseValue):
    index: int = Field(gt=0)
    previous_sha256: Digest
    recorded_at: datetime
    key: Slot
    action: Literal[
        "started",
        "importing",
        "imported",
        "completed",
        "failed",
        "parent_failed",
        "replacement",
        "bounded_replacement",
    ]
    result_sha256: Digest | None = None
    stage: Literal["formation", "answer"] | None = None
    error_class: (
        Literal[
            "CancelledError",
            "TimeoutError",
            "ModelProtocolError",
            "BudgetExceededError",
            "ConnectionError",
            "RuntimeError",
            "ValueError",
            "OtherError",
        ]
        | None
    ) = None
    ledger_sha256: Digest | None = None
    source_sha256: Digest | None = None


class CheckpointStore:
    """One process owns immutable records, and unknown sends stay unresolved."""

    def __init__(
        self,
        directory: Path,
        plan: ContinuationPlan,
        *,
        create: bool = False,
        clock: Clock | None = None,
    ) -> None:
        encoded = plan.encoded()
        self.directory, self.plan = directory, plan
        self.clock = clock or system_clock()
        self._slots = required_slots(plan.scenarios)
        self._states: dict[Slot, _Event] = {}
        self._replaced: set[str] = set()
        self._bounded_replaced: set[Slot] = set()
        self._count = 0
        self._previous = _digest(encoded)
        self._lock: int | None = None
        if create:
            directory.mkdir(mode=0o700)
            for name in ("events", "results"):
                (directory / name).mkdir(mode=0o700)
            _write(directory / "plan.json", encoded)
            _sync(directory.parent)
        for path in (directory, directory / "events", directory / "results"):
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Checkpoint directory is missing or a symlink")
        try:
            self._lock = os.open(
                directory / ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
            )
            if not stat.S_ISREG(os.fstat(self._lock).st_mode):
                raise ValueError("Invalid coordinator lock")
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Checkpoint coordinator is already locked") from error
            if _read(directory / "plan.json") != encoded:
                raise ValueError("Continuation plan or evaluated inputs changed")
            for index, path in enumerate(sorted((directory / "events").iterdir()), 1):
                if path.name != f"{index:06d}.json":
                    raise ValueError("Checkpoint event is missing or incomplete")
                raw = _read(path)
                event = _Event.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
                self._validate_event(event)
                self._apply(event, raw)
            expected: set[Path] = set()
            for key, event in self._states.items():
                if event.action == "importing":
                    retained = self._retained(key)
                    if retained and retained != (self._result_path(key, str(event.result_sha256)),):
                        raise ValueError("Interrupted import changed")
                    expected.update(retained)
                elif event.result_sha256 is not None:
                    path = self._result_path(key, event.result_sha256)
                    if _digest(_read(path)) != event.result_sha256:
                        raise ValueError("Completed checkpoint result changed")
                    expected.add(path)
                elif event.action == "started":
                    # An orphan result is recoverable only through explicit validation.
                    expected.update(self._retained(key))
            if set((directory / "results").iterdir()) != expected:
                raise ValueError("Unexpected or incomplete checkpoint result")
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None

    def _owned(self) -> None:
        if self._lock is None:
            raise ValueError("Checkpoint coordinator is closed")

    def _result_path(self, key: Slot, digest: str) -> Path:
        return self.directory / "results" / f"{_digest(_json(key))}-{digest}.json"

    def _retained(self, key: Slot) -> tuple[Path, ...]:
        paths = tuple((self.directory / "results").glob(f"{_digest(_json(key))}-*.json"))
        if len(paths) > 1 or any(
            path != self._result_path(key, _digest(_read(path))) for path in paths
        ):
            raise ValueError("Conflicting or changed checkpoint result")
        return paths

    def _validate_event(self, event: _Event) -> None:
        self._owned()
        if (
            event.key not in self._slots
            or event.index != self._count + 1
            or event.previous_sha256 != self._previous
            or event.recorded_at.tzinfo is None
            or (event.action in {"importing", "imported", "completed"})
            != (event.result_sha256 is not None)
            or (event.action in {"failed", "parent_failed"}) != (event.stage is not None)
            or (event.action in {"failed", "parent_failed"}) != (event.error_class is not None)
            or (event.action in {"failed", "parent_failed"}) != (event.ledger_sha256 is not None)
            or (event.action in {"parent_failed", "bounded_replacement"})
            != (event.source_sha256 is not None)
        ):
            raise ValueError("Invalid checkpoint event or chain")
        previous = self._states.get(event.key)
        action = previous.action if previous else None
        if event.action == "started":
            if any(self._states[k].action == "failed" for k in self._bounded_replaced):
                raise ValueError("An approved bounded recovery failed; paid admissions must stop")
            if action not in {None, "replacement", "bounded_replacement"}:
                raise ValueError("Unresolved or unknown slot cannot resend")
            if any(
                key != event.key
                and key[0] == event.key[0]
                and value.action in {"started", "failed", "parent_failed"}
                for key, value in self._states.items()
            ):
                raise ValueError("Scenario has an unresolved preparation or answer")
        elif event.action == "bounded_replacement":
            from agent_core.evals.general_recall_checkpoint_recovery import validate_recovery_event

            validate_recovery_event(self, event.key, str(event.source_sha256))
        elif event.action == "importing":
            if previous is not None:
                raise ValueError("Duplicate imported checkpoint slot")
        elif event.action == "imported":
            if (
                previous is None
                or action != "importing"
                or previous.result_sha256 != event.result_sha256
            ):
                raise ValueError("Imported result differs from its durable intent")
        elif event.action in {"completed", "failed"}:
            if action != "started":
                raise ValueError("Only an admitted slot can finish")
        elif event.action == "parent_failed":
            if (
                any(key[0] == event.key[0] for key in self._states)
                or event.stage != "formation"
                or event.error_class not in {"CancelledError", "TimeoutError"}
                or (self.directory / "failures").is_symlink()
                or not (self.directory / "failures").is_dir()
                or _digest(_read(self.directory / "failures" / f"{event.source_sha256}.json"))
                != event.source_sha256
            ):
                raise ValueError("Original preparation failure is not eligible for import")
        elif (
            previous is None
            or action not in {"failed", "parent_failed"}
            or previous.stage != "formation"
            or previous.error_class not in {"CancelledError", "TimeoutError"}
            or event.key[0] in self._replaced
            or any(
                key != event.key
                and key[0] == event.key[0]
                and value.action
                in {"started", "failed", "parent_failed", "completed", "importing", "imported"}
                for key, value in self._states.items()
            )
        ):
            raise ValueError("Formation replacement is not eligible for a retry")

    def _apply(self, event: _Event, raw: bytes) -> None:
        self._states[event.key] = event
        if event.action == "replacement":
            self._replaced.add(event.key[0])
        if event.action == "bounded_replacement":
            self._bounded_replaced.add(event.key)
        self._count, self._previous = event.index, _digest(raw)

    def _record(self, action: str, key: Slot, **details: object) -> None:
        event = _Event.model_validate(
            {
                "index": self._count + 1,
                "previous_sha256": self._previous,
                "recorded_at": self.clock.now(),
                "key": key,
                "action": action,
                **details,
            }
        )
        self._validate_event(event)
        raw = event.model_dump_json().encode() + b"\n"
        _write(self.directory / "events" / f"{event.index:06d}.json", raw)
        self._apply(event, raw)

    def _row(self, raw: bytes, validate: Validator) -> Slot:
        self._owned()
        if not 0 < len(raw) <= _MAX_BYTES:
            raise ValueError("Invalid checkpoint row size")
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or value.get("identity_sha256") != self.plan.identity_sha256:
            raise ValueError("Checkpoint row has a foreign measurement identity")
        parts = tuple(value.get(name) for name in ("scenario_id", "path", "pipeline", "channel"))
        if parts not in self._slots:
            raise ValueError("Checkpoint row has a foreign matrix slot")
        key = (str(parts[0]), str(parts[1]), str(parts[2]), str(parts[3]))
        validate(value)
        return key

    def import_result(self, raw: bytes, *, expected_sha256: str, validate: Validator) -> Slot:
        if _digest(raw) != expected_sha256:
            raise ValueError("Parent result hash changed")
        key = self._row(raw, validate)
        previous = self._states.get(key)
        if previous is None:
            if self._retained(key):
                raise ValueError("An unrecorded result cannot be imported")
            self._record("importing", key, result_sha256=expected_sha256)
        elif previous.action != "importing" or previous.result_sha256 != expected_sha256:
            raise ValueError("Duplicate or changed imported checkpoint slot")
        path = self._result_path(key, expected_sha256)
        retained = self._retained(key)
        if retained and retained != (path,):
            raise ValueError("Conflicting imported result")
        if not retained:
            _write(path, raw)
        self._record("imported", key, result_sha256=expected_sha256)
        return key

    def begin(self, key: Slot) -> bool:
        self._owned()
        event = self._states.get(key)
        if event and event.action in {"completed", "imported"} and event.result_sha256:
            if _digest(_read(self._result_path(key, event.result_sha256))) != event.result_sha256:
                raise ValueError("Completed checkpoint result changed")
            return False
        self._record("started", key)
        return True

    def require_started(self, key: Slot) -> None:
        self._owned()
        event = self._states.get(key)
        if event is None or event.action != "started" or self._retained(key):
            raise ValueError("Provider work requires an admitted unresolved slot")

    def complete(self, key: Slot, raw: bytes, *, validate: Validator) -> Path:
        self._owned()
        if not 0 < len(raw) <= _MAX_BYTES:
            raise ValueError("Invalid checkpoint row size")
        event = self._states.get(key)
        if event is None or event.action != "started":
            raise ValueError("Only an admitted slot can finish")
        path = self._result_path(key, _digest(raw))
        retained = self._retained(key)
        if retained and retained != (path,):
            raise ValueError("Conflicting checkpoint result")
        if not retained:
            _write(path, raw)
        if self._row(raw, validate) != key:
            raise ValueError("Completion belongs to another slot")
        self._record("completed", key, result_sha256=_digest(raw))
        return path

    def reconcile_result(self, key: Slot, *, validate: Validator) -> Path:
        retained = self._retained(key)
        if len(retained) != 1:
            raise ValueError("No unique durable result to reconcile")
        return self.complete(key, _read(retained[0]), validate=validate)

    def fail(self, key: Slot, *, stage: str, error_class: str, ledger_sha256: str) -> None:
        if self._retained(key):
            raise ValueError("A retained result must be reconciled before recording failure")
        self._record(
            "failed", key, stage=stage, error_class=error_class, ledger_sha256=ledger_sha256
        )

    def replace_formation(self, key: Slot) -> None:
        self._record("replacement", key)

    def import_preparation_failure(
        self, key: Slot, *, source: bytes, ledger_sha256: str, error_class: str
    ) -> None:
        """The parent reader validates the original proof before this durable record."""
        self._owned()
        if any(slot[0] == key[0] for slot in self._states):
            raise ValueError("Original preparation already has continuation activity")
        directory = self.directory / "failures"
        if not directory.exists():
            directory.mkdir(mode=0o700)
            _sync(self.directory)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Unsafe preparation evidence directory")
        digest = _digest(source)
        path = directory / f"{digest}.json"
        if path.exists():
            if _read(path) != source:
                raise ValueError("Original preparation proof changed")
        else:
            _write(path, source)
        self._record(
            "parent_failed",
            key,
            stage="formation",
            error_class=error_class,
            ledger_sha256=ledger_sha256,
            source_sha256=digest,
        )

    def pending(self) -> tuple[Slot, ...]:
        self._owned()
        if any(self._states[k].action == "failed" for k in self._bounded_replaced):
            return ()
        blocked = {
            key[0]
            for key, event in self._states.items()
            if event.action in {"started", "failed", "parent_failed", "importing"}
        }
        return tuple(
            key
            for key in self._slots
            if key[0] not in blocked
            and (
                key not in self._states
                or self._states[key].action in {"replacement", "bounded_replacement"}
            )
        )

    def completed(self) -> tuple[Slot, ...]:
        self._owned()
        return tuple(
            key
            for key in self._slots
            if key in self._states and self._states[key].action in {"completed", "imported"}
        )
