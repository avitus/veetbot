"""Account for immutable parent ledgers and every continuation segment.

Only a checkpoint owner may open a segment. Unknown usage is always held; opening
another segment does not renew either its allocation or the aggregate USD 300 cap.
"""

import fcntl
import hashlib
import json
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field

from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.general_recall_release import Digest, ReleaseValue
from agent_core.domain.messages import (
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelRequest,
    ResolvedModel,
)
from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    Slot,
    _json,
    _read,
    _sync,
    _write,
)
from agent_core.evals.people_execution import BudgetedProvider, EvaluationBudget
from agent_core.memory.general_recall_evidence import _unique_object
from agent_core.ports.models import ModelProvider

_CALL_SLOT: ContextVar[Slot | None] = ContextVar("recall_continuation_slot", default=None)
_CALL_RECORD: ContextVar[list[dict[str, object]] | None] = ContextVar(
    "recall_continuation_record", default=None
)


class _Entry(ReleaseValue):
    index: int = Field(gt=0, strict=True)
    provider: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=256)
    request_sha256: Digest
    reservation_usd: Decimal = Field(gt=0, allow_inf_nan=False)
    outcome: Literal["reserved", "completed", "uncertain", "overrun"]
    actual_cost_usd: Decimal | None = Field(default=None, ge=0, allow_inf_nan=False)
    error_class: str | None = Field(default=None, max_length=256)
    failure_kind: str | None = Field(default=None, max_length=256)
    provider_code: str | None = Field(default=None, max_length=256)
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_parameter: str | None = Field(default=None, max_length=256)
    slot: (
        tuple[
            str,
            Literal["automatic", "deliberate"],
            Literal["formation"],
            Literal["sparse", "hybrid"],
        ]
        | None
    ) = None

    @property
    def request_key(self) -> tuple[str, str, str, Decimal, Slot | None]:
        return self.provider, self.model, self.request_sha256, self.reservation_usd, self.slot


@dataclass(frozen=True)
class LedgerAccounting:
    reported: Decimal
    held: Decimal
    calls: int
    unresolved: tuple[int, ...]
    completions: tuple[dict[str, Any], ...]
    overrun: bool = False

    @property
    def exposure(self) -> Decimal:
        return self.reported + self.held


@dataclass(frozen=True)
class ParentLedger:
    name: str
    sha256: str
    raw: bytes
    maximum: Decimal


def account_ledger(raw: bytes, *, maximum: Decimal) -> LedgerAccounting:
    """Reconstruct charges without resolving uncertainty or dropping failed calls."""
    if not maximum.is_finite() or maximum <= 0 or len(raw) > 16 * 1024 * 1024:
        raise ValueError("Invalid ledger allowance or size")
    requests: dict[int, _Entry] = {}
    reported = held = Decimal(0)
    completions: list[dict[str, Any]] = []
    overrun = False
    for line in raw.splitlines():
        if not line.strip():
            raise ValueError("Blank ledger entry")
        entry = _Entry.model_validate(json.loads(line, object_pairs_hook=_unique_object))
        if entry.outcome == "reserved":
            if (
                entry.index != len(requests) + 1
                or entry.actual_cost_usd is not None
                or reported + held + entry.reservation_usd > maximum
                or overrun
            ):
                raise ValueError("Duplicate, missing or over-budget reservation")
            held += entry.reservation_usd
        else:
            original = requests.get(entry.index)
            if (
                original is None
                or original.outcome != "reserved"
                or original.request_key != entry.request_key
            ):
                raise ValueError("Settlement differs from its original reservation")
            if entry.outcome == "uncertain":
                if entry.actual_cost_usd is not None:
                    raise ValueError("Unknown usage cannot claim a settled cost")
            else:
                if entry.actual_cost_usd is None or (
                    entry.actual_cost_usd > entry.reservation_usd
                ) != (entry.outcome == "overrun"):
                    raise ValueError("Invalid final usage or missing overrun")
                held -= entry.reservation_usd
                reported += entry.actual_cost_usd
                overrun |= entry.outcome == "overrun"
                completions.append(entry.model_dump(mode="json"))
        requests[entry.index] = entry
    return LedgerAccounting(
        reported,
        held,
        len(requests),
        tuple(
            index for index, entry in requests.items() if entry.outcome in {"reserved", "uncertain"}
        ),
        tuple(completions),
        overrun,
    )


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _journal(path: Path) -> bytes:
    # Empty is valid only for a segment that admitted no provider call.
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing or unsafe provider journal")
    if path.stat().st_size == 0:
        return b""
    return _read(path)


class ContinuationBudget(EvaluationBudget):
    """One live segment; immutable predecessor hashes reduce its available balance."""

    def __init__(self, store: CheckpointStore, parents: tuple[ParentLedger, ...]) -> None:
        store._owned()
        self.store = store
        self._segment_lock: int | None = None
        self.active_streams = 0
        self.halt_reason = "collection_stopped"
        if len(parents) != 8 or len({parent.name for parent in parents}) != 8:
            raise ValueError("All eight distinct original ledgers are required")
        original = Decimal(0)
        references = []
        for parent in parents:
            if _digest(parent.raw) != parent.sha256:
                raise ValueError("Original provider ledger changed")
            account = account_ledger(parent.raw, maximum=parent.maximum)
            if account.overrun:
                raise ValueError("A parent reservation overrun requires explicit assessment")
            original += account.exposure
            references.append(
                {"name": parent.name, "sha256": parent.sha256, "maximum": str(parent.maximum)}
            )
        if original != store.plan.prior_exposure_usd:
            raise ValueError("Original exposure differs from the approved plan")
        anchor = _json(
            {
                "plan_sha256": _digest(store.plan.encoded()),
                "parents": references,
                "prior_exposure_usd": str(original),
                "allocation_usd": str(store.plan.allowance_usd),
            }
        )
        directory = store.directory / "budget"
        if not directory.exists():
            directory.mkdir(mode=0o700)
            _write(directory / "anchor.json", anchor)
            _sync(store.directory)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Unsafe budget directory")
        try:
            self._segment_lock = os.open(
                directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            try:
                fcntl.flock(self._segment_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("A provider segment is already active or locked") from error
            if _read(directory / "anchor.json") != anchor:
                raise ValueError("Parent ledger lineage or allocation changed")
            remaining = store.plan.allowance_usd
            previous: dict[str, str] = {}
            expected = {directory / ".lock", directory / "anchor.json"}
            paths = sorted(directory.glob("*.jsonl"))
            for index, path in enumerate(paths, 1):
                if path.name != f"{index:06d}.jsonl":
                    raise ValueError("Provider segment is missing or renumbered")
                admission = path.with_suffix(".admission.json")
                receipt = _json(
                    {
                        "anchor_sha256": _digest(anchor),
                        "maximum_usd": str(remaining),
                        "previous": previous,
                    }
                )
                if _read(admission) != receipt:
                    raise ValueError("Provider segment ancestry or allowance changed")
                raw = _journal(path)
                account = account_ledger(raw, maximum=remaining)
                if account.overrun:
                    raise ValueError("A reservation overrun requires explicit assessment")
                sealed = path.with_suffix(".sealed.json")
                seal = _json(
                    {
                        "journal_sha256": _digest(raw),
                        "reported_usd": str(account.reported),
                        "held_usd": str(account.held),
                    }
                )
                if sealed.exists():
                    if _read(sealed) != seal:
                        raise ValueError("Sealed provider segment changed")
                else:
                    # Exclusive process ownership proves the previous writer is gone.
                    # Unmatched reservations stay held; no result or send is synthesized.
                    _write(sealed, seal)
                remaining -= account.exposure
                previous[path.name] = _digest(raw)
                expected.update((path, admission, sealed))
            if set(directory.iterdir()) != expected:
                raise ValueError("Unrecognized or incomplete provider segment")
            if remaining <= 0:
                raise BudgetExceededError("cost", "Continuation allocation is exhausted")
            path = directory / f"{len(paths) + 1:06d}.jsonl"
            _write(
                path.with_suffix(".admission.json"),
                _json(
                    {
                        "anchor_sha256": _digest(anchor),
                        "maximum_usd": str(remaining),
                        "previous": previous,
                    }
                ),
            )
            super().__init__(remaining, path)
            _sync(directory)
        except BaseException:
            self._unlock()
            raise

    def _unlock(self) -> None:
        if self._segment_lock is not None:
            os.close(self._segment_lock)
            self._segment_lock = None

    def append(self, record: dict[str, object]) -> None:
        self.store._owned()
        if self._segment_lock is None:
            raise ValueError("Provider segment is sealed")
        slot = _CALL_SLOT.get()
        super().append(record if slot is None else {**record, "slot": slot})
        current = _CALL_RECORD.get()
        if current is not None and record.get("outcome") == "reserved":
            current.append(dict(record))
        if record.get("outcome") in {"uncertain", "overrun"}:
            self.halt_reason = (
                "uncertain_usage" if record["outcome"] == "uncertain" else "reservation_overrun"
            )

    def retain_terminal(
        self,
        slot: Slot,
        records: list[dict[str, object]],
        event: ModelCompletedEvent | ModelFailedEvent,
    ) -> None:
        """Keep normalized terminal facts before a downstream consumer can reject them."""
        self.store._owned()
        if len(records) != 1 or self._segment_lock is None:
            raise ValueError("Terminal facts require one live, slot-bound reservation")
        record = records[0]
        directory = self.store.directory / "provider-terminals"
        if not directory.exists():
            directory.mkdir(mode=0o700)
            _sync(self.store.directory)
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("Unsafe provider terminal directory")
        facts: dict[str, Any] = {
            "version": "recall-provider-terminal@1",
            "ledger_file": self.journal.relative_to(self.store.directory).as_posix(),
            "index": record["index"],
            "request_sha256": record["request_sha256"],
            "slot": slot,
            "event": "completed" if isinstance(event, ModelCompletedEvent) else "failed",
        }
        if isinstance(event, ModelCompletedEvent):
            facts.update(
                stop_reason=event.stop_reason.value,
                turn_stop_reason=event.turn.stop_reason.value,
                internal_retry_count=event.internal_retry_count,
                usage=event.turn.usage.model_dump(mode="json", exclude={"provider", "model"}),
                tool_calls=len(event.turn.tool_calls),
                assistant_parts=[
                    part.kind
                    for message in event.turn.assistant_messages
                    for part in message.content
                ],
            )
        else:
            facts.update(error_class=type(event.error).__name__, failure_kind=event.error.kind)
        _write(directory / f"{self.journal.stem}-{record['index']:06}.json", _json(facts))

    def seal(self) -> LedgerAccounting:
        """Close only after admitted streams settle; unknown usage is never refunded."""
        self.store._owned()
        if self._segment_lock is None or self.active_streams:
            raise ValueError("Provider segment is sealed or still has active streams")
        raw = _journal(self.journal)
        account = account_ledger(raw, maximum=self.maximum)
        _write(
            self.journal.with_suffix(".sealed.json"),
            _json(
                {
                    "journal_sha256": _digest(raw),
                    "reported_usd": str(account.reported),
                    "held_usd": str(account.held),
                }
            ),
        )
        self._unlock()
        return account


class ContinuationHaltedError(RuntimeError):
    """A previous call stopped admissions; this is not proof of an empty allowance."""


class ContinuationProvider(BudgetedProvider):
    """Retain the original reservation protocol and identify its shared stop correctly."""

    def __init__(
        self, provider: ModelProvider, budget: ContinuationBudget, *, slot: Slot | None = None
    ) -> None:
        super().__init__(provider, budget)
        self.slot = slot

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        ledger = self.budget
        if not isinstance(ledger, ContinuationBudget):
            raise ValueError("Checkpoint continuation requires its reconciled segment budget")
        ledger.store._owned()
        if self.slot is None:
            raise ValueError("Provider work requires a durable slot admission")
        ledger.store.require_started(self.slot)
        if ledger._segment_lock is None:
            raise ValueError("Provider segment is sealed")
        if ledger.failed:
            raise ContinuationHaltedError(ledger.halt_reason)
        ledger.active_streams += 1
        stream = cast(AsyncGenerator[ModelEvent, None], super().stream(request, resolved, attempt))
        records: list[dict[str, object]] = []
        terminal: ModelCompletedEvent | ModelFailedEvent | None = None
        try:
            while True:
                # Never retain a ContextVar token across a yield to the consumer:
                # async-generator finalization can run in another task/context.
                token = _CALL_SLOT.set(self.slot)
                record_token = _CALL_RECORD.set(records)
                try:
                    event = await anext(stream)
                except StopAsyncIteration:
                    break
                finally:
                    _CALL_RECORD.reset(record_token)
                    _CALL_SLOT.reset(token)
                if isinstance(event, ModelCompletedEvent | ModelFailedEvent):
                    ledger.retain_terminal(self.slot, records, event)
                    terminal = event
                else:
                    yield event
        except BudgetExceededError as error:
            if ledger.failed:
                raise ContinuationHaltedError(ledger.halt_reason) from error
            raise
        finally:
            token = _CALL_SLOT.set(self.slot)
            try:
                await stream.aclose()
            finally:
                _CALL_SLOT.reset(token)
                ledger.active_streams -= 1
        # The reservation wrapper has now validated EOF and settled the stream.
        # A consumer rejecting this terminal cannot leak a stream or mark the
        # otherwise completed provider call uncertain during generator cleanup.
        if terminal is not None:
            yield terminal
