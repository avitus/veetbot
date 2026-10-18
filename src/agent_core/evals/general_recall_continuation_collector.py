"""Two-worker collection over durable, outcome-blind continuation slots."""

import asyncio
import fcntl
import hashlib
import os
import re
import stat
from collections.abc import Awaitable, Callable
from typing import Any

from agent_core.adapters.determinism import elapsed_time
from agent_core.evals.general_recall_checkpoint import (
    CheckpointStore,
    Slot,
    Validator,
    _json,
    _sync,
    _write,
)

_ERROR_CLASSES = frozenset(
    {
        "CancelledError",
        "TimeoutError",
        "ModelProtocolError",
        "BudgetExceededError",
        "ConnectionError",
        "RuntimeError",
        "ValueError",
    }
)


def _error_class(error: BaseException) -> str:
    name = type(error).__name__
    return name if name in _ERROR_CLASSES else "OtherError"


class CollectionFailureError(Exception):
    """An evaluator identifies the stage without copying provider error text."""

    def __init__(self, stage: str, cause: BaseException) -> None:
        if stage not in {"formation", "answer"}:
            raise ValueError("Unknown collection stage")
        self.stage, self.cause = stage, cause
        super().__init__("Collection stopped")


async def collect_segment(
    store: CheckpointStore,
    *,
    evaluate: Callable[[Slot, int], Awaitable[bytes]],
    validate: Validator,
    admit: Callable[[Slot], None],
    seal_ledger: Callable[[], str],
    monotonic: Callable[[], float] = elapsed_time,
) -> dict[str, Any]:
    """Stop new admissions on failure, then retain every already admitted outcome.

    The caller supplies frozen-runtime admission, the original mechanical compiler,
    and a ledger seal that refuses active provider streams. This function cannot
    construct a provider, authorize a replacement, or certify a release.
    """
    store._owned()
    descriptor = os.open(
        store.directory / ".collector-lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Invalid collector lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("A collection segment is already active") from error
        return await _collect(
            store,
            evaluate=evaluate,
            validate=validate,
            admit=admit,
            seal_ledger=seal_ledger,
            monotonic=monotonic,
        )
    finally:
        os.close(descriptor)


async def _collect(
    store: CheckpointStore,
    *,
    evaluate: Callable[[Slot, int], Awaitable[bytes]],
    validate: Validator,
    admit: Callable[[Slot], None],
    seal_ledger: Callable[[], str],
    monotonic: Callable[[], float] = elapsed_time,
) -> dict[str, Any]:
    directory = store.directory / "segments"
    if not directory.exists():
        directory.mkdir(mode=0o700)
        _sync(store.directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Invalid collection segment directory")
    groups: dict[str, list[Slot]] = {}
    for key in store.pending():
        groups.setdefault(key[0], []).append(key)
    cases = iter(groups.values())
    attempts: list[dict[str, Any]] = []
    stopping = interrupted = False
    started_at = store.clock.now().isoformat()
    segment_start = monotonic()

    async def worker(number: int) -> None:
        nonlocal stopping
        for slots in cases:
            for key in slots:
                if stopping:
                    return
                attempt: dict[str, Any] = {
                    "key": key,
                    "worker": number,
                    "state": "refused",
                    "started_at": store.clock.now().isoformat(),
                }
                attempts.append(attempt)
                start = monotonic()
                admitted = returned = False
                try:
                    admit(key)
                    admitted = store.begin(key)
                    if not admitted:
                        raise ValueError("A pending slot was concurrently completed")
                    data = await evaluate(key, number)
                    returned = True
                    attempt["result_sha256"] = hashlib.sha256(data).hexdigest()
                    store.complete(key, data, validate=validate)
                    attempt["state"] = "completed"
                except (Exception, asyncio.CancelledError) as error:
                    stopping = True
                    cause = error.cause if isinstance(error, CollectionFailureError) else error
                    stage = error.stage if isinstance(error, CollectionFailureError) else "answer"
                    attempt.update({"error_class": _error_class(cause), "stage": stage})
                    if admitted:
                        attempt["state"] = (
                            "retained_unvalidated"
                            if store._retained(key)
                            else "unknown_result"
                            if returned
                            else "failed"
                        )
                finally:
                    attempt["finished_at"] = store.clock.now().isoformat()
                    attempt["wall_seconds"] = monotonic() - start
                # A scenario remains on its worker; yield only between completed arms.
                await asyncio.sleep(0)

    settling = asyncio.gather(worker(1), worker(2))
    while not settling.done():
        try:
            await asyncio.shield(settling)
        except asyncio.CancelledError:
            # A cancelled observer/coordinator must not cancel another admitted answer.
            stopping = interrupted = True
    await settling
    ledger_sha256 = seal_ledger()
    if re.fullmatch(r"[0-9a-f]{64}", ledger_sha256) is None:
        raise ValueError("Invalid sealed provider ledger identity")
    for attempt in attempts:
        if attempt["state"] == "failed":
            store.fail(
                attempt["key"],
                stage=attempt["stage"],
                error_class=attempt["error_class"],
                ledger_sha256=ledger_sha256,
            )
    completed = len(store.completed())
    report: dict[str, Any] = {
        "version": "recall-continuation-segment@1",
        "plan_sha256": hashlib.sha256(store.plan.encoded()).hexdigest(),
        "measurement_identity_sha256": store.plan.identity_sha256,
        "ledger_sha256": ledger_sha256,
        "state": "collected" if completed == 1600 else "incomplete",
        "completed_slots": completed,
        "required_slots": 1600,
        "interrupted": interrupted,
        "started_at": started_at,
        "finished_at": store.clock.now().isoformat(),
        "wall_seconds": monotonic() - segment_start,
        "attempts": attempts,
    }
    encoded = _json(report)
    _write(directory / f"{hashlib.sha256(encoded).hexdigest()}.json", encoded)
    if interrupted:
        raise asyncio.CancelledError
    return report
