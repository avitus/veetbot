"""Provider-neutral rendered-browser port."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, cast

from agent_core.domain.browser import BrowserAction, BrowserActionContext, BrowserObservation
from agent_core.domain.tools import ToolExecutionContext


class BrowserProvider(Protocol):
    """A trusted-composition binding to one principal and browser profile."""

    name: str

    def allows(self, url: str) -> bool: ...

    async def navigate(self, url: str) -> BrowserObservation: ...

    async def observe(self) -> BrowserObservation: ...

    async def act(self, action: BrowserAction) -> BrowserObservation: ...

    async def close(self) -> None: ...


async def bind_browser_execution(
    provider: BrowserProvider,
    context: ToolExecutionContext,
) -> None:
    """Bind execution scope when a hosted provider requires a lease."""

    candidate = getattr(provider, "bind_execution", None)
    if candidate is None:
        return
    binder = cast(Callable[[ToolExecutionContext], Awaitable[None]], candidate)
    await binder(context)


async def browser_action_context(
    provider: BrowserProvider,
    action: BrowserAction,
) -> BrowserActionContext | None:
    candidate = getattr(provider, "action_context", None)
    if candidate is None:
        return None
    resolver = cast(Callable[[BrowserAction], Awaitable[BrowserActionContext]], candidate)
    return await resolver(action)
