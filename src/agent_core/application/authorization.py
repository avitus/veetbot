"""Entry-point-neutral exact scope authorization."""

from agent_core.domain.agents import Principal
from agent_core.domain.errors import AuthorizationError


def require_scope(principal: Principal, scope: str) -> None:
    if scope not in principal.scopes:
        raise AuthorizationError(f"missing required scope: {scope}")
