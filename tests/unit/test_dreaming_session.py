"""Persistent review credentials never become general API authority."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import Request, Response
from pydantic import SecretStr
from starlette.types import Scope

from agent_core.adapters.determinism import FixedClock
from agent_core.api import dreaming_session
from agent_core.api.auth import Authenticator
from agent_core.config import AuthMode
from agent_core.domain.errors import AuthenticationError
from tests.contract.reconsolidation_surface_cases import owner
from tests.integration.m2_support import memory_settings


def scope(cookie: str, *, path: str = "/v1/dreaming", method: str = "GET", **headers: str) -> Scope:
    return {
        "type": "http",
        "scheme": "https",
        "server": ("agent.test", 443),
        "path": path,
        "method": method,
        "query_string": b"",
        "headers": [
            (k.encode(), v.encode())
            for k, v in {
                "host": "agent.test",
                "cookie": cookie,
                "x-dreaming-session": "1",
                **headers,
            }.items()
        ],
    }


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "future",
        "tampered",
        "malformed",
        "rotated",
        "owner",
        "scopes",
        "disabled",
        "http",
        "cross_site",
        "bearer",
        "renewal",
        "other_api",
        "missing_origin",
    ],
)
def test_review_cookie_refuses_changed_authority_or_invalid_request(change: str) -> None:
    settings = replace(
        memory_settings(),
        auth_mode=AuthMode.TOKEN,
        auth_token=SecretStr("synthetic-review-signing-secret"),
        memory_api_enabled=True,
        memory_reconsolidation_api_enabled=True,
        memory_dreaming_review_enabled=True,
    )
    principal = owner()
    clock = FixedClock(datetime.fromtimestamp(1800000000, UTC))
    response = Response()
    dreaming_session.issue(response, settings, principal, clock)
    cookie = response.headers["set-cookie"].split(";", 1)[0]
    request = scope(cookie)
    if change in {"expired", "future"}:
        delta = dreaming_session.MAX_AGE if change == "expired" else -1
        clock.advance(timedelta(seconds=delta))
    elif change in {"tampered", "malformed"}:
        request = scope(cookie[:-1] + ("z" if change == "tampered" else ".junk"))
    elif change == "rotated":
        settings = replace(settings, auth_token=SecretStr("rotated-synthetic-owner-secret"))
    elif change == "owner":
        principal = principal.model_copy(update={"principal_id": "another-owner"})
    elif change == "scopes":
        principal = principal.model_copy(update={"scopes": {"memory.read"}})
    elif change == "disabled":
        settings = replace(settings, memory_dreaming_review_enabled=False)
    elif change == "http":
        request["scheme"] = "http"
    elif change == "cross_site":
        request = scope(cookie, **{"sec-fetch-site": "cross-site"})
    elif change == "bearer":
        request = scope(cookie, authorization="Bearer incorrect")
    elif change == "renewal":
        request = scope(
            cookie, path="/v1/dreaming/session", method="POST", origin="https://agent.test"
        )
    elif change == "other_api":
        request = scope(cookie, path="/v1/memories")
    elif change == "missing_origin":
        request = scope(cookie, path="/v1/dreaming/schedule", method="POST")
    with pytest.raises(AuthenticationError):
        Authenticator(settings, principal, clock).authenticate(Request(request))
