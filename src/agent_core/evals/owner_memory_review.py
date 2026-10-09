"""Capability-addressed loopback review. Private input and output live only in RAM."""

import asyncio
import json
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from agent_core.evals.owner_memory_budget import ExperimentBudget
from agent_core.evals.owner_memory_fixture import (
    FixtureConfirmation,
    FixturePacket,
    validate_confirmation,
)
from agent_core.evals.owner_memory_preflight import preview_selection
from agent_core.evals.owner_memory_runtime import FixtureResources, run_fixture
from agent_core.ports.determinism import Clock
from agent_core.ports.models import ModelProvider


def describe_result(result: dict[str, Any]) -> str:
    """Explain only observed stages; a missing receipt is not model abstention."""
    operations = result.get("operations", [])
    if operations:
        return (
            f"{len(operations)} change(s) accepted in test memory. "
            "Your stored memories are unchanged."
        )
    reason = result.get("reason", "")
    if reason == "no_related_groups":
        return "No related group was found. The model was not called and no changes were made."
    if reason == "reviewed":
        reviews = [
            r
            for r in result.get("reviews", [])
            if r.get("kind") != "no_change" or not r.get("requires_local_validation")
        ]
        if not reviews:
            return "The model proposed no changes for the compared statements. Nothing was changed."
        if not any(r.get("requires_local_validation") for r in reviews):
            if any(r.get("reason") == "unsupported_clause" for r in reviews):
                return (
                    "The verifier rejected the proposed changes because their "
                    "claims were not supported by the supplied statements. "
                    "Nothing was changed."
                )
            return (
                "The proposed changes failed verification. Nothing was "
                "changed; the rejection reasons are in Execution details."
            )
        if any(s in {"retry", "deferred"} for s in result.get("application", [])):
            return (
                "Verification finished, but the local write checks deferred "
                "the changes. No changes were applied."
            )
        return (
            "Verification finished, but no proposal passed the final local "
            "memory checks. Nothing was changed."
        )
    if reason == "deferred":
        if result.get("calls") == 0:
            return "Input admission deferred this run before any model call. Nothing was changed."
        return "The run stopped at admission to verification. No changes were accepted."
    return {
        "invalid_response": "The model returned an invalid response. No changes were accepted.",
        "timeout": "The experiment timed out. No changes were accepted.",
        "unavailable": "Model processing was unavailable. No changes were accepted.",
        "admission_withdrawn": (
            "Permission to process this experiment was withdrawn. No changes were accepted."
        ),
        "lease_lost": "The experiment lost its processing lease. No changes were accepted.",
        "settlement_failed": "Usage accounting could not be completed. No changes were accepted.",
        "cancelled": "The experiment was cancelled. No changes were accepted.",
        "runtime_failure": (
            "The experiment failed before a valid result could be produced. "
            "No changes were accepted."
        ),
        "confirmation_or_budget_refused": (
            "The experiment was refused by its confirmation or spending limit "
            "check. No model call was started."
        ),
    }.get(
        reason, "The experiment ended without an explained result. No successful output is claimed."
    )


class _Review:
    def __init__(self, packet: FixturePacket) -> None:
        self.packet: FixturePacket | None = packet.model_copy(deep=True)
        self.confirmations: dict[UUID, FixtureConfirmation] = {}
        self.result: dict[str, Any] | None = None
        self.task: asyncio.Task[None] | None = None
        self.status = "pending"

    def discard(self) -> None:
        self.packet = None
        self.confirmations.clear()
        self.result = None
        self.status = "closed"
        if self.task is not None and not self.task.done():
            self.task.cancel()


def create_review_app(
    packet: FixturePacket,
    *,
    provider: ModelProvider,
    budget: ExperimentBudget,
    clock: Clock,
    resources: Callable[[datetime], FixtureResources],
    port: int,
    token: str | None = None,
) -> FastAPI:
    if not 1024 <= port <= 65535:
        raise ValueError("invalid loopback port")
    token = token or secrets.token_urlsafe(24)
    if re.fullmatch(r"[A-Za-z0-9_-]{32,64}", token) is None:
        raise ValueError("invalid review capability")
    state = _Review(packet)
    base = f"/{token}/"
    origin = f"http://127.0.0.1:{port}"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            state.discard()
            if state.task is not None:
                await asyncio.gather(state.task, return_exceptions=True)
            async with asyncio.timeout(5):
                await provider.close()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.review_url = origin + base

    @app.middleware("http")
    async def boundaries(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response: Response
        if request.headers.get("host") != f"127.0.0.1:{port}" or not request.url.path.startswith(
            base
        ):
            response = JSONResponse({"error": "not_found"}, status_code=404)
        elif request.method != "GET" and request.headers.get("origin") != origin:
            response = JSONResponse({"error": "origin_required"}, status_code=403)
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "private, no-store",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'unsafe-inline'; "
                    "style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
                    "form-action 'none'; frame-ancestors 'none'"
                ),
            }
        )
        return response

    def current() -> FixturePacket | None:
        return state.packet

    async def body(request: Request) -> dict[str, Any] | None:
        data = b""
        async for part in request.stream():
            data += part
            if len(data) > 4096:
                return None
        try:
            value = json.loads(data)
            return value if isinstance(value, dict) else None
        except (ValueError, UnicodeError):
            return None

    def error(reason: str, code: int) -> JSONResponse:
        return JSONResponse({"error": reason}, status_code=code)

    @app.get(base)
    async def page() -> HTMLResponse:
        return HTMLResponse(Path(__file__).with_suffix(".html").read_text())

    @app.get(base + "packet")
    async def read() -> Response:
        value = current()
        if value is None:
            return error("closed", 410)
        public = value.model_dump(mode="json", exclude={"model", "owner_digest"})
        public["provider"] = value.model.provider
        public["model"] = value.model.model
        public["packet_digest"] = value.digest
        public["maximum_usd"] = "0.25"
        return JSONResponse(
            {
                "packet": public,
                "status": state.status,
                "confirmed": [str(k) for k in state.confirmations],
                "preflight": preview_selection(
                    value, set(state.confirmations), clock.now()
                ).model_dump(mode="json"),
                "result": None
                if state.result is None
                else {**state.result, "summary": describe_result(state.result)},
            }
        )

    @app.post(base + "edit")
    async def edit(request: Request) -> Response:
        data = await body(request)
        value = current()
        if value is None:
            return error("closed", 410)
        if data is None or set(data) != {"packet_digest", "reference_id", "statement"}:
            return error("invalid_correction", 400)
        if data["packet_digest"] != value.digest or state.status != "pending":
            return error("changed_or_started", 409)
        statement = data["statement"]
        if (
            not isinstance(statement, str)
            or not statement.strip()
            or len(statement.encode()) > 2048
        ):
            return error("invalid_correction", 400)
        try:
            reference = UUID(data["reference_id"])
        except (ValueError, TypeError, AttributeError):
            return error("invalid_reference", 400)
        if reference not in {s.reference_id for s in value.sources}:
            return error("unknown_reference", 400)
        # Only the draft text can change. Original provenance/classification stays
        # attached, and the changed packet requires fresh exact-text confirmations.
        state.packet = value.model_copy(
            update={
                "sources": tuple(
                    s.model_copy(update={"statement": statement})
                    if s.reference_id == reference
                    else s
                    for s in value.sources
                )
            }
        )
        state.confirmations.clear()
        return JSONResponse({"saved": True})

    @app.post(base + "confirm")
    async def confirm(request: Request) -> Response:
        data = await body(request)
        value = current()
        if value is None:
            return error("closed", 410)
        if (
            data is None
            or set(data) != {"packet_digest", "reference_id", "human_confirmed"}
            or data["human_confirmed"] is not True
        ):
            return error("invalid_confirmation", 400)
        if data["packet_digest"] != value.digest or state.status != "pending":
            return error("changed_or_started", 409)
        try:
            reference = UUID(data["reference_id"])
        except (ValueError, TypeError, AttributeError):
            return error("invalid_reference", 400)
        if reference not in {s.reference_id for s in value.sources}:
            return error("unknown_reference", 400)
        # A retry retains the actual first gesture's time. No all-at-once fake events.
        state.confirmations.setdefault(
            reference,
            FixtureConfirmation(
                packet_digest=value.digest,
                reference_id=reference,
                confirmed_at=clock.now(),
                human_confirmed=True,
            ),
        )
        return JSONResponse({"recorded": True})

    @app.post(base + "unconfirm")
    async def unconfirm(request: Request) -> Response:
        data = await body(request)
        value = current()
        if value is None:
            return error("closed", 410)
        if data is None or set(data) != {"packet_digest", "reference_id"}:
            return error("invalid_confirmation", 400)
        if data["packet_digest"] != value.digest or state.status != "pending":
            return error("changed_or_started", 409)
        try:
            reference = UUID(data["reference_id"])
        except (ValueError, TypeError, AttributeError):
            return error("invalid_reference", 400)
        if reference not in {s.reference_id for s in value.sources}:
            return error("unknown_reference", 400)
        state.confirmations.pop(reference, None)
        return JSONResponse({"removed": True})

    async def evaluate(
        value: FixturePacket, confirmations: tuple[FixtureConfirmation, ...], submitted_at: datetime
    ) -> None:
        try:
            result = await run_fixture(
                value,
                confirmations,
                owner_digest=value.owner_digest,
                model=value.model,
                submitted_at=submitted_at,
                provider=provider,
                budget=budget,
                clock=clock,
                resources=resources,
                admitted=lambda: state.packet is not None,
            )
        except ValueError:
            result = {
                "outcome": "refused",
                "reason": "confirmation_or_budget_refused",
                "operations": [],
            }
        if state.packet is not None:
            state.result = result
            state.status = "finished"

    @app.post(base + "run")
    async def run(request: Request) -> Response:
        data = await body(request)
        value = current()
        if value is None:
            return error("closed", 410)
        if (
            data is None
            or set(data) != {"packet_digest", "reference_ids", "human_confirmed"}
            or data["human_confirmed"] is not True
        ):
            return error("invalid_confirmation", 400)
        if state.status != "pending" or data["packet_digest"] != value.digest:
            return error("changed_or_started", 409)
        confirmations = tuple(state.confirmations.values())
        # The owner submits exactly the selection visible in this browser tab.
        # Another tab changing the selection cannot silently change what is sent.
        references = data["reference_ids"]
        if (
            not isinstance(references, list)
            or any(not isinstance(ref, str) for ref in references)
            or len(references) != len(confirmations)
            or set(references) != {str(c.reference_id) for c in confirmations}
        ):
            return error("selection_changed", 409)
        submitted_at = clock.now()
        try:
            validate_confirmation(
                value,
                confirmations,
                owner_digest=value.owner_digest,
                model=value.model,
                submitted_at=submitted_at,
                now=submitted_at,
            )
        except ValueError:
            return error("two_current_confirmations_required", 409)
        preview = preview_selection(value, set(state.confirmations), clock.now())
        if not preview.ready:
            return error(preview.reason, 409)
        state.status = "running"
        state.task = asyncio.create_task(evaluate(value, confirmations, submitted_at))
        return JSONResponse({"started": True}, status_code=202)

    @app.post(base + "cancel")
    async def cancel() -> Response:
        state.discard()
        return JSONResponse({"closed": True})

    return app
