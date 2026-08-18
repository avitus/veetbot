"""Provider-neutral web access port."""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.web import WebPage, WebSearchRequest, WebSearchResult


class WebProvider(Protocol):
    name: str

    async def search(self, request: WebSearchRequest) -> tuple[WebSearchResult, ...]: ...

    async def fetch(self, url: str) -> WebPage: ...

    async def close(self) -> None: ...
