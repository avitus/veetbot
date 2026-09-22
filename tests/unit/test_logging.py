"""Structured-log redaction and rendering tests."""

import ast
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

import agent_core
from agent_core.config import DeploymentMode
from agent_core.observability.logging import (
    CONTENT_PREVIEW_CHARS,
    configure_logging,
    redact_sensitive,
)


def test_redacts_sensitive_keys_and_provider_values() -> None:
    provider_value = "sk-" + ("x" * 24)
    event = redact_sensitive(
        None,
        "info",
        {
            "event": "provider.call",
            "api_key": "synthetic-value-for-test",
            "provider_value": provider_value,
        },
    )
    assert event["api_key"] == "[REDACTED]"
    assert event["provider_value"] == "[REDACTED]"
    assert provider_value not in repr(event)


def test_redaction_recurses_and_removes_embedded_provider_keys() -> None:
    provider_value = "sk-ant-" + ("y" * 24)
    event = redact_sensitive(
        None,
        "info",
        {
            "nested": {
                "authorization": "Bearer synthetic-test-value",
                "items": [f"provider returned {provider_value} in an error"],
            }
        },
    )
    assert event["nested"] == {
        "authorization": "[REDACTED]",
        "items": ["provider returned [REDACTED] in an error"],
    }
    assert provider_value not in repr(event)


def test_content_is_bounded_and_reports_original_length() -> None:
    content = "a" * (CONTENT_PREVIEW_CHARS + 50)
    event = redact_sensitive(None, "info", {"event": "tool.returned", "content": content})
    rendered = event["content"]
    assert isinstance(rendered, dict)
    assert rendered == {"preview": "a" * CONTENT_PREVIEW_CHARS, "length": len(content)}


def test_content_preview_is_sanitized_before_truncation() -> None:
    provider_value = "sk-" + ("z" * 24)
    original = {"password": "synthetic-password", "result": provider_value}
    event = redact_sensitive(
        None,
        "info",
        {"content": original},
    )
    assert provider_value not in repr(event)
    assert "synthetic-password" not in repr(event)
    assert event["content"]["length"] == len(str(original))


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """Put the root logger, the first-party level, and structlog back afterwards."""

    root = logging.getLogger()
    first_party = logging.getLogger("agent_core")
    handlers, root_level, first_party_level = root.handlers[:], root.level, first_party.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(root_level)
        first_party.setLevel(first_party_level)
        structlog.reset_defaults()


def _lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, object]]:
    rendered = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert all(line.startswith("{") for line in rendered), rendered
    return [json.loads(line) for line in rendered]


@pytest.mark.usefixtures("restored_logging")
def test_a_standard_library_record_renders_its_fields_through_the_chain(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(DeploymentMode.PRODUCTION)

    logging.getLogger("agent_core.sample").info(
        "sample_event",
        extra={"tool": "web.search", "signals": ("personal_data",), "api_key": "synthetic"},
    )

    (line,) = _lines(capsys)
    assert line["event"] == "sample_event"
    assert line["level"] == "info"
    assert line["logger"] == "agent_core.sample"
    assert line["tool"] == "web.search"
    assert line["signals"] == ["personal_data"]
    assert line["api_key"] == "[REDACTED]"
    assert isinstance(line["timestamp"], str)


@pytest.mark.usefixtures("restored_logging")
def test_first_party_lines_start_at_info_and_every_other_logger_at_warning(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(DeploymentMode.PRODUCTION)

    logging.getLogger("httpx").info("HTTP Request: POST https://vendor.invalid/bot-credential/x")
    logging.getLogger("httpx").warning("third_party_warning")
    logging.getLogger("agent_core.sample").debug("first_party_debug")
    logging.getLogger("agent_core.sample").info("first_party_info")

    assert [line["event"] for line in _lines(capsys)] == ["third_party_warning", "first_party_info"]


@pytest.mark.usefixtures("restored_logging")
def test_configuring_twice_keeps_one_handler_and_leaves_a_foreign_handler_alone(
    capsys: pytest.CaptureFixture[str],
) -> None:
    foreign = logging.NullHandler()
    logging.getLogger().addHandler(foreign)

    configure_logging(DeploymentMode.PRODUCTION)
    configure_logging(DeploymentMode.PRODUCTION)
    logging.getLogger("agent_core.sample").info("once")

    assert [line["event"] for line in _lines(capsys)] == ["once"]
    assert foreign in logging.getLogger().handlers


def test_no_log_call_names_a_field_the_log_record_reserves() -> None:
    """A reserved key raises only once the level is enabled, which a service now does."""

    reserved = set(logging.LogRecord("n", 0, "p", 0, "m", (), None).__dict__) | {
        "message",
        "asctime",
    }
    methods = {"debug", "info", "warning", "error", "exception", "critical", "log"}
    offenders: list[str] = []
    for path in sorted(Path(agent_core.__file__).parent.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in methods
            ):
                continue
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                offenders.extend(
                    f"{path.name}:{node.lineno}:{key.value}"
                    for key in keyword.value.keys
                    if isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in reserved
                )
    assert offenders == []


@pytest.mark.usefixtures("restored_logging")
def test_a_traceback_is_rendered_as_text_and_passes_redaction(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(DeploymentMode.PRODUCTION)
    provider_value = "sk-" + ("x" * 24)

    try:
        raise RuntimeError(f"refused {provider_value}")
    except RuntimeError:
        logging.getLogger("agent_core.sample").exception("iteration_failed")

    (line,) = _lines(capsys)
    assert "exc_info" not in line
    rendered = line["exception"]
    assert isinstance(rendered, str)
    assert "Traceback (most recent call last)" in rendered
    assert "RuntimeError: refused [REDACTED]" in rendered
    assert provider_value not in json.dumps(line)
