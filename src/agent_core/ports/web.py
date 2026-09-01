"""Provider-neutral web access port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_core.domain.web import WebPage, WebSearchRequest, WebSearchResult


class WebProvider(Protocol):
    name: str

    async def search(self, request: WebSearchRequest) -> tuple[WebSearchResult, ...]: ...

    async def fetch(self, url: str) -> WebPage: ...

    async def close(self) -> None: ...


@runtime_checkable
class WebProviderRouter(Protocol):
    def select(self, *, routing_key: str) -> WebProvider: ...
