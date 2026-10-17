"""Content-free failure identities: never format an exception or inspect locals."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.<>]{0,127}$")
_PACKAGES = ("agent_core", "gmail_mcp", "svp_mcp")
MAX_CAUSES = 5
MAX_FRAMES = 12


def failure_fields(exc: BaseException, *, phase: str) -> dict[str, Any]:
    """Describe bounded code locations and causes without touching exception text."""

    causes: list[str] = []
    frames: list[dict[str, str | int]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(causes) < MAX_CAUSES:
        seen.add(id(current))
        name = type(current).__name__
        causes.append(name if _IDENTIFIER.fullmatch(name) else "Exception")
        traceback = current.__traceback__
        # Bound traversal as well as the emitted result, even for recursive failures.
        for _ in range(128):
            if traceback is None:
                break
            code = traceback.tb_frame.f_code
            parts = code.co_filename.replace("\\", "/").split("/")
            for package in _PACKAGES:
                if package not in parts:
                    continue
                relative = parts[parts.index(package) :]
                if all(_IDENTIFIER.fullmatch(part) for part in relative) and _IDENTIFIER.fullmatch(
                    code.co_name
                ):
                    frames.append(
                        {
                            "file": "/".join(relative),
                            "function": code.co_name,
                            "line": traceback.tb_lineno,
                        }
                    )
                    frames = frames[-MAX_FRAMES:]
                break
            traceback = traceback.tb_next
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    identity = {"phase": phase, "classes": causes, "frames": frames}
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "error_class": causes[0],
        "error_causes": causes,
        "error_frames": frames,
        "error_phase": phase,
        "error_fingerprint": fingerprint,
    }


def log_failure(
    logger: logging.Logger,
    event: str,
    exc: BaseException,
    *,
    phase: str,
    diagnostic_id: str | None = None,
    **identity: object,
) -> dict[str, Any]:
    """Log a safe record and return only opaque references for durable/public use."""

    fields = failure_fields(exc, phase=phase)
    context = failure_context()
    extra = {**context, **fields, **identity}
    if diagnostic_id is not None:
        extra["diagnostic_id"] = diagnostic_id
    logger.error(event, extra=extra)
    return {
        key: value
        for key, value in extra.items()
        if key in {"diagnostic_id", "error_fingerprint", "error_phase", "release_id"}
    }


_CONTEXT: ContextVar[dict[str, object] | None] = ContextVar("failure_context", default=None)


def failure_context() -> dict[str, object]:
    """Return a copy so consumers cannot alter another scope's fields."""
    return dict(_CONTEXT.get() or {})


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Restore identifiers after nested dispatch, errors and cancellation."""
    token = _CONTEXT.set({**failure_context(), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)
