"""Owner model settings routes (ADR-0118). Exact scopes, private responses."""

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from agent_core.application.services import ModelSettingsService
from agent_core.domain.agents import Principal
from agent_core.domain.model_settings import ModelChoice, ModelSettingsView


class UpdateModelSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0)
    chat: ModelChoice
    memory: ModelChoice


def model_settings_router(
    service: ModelSettingsService, secured: Callable[[str], object]
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/settings/models", openapi_extra={"required_scope": "settings.read"})
    async def get_model_settings(
        response: Response,
        authenticated: Annotated[Principal, secured("settings.read")],
    ) -> ModelSettingsView:
        response.headers["Cache-Control"] = "private, no-store"
        return await service.get(authenticated)

    @router.put("/v1/settings/models", openapi_extra={"required_scope": "settings.write"})
    async def update_model_settings(
        body: UpdateModelSettingsRequest,
        response: Response,
        authenticated: Annotated[Principal, secured("settings.write")],
    ) -> ModelSettingsView:
        response.headers["Cache-Control"] = "private, no-store"
        return await service.update(
            authenticated,
            expected_version=body.expected_version,
            chat=body.chat,
            memory=body.memory,
        )

    return router
