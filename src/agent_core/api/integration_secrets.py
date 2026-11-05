"""Dedicated secret ingestion; neither validation nor responses reflect input."""

from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agent_core.application.services import IntegrationSecretSettings
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import SecretValue
from agent_core.domain.errors import IntegrationSecretUnavailableError


class SecretWrite(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    label: str = Field(min_length=1, max_length=80)
    origin: str = Field(max_length=512)
    auth_scheme: Literal["bearer", "x-api-key"]
    value: SecretStr
    expected_revision: int = Field(ge=0, strict=True)


def integration_secrets_router(
    service: IntegrationSecretSettings, secured: Callable[[str], object]
) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/integration-secrets", openapi_extra={"required_scope": "settings.read"})
    async def list_secrets(
        authenticated: Annotated[Principal, secured("settings.read")],
    ) -> dict[str, object]:
        return {"secrets": await service.list(authenticated)}

    @router.put(
        "/v1/integration-secrets/{key_id}", openapi_extra={"required_scope": "settings.write"}
    )
    async def put_secret(
        key_id: UUID,
        request: Request,
        authenticated: Annotated[Principal, secured("settings.write")],
    ) -> dict[str, object]:
        try:
            body = SecretWrite.model_validate_json(await request.body())
            return await service.put(
                authenticated,
                key_id,
                label=body.label,
                origin=body.origin,
                auth_scheme=body.auth_scheme,
                value=SecretValue(body.value.get_secret_value()),
                expected_revision=body.expected_revision,
            )
        except (ValidationError, ValueError):
            raise HTTPException(400, "The integration key request is malformed.") from None
        except PermissionError:
            raise IntegrationSecretUnavailableError(
                "The integration key cannot be opened; check the server master key."
            ) from None

    @router.delete(
        "/v1/integration-secrets/{key_id}",
        status_code=204,
        openapi_extra={"required_scope": "settings.write"},
    )
    async def delete_secret(
        key_id: UUID,
        authenticated: Annotated[Principal, secured("settings.write")],
        if_match: Annotated[str, Header()],
    ) -> Response:
        if not if_match.isascii() or not if_match.isdigit() or len(if_match) > 10:
            raise HTTPException(400, "The integration key revision is malformed.")
        await service.delete(authenticated, key_id, int(if_match))
        return Response(status_code=204)

    return router
