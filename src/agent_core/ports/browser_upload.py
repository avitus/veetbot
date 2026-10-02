"""Optional image-upload capability; providers without it remain read/action compatible."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_core.domain.browser import BrowserAction, BrowserObservation, BrowserProviderError
from agent_core.domain.browser_upload import BrowserImageFile
from agent_core.ports.browser_sessions import BrowserSessionPage


@runtime_checkable
class BrowserImageUploader(Protocol):
    async def upload(
        self, action: BrowserAction, image: BrowserImageFile
    ) -> BrowserObservation: ...


@runtime_checkable
class BrowserImageSessionUploader(Protocol):
    async def upload(
        self, lease_ref: str, action: BrowserAction, image: BrowserImageFile, *, sequence: int
    ) -> BrowserSessionPage: ...


async def upload_browser_image(
    provider: object, action: BrowserAction, image: BrowserImageFile
) -> BrowserObservation:
    if not isinstance(provider, BrowserImageUploader):
        raise BrowserProviderError("tool.browser.action_not_allowed", retryable=False)
    return await provider.upload(action, image)
