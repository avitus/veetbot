"""Device sign-in handoff validation and the service's scope filter (ADR-0128)."""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from agent_core.browser_control_plane import handoff as handoff_module
from agent_core.browser_control_plane.handoff import (
    DeviceSessionHandoff,
    confirmed_url_in_scope,
    device_session_material,
    in_scope_cookie_domain,
)

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
ALLOWED = ("https://www.example.org", "https://owner.github.io")
HOSTS = ("www.example.org", "owner.github.io")
SENTINEL_VALUE = "handoff-sentinel-cookie-value"


def cookie(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "session",
        "value": SENTINEL_VALUE,
        "domain": ".example.org",
        "path": "/",
        "expires": NOW.timestamp() + 3600,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    base.update(overrides)
    return base


def payload(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "confirmed_url": "https://www.example.org/learn#section",
        "cookies": [cookie()],
        "origins": [
            {
                "origin": "https://www.example.org",
                "localStorage": [{"name": "state", "value": "stored"}],
            }
        ],
    }
    base.update(overrides)
    return base


def test_a_well_formed_handoff_parses_with_playwright_field_names() -> None:
    handoff = DeviceSessionHandoff.model_validate(payload())

    assert handoff.cookies[0].http_only is True
    assert handoff.cookies[0].same_site == "Lax"
    assert handoff.origins[0].local_storage[0].name == "state"
    assert SENTINEL_VALUE not in repr(handoff)
    assert "learn" not in repr(handoff)


def _with_extra_field(level: str) -> dict[str, Any]:
    body = payload()
    if level == "top":
        body["extra"] = True
    elif level == "cookie":
        body["cookies"][0]["extra"] = True
    elif level == "origin":
        body["origins"][0]["extra"] = True
    else:
        body["origins"][0]["localStorage"][0]["extra"] = True
    return body


INVALID_PAYLOADS: list[tuple[str, dict[str, Any]]] = [
    *(
        (f"extra field at {level}", _with_extra_field(level))
        for level in (
            "top",
            "cookie",
            "origin",
            "storage item",
        )
    ),
    ("control character in a value", payload(cookies=[cookie(value="a\x01b")])),
    ("semicolon in a value", payload(cookies=[cookie(value="a;b")])),
    ("DEL in a value", payload(cookies=[cookie(value="a\x7fb")])),
    ("space in a name", payload(cookies=[cookie(name="a b")])),
    ("equals in a name", payload(cookies=[cookie(name="a=b")])),
    ("empty name", payload(cookies=[cookie(name="")])),
    ("name and value over 4096 bytes", payload(cookies=[cookie(name="n", value="v" * 4096)])),
    ("multibyte value over 4096 bytes", payload(cookies=[cookie(value="é" * 2048)])),
    (
        "301 cookies",
        payload(cookies=[cookie(name=f"c{index}") for index in range(301)]),
    ),
    ("duplicate cookie key", payload(cookies=[cookie(), cookie(value="other")])),
    ("SameSite None without Secure", payload(cookies=[cookie(sameSite="None", secure=False)])),
    ("unknown SameSite", payload(cookies=[cookie(sameSite="lax")])),
    ("__Host- with a domain", payload(cookies=[cookie(name="__Host-id")])),
    (
        "__Host- with a path",
        payload(cookies=[cookie(name="__Host-id", domain="www.example.org", path="/app")]),
    ),
    (
        "__Host- without Secure",
        payload(cookies=[cookie(name="__Host-id", domain="www.example.org", secure=False)]),
    ),
    ("__Secure- without Secure", payload(cookies=[cookie(name="__Secure-id", secure=False)])),
    ("two leading dots", payload(cookies=[cookie(domain="..example.org")])),
    ("trailing dot", payload(cookies=[cookie(domain="example.org.")])),
    ("uppercase domain", payload(cookies=[cookie(domain=".Example.org")])),
    ("single-label domain", payload(cookies=[cookie(domain="localhost")])),
    ("dotted top-level domain", payload(cookies=[cookie(domain=".org")])),
    ("underscore in a domain", payload(cookies=[cookie(domain="a_b.example.org")])),
    ("path without a slash", payload(cookies=[cookie(path="app")])),
    ("semicolon in a path", payload(cookies=[cookie(path="/a;b")])),
    ("expires as a string", payload(cookies=[cookie(expires="1790000000")])),
    ("expires zero", payload(cookies=[cookie(expires=0)])),
    ("expires past 2^53", payload(cookies=[cookie(expires=2.0**53 + 2)])),
    ("httpOnly as a string", payload(cookies=[cookie(httpOnly="true")])),
    ("confirmed_url over http", payload(confirmed_url="http://www.example.org/learn")),
    ("confirmed_url on an address", payload(confirmed_url="https://127.0.0.1/learn")),
    ("empty confirmed_url", payload(confirmed_url="")),
    (
        "65 origins",
        payload(
            origins=[
                {"origin": f"https://o{index}.example.org", "localStorage": []}
                for index in range(65)
            ]
        ),
    ),
    (
        "2001 storage items",
        payload(
            origins=[
                {
                    "origin": "https://www.example.org",
                    "localStorage": [{"name": f"k{index}", "value": ""} for index in range(2001)],
                }
            ]
        ),
    ),
    (
        "duplicate storage name",
        payload(
            origins=[
                {
                    "origin": "https://www.example.org",
                    "localStorage": [{"name": "k", "value": "1"}, {"name": "k", "value": "2"}],
                }
            ]
        ),
    ),
    (
        "duplicate origin",
        payload(
            origins=[
                {"origin": "https://www.example.org", "localStorage": []},
                {"origin": "https://WWW.example.org", "localStorage": []},
            ]
        ),
    ),
    (
        "origin with a path",
        payload(origins=[{"origin": "https://www.example.org/app", "localStorage": []}]),
    ),
]


@pytest.mark.parametrize(
    "body", [body for _case, body in INVALID_PAYLOADS], ids=[case for case, _ in INVALID_PAYLOADS]
)
def test_the_handoff_contract_refuses_malformed_bodies_with_fixed_text(
    body: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError) as raised:
        DeviceSessionHandoff.model_validate(body)

    for error in raised.value.errors():
        assert SENTINEL_VALUE not in str(error.get("msg", ""))


@pytest.mark.parametrize(
    "overrides",
    [
        {"expires": -1},
        {"expires": 1790000000},
        {"expires": 1790000000.25},
        {"sameSite": "None", "secure": True},
        {"sameSite": "Strict"},
        {"name": "__Host-id", "domain": "www.example.org", "path": "/"},
        {"name": "__Secure-id"},
        {"value": ""},
        {"domain": ".1.2.3.4"},
    ],
)
def test_the_handoff_contract_accepts_every_valid_cookie_shape(overrides: dict[str, Any]) -> None:
    DeviceSessionHandoff.model_validate(payload(cookies=[cookie(**overrides)]))


def test_the_confirmed_page_must_be_on_an_allowed_origin() -> None:
    inside = DeviceSessionHandoff.model_validate(payload())
    outside = DeviceSessionHandoff.model_validate(payload(confirmed_url="https://example.net/a"))
    bare = DeviceSessionHandoff.model_validate(payload(confirmed_url="https://example.org/learn"))

    assert confirmed_url_in_scope(inside, ALLOWED) is True
    assert confirmed_url_in_scope(outside, ALLOWED) is False
    assert confirmed_url_in_scope(bare, ALLOWED) is False


@pytest.mark.parametrize(
    ("domain", "kept"),
    [
        ("www.example.org", True),
        (".www.example.org", True),
        (".example.org", True),
        ("owner.github.io", True),
        (".owner.github.io", True),
        (".org", False),
        (".github.io", False),
        ("api.www.example.org", False),
        (".api.www.example.org", False),
        ("other.example.org", False),
        ("example.org", False),
        (".evil.example.net", False),
        (".1.2.3.4", False),
        ("1.2.3.4", False),
        (".example.123", False),
    ],
)
def test_the_scope_filter_keeps_only_cookies_for_the_allowed_hosts(domain: str, kept: bool) -> None:
    assert in_scope_cookie_domain(domain, HOSTS) is kept


def test_the_filter_never_depends_on_hosts_being_names() -> None:
    """An address or numeric allowed host is refused even if one ever gets in (S7)."""

    assert in_scope_cookie_domain("1.2.3.4", ("1.2.3.4",)) is False
    assert in_scope_cookie_domain(".2.3.4", ("1.2.3.4",)) is False
    assert in_scope_cookie_domain("site.123", ("site.123",)) is False


def _material(handoff: dict[str, Any]) -> dict[str, Any] | None:
    raw = device_session_material(DeviceSessionHandoff.model_validate(handoff), ALLOWED, NOW)
    if raw is None:
        return None
    decoded: dict[str, Any] = json.loads(raw)
    return decoded


def test_material_keeps_in_scope_cookies_and_storage_and_drops_the_rest() -> None:
    ten_years = NOW.timestamp() + 10 * 365 * 86_400
    material = _material(
        payload(
            cookies=[
                cookie(name="kept"),
                cookie(name="session-cookie", expires=-1),
                cookie(name="fractional", expires=NOW.timestamp() + 60.5),
                cookie(name="long", expires=ten_years),
                cookie(name="expired", expires=NOW.timestamp() - 1),
                cookie(name="at-now", expires=NOW.timestamp()),
                cookie(name="foreign", domain=".example.net"),
                cookie(name="private-suffix", domain=".github.io"),
                cookie(name="sibling", domain="other.example.org"),
            ],
            origins=[
                {
                    "origin": "https://www.example.org",
                    "localStorage": [{"name": "state", "value": "stored"}],
                },
                {
                    "origin": "https://tracker.example.net",
                    "localStorage": [{"name": "foreign", "value": "dropped"}],
                },
            ],
        )
    )

    assert material is not None
    assert material["format_version"] == 1
    state = material["storage_state"]
    by_name = {item["name"]: item for item in state["cookies"]}
    assert set(by_name) == {"kept", "session-cookie", "fractional", "long"}
    assert by_name["session-cookie"]["expires"] == -1
    assert by_name["fractional"]["expires"] == NOW.timestamp() + 60.5
    assert by_name["long"]["expires"] == (NOW + timedelta(days=400)).timestamp()
    assert by_name["kept"] == {
        "name": "kept",
        "value": SENTINEL_VALUE,
        "domain": ".example.org",
        "path": "/",
        "expires": NOW.timestamp() + 3600,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }
    assert state["origins"] == [
        {
            "origin": "https://www.example.org",
            "localStorage": [{"name": "state", "value": "stored"}],
        }
    ]


def test_material_is_none_when_nothing_is_in_scope() -> None:
    assert (
        _material(
            payload(
                cookies=[cookie(domain=".example.net")],
                origins=[
                    {
                        "origin": "https://tracker.example.net",
                        "localStorage": [{"name": "k", "value": "v"}],
                    },
                    {"origin": "https://www.example.org", "localStorage": []},
                ],
            )
        )
        is None
    )


def test_storage_alone_is_a_session_worth_verifying() -> None:
    material = _material(payload(cookies=[]))

    assert material is not None
    assert material["storage_state"]["cookies"] == []


def test_only_the_service_handoff_filter_imports_the_public_suffix_list() -> None:
    """The dependency is the isolated service's alone (ADR-0128)."""

    source_root = Path(handoff_module.__file__).resolve().parents[1]
    importers = set()
    for path in source_root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            if any(name.split(".")[0] == "publicsuffixlist" for name in names):
                importers.add(path.relative_to(source_root).as_posix())

    assert importers == {"browser_control_plane/handoff.py"}
