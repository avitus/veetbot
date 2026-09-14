"""Dedicated provider ingress; this app never accepts owner runs or reads call content."""

import asyncio

from fastapi import FastAPI, Request, Response

from agent_core.application.services import CallIngressService
from agent_core.domain.calls import MAX_CALLBACK_BYTES
from agent_core.domain.errors import ConflictError


def create_call_ingress(service: CallIngressService, secret: str) -> FastAPI:
    if not secret:
        raise ValueError("calling webhook secret is required")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/webhooks/bland")
    async def receive(request: Request) -> Response:
        body = bytearray()
        try:
            async with asyncio.timeout(10):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > MAX_CALLBACK_BYTES:
                        return Response(status_code=413)
                    body.extend(chunk)
        except TimeoutError:
            return Response(status_code=408)
        try:
            accepted = await service.receive(
                bytes(body), request.headers.get("X-Webhook-Signature", ""), secret
            )
        except ConflictError:
            return Response(status_code=429, headers={"Retry-After": "60"})
        return Response(status_code=202 if accepted else 401, headers={"Cache-Control": "no-store"})

    return app
