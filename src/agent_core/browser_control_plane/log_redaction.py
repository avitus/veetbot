"""Log configuration that keeps exception detail out of the profile service's logs.

ADR-0128 D18: Starlette re-raises every exception that reaches the generic
handler, and uvicorn then logs "Exception in ASGI application" with the
message, the traceback and every chained cause. A pydantic error prints its
input, a Playwright error carries URLs and a ``KeyError`` carries its key, so
any of them could put a handed-off cookie or a capability into the service's
logs. The service therefore runs with one handler, shared by the root and
``uvicorn`` loggers, whose filter replaces every record that carries
exception detail with the exception's class name. Records from outside the
handoff path, such as asyncio's "Task exception was never retrieved", pass
through the same filter.

This module is not called ``logging.py``, to avoid confusion with the
standard library module.
"""

from __future__ import annotations

import logging
from typing import Any

_REPLACEMENT = "profile service request failed ({})"


class ExceptionDetailFilter(logging.Filter):
    """Replace exception detail with the exception's class name; keep the record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info or record.exc_text or record.stack_info:
            failure = record.exc_info[1] if record.exc_info else None
            name = type(failure).__name__ if failure is not None else "Exception"
            record.msg = _REPLACEMENT.format(name)
            record.args = None
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        failure_type = getattr(record, "failure_type", None)
        if isinstance(failure_type, str) and failure_type:
            record.msg = f"{record.msg} ({failure_type})"
            # The class name is now in the message; a second pass adds nothing.
            record.failure_type = None
        return True


def profile_service_log_config() -> dict[str, Any]:
    """The service's ``logging.config.dictConfig`` document, for ``uvicorn.run``."""

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {"exception_detail": {"()": ExceptionDetailFilter}},
        "formatters": {"service": {"format": "%(levelname)s %(name)s %(message)s"}},
        "handlers": {
            "service": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "formatter": "service",
                "filters": ["exception_detail"],
            }
        },
        "root": {"level": "WARNING", "handlers": ["service"]},
        "loggers": {
            "uvicorn": {"level": "INFO", "handlers": ["service"], "propagate": False},
            "uvicorn.error": {"level": "INFO", "handlers": [], "propagate": True},
            "uvicorn.access": {"level": "INFO", "handlers": [], "propagate": False},
            "agent_core": {"level": "INFO", "propagate": True},
        },
    }
