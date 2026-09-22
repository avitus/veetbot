"""Structured logging configured once at process startup."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterable, Mapping
from typing import TextIO

import structlog
from opentelemetry import trace
from structlog.typing import EventDict, Processor, WrappedLogger

from agent_core.config import DeploymentMode

SENSITIVE_KEY = re.compile(r"secret|token|password|api_?key|authorization", re.IGNORECASE)
CONTENT_KEYS = frozenset({"prompt", "messages", "reasoning", "tool_result", "content"})
DEFAULT_PROVIDER_PREFIXES = ("sk-", "sk-ant-")
CONTENT_PREVIEW_CHARS = 200


def _redact_provider_text(value: str, provider_key_prefixes: tuple[str, ...]) -> str:
    redacted = value
    for prefix in sorted(provider_key_prefixes, key=len, reverse=True):
        if not prefix:
            continue
        pattern = re.compile(re.escape(prefix) + r"[A-Za-z0-9_./+=-]*")
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _sanitize_value(value: object, provider_key_prefixes: tuple[str, ...]) -> object:
    if isinstance(value, str):
        return _redact_provider_text(value, provider_key_prefixes)
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for raw_key, nested in value.items():
            key = str(raw_key)
            if SENSITIVE_KEY.search(key):
                sanitized[key] = "[REDACTED]"
            elif key in CONTENT_KEYS:
                original_length = len(str(nested))
                safe_nested = _sanitize_value(nested, provider_key_prefixes)
                rendered = str(safe_nested)
                sanitized[key] = {
                    "preview": rendered[:CONTENT_PREVIEW_CHARS],
                    "length": original_length,
                }
            else:
                sanitized[key] = _sanitize_value(nested, provider_key_prefixes)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_value(item, provider_key_prefixes) for item in value]
    if isinstance(value, tuple):
        return tuple(_sanitize_value(item, provider_key_prefixes) for item in value)
    if isinstance(value, (set, frozenset)):
        return [_sanitize_value(item, provider_key_prefixes) for item in sorted(value, key=repr)]
    return value


def redact_sensitive(
    _logger: WrappedLogger,
    _method_name: str,
    event_dict: EventDict,
    *,
    provider_key_prefixes: tuple[str, ...] = DEFAULT_PROVIDER_PREFIXES,
) -> EventDict:
    """Remove secrets and bound large content before rendering a log event."""

    sanitized = _sanitize_value(event_dict, provider_key_prefixes)
    assert isinstance(sanitized, dict)
    event_dict.clear()
    event_dict.update(sanitized)
    return event_dict


def add_trace_context(
    _logger: WrappedLogger, _method_name: str, event_dict: EventDict
) -> EventDict:
    """Bind identifiers from the active OpenTelemetry span when one exists."""

    span_context = trace.get_current_span().get_span_context()
    if span_context.is_valid:
        event_dict["trace_id"] = f"{span_context.trace_id:032x}"
        event_dict["span_id"] = f"{span_context.span_id:016x}"
    return event_dict


def _redaction_processor(prefixes: tuple[str, ...]) -> Processor:
    def processor(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
        return redact_sensitive(
            logger,
            method_name,
            event_dict,
            provider_key_prefixes=prefixes,
        )

    return processor


FIRST_PARTY_LOGGER = "agent_core"


class _ServiceLogHandler(logging.StreamHandler[TextIO]):
    """The one handler this module installs, so a second call replaces only it."""


def configure_logging(
    deployment_mode: DeploymentMode,
    *,
    level: int = logging.INFO,
    provider_key_prefixes: Iterable[str] = DEFAULT_PROVIDER_PREFIXES,
) -> None:
    """Render every log record as console development or JSON production output.

    The platform's modules log through the standard library with their fields in
    ``extra``, so the root handler runs the processor chain over those records
    as well as over native structlog events: fields, context variables and
    redaction apply to both.
    """

    renderer: Processor
    if deployment_mode is DeploymentMode.PRODUCTION:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        add_trace_context,
        # Before redaction, so a traceback is text the redaction processor reads.
        structlog.processors.format_exc_info,
        _redaction_processor(tuple(provider_key_prefixes)),
    ]
    handler = _ServiceLogHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    # Only this module's own handler is replaced: a capture handler a test
    # harness attached, or one an embedding process owns, stays where it is.
    root.handlers[:] = [
        existing for existing in root.handlers if not isinstance(existing, _ServiceLogHandler)
    ]
    root.addHandler(handler)
    # Third-party libraries stay at WARNING: HTTP clients log each request line
    # at INFO, and one integration carries its credential in the URL path.
    root.setLevel(max(level, logging.WARNING))
    logging.getLogger(FIRST_PARTY_LOGGER).setLevel(level)
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def bind_request_context(request_id: str) -> None:
    """Bind the request identifier for every log in the current context."""

    structlog.contextvars.bind_contextvars(request_id=request_id)


def bind_run_context(run_id: str, session_id: str, tenant_id: str) -> None:
    """Bind the run-scoped identifiers for every log in the current context."""

    structlog.contextvars.bind_contextvars(
        run_id=run_id,
        session_id=session_id,
        tenant_id=tenant_id,
    )


def bind_tool_context(tool_invocation_id: str) -> None:
    """Bind the current tool invocation identifier."""

    structlog.contextvars.bind_contextvars(tool_invocation_id=tool_invocation_id)


def clear_log_context() -> None:
    """Clear bound context when a request or run scope ends."""

    structlog.contextvars.clear_contextvars()
