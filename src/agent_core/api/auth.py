"""Development and static-token authentication."""

from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Callable

from fastapi import Request
from starlette.datastructures import Headers
from starlette.types import Scope

from agent_core.application.authorization import require_scope
from agent_core.config import AuthMode, Settings
from agent_core.domain.agents import Principal
from agent_core.domain.errors import AuthenticationError


class Authenticator:
    def __init__(self, settings: Settings, principal: Principal) -> None:
        self._settings = settings
        self._principal = principal

    def authenticate(self, request: Request) -> Principal:
        cached = request.scope.get("state", {}).get("authenticated_principal")
        if isinstance(cached, Principal):
            return cached.model_copy(deep=True)
        return self.authenticate_scope(request.scope)

    def authenticate_scope(self, scope: Scope) -> Principal:
        headers = Headers(scope=scope)
        if self._settings.auth_mode is AuthMode.DEV:
            forwarded = "forwarded" in headers or any(
                name.lower().startswith("x-forwarded-") for name in headers
            )
            if forwarded:
                raise AuthenticationError("authentication failed")
            client = scope.get("client")
            host = "" if client is None else client[0]
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host == "localhost"
            if not loopback:
                raise AuthenticationError("authentication failed")
            return self._principal.model_copy(deep=True)

        authorization = headers.get("authorization", "")
        scheme, separator, supplied = authorization.partition(" ")
        expected = self._settings.auth_token
        matches = (
            separator == " "
            and scheme.lower() == "bearer"
            and bool(supplied)
            and expected is not None
            and hmac.compare_digest(
                supplied.encode("utf-8", errors="surrogateescape"),
                expected.get_secret_value().encode("utf-8"),
            )
        )
        if not matches:
            raise AuthenticationError("authentication failed")
        return self._principal.model_copy(deep=True)

    def require(self, scope: str) -> Callable[[Request], Principal]:
        def dependency(request: Request) -> Principal:
            principal = self.authenticate(request)
            require_scope(principal, scope)
            return principal

        return dependency
