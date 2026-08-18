"""Provider-neutral web search and page extraction values."""

from __future__ import annotations

import ipaddress
import re
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

_HOST_LABEL = re.compile(r"^(?:xn--)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
_PRIVATE_HOST_SUFFIXES = (".internal", ".local", ".localhost", ".home", ".lan")


def _normalized_public_hostname(value: str) -> str:
    candidate = value.strip().rstrip(".").lower()
    if not candidate or len(candidate) > 253 or "/" in candidate or ":" in candidate:
        raise ValueError("domain must be a hostname")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        pass
    else:
        raise ValueError("IP literals are not public-web hostnames")
    if (
        candidate == "localhost"
        or candidate.endswith(_PRIVATE_HOST_SUFFIXES)
        or "." not in candidate
    ):
        raise ValueError("private or single-label hostnames are not allowed")
    if candidate.rsplit(".", 1)[1].isdigit():
        raise ValueError("numeric top-level labels are not public-web hostnames")
    try:
        ascii_hostname = candidate.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("domain is not a valid IDNA hostname") from exc
    if len(ascii_hostname) > 253:
        raise ValueError("IDNA hostname exceeds the DNS length limit")
    if any(_HOST_LABEL.fullmatch(label) is None for label in ascii_hostname.split(".")):
        raise ValueError("domain contains an invalid hostname label")
    return ascii_hostname


def is_public_https_url(value: str) -> bool:
    """Accept only credential-free HTTPS URLs with a public-shaped DNS hostname."""

    if not value or len(value) > 4096:
        return False
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            return False
        _normalized_public_hostname(parsed.hostname)
    except (UnicodeError, ValueError):
        return False
    return True


def is_web_result_url(value: str) -> bool:
    if not value or len(value) > 4096:
        return False
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        _normalized_public_hostname(parsed.hostname)
    except (UnicodeError, ValueError):
        return False
    return True


class WebRecency(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class WebSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)
    max_results: int = Field(default=5, ge=1, le=10)
    include_domains: tuple[str, ...] = Field(default=(), max_length=10)
    exclude_domains: tuple[str, ...] = Field(default=(), max_length=10)
    recency: WebRecency | None = None

    @field_validator("query")
    @classmethod
    def query_is_not_whitespace(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must contain non-whitespace text")
        return normalized

    @field_validator("include_domains", "exclude_domains")
    @classmethod
    def domains_are_public_hostnames(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_normalized_public_hostname(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("domain filters must not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def domain_filter_modes_are_mutually_exclusive(self) -> WebSearchRequest:
        if self.include_domains and self.exclude_domains:
            raise ValueError("include_domains and exclude_domains are mutually exclusive")
        return self


class WebSearchResult(BaseModel):
    title: str = Field(max_length=1024)
    url: str = Field(max_length=4096)
    snippet: str = Field(max_length=8192)

    @field_validator("url")
    @classmethod
    def result_url_is_public_web(cls, value: str) -> str:
        if not is_web_result_url(value):
            raise ValueError("search result URL is not a public web URL")
        return value


class WebPage(BaseModel):
    url: str = Field(max_length=4096)
    title: str | None = Field(default=None, max_length=1024)
    content: str = Field(max_length=524_288)

    @field_validator("url")
    @classmethod
    def page_url_is_public_https(cls, value: str) -> str:
        if not is_public_https_url(value):
            raise ValueError("page URL is not a public HTTPS URL")
        return value


class WebProviderError(RuntimeError):
    def __init__(self, reason_code: str, *, retryable: bool) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.retryable = retryable
