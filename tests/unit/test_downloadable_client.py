"""The downloadable client follows the public API and SSE contracts."""

from __future__ import annotations

import getpass
import io
import subprocess
import sys
from collections.abc import Iterator
from email.message import Message
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from client.veetbot_client import __main__ as client_main
from client.veetbot_client import __version__
from client.veetbot_client.api import (
    ApiClient,
    ApiError,
    ConfigurationError,
    ConnectionFailureError,
    SSEEvent,
    parse_sse,
)
from client.veetbot_client.chat import ChatApi, ChatApplication, Console
from scripts.build_client import build

ROOT = Path(__file__).resolve().parents[2]
OPAQUE_AUTH_VALUE = "-".join(("top", "secret", "token"))


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.status = status
        self._body = body
        self._buffer = io.BytesIO(body)

    def read(self, amount: int = -1) -> bytes:
        return self._buffer.read(amount)

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._body.splitlines(keepends=True))

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        del exc_type, exc_value, traceback


class FakeOpener:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[Request] = []

    def open(
        self,
        fullurl: Request,
        data: bytes | None = None,
        timeout: float = 0,
    ) -> FakeResponse:
        del data, timeout
        self.requests.append(fullurl)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_api_client_sends_bearer_auth_and_idempotent_message() -> None:
    opener = FakeOpener(
        [
            FakeResponse(b'{"id":"session-1","active_run_id":null}'),
            FakeResponse(b'{"run_id":"run-1","status":"QUEUED"}'),
        ]
    )
    client = ApiClient(
        "https://agent.example",
        token=OPAQUE_AUTH_VALUE,
        opener=opener,
    )

    assert client.create_session()["id"] == "session-1"
    assert (
        client.submit_message("session-1", "hello", idempotency_key="message-key")["run_id"]
        == "run-1"
    )

    create, submit = opener.requests
    assert create.get_method() == "POST"
    assert create.full_url == "https://agent.example/v1/sessions"
    assert create.get_header("Authorization") == f"Bearer {OPAQUE_AUTH_VALUE}"
    assert create.get_header("User-agent") == f"veetbot-client/{__version__}"
    assert submit.get_header("Idempotency-key") == "message-key"
    assert submit.data == b'{"content":[{"text":"hello","type":"text"}]}'


def test_api_client_refuses_remote_plaintext_bearer_token() -> None:
    with pytest.raises(ConfigurationError, match="require HTTPS"):
        ApiClient("http://agent.example", token="unsafe-token")  # noqa: S106

    ApiClient("http://127.0.0.1:8000", token="local-token")  # noqa: S106


def test_api_client_preserves_structured_errors_without_echoing_token() -> None:
    body = (
        b'{"error":{"code":"authentication_error","message":"authentication failed",'
        b'"details":{},"request_id":"request-7"}}'
    )
    error = HTTPError(
        "https://agent.example/v1/sessions", 401, "Unauthorized", Message(), io.BytesIO(body)
    )
    client = ApiClient(
        "https://agent.example",
        token=OPAQUE_AUTH_VALUE,
        opener=FakeOpener([error]),
    )

    with pytest.raises(ApiError) as raised:
        client.create_session()

    assert raised.value.status == 401
    assert raised.value.code == "authentication_error"
    assert raised.value.request_id == "request-7"
    assert OPAQUE_AUTH_VALUE not in str(raised.value)


@pytest.mark.parametrize(
    "read_error",
    [OSError("socket failed"), TimeoutError("socket timed out"), URLError("read failed")],
)
def test_api_client_sanitizes_transport_failure_while_reading_error(
    read_error: OSError,
) -> None:
    class FailingErrorBody:
        def read(self, amount: int = -1) -> bytes:
            del amount
            raise read_error

    error = HTTPError(
        "https://agent.example/v1/sessions",
        503,
        "Unavailable",
        Message(),
        cast(Any, FailingErrorBody()),
    )
    client = ApiClient("https://agent.example", opener=FakeOpener([error]))

    with pytest.raises(ApiError) as raised:
        client.create_session()

    assert raised.value.status == 503
    assert raised.value.code == "http_503"
    assert str(raised.value) == "http_503: API returned HTTP 503"


def test_api_client_connection_error_names_the_api_and_required_action() -> None:
    client = ApiClient(
        "http://127.0.0.1:8000",
        opener=FakeOpener([URLError("connection refused")]),
    )

    with pytest.raises(ConnectionFailureError) as raised:
        client.health_ready()

    assert str(raised.value) == (
        "could not connect to API at http://127.0.0.1:8000; "
        "verify the server is running and the URL is correct"
    )


def test_sse_parser_keeps_transient_frames_unidentified() -> None:
    events = list(
        parse_sse(
            [
                b": heartbeat\n",
                b"\n",
                b"event: message.delta\n",
                b'data: {"text":"hel"}\n',
                b"\n",
                b"id: 42\n",
                b"event: assistant.message.completed\n",
                b'data: {"message":{"kind":"assistant","content":[]}}\n',
                b"\n",
            ]
        )
    )

    assert [(event.event, event.event_id) for event in events] == [
        ("message.delta", None),
        ("assistant.message.completed", 42),
    ]


def test_event_stream_sends_only_the_last_persisted_identifier() -> None:
    opener = FakeOpener(
        [
            FakeResponse(
                b"event: message.delta\n"
                b'data: {"text":"a"}\n\n'
                b"id: 9\n"
                b"event: run.completed\n"
                b'data: {"final_message":{"kind":"assistant","content":[]}}\n\n'
            )
        ]
    )
    client = ApiClient("https://agent.example", opener=opener)

    events = list(client.stream_events("run-1", 7))

    assert [event.event_id for event in events] == [None, 9]
    assert opener.requests[0].get_header("Last-event-id") == "7"


class ScriptedChatApi:
    def __init__(self) -> None:
        self.delivered: list[tuple[str, str, str | None]] = []
        self.resolutions: list[tuple[str, str, str | None]] = []
        self.stream_calls: list[int | None] = []
        self._stream_count = 0

    def create_session(self, agent_id: str = "general") -> dict[str, object]:
        assert agent_id == "general"
        return {"id": "session-1", "active_run_id": None}

    def get_session(self, session_id: str) -> dict[str, object]:
        return {"id": session_id, "active_run_id": None}

    def submit_message(
        self,
        session_id: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        assert (session_id, text) == ("session-1", "help")
        assert idempotency_key is not None
        return {"run_id": "run-1", "status": "QUEUED"}

    def get_run(self, run_id: str) -> dict[str, object]:
        return {"id": run_id, "status": "RUNNING"}

    def cancel_run(self, run_id: str) -> dict[str, object]:
        return {"id": run_id, "status": "CANCELLED"}

    def deliver_input(self, run_id: str, text: str, question_id: str | None) -> dict[str, object]:
        self.delivered.append((run_id, text, question_id))
        return {"run_id": run_id, "status": "QUEUED"}

    def get_approval(self, approval_id: str) -> dict[str, object]:
        return {
            "id": approval_id,
            "action_summary": "Write the report",
            "risk": "MEDIUM",
        }

    def resolve_approval(
        self,
        approval_id: str,
        decision: str,
        reason: str | None = None,
    ) -> dict[str, object]:
        self.resolutions.append((approval_id, decision, reason))
        return {"id": approval_id, "decision": decision}

    def stream_events(self, run_id: str, last_event_id: int | None = None) -> Iterator[SSEEvent]:
        assert run_id == "run-1"
        self.stream_calls.append(last_event_id)
        self._stream_count += 1
        if self._stream_count == 1:
            yield SSEEvent("message.delta", {"text": "Hello"})
            yield SSEEvent("tool.call.started", {"name": "math.calculate"}, 1)
            raise ConnectionFailureError("fixture disconnect")
        yield SSEEvent(
            "context.working_state.updated",
            {"working_state": {"open_questions": ["Which region?"]}},
            2,
        )
        yield SSEEvent("run.waiting_for_user", {"question_id": "question-1"}, 3)
        yield SSEEvent("approval.requested", {"approval_id": "approval-1"}, 4)
        final_message = {
            "kind": "assistant",
            "content": [{"kind": "text", "text": "Hello"}],
        }
        yield SSEEvent("assistant.message.completed", {"message": final_message}, 5)
        yield SSEEvent("run.completed", {"final_message": final_message}, 6)


def test_chat_replays_resolves_suspensions_and_reconciles_final_message() -> None:
    api = ScriptedChatApi()
    stdout = io.StringIO()
    stderr = io.StringIO()
    answers = iter(["EU", "a"])
    console = Console(stdout, stderr, read_line=lambda _label: next(answers))
    application = ChatApplication(
        cast(ChatApi, api),
        console,
        sleeper=lambda _seconds: None,
    )

    assert application.run(once="help") == 0

    assert api.stream_calls == [None, 1]
    assert api.delivered == [("run-1", "EU", "question-1")]
    assert api.resolutions == [("approval-1", "approve_once", None)]
    assert "[tool] math.calculate: started" in stdout.getvalue()
    assert stdout.getvalue().count("assistant> Hello") == 1
    assert "reconnecting from 1" in stderr.getvalue()


def test_chat_labels_non_successful_tool_outcomes_semantically() -> None:
    class OutcomeChatApi(ScriptedChatApi):
        def stream_events(
            self, run_id: str, last_event_id: int | None = None
        ) -> Iterator[SSEEvent]:
            del run_id, last_event_id
            outcome = (
                '{"status":"failed","action":"web.fetch",'
                '"reason_code":"tool.web.provider_rejected",'
                '"message":"The selected web provider rejected this request.",'
                '"retryable":false,"remediation":"none"}'
            )
            yield SSEEvent(
                "tool.call.failed",
                {
                    "name": "web.fetch",
                    "result_item": {
                        "content": [{"type": "text", "text": outcome}],
                        "is_error": True,
                        "trust": "external_untrusted",
                    },
                },
                1,
            )
            unavailable = (
                '{"status":"unavailable","action":"web.fetch",'
                '"reason_code":"tool.web.provider_unavailable",'
                '"message":"The selected web provider is unavailable.",'
                '"retryable":true,"remediation":"none"}'
            )
            yield SSEEvent(
                "tool.call.failed",
                {
                    "name": "web.fetch",
                    "result_item": {
                        "content": [{"type": "text", "text": unavailable}],
                        "is_error": True,
                        "trust": "external_untrusted",
                    },
                },
                2,
            )
            yield SSEEvent("tool.call.denied", {"name": "workspace.write_text"}, 3)
            yield SSEEvent("tool.call.uncertain", {"name": "device.send_sms"}, 4)
            yield SSEEvent(
                "run.completed",
                {"final_message": {"kind": "assistant", "content": []}},
                5,
            )

    stdout = io.StringIO()
    application = ChatApplication(
        cast(ChatApi, OutcomeChatApi()),
        Console(stdout, io.StringIO()),
        sleeper=lambda _seconds: None,
    )

    assert application.watch_run("run-1") == "COMPLETED"
    assert "[tool] web.fetch: rejected" in stdout.getvalue()
    assert "[tool] web.fetch: unavailable" in stdout.getvalue()
    assert "[tool] workspace.write_text: denied" in stdout.getvalue()
    assert "[tool] device.send_sms: uncertain" in stdout.getvalue()


def test_chat_bounds_total_reconnects_even_when_each_stream_yields_an_event() -> None:
    class ReconnectingChatApi(ScriptedChatApi):
        def stream_events(
            self, run_id: str, last_event_id: int | None = None
        ) -> Iterator[SSEEvent]:
            del run_id
            self.stream_calls.append(last_event_id)
            yield SSEEvent(
                "tool.call.started",
                {"name": "math.calculate"},
                len(self.stream_calls),
            )

    api = ReconnectingChatApi()
    application = ChatApplication(
        cast(ChatApi, api),
        Console(io.StringIO(), io.StringIO()),
        sleeper=lambda _seconds: None,
        max_reconnect_attempts=1,
        max_total_reconnects=3,
    )

    with pytest.raises(ConnectionFailureError, match="reconnect limit"):
        application.watch_run("run-1")

    assert api.stream_calls == [None, 1, 2, 3]


def test_chat_renders_only_sanitized_provider_failure_diagnostics() -> None:
    class FailedChatApi(ScriptedChatApi):
        def stream_events(
            self, run_id: str, last_event_id: int | None = None
        ) -> Iterator[SSEEvent]:
            del run_id, last_event_id
            yield SSEEvent(
                "run.failed",
                {
                    "failure": {
                        "reason": "model_permanent_error",
                        "message": "the model provider rejected the request",
                        "details": {
                            "http_status": 400,
                            "provider_code": "missing_required_parameter",
                            "provider_parameter": "input[12].summary",
                            "ignored": "provider body must not be displayed",
                        },
                    }
                },
                1,
            )

    stderr = io.StringIO()
    application = ChatApplication(
        cast(ChatApi, FailedChatApi()),
        Console(io.StringIO(), stderr),
    )

    assert application.run(once="help") == 1
    assert stderr.getvalue() == (
        "Run failed: the model provider rejected the request "
        "(HTTP 400; code=missing_required_parameter; parameter=input[12].summary)\n"
    )
    assert "provider body" not in stderr.getvalue()


def test_interactive_chat_reports_client_error_and_keeps_prompting() -> None:
    class FailingSubmitApi(ScriptedChatApi):
        def submit_message(
            self,
            session_id: str,
            text: str,
            *,
            idempotency_key: str | None = None,
        ) -> dict[str, object]:
            del session_id, text, idempotency_key
            raise ConnectionFailureError("temporary outage")

    api = FailingSubmitApi()
    stdout = io.StringIO()
    stderr = io.StringIO()
    inputs = iter(["help", "/quit"])
    application = ChatApplication(
        cast(ChatApi, api),
        Console(stdout, stderr, read_line=lambda _label: next(inputs)),
        sleeper=lambda _seconds: None,
    )

    assert application.run() == 0
    assert "temporary outage" in stderr.getvalue()


def test_console_removes_terminal_control_sequences_from_remote_text() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    labels: list[str] = []

    def read_line(label: str) -> str:
        labels.append(label)
        return "answer"

    console = Console(stdout, stderr, read_line=read_line)
    console.print("\x1b]52;c;clipboard-payload\x07safe")
    console.write("\x1b[31mred\x1b[0m")
    assert console.prompt("\x9b31mdanger\x9b0m> ") == "answer"

    assert stdout.getvalue() == "safe\nred"
    assert labels == ["danger> "]
    assert "\x1b" not in stdout.getvalue()


def test_interactive_session_commands_report_failures_and_preserve_session() -> None:
    class FailingSessionApi(ScriptedChatApi):
        def __init__(self) -> None:
            super().__init__()
            self.create_calls = 0

        def create_session(self, agent_id: str = "general") -> dict[str, object]:
            self.create_calls += 1
            if self.create_calls > 1:
                raise ConnectionFailureError("new session unavailable")
            return super().create_session(agent_id)

        def get_session(self, session_id: str) -> dict[str, object]:
            if session_id == "missing":
                raise ConnectionFailureError("session lookup unavailable")
            return super().get_session(session_id)

    api = FailingSessionApi()
    stdout = io.StringIO()
    stderr = io.StringIO()
    inputs = iter(["/new", "/session missing", "/quit"])
    application = ChatApplication(
        cast(ChatApi, api),
        Console(stdout, stderr, read_line=lambda _label: next(inputs)),
    )

    assert application.run() == 0
    assert application.session_id == "session-1"
    assert "new session unavailable" in stderr.getvalue()
    assert "session lookup unavailable" in stderr.getvalue()


def test_client_zipapp_builds_and_runs_without_project_dependencies(tmp_path: Path) -> None:
    artifact = build(tmp_path / "veetbot-client.pyz")

    result = subprocess.run(
        [sys.executable, "-I", str(artifact), "--version"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "0.1.0.dev0"
    assert artifact.stat().st_mode & 0o111


def test_client_main_retries_only_readiness_before_running_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubClient:
        def __init__(self) -> None:
            self.has_token = False
            self.health_calls = 0
            self.supplied_token: str | None = None

        def health_ready(self) -> dict[str, object]:
            self.health_calls += 1
            if self.health_calls == 1:
                raise ApiError(status=401, code="authentication_error", message="authenticate")
            return {"status": "ready"}

        def set_token(self, token: str | None) -> None:
            self.supplied_token = token
            self.has_token = bool(token)

    client = StubClient()
    run_calls: list[object] = []
    monkeypatch.delenv("VEETBOT_API_TOKEN", raising=False)
    monkeypatch.setattr(client_main, "ApiClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: OPAQUE_AUTH_VALUE)
    monkeypatch.setattr(sys, "stdin", type("TTY", (), {"isatty": lambda self: True})())

    def run_once(args: object, api: object) -> int:
        run_calls.append((args, api))
        return 0

    monkeypatch.setattr(client_main, "_run", run_once)

    assert client_main.main(["--once", "hello"]) == 0
    assert client.health_calls == 2
    assert client.supplied_token == OPAQUE_AUTH_VALUE
    assert len(run_calls) == 1
