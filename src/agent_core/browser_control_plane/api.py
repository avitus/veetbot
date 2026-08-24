"""Strict internal HTTP boundary for browser-profile lifecycle operations."""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_core.browser_control_plane.sessions import HostedProfileSessionService
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserAction,
    BrowserInteractiveEvent,
    BrowserProviderError,
)
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import ConflictError
from agent_core.ports.browser_profiles import BrowserProfileControlPlane

MAX_PROFILE_SERVICE_BODY_BYTES = 64 * 1024
MAX_AUTHENTICATION_EVENT_BYTES = 8 * 1024
_LOGGER = logging.getLogger(__name__)
_AUTHENTICATION_PATH = re.compile(
    r"^/authentication/(?P<ceremony>[0-9a-fA-F-]{36})(?:/(?P<operation>frame|events))?$"
)


class _ProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    allowed_origins: tuple[str, ...] = Field(min_length=1, max_length=64)


class _LifecycleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)
    provider_ref: str = Field(min_length=32, max_length=512)


class _AcquireRequest(_LifecycleRequest):
    run_id: UUID
    attempt_number: int = Field(ge=1)
    deadline_at: AwareDatetime


class _LeaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lease_ref: str = Field(min_length=32, max_length=128)


class _NavigateRequest(_LeaseRequest):
    url: str = Field(min_length=1, max_length=4096)


class _ActRequest(_LeaseRequest):
    action: BrowserAction
    sequence: int = Field(ge=1)


class _BeginAuthenticationRequest(_LifecycleRequest):
    login_url: str = Field(min_length=1, max_length=4096)


class _CeremonyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ceremony_id: UUID
    tenant_id: str = Field(min_length=1, max_length=255)
    principal_id: str = Field(min_length=1, max_length=255)


def _error(
    status: int, code: str, message: str, *, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


class _ProfileServiceBoundary:
    """Authenticate and bound requests before FastAPI parses their bodies."""

    def __init__(self, app: ASGIApp, authorization: SecretValue) -> None:
        self._app = app
        self._authorization = authorization.reveal()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not (
            scope["method"] == "POST" and str(scope["path"]).startswith("/v1/")
        ):
            await self._app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope["headers"]}
        presented = headers.get(b"authorization", b"").decode("latin-1")
        expected = "Bearer " + self._authorization
        if not hmac.compare_digest(presented, expected):
            await _send_response(
                _error(
                    401,
                    "unauthorized",
                    "authentication required",
                    headers={"WWW-Authenticate": "Bearer"},
                ),
                scope,
                receive,
                send,
            )
            return
        media_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if media_type != b"application/json":
            await _send_response(
                _error(415, "unsupported_media_type", "application/json required"),
                scope,
                receive,
                send,
            )
            return
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > MAX_PROFILE_SERVICE_BODY_BYTES:
                    raise ValueError
            except ValueError:
                await _send_response(
                    _error(413, "payload_too_large", "request body too large"), scope, receive, send
                )
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_PROFILE_SERVICE_BODY_BYTES:
                await _send_response(
                    _error(413, "payload_too_large", "request body too large"), scope, receive, send
                )
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self._app(scope, bounded_receive, send)


class _AuthenticationSurfaceBoundary:
    """Bind direct surface requests to a live capability before body parsing."""

    def __init__(self, app: ASGIApp, sessions: HostedProfileSessionService) -> None:
        self._app = app
        self._sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not str(scope["path"]).startswith("/authentication"):
            await self._app(scope, receive, send)
            return

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (
                            b"content-security-policy",
                            b"default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; "
                            b"img-src blob:; connect-src 'self'; base-uri 'none'; "
                            b"form-action 'none'; frame-ancestors 'none'",
                        ),
                    ]
                )
                message = {**message, "headers": headers}
            await send(message)

        match = _AUTHENTICATION_PATH.fullmatch(str(scope["path"]))
        if match is None or match.group("operation") is None:
            await self._app(scope, receive, secured_send)
            return
        try:
            ceremony_id = UUID(match.group("ceremony"))
        except ValueError:
            await _send_response(
                _error(404, "not_found", "resource not found"),
                scope,
                receive,
                secured_send,
            )
            return
        headers = {key.lower(): value for key, value in scope["headers"]}
        capability = headers.get(b"x-browser-ceremony-capability", b"").decode("latin-1")
        if not await self._sessions.authenticate_surface(ceremony_id, capability):
            await _send_response(
                _error(401, "unauthorized", "authentication required"),
                scope,
                receive,
                secured_send,
            )
            return
        if match.group("operation") != "events":
            await self._app(scope, receive, secured_send)
            return
        media_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if media_type != b"application/json":
            await _send_response(
                _error(415, "unsupported_media_type", "application/json required"),
                scope,
                receive,
                secured_send,
            )
            return
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > MAX_AUTHENTICATION_EVENT_BYTES:
                    raise ValueError
            except ValueError:
                await _send_response(
                    _error(413, "payload_too_large", "request body too large"),
                    scope,
                    receive,
                    secured_send,
                )
                return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_AUTHENTICATION_EVENT_BYTES:
                await _send_response(
                    _error(413, "payload_too_large", "request body too large"),
                    scope,
                    receive,
                    secured_send,
                )
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def bounded_receive() -> Message:
            nonlocal delivered
            if delivered:
                return {"type": "http.request", "body": b"", "more_body": False}
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self._app(scope, bounded_receive, secured_send)


async def _send_response(
    response: Response,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    await response(scope, receive, send)


def create_profile_service_app(
    lifecycle: BrowserProfileControlPlane,
    authorization: SecretValue,
    *,
    readiness: Callable[[], bool] = lambda: True,
    sessions: HostedProfileSessionService | None = None,
) -> FastAPI:
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.add_middleware(_ProfileServiceBoundary, authorization=authorization)
    if sessions is not None:
        app.add_middleware(_AuthenticationSurfaceBoundary, sessions=sessions)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        del request, exc
        return _error(400, "invalid_request", "request is invalid")

    @app.exception_handler(ConflictError)
    async def conflict_error(request: Request, exc: ConflictError) -> JSONResponse:
        del request, exc
        return _error(409, "conflict", "profile lifecycle conflict")

    @app.exception_handler(BrowserProviderError)
    async def browser_error(request: Request, exc: BrowserProviderError) -> JSONResponse:
        del request
        return _error(409, exc.reason_code, "browser operation rejected")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        del request
        if exc.status_code == 404:
            return _error(404, "not_found", "resource not found")
        return _error(exc.status_code, "request_rejected", "request rejected")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        del request
        _LOGGER.error(
            "profile service request failed",
            extra={"failure_type": type(exc).__name__},
        )
        return _error(500, "internal_error", "service unavailable")

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        if readiness():
            return JSONResponse({"status": "ok"})
        return JSONResponse({"status": "not_ready"}, status_code=503)

    @app.post("/v1/browser-profiles:provision", status_code=201, response_model=None)
    async def provision(
        payload: _ProvisionRequest, request: Request
    ) -> dict[str, Any] | JSONResponse:
        rejected = _validate_idempotency(request, payload.profile_id, "provision")
        if rejected is not None:
            return rejected
        result = await lifecycle.provision(
            payload.profile_id,
            _principal(payload.tenant_id, payload.principal_id),
            payload.allowed_origins,
        )
        return result.model_dump(mode="json")

    @app.post(
        "/v1/browser-profiles/{profile_id}:revoke",
        status_code=204,
        response_model=None,
    )
    async def revoke(
        profile_id: UUID, payload: _LifecycleRequest, request: Request
    ) -> Response | JSONResponse:
        rejected = _validate_lifecycle_request(request, profile_id, payload.profile_id, "revoke")
        if rejected is not None:
            return rejected
        await lifecycle.revoke(
            profile_id,
            _principal(payload.tenant_id, payload.principal_id),
            payload.provider_ref,
        )
        return Response(status_code=204)

    @app.post(
        "/v1/browser-profiles/{profile_id}:delete",
        status_code=204,
        response_model=None,
    )
    async def delete(
        profile_id: UUID, payload: _LifecycleRequest, request: Request
    ) -> Response | JSONResponse:
        rejected = _validate_lifecycle_request(request, profile_id, payload.profile_id, "delete")
        if rejected is not None:
            return rejected
        await lifecycle.delete(
            profile_id,
            _principal(payload.tenant_id, payload.principal_id),
            payload.provider_ref,
        )
        return Response(status_code=204)

    if sessions is not None:

        @app.post("/v1/browser-sessions:acquire", response_model=None)
        async def acquire(
            payload: _AcquireRequest, request: Request
        ) -> dict[str, Any] | JSONResponse:
            expected = (
                f"browser-session:{payload.profile_id}:{payload.run_id}:"
                f"{payload.attempt_number}:acquire"
            )
            rejected = _require_idempotency(request, expected)
            if rejected is not None:
                return rejected
            lease = await sessions.acquire(
                payload.profile_id,
                _principal(payload.tenant_id, payload.principal_id),
                payload.provider_ref,
                run_id=payload.run_id,
                attempt_number=payload.attempt_number,
                deadline_at=payload.deadline_at,
            )
            return lease.model_dump(mode="json")

        @app.post("/v1/browser-sessions:navigate", response_model=None)
        async def navigate(payload: _NavigateRequest) -> dict[str, Any]:
            result = await sessions.navigate(payload.lease_ref, payload.url)
            return result.model_dump(mode="json")

        @app.post("/v1/browser-sessions:observe", response_model=None)
        async def observe(payload: _LeaseRequest) -> dict[str, Any]:
            result = await sessions.observe(payload.lease_ref)
            return result.model_dump(mode="json")

        @app.post("/v1/browser-sessions:act", response_model=None)
        async def act(payload: _ActRequest, request: Request) -> dict[str, Any] | JSONResponse:
            expected = (
                f"browser-session:{_private_ref_digest(payload.lease_ref)}:act:{payload.sequence}"
            )
            rejected = _require_idempotency(request, expected)
            if rejected is not None:
                return rejected
            result = await sessions.act(
                payload.lease_ref,
                payload.action,
                sequence=payload.sequence,
            )
            return result.model_dump(mode="json")

        @app.post("/v1/browser-sessions:close", status_code=204, response_model=None)
        async def close(payload: _LeaseRequest, request: Request) -> Response | JSONResponse:
            expected = f"browser-session:{_private_ref_digest(payload.lease_ref)}:close"
            rejected = _require_idempotency(request, expected)
            if rejected is not None:
                return rejected
            await sessions.close(payload.lease_ref)
            return Response(status_code=204)

        @app.post("/v1/browser-authentications:begin", status_code=201, response_model=None)
        async def begin_authentication(
            payload: _BeginAuthenticationRequest,
            request: Request,
        ) -> dict[str, Any] | JSONResponse:
            rejected = _require_idempotency(
                request,
                f"browser-authentication:{payload.profile_id}:begin",
            )
            if rejected is not None:
                return rejected
            result = await sessions.begin_authentication(
                payload.profile_id,
                _principal(payload.tenant_id, payload.principal_id),
                payload.provider_ref,
                login_url=payload.login_url,
            )
            return result.model_dump(mode="json")

        @app.post("/v1/browser-authentications:status", response_model=None)
        async def authentication_status(payload: _CeremonyRequest) -> dict[str, Any]:
            result = await sessions.refresh_authentication(
                payload.ceremony_id,
                _principal(payload.tenant_id, payload.principal_id),
            )
            return result.model_dump(mode="json")

        @app.post("/v1/browser-authentications:cancel", response_model=None)
        async def cancel_authentication(
            payload: _CeremonyRequest,
            request: Request,
        ) -> dict[str, Any] | JSONResponse:
            rejected = _require_idempotency(
                request,
                f"browser-authentication:{payload.ceremony_id}:cancel",
            )
            if rejected is not None:
                return rejected
            result = await sessions.cancel_authentication(
                payload.ceremony_id,
                _principal(payload.tenant_id, payload.principal_id),
            )
            return result.model_dump(mode="json")

        @app.get("/authentication-surface.js", response_model=None)
        async def authentication_script() -> PlainTextResponse:
            return PlainTextResponse(_AUTHENTICATION_SCRIPT, media_type="text/javascript")

        @app.get("/authentication/{ceremony_id}", response_model=None)
        async def authentication_surface(ceremony_id: UUID) -> HTMLResponse:
            del ceremony_id
            return HTMLResponse(_AUTHENTICATION_HTML)

        @app.get("/authentication/{ceremony_id}/frame", response_model=None)
        async def authentication_frame(
            ceremony_id: UUID,
            request: Request,
        ) -> Response:
            frame = await sessions.authentication_frame(
                ceremony_id,
                request.headers.get("x-browser-ceremony-capability", ""),
            )
            return Response(frame, media_type="image/png")

        @app.post(
            "/authentication/{ceremony_id}/events",
            status_code=204,
            response_model=None,
        )
        async def authentication_event(
            ceremony_id: UUID,
            payload: BrowserInteractiveEvent,
            request: Request,
        ) -> Response:
            await sessions.authentication_event(
                ceremony_id,
                request.headers.get("x-browser-ceremony-capability", ""),
                payload,
            )
            return Response(status_code=204)

    return app


def _principal(tenant_id: str, principal_id: str) -> Principal:
    return Principal(tenant_id=tenant_id, principal_id=principal_id)


def _validate_idempotency(
    request: Request, profile_id: UUID, operation: str
) -> JSONResponse | None:
    """Validate caller intent; the key is not a cached-response mechanism."""
    expected = f"browser-profile:{profile_id}:{operation}"
    if request.headers.get("idempotency-key") != expected:
        return _error(400, "invalid_request", "idempotency key is invalid")
    return None


def _require_idempotency(request: Request, expected: str) -> JSONResponse | None:
    """Assert exact retry intent; successful action replays remain sequence-bound."""
    if request.headers.get("idempotency-key") != expected:
        return _error(400, "invalid_request", "idempotency key is invalid")
    return None


def _private_ref_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:24]


_AUTHENTICATION_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Secure website login · Veetbot</title>
<style>
:root {
  color-scheme:dark;--ink:#f5f7fa;--muted:#aeb8c6;--panel:#172131;--line:#334155;
  --turquoise:#32d6c4;--orange:#ff9f43;--navy:#0b1220;--danger:#ff7b86
}
* {box-sizing:border-box}
body {
  font:16px/1.45 system-ui,-apple-system,sans-serif;margin:0;color:var(--ink);
  background:radial-gradient(circle at top left,#12354a 0,var(--navy) 44%)
}
main {max-width:1180px;margin:auto;padding:clamp(18px,4vw,44px)}
.eyebrow {
  color:var(--turquoise);font-size:.78rem;font-weight:750;letter-spacing:.11em;
  text-transform:uppercase
}
h1 {font-size:clamp(1.8rem,4vw,3rem);line-height:1.05;margin:.35rem 0 .6rem}
h2 {font-size:1.05rem;margin:0 0 .75rem}
.lede {color:var(--muted);max-width:760px;margin:0 0 1.4rem}
.grid {display:grid;gap:18px;grid-template-columns:minmax(240px,340px) minmax(0,1fr)}
.card {
  background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:18px;
  box-shadow:0 20px 45px rgba(0,0,0,.18)
}
ol {padding-left:1.25rem;margin:.25rem 0 1rem}
li {margin:.7rem 0;color:var(--muted)}
li strong {color:var(--ink)}
.privacy {
  border-left:3px solid var(--turquoise);padding:.15rem 0 .15rem .8rem;
  color:var(--muted);font-size:.88rem
}
.status {
  display:flex;align-items:flex-start;gap:.65rem;margin-top:1rem;padding:.75rem .85rem;
  border-radius:10px;background:#0d1726;color:var(--muted)
}
.status::before {
  content:'';width:.65rem;height:.65rem;margin-top:.32rem;border-radius:50%;
  background:var(--orange);flex:none
}
.status[data-state=connected]::before {background:var(--turquoise)}
.status[data-state=error]::before {background:var(--danger)}
.viewport {padding:0;overflow:hidden}
.viewport-head {
  display:flex;justify-content:space-between;gap:12px;align-items:center;padding:13px 16px;
  border-bottom:1px solid var(--line)
}
.viewport-head span {font-size:.82rem;color:var(--muted)}
.frame-wrap {min-height:330px;background:#eef2f7;display:grid;place-items:center}
img {
  display:block;width:100%;height:auto;max-height:68vh;object-fit:contain;
  cursor:crosshair;color:#263244
}
.controls {padding:16px;border-top:1px solid var(--line)}
label {display:block;font-size:.88rem;font-weight:700;margin-bottom:.45rem}
.send-row {display:flex;gap:8px}
input {
  min-width:0;flex:1;border:1px solid #4c5d73;border-radius:9px;background:#0d1726;
  color:var(--ink);padding:12px;font:inherit
}
button {
  border:1px solid #52647b;border-radius:9px;background:#26364b;color:var(--ink);
  font:inherit;font-weight:700;padding:10px 13px;cursor:pointer
}
button.primary {background:var(--turquoise);border-color:var(--turquoise);color:#07151b}
button:disabled {cursor:not-allowed;opacity:.45}
.keys {display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
.hint {color:var(--muted);font-size:.8rem;margin:.7rem 0 0}
@media(max-width:820px) {
  .grid {grid-template-columns:1fr}.frame-wrap {min-height:240px}
  .send-row {align-items:stretch;flex-direction:column}
}
</style></head>
<body><main><div class="eyebrow">Veetbot · isolated browser</div>
<h1>Secure website login</h1>
<p class="lede">You control this temporary browser. The Veetbot agent and public API
receive only the finished browser profile—not the username, password, passkey, or MFA
value you send directly to this isolated browser.</p>
<div class="grid"><aside class="card"><h2>How to sign in</h2><ol>
<li><strong>Click the website field in the remote browser</strong> on the right.</li>
<li>Type the matching username, password, or MFA value in the secure field below it,
then choose <strong>Send securely</strong>.</li>
<li>Use Tab, Enter, and Backspace as needed. Repeat until the website confirms you are
signed in.</li>
<li><strong>Return to Veetbot</strong> and choose <strong>Check login status</strong>.</li></ol>
<p class="privacy">Keep this tab open. This link works once and expires after five
minutes. If it was closed, copied, or reloaded, return to Veetbot and choose
<strong>Start over</strong>.</p>
<p id="status" class="status" data-state="connecting" role="status" aria-live="polite">
Connecting to the isolated browser…</p></aside>
<section class="card viewport" aria-label="Interactive remote browser">
<div class="viewport-head"><h2>Remote browser</h2>
<span>Click a field in the image first</span></div>
<div class="frame-wrap"><img id="frame"
alt="Remote browser view. Click a website field to focus it."></div>
<div class="controls"><label for="text">Send text to the focused website field</label>
<div class="send-row"><input id="text" type="password" autocomplete="off"
autocapitalize="off" spellcheck="false" placeholder="Username, password, or MFA value">
<button id="send" class="primary" type="button" disabled>Send securely</button></div>
<div class="keys"><button type="button" data-key="Tab" disabled>Tab</button>
<button type="button" data-key="Enter" disabled>Enter</button>
<button type="button" data-key="Backspace" disabled>Backspace</button></div>
<p class="hint">Text is cleared from this page immediately after it is sent.</p>
</div></section></div></main>
<script src="/authentication-surface.js"></script></body></html>"""

_AUTHENTICATION_SCRIPT = """(()=>{'use strict';const p=new URLSearchParams(location.hash.slice(1));
const capability=p.get('capability');const root=location.pathname;
const frame=document.getElementById('frame');const status=document.getElementById('status');
const input=document.getElementById('text');const send=document.getElementById('send');
const controls=[send,...document.querySelectorAll('[data-key]')];
let refreshTimer=null;let connected=false;
function setStatus(message,state){status.textContent=message;status.dataset.state=state;}
function disable(message){connected=false;controls.forEach(control=>control.disabled=true);
setStatus(message,'error');
if(refreshTimer!==null){clearInterval(refreshTimer);refreshTimer=null;}}
if(!capability){disable('This secure login link is incomplete or has expired. '
+'Return to Veetbot and choose Start over.');return;}
history.replaceState(null,'',location.pathname);
const headers={'X-Browser-Ceremony-Capability':capability};
async function event(value){const r=await fetch(root+'/events',
{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify(value)});
if(r.status===401){disable('This secure login has expired. Return to Veetbot and '
+'choose Start over.');throw new Error('expired');}
if(!r.ok)throw new Error('interaction rejected');}
async function refresh(){try{const r=await fetch(root+'/frame',{headers});
if(r.status===401){disable('This secure login has expired. Return to Veetbot and '
+'choose Start over.');return;}
if(!r.ok)throw new Error('session unavailable');const blob=await r.blob();const old=frame.src;
frame.src=URL.createObjectURL(blob);if(old)URL.revokeObjectURL(old);connected=true;
controls.forEach(control=>control.disabled=false);
setStatus('Connected — click a website field in the remote browser.','connected');}
catch(e){if(refreshTimer!==null){setStatus(
'The isolated browser is temporarily unavailable. Retrying…','connecting');}}}
frame.addEventListener('click',async e=>{
if(!connected||!frame.naturalWidth)return;const box=frame.getBoundingClientRect();
try{await event({kind:'click',x:Math.round((e.clientX-box.left)*frame.naturalWidth/box.width),
y:Math.round((e.clientY-box.top)*frame.naturalHeight/box.height)});await refresh();input.focus();}
catch(e){if(connected)setStatus('The click could not be sent. Try again.','error');}});
async function sendText(){if(!connected)return;const text=input.value;input.value='';
if(!text)return;controls.forEach(control=>control.disabled=true);
try{await event({kind:'text',text});await refresh();}
catch(e){if(connected){setStatus(
'The text could not be sent. Refocus the website field and try again.','error');}}
finally{if(connected)controls.forEach(control=>control.disabled=false);input.focus();}}
send.addEventListener('click',sendText);
input.addEventListener('keydown',e=>{
if(e.key==='Enter'){e.preventDefault();sendText();}});
document.querySelectorAll('[data-key]').forEach(button=>button.addEventListener('click',async()=>{
if(!connected)return;try{await event({kind:'key',key:button.dataset.key});await refresh();}
catch(e){if(connected)setStatus('The key could not be sent. Try again.','error');}}));
refresh();refreshTimer=setInterval(refresh,1000);})();"""


def _validate_lifecycle_request(
    request: Request,
    path_profile_id: UUID,
    body_profile_id: UUID,
    operation: str,
) -> JSONResponse | None:
    if path_profile_id != body_profile_id:
        return _error(400, "invalid_request", "request identity is invalid")
    return _validate_idempotency(request, path_profile_id, operation)
