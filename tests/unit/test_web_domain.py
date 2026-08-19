"""Provider-neutral web domain values: hostname, URL, and request validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_core.domain.web import (
    WebPage,
    WebProviderError,
    WebSearchRequest,
    WebSearchResult,
    is_public_https_url,
    is_web_result_url,
)


def test_query_is_trimmed_and_whitespace_is_rejected() -> None:
    assert WebSearchRequest(query="  Ada Lovelace \n").query == "Ada Lovelace"
    with pytest.raises(ValidationError):
        WebSearchRequest(query=" \t\n ")


def test_domain_filters_are_normalized_idna_hostnames() -> None:
    request = WebSearchRequest(query="Ada", include_domains=("Example.ORG.", "bücher.example"))
    assert request.include_domains == ("example.org", "xn--bcher-kva.example")


@pytest.mark.parametrize(
    "domain",
    [
        "127.0.0.1",
        "2001:db8::1",
        "localhost",
        "intranet",
        "service.internal",
        "printer.local",
        "router.lan",
        "media.home",
        "app.localhost",
        "host.123",
        "bad_label.example",
        "a" * 254 + ".example",
        "example.org/path",
    ],
)
def test_private_or_malformed_domain_filters_are_rejected(domain: str) -> None:
    with pytest.raises(ValidationError):
        WebSearchRequest(query="Ada", include_domains=(domain,))
    with pytest.raises(ValidationError):
        WebSearchRequest(query="Ada", exclude_domains=(domain,))


def test_duplicate_and_mutually_exclusive_domain_filters_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WebSearchRequest(query="Ada", include_domains=("example.org", "EXAMPLE.org"))
    with pytest.raises(ValidationError):
        WebSearchRequest(
            query="Ada",
            include_domains=("example.org",),
            exclude_domains=("example.net",),
        )


def test_result_count_and_filter_lists_are_bounded() -> None:
    assert WebSearchRequest(query="Ada").max_results == 5
    for invalid in (0, 11):
        with pytest.raises(ValidationError):
            WebSearchRequest(query="Ada", max_results=invalid)
    with pytest.raises(ValidationError):
        WebSearchRequest(
            query="Ada",
            include_domains=tuple(f"host{index}.example" for index in range(11)),
        )


@pytest.mark.parametrize(
    ("url", "public_https", "web_result"),
    [
        ("https://example.org/ada", True, True),
        ("https://example.org:443/ada", True, True),
        # Search results tolerate plain HTTP and non-standard ports; fetch does not.
        ("http://example.org/ada", False, True),
        ("https://example.org:8443/ada", False, True),
        ("ftp://example.org/ada", False, False),
        ("https://user:" + "credential@example.org/", False, False),
        ("https://127.0.0.1/", False, False),
        ("https://localhost/", False, False),
        ("https://intranet/", False, False),
        ("https://service.internal/", False, False),
        ("https://169.254.169.254/latest/meta-data/", False, False),
        ("https://10.1/", False, False),
        ("", False, False),
        ("https://example.org/" + "a" * 4096, False, False),
    ],
)
def test_url_predicates_agree_on_public_hosts_and_diverge_on_scheme_and_port(
    url: str,
    public_https: bool,
    web_result: bool,
) -> None:
    assert is_public_https_url(url) is public_https
    assert is_web_result_url(url) is web_result


def test_result_and_page_models_enforce_their_url_predicates() -> None:
    assert WebSearchResult(title="t", url="http://example.org/x", snippet="s").url == (
        "http://example.org/x"
    )
    with pytest.raises(ValidationError):
        WebSearchResult(title="t", url="https://127.0.0.1/x", snippet="s")
    assert WebPage(url="https://example.org/x", content="c").title is None
    with pytest.raises(ValidationError):
        WebPage(url="http://example.org/x", content="c")


def test_provider_error_carries_its_stable_reason_and_retryability() -> None:
    error = WebProviderError("tool.web.provider_unavailable", retryable=True)
    assert error.reason_code == "tool.web.provider_unavailable"
    assert error.retryable is True
    assert str(error) == "tool.web.provider_unavailable"
