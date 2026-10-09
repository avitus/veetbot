"""The local Reactor-versus-TensorScale benchmark server (scripts/media_benchmark/README.md)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from scripts.media_benchmark.server import (
    REACTOR_MINT_URL,
    TENSORSCALE_ORIGIN,
    BenchmarkSettings,
    create_app,
    load_settings,
)

REACTOR_KEY = "rk-fake"
TS_KEY = "ts-fake"
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64
LOCAL = "http://127.0.0.1:8765"

Handler = Callable[[httpx.Request], httpx.Response]


class Upstream:
    """Records every upstream request and answers with a programmable handler."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = lambda _request: httpx.Response(500)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


def _client(
    upstream: Upstream,
    *,
    reactor: str | None = REACTOR_KEY,
    tensorscale: str | None = TS_KEY,
    max_video_bytes: int = 64 * 1024 * 1024,
) -> TestClient:
    settings = BenchmarkSettings(
        reactor_api_key=reactor,
        tensorscale_api_key=tensorscale,
        max_video_bytes=max_video_bytes,
    )
    return TestClient(create_app(settings, transport=httpx.MockTransport(upstream)), base_url=LOCAL)


@pytest.fixture
def client(upstream: Upstream) -> Iterator[TestClient]:
    with _client(upstream) as test_client:
        yield test_client


def _mp4_response(**headers: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "video/mp4", **headers}, content=MP4)


def _h3(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "model": "minimax-h3-fast",
        "prompt": "Three cats march through a bedroom playing brass instruments.",
        "duration_seconds": 5,
        "aspect_ratio": "16:9",
        "resolution": "480p",
        "seed": 7,
    }
    body.update(overrides)
    return body


def test_settings_strip_blank_keys_to_absent() -> None:
    settings = load_settings({"REACTOR_API_KEY": "  rk-x  ", "TENSORSCALE_API_KEY": "   "})

    assert settings.reactor_api_key == "rk-x"
    assert settings.tensorscale_api_key is None


def test_page_and_script_are_served(client: TestClient) -> None:
    page = client.get("/")
    script = client.get("/static/benchmark.js")

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "@reactor-team/js-sdk@3.0.2" in page.text
    assert script.status_code == 200


def test_config_reports_configuration_without_revealing_keys(upstream: Upstream) -> None:
    with _client(upstream, tensorscale=None) as client:
        response = client.get("/api/config")

    body = response.json()
    assert body["reactor"]["configured"] is True
    assert body["reactor"]["model"]["name"] == "reactor/fast-h3"
    assert body["tensorscale"]["configured"] is False
    assert [model["id"] for model in body["tensorscale"]["models"]] == [
        "minimax-h3-fast",
        "ltx-2.5-fast",
    ]
    assert REACTOR_KEY not in response.text
    assert upstream.requests == []


def test_reactor_token_is_scoped_to_one_fast_h3_session(
    client: TestClient, upstream: Upstream
) -> None:
    upstream.handler = lambda _request: httpx.Response(
        200, json={"jwt": "header.claims.sig", "expires_at": 1700003600}
    )

    response = client.post("/api/reactor/token", json={"model": "fast-h3"})

    assert response.status_code == 200
    body = response.json()
    assert body["jwt"] == "header.claims.sig"
    assert body["expires_at"] == 1700003600
    assert body["model_name"] == "reactor/fast-h3"
    assert body["mint_ms"] >= 0
    assert response.headers["cache-control"] == "no-store"
    assert REACTOR_KEY not in response.text
    [request] = upstream.requests
    assert str(request.url) == REACTOR_MINT_URL
    assert request.headers["reactor-api-key"] == REACTOR_KEY
    assert json.loads(request.content) == {
        "expires_after": 3600,
        "authorization_details": [
            {
                "type": "session",
                "resources": {"models": {"match": ["reactor/fast-h3"]}},
                "constraints": {"max_sessions": 1, "max_session_duration_seconds": 3600},
            }
        ],
    }


def test_reactor_token_refuses_an_unknown_model(client: TestClient, upstream: Upstream) -> None:
    response = client.post("/api/reactor/token", json={"model": "helios"})

    assert response.status_code == 422
    assert upstream.requests == []


def test_reactor_token_without_a_key_is_unavailable(upstream: Upstream) -> None:
    with _client(upstream, reactor=None) as client:
        response = client.post("/api/reactor/token", json={"model": "fast-h3"})

    assert response.status_code == 503
    assert "REACTOR_API_KEY" in response.json()["error"]["message"]
    assert upstream.requests == []


def test_reactor_token_failure_reports_status_without_the_key(
    client: TestClient, upstream: Upstream
) -> None:
    upstream.handler = lambda _request: httpx.Response(401, json={"error": "invalid key"})

    response = client.post("/api/reactor/token", json={"model": "fast-h3"})

    assert response.status_code == 502
    assert response.json()["error"]["upstream_status"] == 401
    assert REACTOR_KEY not in response.text


def test_h3_request_is_translated_and_timed(client: TestClient, upstream: Upstream) -> None:
    upstream.handler = lambda _request: _mp4_response(
        **{"x-tensorscale-request-id": "ts-req-1", "server-timing": "gen;dur=4100.5"}
    )

    response = client.post("/api/tensorscale/generate", json=_h3())

    assert response.status_code == 200
    assert response.headers["content-type"] == "video/mp4"
    assert response.content == MP4
    assert float(response.headers["x-bench-upstream-headers-ms"]) >= 0
    assert response.headers["x-bench-upstream-request-id"] == "ts-req-1"
    assert response.headers["x-bench-upstream-server-timing"] == "gen;dur=4100.5"
    assert response.headers["x-bench-endpoint"] == "/v2/MiniMax-H3-Fast/fl2va"
    [request] = upstream.requests
    assert str(request.url) == TENSORSCALE_ORIGIN + "/v2/MiniMax-H3-Fast/fl2va"
    assert request.headers["authorization"] == "Bearer " + TS_KEY
    assert request.headers["accept"] == "video/mp4"
    assert json.loads(request.content) == {
        "prompt": "Three cats march through a bedroom playing brass instruments.",
        "resolution": "480p",
        "aspect_ratio": "16:9",
        "duration_seconds": 5,
        "seed": 7,
    }


def test_ltx_uses_the_streaming_endpoint_and_frame_count(
    client: TestClient, upstream: Upstream
) -> None:
    upstream.handler = lambda _request: _mp4_response()

    response = client.post(
        "/api/tensorscale/generate",
        json=_h3(model="ltx-2.5-fast", duration_seconds=10, aspect_ratio="9:16", resolution="768p"),
    )

    assert response.status_code == 200
    [request] = upstream.requests
    assert str(request.url) == TENSORSCALE_ORIGIN + "/v2/ltx-2.5/fast/stream"
    assert json.loads(request.content) == {
        "prompt": "Three cats march through a bedroom playing brass instruments.",
        "width": 768,
        "height": 1280,
        "num_frames": 241,
        "frame_rate": 24,
        "seed": 7,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "sora"},
        {"prompt": "   "},
        {"prompt": "x" * 2001},
        {"duration_seconds": 3},
        {"duration_seconds": 16},
        {"resolution": "1080p"},
        {"resolution": "360p", "aspect_ratio": "1:1"},
        {"aspect_ratio": "2:1"},
        {"seed": -1},
        {"model": "ltx-2.5-fast", "resolution": "768p", "duration_seconds": 7},
        {"model": "ltx-2.5-fast", "resolution": "768p", "aspect_ratio": "1:1"},
        {"model": "ltx-2.5-fast", "resolution": "480p"},
    ],
)
def test_settings_a_model_does_not_offer_are_refused_before_spending(
    client: TestClient, upstream: Upstream, overrides: dict[str, object]
) -> None:
    response = client.post("/api/tensorscale/generate", json=_h3(**overrides))

    assert response.status_code == 422
    assert upstream.requests == []


def test_tensorscale_without_a_key_is_unavailable(upstream: Upstream) -> None:
    with _client(upstream, tensorscale=None) as client:
        response = client.post("/api/tensorscale/generate", json=_h3())

    assert response.status_code == 503
    assert "TENSORSCALE_API_KEY" in response.json()["error"]["message"]
    assert upstream.requests == []


def test_tensorscale_error_is_relayed_with_its_status(
    client: TestClient, upstream: Upstream
) -> None:
    upstream.handler = lambda _request: httpx.Response(
        403,
        json={"error": {"code": "forbidden", "message": "key lacks scope MiniMax-H3-Fast"}},
        headers={"x-tensorscale-request-id": "ts-req-2"},
    )

    response = client.post("/api/tensorscale/generate", json=_h3())

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["upstream_status"] == 403
    assert "key lacks scope MiniMax-H3-Fast" in error["message"]
    assert error["request_id"] == "ts-req-2"
    assert TS_KEY not in response.text


def test_tensorscale_success_that_is_not_mp4_is_refused(
    client: TestClient, upstream: Upstream
) -> None:
    upstream.handler = lambda _request: httpx.Response(
        200, headers={"content-type": "text/html"}, content=b"<html></html>"
    )

    response = client.post("/api/tensorscale/generate", json=_h3())

    assert response.status_code == 502
    assert "video/mp4" in response.json()["error"]["message"]


def test_tensorscale_declared_oversize_is_refused(upstream: Upstream) -> None:
    upstream.handler = lambda _request: _mp4_response(**{"content-length": str(len(MP4))})

    with _client(upstream, max_video_bytes=len(MP4) - 1) as client:
        response = client.post("/api/tensorscale/generate", json=_h3())

    assert response.status_code == 502
    assert "limit" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("path", "body"),
    [("/api/reactor/token", {"model": "fast-h3"}), ("/api/tensorscale/generate", _h3())],
)
def test_cross_origin_posts_are_refused(
    client: TestClient, upstream: Upstream, path: str, body: dict[str, object]
) -> None:
    response = client.post(path, json=body, headers={"origin": "https://evil.example"})

    assert response.status_code == 403
    assert upstream.requests == []


def test_same_origin_post_is_accepted(client: TestClient, upstream: Upstream) -> None:
    upstream.handler = lambda _request: httpx.Response(200, json={"jwt": "a.b.c", "expires_at": 1})

    response = client.post(
        "/api/reactor/token", json={"model": "fast-h3"}, headers={"origin": LOCAL}
    )

    assert response.status_code == 200


def test_a_rebound_host_name_is_refused(client: TestClient, upstream: Upstream) -> None:
    response = client.get("/api/config", headers={"host": "attacker.example"})

    assert response.status_code == 400
    assert upstream.requests == []
