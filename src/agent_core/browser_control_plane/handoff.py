"""Device sign-in handoff: validation and the service's scope filter (ADR-0128).

The owner's client signs in to one website in its own web view and hands the
resulting cookies and ``localStorage`` to this service. The service trusts none
of it: every field is bounded and shaped here, everything outside the profile's
allowed origins is dropped, and what remains only seeds a verifying browser.

Nothing a caller sends ever appears in an error message or a representation:
validators raise fixed text, and every value and name is kept out of ``repr``.
"""

from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Collection, Iterable
from datetime import datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from publicsuffixlist import PublicSuffixList
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.browser_control_plane.runtime import MAXIMUM_PROFILE_MATERIAL_BYTES
from agent_core.domain.browser import browser_origin, normalize_browser_origin
from agent_core.domain.web import is_public_https_url

MAX_DEVICE_HANDOFF_BYTES = 1024 * 1024
MAXIMUM_HANDOFF_COOKIES = 300
MAXIMUM_COOKIE_BYTES = 4096
MAXIMUM_HANDOFF_ORIGINS = 64
MAXIMUM_STORAGE_ITEMS = 2000
# Chromium caps a cookie's lifetime at 400 days; a longer expiry is clamped.
MAXIMUM_COOKIE_LIFETIME = timedelta(days=400)
_LATEST_EXPIRY = float(2**53)

# RFC 6265 token characters: printable ASCII except separators the header uses.
_COOKIE_NAME = re.compile(r"[!#-+\--:<>-\[\]-~]{1,256}")
_COOKIE_VALUE_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f;]")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_COOKIE_DOMAIN = re.compile(rf"\.?{_LABEL}(?:\.{_LABEL})+")
_PATH_FORBIDDEN = re.compile(r"[\x00-\x1f\x7f;]")

# Loaded once at import from the list bundled with the pinned package; it never
# touches the network. Private entries (github.io and the like) count as public
# suffixes, so a cookie can never span tenants of one hosting provider.
_PUBLIC_SUFFIXES = PublicSuffixList(only_icann=False)

_FROZEN = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class HandoffCookie(BaseModel):
    """One cookie in Playwright's storage-state shape."""

    model_config = _FROZEN

    name: str = Field(repr=False)
    value: str = Field(repr=False)
    domain: str = Field(min_length=1, max_length=255, repr=False)
    path: str = Field(min_length=1, max_length=1024, repr=False)
    expires: float = Field(strict=True, repr=False)
    http_only: bool = Field(alias="httpOnly", strict=True)
    secure: bool = Field(strict=True)
    same_site: Literal["Strict", "Lax", "None"] = Field(alias="sameSite")

    @field_validator("name")
    @classmethod
    def name_is_a_token(cls, value: str) -> str:
        if _COOKIE_NAME.fullmatch(value) is None:
            raise ValueError("cookie name is invalid")
        return value

    @field_validator("value")
    @classmethod
    def value_is_plain(cls, value: str) -> str:
        if (
            len(value.encode("utf-8")) > MAXIMUM_COOKIE_BYTES
            or _COOKIE_VALUE_FORBIDDEN.search(value) is not None
        ):
            raise ValueError("cookie value is invalid")
        return value

    @field_validator("domain")
    @classmethod
    def domain_is_lowercase_ldh(cls, value: str) -> str:
        if _COOKIE_DOMAIN.fullmatch(value) is None:
            raise ValueError("cookie domain is invalid")
        return value

    @field_validator("path")
    @classmethod
    def path_is_absolute(cls, value: str) -> str:
        if not value.startswith("/") or _PATH_FORBIDDEN.search(value) is not None:
            raise ValueError("cookie path is invalid")
        return value

    @field_validator("expires")
    @classmethod
    def expiry_is_session_or_epoch_seconds(cls, value: float) -> float:
        if value != -1 and not 0 < value <= _LATEST_EXPIRY:
            raise ValueError("cookie expiry is invalid")
        return value

    @model_validator(mode="after")
    def cookie_is_consistent(self) -> HandoffCookie:
        if len(self.name.encode()) + len(self.value.encode("utf-8")) > MAXIMUM_COOKIE_BYTES:
            raise ValueError("cookie is too large")
        if self.same_site == "None" and not self.secure:
            raise ValueError("cookie is invalid")
        if self.name.startswith("__Host-") and (
            self.domain.startswith(".") or not self.secure or self.path != "/"
        ):
            raise ValueError("cookie is invalid")
        if self.name.startswith("__Secure-") and not self.secure:
            raise ValueError("cookie is invalid")
        return self

    def storage_state(self, *, expires: float) -> dict[str, Any]:
        """This cookie as Playwright stores it, with ``expires`` substituted."""

        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "expires": expires,
            "httpOnly": self.http_only,
            "secure": self.secure,
            "sameSite": self.same_site,
        }


class HandoffStorageItem(BaseModel):
    model_config = _FROZEN

    name: str = Field(max_length=1024, repr=False)
    value: str = Field(repr=False)


class HandoffOrigin(BaseModel):
    model_config = _FROZEN

    origin: str = Field(min_length=1, max_length=4096)
    local_storage: tuple[HandoffStorageItem, ...] = Field(
        alias="localStorage", max_length=MAXIMUM_STORAGE_ITEMS, repr=False
    )

    @field_validator("origin")
    @classmethod
    def origin_is_exact(cls, value: str) -> str:
        try:
            return normalize_browser_origin(value)
        except ValueError:
            raise ValueError("storage origin is invalid") from None

    @field_validator("local_storage")
    @classmethod
    def names_are_unique(
        cls, items: tuple[HandoffStorageItem, ...]
    ) -> tuple[HandoffStorageItem, ...]:
        if len({item.name for item in items}) != len(items):
            raise ValueError("storage names must be unique")
        return items


class DeviceSessionHandoff(BaseModel):
    """What the owner's client hands over: the page it confirmed and the session."""

    model_config = _FROZEN

    confirmed_url: str = Field(min_length=1, max_length=4096, repr=False)
    cookies: tuple[HandoffCookie, ...] = Field(max_length=MAXIMUM_HANDOFF_COOKIES, repr=False)
    origins: tuple[HandoffOrigin, ...] = Field(max_length=MAXIMUM_HANDOFF_ORIGINS, repr=False)

    @field_validator("confirmed_url")
    @classmethod
    def confirmed_url_is_public_https(cls, value: str) -> str:
        if not is_public_https_url(value):
            raise ValueError("confirmed page is invalid")
        return value

    @field_validator("cookies")
    @classmethod
    def cookies_are_unique(cls, cookies: tuple[HandoffCookie, ...]) -> tuple[HandoffCookie, ...]:
        if len({(item.name, item.domain, item.path) for item in cookies}) != len(cookies):
            raise ValueError("cookies must be unique")
        return cookies

    @field_validator("origins")
    @classmethod
    def origins_are_unique(cls, origins: tuple[HandoffOrigin, ...]) -> tuple[HandoffOrigin, ...]:
        if len({item.origin for item in origins}) != len(origins):
            raise ValueError("storage origins must be unique")
        return origins

    def confirmed_page(self) -> str:
        """The confirmed URL without its fragment, which the site never sees."""

        return self.confirmed_url.split("#", 1)[0]


def _numeric_host(host: str) -> bool:
    """An IP address, or a name whose last label is all digits (ADR-0128 D22)."""

    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host.rsplit(".", 1)[-1].isdigit()
    return True


def in_scope_cookie_domain(domain: str, allowed_hosts: Collection[str]) -> bool:
    """Whether a cookie for ``domain`` would reach one of ``allowed_hosts`` and no other site.

    A host-only cookie must name an allowed host exactly. A domain cookie
    (leading dot) must be an allowed host or a parent of one, and never a
    public suffix, private suffixes included.
    """

    bare = domain.removeprefix(".")
    if _numeric_host(bare):
        return False
    if not domain.startswith("."):
        return domain in allowed_hosts and not _numeric_host(domain)
    matched = [host for host in allowed_hosts if host == bare or host.endswith("." + bare)]
    if not matched or any(_numeric_host(host) for host in matched):
        return False
    return _PUBLIC_SUFFIXES.privatesuffix(bare) is not None


def confirmed_url_in_scope(handoff: DeviceSessionHandoff, allowed_origins: Collection[str]) -> bool:
    """Whether the page the owner confirmed on is on one of the profile's origins."""

    try:
        return browser_origin(handoff.confirmed_url) in allowed_origins
    except ValueError:
        return False


def _hosts(allowed_origins: Iterable[str]) -> tuple[str, ...]:
    return tuple(urlsplit(origin).hostname or "" for origin in allowed_origins)


def device_session_material(
    handoff: DeviceSessionHandoff,
    allowed_origins: Collection[str],
    now: datetime,
) -> bytes | None:
    """The in-scope part of a handoff as versioned material, or ``None`` if nothing is.

    Out-of-scope cookies and storage are dropped, not refused. An expired
    cookie is dropped, and an expiry beyond 400 days is clamped. The result
    only seeds the verifying browser; what is sealed is that browser's state.
    """

    hosts = _hosts(allowed_origins)
    now_seconds = now.timestamp()
    latest = (now + MAXIMUM_COOKIE_LIFETIME).timestamp()
    cookies: list[dict[str, Any]] = []
    for cookie in handoff.cookies:
        if not in_scope_cookie_domain(cookie.domain, hosts):
            continue
        if 0 < cookie.expires <= now_seconds:
            continue
        cookies.append(cookie.storage_state(expires=min(cookie.expires, latest)))
    origins = [
        {
            "origin": item.origin,
            "localStorage": [
                {"name": entry.name, "value": entry.value} for entry in item.local_storage
            ],
        }
        for item in handoff.origins
        if item.origin in allowed_origins and item.local_storage
    ]
    if not cookies and not origins:
        return None
    encoded = json.dumps(
        {"format_version": 1, "storage_state": {"cookies": cookies, "origins": origins}},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    if len(encoded) > MAXIMUM_PROFILE_MATERIAL_BYTES:
        raise ValueError("browser profile material exceeds its bound")
    return encoded
