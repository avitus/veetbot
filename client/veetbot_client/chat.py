"""Interactive terminal shell over the Veetbot API client."""

from __future__ import annotations

import re
import time
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Literal, Protocol, TextIO
from uuid import uuid4

from .api import ApiError, ClientError, ConnectionFailureError, ProtocolError, SSEEvent

TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})
_SAFE_PROVIDER_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_SAFE_PROVIDER_PARAMETER = re.compile(r"[A-Za-z0-9_.\[\]-]{1,128}")


def _provider_failure_suffix(failure: dict[str, object]) -> str:
    details = failure.get("details")
    if not isinstance(details, dict):
        return ""
    diagnostics: list[str] = []
    status = details.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        diagnostics.append(f"HTTP {status}")
    code = details.get("provider_code")
    if isinstance(code, str) and _SAFE_PROVIDER_CODE.fullmatch(code):
        diagnostics.append(f"code={code}")
    parameter = details.get("provider_parameter")
    if isinstance(parameter, str) and _SAFE_PROVIDER_PARAMETER.fullmatch(parameter):
        diagnostics.append(f"parameter={parameter}")
    return "" if not diagnostics else f" ({'; '.join(diagnostics)})"


def _terminal_safe(value: str) -> str:
    """Remove terminal control sequences from API- and model-controlled text."""

    result: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\x1b":
            index += 1
            if index >= len(value):
                break
            marker = value[index]
            index += 1
            if marker == "[":
                while index < len(value):
                    final = ord(value[index])
                    index += 1
                    if 0x40 <= final <= 0x7E:
                        break
            elif marker in "]P^_X":
                while index < len(value):
                    if value[index] == "\x07":
                        index += 1
                        break
                    if value[index : index + 2] == "\x1b\\":
                        index += 2
                        break
                    index += 1
            continue
        if character == "\x9b":
            index += 1
            while index < len(value):
                final = ord(value[index])
                index += 1
                if 0x40 <= final <= 0x7E:
                    break
            continue
        if character in "\x90\x98\x9d\x9e\x9f":
            index += 1
            while index < len(value) and value[index] not in "\x07\x9c":
                index += 1
            index += index < len(value)
            continue
        if character in "\n\t" or unicodedata.category(character) != "Cc":
            result.append(character)
        index += 1
    return "".join(result)


class ChatApi(Protocol):
    def create_session(self, agent_id: str = "general") -> dict[str, object]: ...

    def get_session(self, session_id: str) -> dict[str, object]: ...

    def submit_message(
        self,
        session_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]: ...

    def get_run(self, run_id: str) -> dict[str, object]: ...

    def cancel_run(self, run_id: str) -> dict[str, object]: ...

    def deliver_input(
        self, run_id: str, text: str, question_id: str | None
    ) -> dict[str, object]: ...

    def get_approval(self, approval_id: str) -> dict[str, object]: ...

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        reason: str | None = None,
    ) -> dict[str, object]: ...

    def stream_events(
        self, run_id: str, last_event_id: int | None = None
    ) -> Iterator[SSEEvent]: ...


class Console:
    def __init__(
        self,
        stdout: TextIO,
        stderr: TextIO,
        *,
        read_line: Callable[[str], str] = input,
    ) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self._read_line = read_line

    def print(self, message: str = "", *, error: bool = False) -> None:
        stream = self.stderr if error else self.stdout
        stream.write(f"{_terminal_safe(message)}\n")
        stream.flush()

    def write(self, message: str) -> None:
        self.stdout.write(_terminal_safe(message))
        self.stdout.flush()

    def prompt(self, label: str) -> str:
        return self._read_line(_terminal_safe(label))


@dataclass(slots=True)
class _WatchState:
    last_event_id: int | None = None
    streamed_parts: list[str] = field(default_factory=list)
    durable_message: dict[str, object] | None = None
    latest_question: str | None = None
    delta_line_open: bool = False
    terminal_status: str | None = None


def _message_text(message: object) -> str:
    if not isinstance(message, dict):
        return ""
    raw_content = message.get("content")
    if not isinstance(raw_content, list):
        return ""
    parts: list[str] = []
    for part in raw_content:
        if (
            isinstance(part, dict)
            and part.get("kind") == "text"
            and isinstance(part.get("text"), str)
        ):
            parts.append(part["text"])
    return "\n".join(parts)


def _artifact_lines(message: object) -> list[str]:
    if not isinstance(message, dict) or not isinstance(message.get("content"), list):
        return []
    lines: list[str] = []
    for part in message["content"]:
        if not isinstance(part, dict) or part.get("kind") not in {"file", "image"}:
            continue
        artifact_id = part.get("artifact_id")
        if isinstance(artifact_id, str):
            label = part.get("filename") if isinstance(part.get("filename"), str) else part["kind"]
            lines.append(f"[artifact] {label}: {artifact_id}")
    return lines


class ChatApplication:
    """One thin client session with replay-safe run watching."""

    def __init__(
        self,
        api: ChatApi,
        console: Console,
        *,
        agent_id: str = "general",
        session_id: str | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_reconnect_attempts: int = 8,
        max_total_reconnects: int = 32,
    ) -> None:
        self.api = api
        self.console = console
        self.agent_id = agent_id
        self.session_id = session_id
        self._sleep = sleeper
        self._max_reconnect_attempts = max_reconnect_attempts
        self._max_total_reconnects = max_total_reconnects

    @staticmethod
    def _required_string(payload: dict[str, object], field_name: str) -> str:
        value = payload.get(field_name)
        if not isinstance(value, str) or not value:
            raise ProtocolError(f"API response omitted {field_name}")
        return value

    def open_session(self) -> str:
        if self.session_id is None:
            session = self.api.create_session(self.agent_id)
            self.session_id = self._required_string(session, "id")
            self.console.print(f"Created session {self.session_id}")
        else:
            session = self.api.get_session(self.session_id)
            self.console.print(f"Resumed session {self.session_id}")
        active_run = session.get("active_run_id")
        if isinstance(active_run, str) and active_run:
            self.console.print(f"Attaching to active run {active_run}")
            self.watch_run(active_run)
        return self.session_id

    def _finish_delta_line(self, state: _WatchState) -> None:
        if state.delta_line_open:
            self.console.write("\n")
            state.delta_line_open = False

    def _render_final(self, state: _WatchState, message: object) -> None:
        final_text = _message_text(message)
        streamed = "".join(state.streamed_parts)
        self._finish_delta_line(state)
        if final_text and streamed != final_text:
            self.console.print(f"assistant> {final_text}")
        for line in _artifact_lines(message):
            self.console.print(line)

    def _resolve_approval(self, run_id: str, approval_id: str, state: _WatchState) -> None:
        self._finish_delta_line(state)
        approval = self.api.get_approval(approval_id)
        summary = approval.get("action_summary")
        risk = approval.get("risk")
        self.console.print(
            f"[approval] {summary if isinstance(summary, str) else approval_id}"
            + (f" (risk: {risk})" if isinstance(risk, str) else "")
        )
        while True:
            try:
                answer = self.console.prompt("Approve once or deny? [a/d] ").strip().lower()
            except EOFError:
                self.api.cancel_run(run_id)
                self.console.print("Input closed; cancellation requested.", error=True)
                return
            if answer in {"a", "approve"}:
                self.api.resolve_approval(approval_id, "approve_once")
                return
            if answer in {"d", "deny"}:
                reason = self.console.prompt("Reason (optional): ").strip() or None
                self.api.resolve_approval(approval_id, "deny", reason)
                return
            self.console.print("Enter a to approve or d to deny.", error=True)

    def _answer_question(self, run_id: str, event: SSEEvent, state: _WatchState) -> None:
        self._finish_delta_line(state)
        question_id = event.data.get("question_id")
        if not isinstance(question_id, str):
            question_id = None
        question = state.latest_question or "The agent needs more information."
        try:
            answer = self.console.prompt(f"agent asks> {question}\nyou> ").strip()
        except EOFError:
            self.api.cancel_run(run_id)
            self.console.print("Input closed; cancellation requested.", error=True)
            return
        if not answer:
            answer = "Please continue with your best judgment."
        self.api.deliver_input(run_id, answer, question_id)
        state.latest_question = None

    def _handle_event(
        self, run_id: str, event: SSEEvent, state: _WatchState
    ) -> Literal["continue", "reconnect", "terminal"]:
        if event.event_id is not None:
            state.last_event_id = event.event_id
        if event.event == "message.delta":
            text = event.data.get("text")
            if isinstance(text, str):
                if not state.delta_line_open:
                    self.console.write("assistant> ")
                    state.delta_line_open = True
                self.console.write(text)
                state.streamed_parts.append(text)
        elif event.event == "assistant.message.completed":
            message = event.data.get("message")
            if isinstance(message, dict):
                state.durable_message = message
        elif event.event in {"tool.call.started", "tool.call.completed", "tool.call.failed"}:
            self._finish_delta_line(state)
            tool_name = event.data.get("name") or event.data.get("tool_name") or "tool"
            phase = event.event.rsplit(".", maxsplit=1)[-1]
            self.console.print(f"[tool] {tool_name}: {phase}")
        elif event.event == "context.working_state.updated":
            working_state = event.data.get("working_state")
            if isinstance(working_state, dict):
                questions = working_state.get("open_questions")
                if isinstance(questions, list) and questions and isinstance(questions[-1], str):
                    state.latest_question = questions[-1]
        elif event.event == "approval.requested":
            approval_id = event.data.get("approval_id")
            if isinstance(approval_id, str):
                self._resolve_approval(run_id, approval_id, state)
        elif event.event == "run.waiting_for_user":
            self._answer_question(run_id, event, state)
        elif event.event == "stream.overflow":
            sequence = event.data.get("last_sequence")
            if isinstance(sequence, int) and sequence >= 0:
                state.last_event_id = max(state.last_event_id or 0, sequence)
            self._finish_delta_line(state)
            self.console.print(
                "Event stream overflowed; replaying from durable history.", error=True
            )
            return "reconnect"
        elif event.event == "run.completed":
            message = event.data.get("final_message") or state.durable_message
            self._render_final(state, message)
            state.terminal_status = "COMPLETED"
            return "terminal"
        elif event.event == "run.failed":
            self._finish_delta_line(state)
            failure = event.data.get("failure")
            message = failure.get("message") if isinstance(failure, dict) else None
            reason = failure.get("reason") if isinstance(failure, dict) else None
            diagnostics = _provider_failure_suffix(failure) if isinstance(failure, dict) else ""
            self.console.print(
                "Run failed: "
                f"{message if isinstance(message, str) else reason or 'unknown error'}"
                f"{diagnostics}",
                error=True,
            )
            state.terminal_status = "FAILED"
            return "terminal"
        elif event.event == "run.cancelled":
            self._finish_delta_line(state)
            self.console.print("Run cancelled.", error=True)
            state.terminal_status = "CANCELLED"
            return "terminal"
        return "continue"

    def watch_run(self, run_id: str) -> str:
        state = _WatchState()
        reconnect_attempts = 0
        total_reconnects = 0
        while state.terminal_status is None:
            reconnect = False
            try:
                for event in self.api.stream_events(run_id, state.last_event_id):
                    reconnect_attempts = 0
                    action = self._handle_event(run_id, event, state)
                    if action == "terminal":
                        return state.terminal_status or "COMPLETED"
                    if action == "reconnect":
                        reconnect = True
                        break
                else:
                    reconnect = True
            except ConnectionFailureError:
                reconnect = True
            if not reconnect:
                continue
            reconnect_attempts += 1
            total_reconnects += 1
            if (
                reconnect_attempts > self._max_reconnect_attempts
                or total_reconnects > self._max_total_reconnects
            ):
                raise ConnectionFailureError("event stream reconnect limit exceeded")
            delay = min(0.25 * (2 ** (reconnect_attempts - 1)), 4.0)
            self.console.print(
                f"Event stream disconnected; reconnecting from {state.last_event_id or 'start'}...",
                error=True,
            )
            self._sleep(delay)
        return state.terminal_status

    def send(self, text: str) -> str:
        if self.session_id is None:
            self.open_session()
        assert self.session_id is not None
        idempotency_key = str(uuid4())
        attempts = 0
        while True:
            try:
                submitted = self.api.submit_message(
                    self.session_id,
                    text,
                    idempotency_key=idempotency_key,
                )
                run_id = self._required_string(submitted, "run_id")
                break
            except ConnectionFailureError:
                attempts += 1
                if attempts >= 3:
                    raise
                self._sleep(0.25 * attempts)
            except ApiError as exc:
                active_run = exc.details.get("run_id")
                if (
                    exc.code == "conflict"
                    and exc.details.get("reason") == "active_run_exists"
                    and isinstance(active_run, str)
                ):
                    self.console.print(f"Attaching to active run {active_run}")
                    run_id = active_run
                    break
                raise
        return self.watch_run(run_id)

    def _switch_session(self, session_id: str) -> None:
        session = self.api.get_session(session_id)
        self.session_id = self._required_string(session, "id")
        self.console.print(f"Resumed session {self.session_id}")

    def run(self, *, once: str | None = None) -> int:
        self.open_session()
        if once is not None:
            return 0 if self.send(once) == "COMPLETED" else 1
        self.console.print("Commands: /new, /session <id>, /help, /quit")
        while True:
            try:
                text = self.console.prompt("you> ").strip()
            except EOFError:
                self.console.print()
                return 0
            if not text:
                continue
            if text in {"/quit", "/exit"}:
                return 0
            if text == "/help":
                self.console.print("/new  /session <id>  /quit")
                continue
            try:
                if text == "/new":
                    previous_session = self.session_id
                    self.session_id = None
                    try:
                        self.open_session()
                    except ClientError:
                        self.session_id = previous_session
                        raise
                    continue
                if text.startswith("/session "):
                    self._switch_session(text.removeprefix("/session ").strip())
                    continue
                self.send(text)
            except KeyboardInterrupt:
                self.console.print("Interrupted.", error=True)
            except ClientError as exc:
                self.console.print(str(exc), error=True)
