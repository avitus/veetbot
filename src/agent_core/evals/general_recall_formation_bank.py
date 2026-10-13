"""Replay identical structured formation replies for paired synthetic comparisons.

The provider runs once. Every later arm must make the same ordered requests and
produce the same formed state. No gold label, provider credential, opaque provider
state or reasoning content is retained in the bank.
"""

import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.messages import (
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ModelTurn,
    ModelUsage,
    ResolvedModel,
    StopReason,
    TextPart,
)
from agent_core.evals.people_execution import BorrowedProvider
from agent_core.ports.models import ModelProvider


class _Call(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    completion: ModelCompletedEvent


class _Bank(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    identity: str = Field(pattern=r"^[0-9a-f]{64}$")
    state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    calls: tuple[_Call, ...] = Field(max_length=128)


def _read_bank(path: Path) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 8 * 1024 * 1024:
            raise ValueError("Invalid or oversized formation bank")
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) != info.st_size:
        raise ValueError("The formation bank changed while reading")
    return raw


def _key(request: ModelRequest, resolved: ResolvedModel) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "model": resolved.model_dump(
                    mode="json", exclude={"resolved_at", "credential_ref"}
                ),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _completion(event: ModelCompletedEvent) -> ModelCompletedEvent:
    if (
        event.internal_retry_count
        or event.stop_reason is not StopReason.END_TURN
        or event.turn.tool_calls
        or any(
            not isinstance(part, TextPart)
            for message in event.turn.assistant_messages
            for part in message.content
        )
    ):
        raise ValueError("A formation bank requires one ordinary structured completion")
    return event.model_copy(
        update={
            "turn": ModelTurn(
                assistant_messages=event.turn.assistant_messages,
                usage=event.turn.usage,
                stop_reason=event.turn.stop_reason,
            )
        },
        deep=True,
    )


class FormationBank(BorrowedProvider):
    def __init__(self, provider: ModelProvider, path: Path, identity: str) -> None:
        super().__init__(provider)
        self.path, self.identity = path, identity
        self._calls: list[_Call] = []
        self._cursor = 0
        self._failed = self._finished = False
        self._pending = path.with_suffix(path.suffix + ".pending.jsonl")
        self._recorded: _Bank | None = None
        self._digest: str | None = None
        if path.is_symlink():
            raise ValueError("A formation bank cannot be a symlink")
        if path.exists():
            raw = _read_bank(path)
            self._digest = hashlib.sha256(raw).hexdigest()
            self._recorded = _Bank.model_validate_json(raw)
            if self._recorded.identity != identity:
                raise ValueError("The formation bank identity changed")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            # A failed or interrupted recording cannot be silently replaced.
            with self._pending.open("x") as stream:
                os.chmod(self._pending, 0o600)
                stream.write(json.dumps({"identity": identity, "state": "recording"}) + "\n")

    @property
    def replaying(self) -> bool:
        return self._recorded is not None

    @property
    def usages(self) -> tuple[ModelUsage, ...]:
        calls = self._calls if self._recorded is None else self._recorded.calls
        return tuple(call.completion.turn.usage for call in calls)

    @property
    def digest(self) -> str:
        if not self._finished or self._failed:
            raise ValueError("The formation bank has not been verified")
        return self._verify_bytes()

    def _verify_bytes(self) -> str:
        digest = hashlib.sha256(_read_bank(self.path)).hexdigest()
        if self._digest != digest:
            self._failed = True
            raise ValueError("The formation bank bytes changed")
        return digest

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        if self._finished or self._failed or self._cursor >= 128:
            self._failed = True
            raise ValueError("The formation bank is closed or exhausted")
        key = _key(request, resolved)
        index = self._cursor
        self._cursor += 1
        if self._recorded is not None:
            self._verify_bytes()
            if (
                index >= len(self._recorded.calls)
                or self._recorded.calls[index].request_sha256 != key
            ):
                self._failed = True
                raise ValueError("The formation bank request sequence changed")
            recorded_event = self._recorded.calls[index].completion
            yield recorded_event.model_copy(
                update={
                    "attempt_id": attempt.attempt_id,
                    "run_id": attempt.run_id,
                    "step_number": attempt.step_number,
                    "sequence": 0,
                },
                deep=True,
            )
            return
        completed = False
        with self._pending.open("a") as stream:
            stream.write(json.dumps({"request_sha256": key, "state": "reserved"}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            async for event in self.provider.stream(request, resolved, attempt):
                if isinstance(event, ModelCompletedEvent):
                    if completed:
                        raise ValueError("The formation bank received duplicate completion")
                    call = _Call(request_sha256=key, completion=_completion(event))
                    self._calls.append(call)
                    completed = True
                    with self._pending.open("a") as stream:
                        stream.write(call.model_dump_json() + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    if self._pending.stat().st_size > 8 * 1024 * 1024:
                        raise ValueError("The formation bank exceeded its size bound")
                yield event
        except BaseException:
            self._failed = True
            raise
        finally:
            if not completed:
                self._failed = True

    def finish(self, state_sha256: str) -> None:
        if self._failed or self._finished:
            raise ValueError("The formation bank failed or was already sealed")
        if self._recorded is not None:
            self._verify_bytes()
            if (
                self._cursor != len(self._recorded.calls)
                or state_sha256 != self._recorded.state_sha256
            ):
                raise ValueError("The formation bank did not reproduce every call and formed state")
        else:
            bank = _Bank(
                identity=self.identity, state_sha256=state_sha256, calls=tuple(self._calls)
            )
            with self.path.open("x") as stream:
                os.chmod(self.path, 0o600)
                stream.write(bank.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            self._digest = hashlib.sha256(_read_bank(self.path)).hexdigest()
            self._pending.unlink()
        self._finished = True
