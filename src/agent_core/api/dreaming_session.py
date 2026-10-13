"""Purpose-bound, expiring review cookies; the owner bearer never reaches storage."""

import hashlib
import hmac
import json
import re
import secrets
from http.cookies import CookieError, SimpleCookie

from fastapi import Request, Response
from starlette.datastructures import URL, Headers
from starlette.types import Scope

from agent_core.config import Settings
from agent_core.domain.agents import Principal
from agent_core.domain.errors import AuthenticationError
from agent_core.ports.determinism import Clock

COOKIE = "__Host-veetbot-dreaming"
MAX_AGE = 30 * 24 * 60 * 60
_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_READ = re.compile(rf"/v1/(?:dreaming|memory-reconsolidations(?:/{_UUID})?)")
_WRITE = re.compile(
    rf"/v1/(?:dreaming/(?:schedule|{_UUID}/decision)|memory-reconsolidations/{_UUID}/undo)"
)


def browser_request(scope: Scope) -> bool:
    """A custom header prevents simple cross-origin requests; writes also bind origin."""
    headers = Headers(scope=scope)
    url = URL(scope=scope)
    origin = f"{url.scheme}://{url.netloc}"
    return (
        url.scheme == "https"
        and headers.get("x-dreaming-session") == "1"
        and headers.get("sec-fetch-site", "same-origin") == "same-origin"
        and (scope["method"] == "GET" or headers.get("origin") == origin)
    )


def _signature(settings: Settings, principal: Principal, payload: str) -> str:
    assert settings.auth_token is not None
    identity = principal.model_dump(mode="json")
    identity["scopes"] = sorted(principal.scopes)
    identity["roles"] = sorted(principal.roles)
    message = json.dumps(["dreaming-session@1", identity, payload], sort_keys=True)
    return hmac.new(
        settings.auth_token.get_secret_value().encode(), message.encode(), hashlib.sha256
    ).hexdigest()


def issue(response: Response, settings: Settings, principal: Principal, clock: Clock) -> None:
    payload = f"{int(clock.now().timestamp())}.{secrets.token_hex(16)}"
    response.set_cookie(
        COOKIE,
        f"{payload}.{_signature(settings, principal, payload)}",
        max_age=MAX_AGE,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )


def authenticate(scope: Scope, settings: Settings, principal: Principal, clock: Clock) -> Principal:
    path, method = scope["path"], scope["method"]
    allowed = (
        (method == "GET" and _READ.fullmatch(path))
        or (method == "POST" and _WRITE.fullmatch(path))
        or (method == "DELETE" and path == "/v1/dreaming/session")
    )
    if not (
        settings.memory_api_enabled
        and settings.memory_reconsolidation_api_enabled
        and settings.memory_dreaming_review_enabled
        and settings.auth_token is not None
        and allowed
        and browser_request(scope)
    ):
        raise AuthenticationError("authentication failed")
    try:
        cookies = SimpleCookie()
        cookies.load(Headers(scope=scope).get("cookie", ""))
        value = cookies[COOKIE].value
        if not re.fullmatch(r"[0-9]{1,12}\.[a-f0-9]{32}\.[a-f0-9]{64}", value):
            raise ValueError
        issued, nonce, signature = value.split(".")
        age = clock.now().timestamp() - int(issued)
        if not 0 <= age < MAX_AGE or not hmac.compare_digest(
            signature, _signature(settings, principal, f"{issued}.{nonce}")
        ):
            raise ValueError
    except (CookieError, KeyError, ValueError):
        raise AuthenticationError("authentication failed") from None
    return principal.model_copy(
        update={"scopes": principal.scopes & {"memory.read", "memory.write"}}, deep=True
    )


def require_browser(request: Request) -> None:
    if not browser_request(request.scope):
        raise AuthenticationError("authentication failed")
