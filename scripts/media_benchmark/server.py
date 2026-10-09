"""Serve the page that runs one prompt through Reactor and TensorScale side by side.

``make media-benchmark`` starts it on loopback (scripts/media_benchmark/README.md). The
server holds both API keys: it mints a Reactor token scoped to one ``fast-h3``
session and proxies TensorScale generations so the page can time them. Neither
key reaches the browser, and the page itself never chooses a provider URL.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
import uvicorn
from dotenv import dotenv_values
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / "static"
LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765

REACTOR_MINT_URL = "https://api.reactor.inc/tokens"
REACTOR_MODEL_NAME = "reactor/fast-h3"
REACTOR_SESSION_SECONDS = 3600
REACTOR_ASPECTS = ("16:9", "9:16", "1:1", "4:3")

TENSORSCALE_ORIGIN = "https://api.tensorscale.io"
TENSORSCALE_TIMEOUT = httpx.Timeout(1800.0, connect=15.0)
ERROR_BODY_BYTES = 16 * 1024
MAX_PROMPT_CHARS = 2000


@dataclass(frozen=True)
class TensorScaleModel:
    """One TensorScale endpoint and the settings it accepts.

    Endpoints, canvases, durations and prices follow https://tensorscale.io/docs.html,
    checked 2026-10-09.
    """

    id: str
    label: str
    endpoint: str
    like_for_like: bool
    streaming: bool
    durations: tuple[int, ...]
    resolutions: Mapping[str, tuple[str, ...]]
    default_resolution: str
    usd_per_second: Mapping[str, float]

    def public(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "endpoint": self.endpoint,
            "like_for_like": self.like_for_like,
            "streaming": self.streaming,
            "durations": list(self.durations),
            "resolutions": {tier: list(aspects) for tier, aspects in self.resolutions.items()},
            "default_resolution": self.default_resolution,
            "usd_per_second": dict(self.usd_per_second),
        }


_H3_SHAPES = ("16:9", "9:16", "1:1", "4:3")
_LTX_CANVAS = {
    ("768p", "16:9"): (1280, 768),
    ("768p", "9:16"): (768, 1280),
    ("1080p", "16:9"): (1920, 1088),
    ("1080p", "9:16"): (1088, 1920),
}
TENSORSCALE_MODELS: dict[str, TensorScaleModel] = {
    model.id: model
    for model in (
        TensorScaleModel(
            id="minimax-h3-fast",
            label="MiniMax H3 Fast (FL2VA)",
            endpoint="/v2/MiniMax-H3-Fast/fl2va",
            like_for_like=True,
            streaming=False,
            durations=tuple(range(4, 16)),
            resolutions={"360p": ("16:9", "9:16"), "480p": _H3_SHAPES, "768p": _H3_SHAPES},
            default_resolution="480p",
            usd_per_second={"360p": 0.025, "480p": 0.0425, "768p": 0.0425},
        ),
        TensorScaleModel(
            id="ltx-2.5-fast",
            label="LTX-2.5 Fast (streaming)",
            endpoint="/v2/ltx-2.5/fast/stream",
            like_for_like=False,
            streaming=True,
            durations=(5, 10),
            resolutions={"768p": ("16:9", "9:16"), "1080p": ("16:9", "9:16")},
            default_resolution="768p",
            usd_per_second={"768p": 0.04, "1080p": 0.04},
        ),
    )
}


@dataclass(frozen=True)
class BenchmarkSettings:
    reactor_api_key: str | None
    tensorscale_api_key: str | None
    max_video_bytes: int = 256 * 1024 * 1024


def _key(values: Mapping[str, str], name: str) -> str | None:
    value = values.get(name, "").strip()
    return value or None


def load_settings(environ: Mapping[str, str] | None = None) -> BenchmarkSettings:
    """Read both keys the way the application does: the process overrides ``.env``."""

    if environ is None:
        values = {key: value or "" for key, value in dotenv_values(ROOT / ".env").items()}
        values.update(os.environ)
    else:
        values = dict(environ)
    return BenchmarkSettings(
        reactor_api_key=_key(values, "REACTOR_API_KEY"),
        tensorscale_api_key=_key(values, "TENSORSCALE_API_KEY"),
    )


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: Literal["fast-h3"]


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    model: str
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    duration_seconds: int
    aspect_ratio: str
    resolution: str
    seed: int = Field(ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def _offered_by_model(self) -> GenerateRequest:
        spec = TENSORSCALE_MODELS.get(self.model)
        if spec is None:
            raise ValueError(f"unknown TensorScale model {self.model!r}")
        if self.duration_seconds not in spec.durations:
            raise ValueError(f"{spec.label} offers durations {list(spec.durations)} seconds")
        aspects = spec.resolutions.get(self.resolution)
        if aspects is None:
            raise ValueError(f"{spec.label} offers resolutions {list(spec.resolutions)}")
        if self.aspect_ratio not in aspects:
            raise ValueError(f"{spec.label} at {self.resolution} offers aspects {list(aspects)}")
        return self

    def payload(self) -> dict[str, object]:
        if self.model == "ltx-2.5-fast":
            width, height = _LTX_CANVAS[(self.resolution, self.aspect_ratio)]
            return {
                "prompt": self.prompt,
                "width": width,
                "height": height,
                "num_frames": self.duration_seconds * 24 + 1,
                "frame_rate": 24,
                "seed": self.seed,
            }
        return {
            "prompt": self.prompt,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "duration_seconds": self.duration_seconds,
            "seed": self.seed,
        }


def _error(status: int, message: str, **extra: object) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, **extra}},
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def _upstream_message(raw: bytes, secret: str) -> str:
    """Pick a provider's human-readable error, never echoing our own key."""

    message = raw.decode("utf-8", "replace").strip()
    try:
        data: object = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        nested = error if isinstance(error, dict) else {}
        for candidate in (
            nested.get("message"),
            data.get("message"),
            data.get("detail"),
            error if isinstance(error, str) else None,
            nested.get("code"),
        ):
            if isinstance(candidate, str) and candidate.strip():
                message = candidate.strip()
                break
    return (message[:500] or "no error body").replace(secret, "[redacted]")


async def _read_capped(response: httpx.Response, limit: int) -> bytes:
    body = b""
    async for chunk in response.aiter_bytes():
        body += chunk
        if len(body) >= limit:
            break
    return body[:limit]


def _require_same_origin(request: Request) -> None:
    """Refuse a browser POST from any other site, so no page can spend on our keys."""

    origin = request.headers.get("origin")
    if origin is not None and origin != f"{request.url.scheme}://{request.headers['host']}":
        raise HTTPException(status_code=403, detail="cross-origin request refused")


class _SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                message["headers"].extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                    ]
                )
            await send(message)

        await self._app(scope, receive, send_with_headers)


def create_app(
    settings: BenchmarkSettings, *, transport: httpx.AsyncBaseTransport | None = None
) -> FastAPI:
    client = httpx.AsyncClient(transport=transport, follow_redirects=False)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(_SecurityHeaders)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[LOOPBACK, "localhost"])
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request: Request, exc: RequestValidationError) -> JSONResponse:
        messages = [str(error.get("msg", "invalid request")) for error in exc.errors()]
        return _error(422, "; ".join(messages) or "invalid request")

    @app.exception_handler(HTTPException)
    async def _refused(_request: Request, exc: HTTPException) -> JSONResponse:
        return _error(exc.status_code, str(exc.detail))

    @app.get("/")
    async def page() -> FileResponse:
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/config")
    async def config() -> JSONResponse:
        return JSONResponse(
            {
                "reactor": {
                    "configured": settings.reactor_api_key is not None,
                    "model": {
                        "id": "fast-h3",
                        "name": REACTOR_MODEL_NAME,
                        "label": "MiniMax H3 Fast (real-time)",
                        "aspects": list(REACTOR_ASPECTS),
                        "min_seconds": 5.167,
                        "max_seconds": 14.375,
                    },
                },
                "tensorscale": {
                    "configured": settings.tensorscale_api_key is not None,
                    "models": [model.public() for model in TENSORSCALE_MODELS.values()],
                },
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/reactor/token", dependencies=[Depends(_require_same_origin)])
    async def reactor_token(_body: TokenRequest) -> Response:
        key = settings.reactor_api_key
        if key is None:
            return _error(503, "REACTOR_API_KEY is not set; run make env-pull or export it")
        started = time.perf_counter()
        try:
            response = await client.post(
                REACTOR_MINT_URL,
                headers={"Reactor-API-Key": key},
                json={
                    "expires_after": REACTOR_SESSION_SECONDS,
                    "authorization_details": [
                        {
                            "type": "session",
                            "resources": {"models": {"match": [REACTOR_MODEL_NAME]}},
                            "constraints": {
                                "max_sessions": 1,
                                "max_session_duration_seconds": REACTOR_SESSION_SECONDS,
                            },
                        }
                    ],
                },
                timeout=15.0,
            )
        except httpx.HTTPError as exc:
            return _error(502, f"Reactor token request failed: {type(exc).__name__}")
        mint_ms = (time.perf_counter() - started) * 1000
        if response.status_code != 200:
            return _error(
                502,
                "Reactor refused the token request: "
                + _upstream_message(response.content[:ERROR_BODY_BYTES], key),
                upstream_status=response.status_code,
            )
        try:
            data: object = response.json()
        except ValueError:
            data = None
        jwt = data.get("jwt") if isinstance(data, dict) else None
        if not isinstance(jwt, str) or not jwt:
            return _error(502, "Reactor answered without a session token")
        expires_at = data.get("expires_at") if isinstance(data, dict) else None
        return JSONResponse(
            {
                "jwt": jwt,
                "expires_at": expires_at,
                "model_name": REACTOR_MODEL_NAME,
                "mint_ms": round(mint_ms, 1),
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/tensorscale/generate", dependencies=[Depends(_require_same_origin)])
    async def tensorscale_generate(body: GenerateRequest) -> Response:
        key = settings.tensorscale_api_key
        if key is None:
            return _error(503, "TENSORSCALE_API_KEY is not set; run make env-pull or export it")
        spec = TENSORSCALE_MODELS[body.model]
        request = client.build_request(
            "POST",
            TENSORSCALE_ORIGIN + spec.endpoint,
            json=body.payload(),
            headers={
                "Authorization": "Bearer " + key,
                "Accept": "video/mp4",
                "Accept-Encoding": "identity",
            },
            timeout=TENSORSCALE_TIMEOUT,
        )
        started = time.perf_counter()
        try:
            upstream = await client.send(request, stream=True)
        except httpx.TimeoutException:
            return _error(504, "TensorScale did not answer within the 30-minute limit")
        except httpx.HTTPError as exc:
            return _error(502, f"TensorScale request failed: {type(exc).__name__}")
        headers_ms = (time.perf_counter() - started) * 1000
        request_id = upstream.headers.get("x-tensorscale-request-id") or upstream.headers.get(
            "x-request-id"
        )
        if upstream.status_code != 200:
            raw = await _read_capped(upstream, ERROR_BODY_BYTES)
            await upstream.aclose()
            return _error(
                502,
                f"TensorScale returned {upstream.status_code}: {_upstream_message(raw, key)}",
                upstream_status=upstream.status_code,
                request_id=request_id,
            )
        content_type = upstream.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "video/mp4":
            await upstream.aclose()
            return _error(502, f"TensorScale answered {content_type or 'nothing'}, not video/mp4")
        declared: int | None = None
        if upstream.headers.get("content-encoding", "identity").lower() == "identity":
            try:
                declared = int(upstream.headers["content-length"])
            except (KeyError, ValueError):
                declared = None
        if declared is not None and declared > settings.max_video_bytes:
            await upstream.aclose()
            return _error(
                502, f"TensorScale video exceeds the {settings.max_video_bytes}-byte limit"
            )

        async def relay() -> AsyncIterator[bytes]:
            total = 0
            try:
                async for chunk in upstream.aiter_bytes():
                    total += len(chunk)
                    if total > settings.max_video_bytes:
                        raise RuntimeError("TensorScale video exceeded the byte limit")
                    yield chunk
            finally:
                await upstream.aclose()

        headers = {
            "Cache-Control": "no-store",
            "X-Bench-Endpoint": spec.endpoint,
            "X-Bench-Upstream-Headers-Ms": f"{headers_ms:.1f}",
        }
        if declared is not None:
            headers["Content-Length"] = str(declared)
        if request_id:
            headers["X-Bench-Upstream-Request-Id"] = request_id
        server_timing = upstream.headers.get("server-timing")
        if server_timing:
            headers["X-Bench-Upstream-Server-Timing"] = server_timing
        return StreamingResponse(relay(), media_type="video/mp4", headers=headers)

    return app


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Benchmark Reactor against TensorScale.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    settings = load_settings()
    for name, value in (
        ("REACTOR_API_KEY", settings.reactor_api_key),
        ("TENSORSCALE_API_KEY", settings.tensorscale_api_key),
    ):
        print(f"{name}: {'configured' if value else 'missing'}")
    print(f"Open http://{LOOPBACK}:{args.port}/")
    uvicorn.run(create_app(settings), host=LOOPBACK, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
