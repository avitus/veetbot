"""The sign-in ceremony proves the grant against the API before it writes it (ADR-0153)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import stat
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
import pytest

import svp_mcp.__main__ as svp_main
from svp_mcp.bootstrap import BootstrapError, bootstrap_credential
from svp_mcp.constants import (
    DOCUMENT_URL,
    LOOPBACK_REDIRECT_URI,
    REGISTRATION_ENDPOINT,
    TOKEN_ENDPOINT,
)
from svp_mcp.credential import read_state

NOW = 1_800_000_000.0
SECRET = "-".join(("client", "value"))
DOCUMENT: dict[str, Any] = {
    "paths": {"/v1/a": {"get": {}}, "/v1/b": {"get": {}, "post": {}}, "/v1/c": {"post": {}}}
}


class Service:
    def __init__(
        self,
        *,
        refresh_token: str | None = "refresh-1",
        document_status: int = 200,
        document: object = DOCUMENT,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.refresh_token = refresh_token
        self.document_status = document_status
        self.document = document

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url == REGISTRATION_ENDPOINT:
            return httpx.Response(201, json={"client_id": "client-1", "client_secret": SECRET})
        if url == TOKEN_ENDPOINT:
            grant: dict[str, object] = {
                "access_token": "access-1",
                "token_type": "Bearer",
                "expires_in": 3600,
            }
            if self.refresh_token is not None:
                grant["refresh_token"] = self.refresh_token
            return httpx.Response(200, json=grant)
        if url == DOCUMENT_URL:
            return httpx.Response(self.document_status, json=self.document)
        return httpx.Response(404)

    @property
    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]


class Browser:
    def __init__(self, code: str = "code-1") -> None:
        self.urls: list[str] = []
        self.code = code

    def __call__(self, url: str) -> str:
        self.urls.append(url)
        return self.code


def _run(output: Path, service: Service, browser: Browser) -> Any:
    return bootstrap_credential(
        output_file=output,
        confirm=lambda: None,
        authorize=browser,
        http_client=httpx.Client(transport=httpx.MockTransport(service)),
        clock=lambda: NOW,
    )


def test_the_ceremony_registers_signs_in_proves_the_grant_then_writes_it(tmp_path: Path) -> None:
    service, browser = Service(), Browser()
    output = tmp_path / "private" / "svp.json"

    outcome = _run(output, service, browser)

    assert (outcome.operations, outcome.document_status) == (2, 200)

    assert service.urls == [REGISTRATION_ENDPOINT, TOKEN_ENDPOINT, DOCUMENT_URL]
    authorization = {
        name: values[0] for name, values in parse_qs(urlsplit(browser.urls[0]).query).items()
    }
    exchange = dict(parse_qsl(service.requests[1].content.decode("ascii")))
    digest = hashlib.sha256(exchange["code_verifier"].encode("ascii")).digest()
    assert authorization["code_challenge"] == (
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    )
    assert authorization["client_id"] == "client-1"
    assert authorization["redirect_uri"] == LOOPBACK_REDIRECT_URI
    assert len(authorization["state"]) >= 32
    assert exchange["code"] == "code-1"
    assert service.requests[2].headers["authorization"].split() == ["Bearer", "access-1"]
    assert read_state(output) == {
        "version": 1,
        "client_id": "client-1",
        "client_secret": SECRET,
        "refresh_token": "refresh-1",
        "access_token": "access-1",
        "expires_at": NOW + 3600,
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "service",
    [
        Service(refresh_token=None),
        Service(document_status=401),
        Service(document_status=403),
        Service(document_status=503),
    ],
    ids=["no-refresh-token", "api-rejects-the-token", "api-forbids-the-token", "api-unavailable"],
)
def test_a_grant_the_api_has_not_accepted_is_never_written(
    tmp_path: Path, service: Service
) -> None:
    output = tmp_path / "svp.json"

    with pytest.raises(BootstrapError):
        _run(output, service, Browser())

    assert not output.exists()


@pytest.mark.parametrize(
    ("service", "status"),
    [(Service(document_status=404), 404), (Service(document=[1]), 200)],
    ids=["no-document-there", "not-a-document"],
)
def test_an_accepted_grant_is_kept_when_the_document_is_not_where_expected(
    tmp_path: Path, service: Service, status: int
) -> None:
    """The sign-in is not spent again just because the document path was wrong."""
    output = tmp_path / "svp.json"

    outcome = _run(output, service, Browser())

    assert (outcome.operations, outcome.document_status) == (None, status)
    assert read_state(output)["refresh_token"] == "refresh-1"


def test_the_command_says_the_grant_is_saved_when_the_document_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = Service(document_status=404)
    output = tmp_path / "svp.json"
    monkeypatch.setattr(
        svp_main,
        "_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(service)),
    )
    monkeypatch.setattr(svp_main, "_confirm_disclosure", lambda: None)
    monkeypatch.setattr(svp_main, "_authorize_via_loopback", lambda _url: "code-1")

    with pytest.raises(SystemExit) as stopped:
        svp_main.main(["bootstrap", "--output-file", str(output)])

    assert "HTTP 404" in str(stopped.value.code)
    assert str(output) in capsys.readouterr().out
    assert output.exists()


@pytest.mark.parametrize("code", ["", "x" * 5000])
def test_an_unusable_authorization_code_is_never_exchanged(tmp_path: Path, code: str) -> None:
    service = Service()

    with pytest.raises(BootstrapError):
        _run(tmp_path / "svp.json", service, Browser(code))

    assert TOKEN_ENDPOINT not in service.urls


def test_an_existing_relative_or_linked_output_is_refused_before_any_request(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "svp.json"
    existing.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "elsewhere.json")
    service, browser = Service(), Browser()

    for output in (existing, link, Path("svp.json")):
        with pytest.raises(BootstrapError):
            _run(output, service, browser)

    assert service.requests == [] and browser.urls == []
    assert existing.read_text(encoding="utf-8") == "{}"


@pytest.mark.parametrize("answer", ["", "yes", "Continue", "continue", " CONTINUE"])
def test_only_the_exact_confirmation_lets_the_ceremony_proceed(answer: str) -> None:
    output = io.StringIO()

    with pytest.raises(BootstrapError, match="CONTINUE"):
        svp_main._confirm_disclosure(read_input=lambda _prompt: answer, output=output)

    svp_main._confirm_disclosure(read_input=lambda _prompt: "CONTINUE", output=output)
    disclosure = output.getvalue()
    assert "hosted model provider" in disclosure
    assert "one sign-in" in disclosure


def test_nothing_is_requested_or_opened_before_the_operator_confirms(tmp_path: Path) -> None:
    service, browser = Service(), Browser()

    def decline() -> None:
        raise BootstrapError("declined")

    with pytest.raises(BootstrapError, match="declined"):
        bootstrap_credential(
            output_file=tmp_path / "svp.json",
            confirm=decline,
            authorize=browser,
            http_client=httpx.Client(transport=httpx.MockTransport(service)),
        )

    assert service.requests == [] and browser.urls == []


def test_the_bootstrap_command_prints_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    service = Service()
    output = tmp_path / "svp.json"
    monkeypatch.setattr(
        svp_main,
        "_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(service)),
    )
    monkeypatch.setattr(svp_main, "_confirm_disclosure", lambda: None)
    monkeypatch.setattr(svp_main, "_authorize_via_loopback", lambda _url: "code-1")

    svp_main.main(["bootstrap", "--output-file", str(output)])

    printed = capsys.readouterr()
    text = printed.out + printed.err
    assert str(output) in text and "2 read operations" in text
    state = json.loads(output.read_text(encoding="utf-8"))
    for secret in (state["client_secret"], state["refresh_token"], state["access_token"]):
        assert secret not in text


def test_the_loopback_accepts_only_the_callback_that_carries_its_state() -> None:
    assert svp_main._callback_code("/callback?code=code-1&state=state-1", "state-1") == "code-1"
    for target in (
        "/callback?code=stolen&state=other",
        "/callback?state=state-1",
        "/elsewhere?code=code-1&state=state-1",
        "/callback?error=access_denied&state=state-1",
    ):
        assert svp_main._callback_code(target, "state-1") is None


def test_declining_the_disclosure_sends_nothing_to_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A near-miss such as `Continue` cancels before a client is even registered."""
    service = Service()
    opened: list[str] = []
    output = tmp_path / "svp.json"
    monkeypatch.setattr(
        svp_main,
        "_http_client",
        lambda: httpx.Client(transport=httpx.MockTransport(service)),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "Continue")

    def loopback(url: str) -> str:
        opened.append(url)
        return "code-1"

    monkeypatch.setattr(svp_main, "_authorize_via_loopback", loopback)

    with pytest.raises(SystemExit) as stopped:
        svp_main.main(["bootstrap", "--output-file", str(output)])

    assert "cancelled" in str(stopped.value.code)
    assert service.requests == []
    assert opened == []
    assert not output.exists()
