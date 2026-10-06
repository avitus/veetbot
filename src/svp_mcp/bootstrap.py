"""One-time sign-in ceremony: register, consent, prove the grant, then write it."""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from svp_mcp import oauth
from svp_mcp.constants import DOCUMENT_URL, MAXIMUM_DOCUMENT_BYTES
from svp_mcp.credential import create_state_file
from svp_mcp.errors import SvpError
from svp_mcp.specification import load_operations


class BootstrapError(RuntimeError):
    """A content-free sign-in ceremony failure."""


@dataclass(frozen=True)
class Outcome:
    """What the API answered when the new grant asked for its document."""

    operations: int | None
    document_status: int


def _probe(http: httpx.Client, access_token: str) -> Outcome:
    """Prove the API accepts the grant, and count the read operations it publishes."""

    try:
        with http.stream(
            "GET",
            DOCUMENT_URL,
            headers={"authorization": f"Bearer {access_token}", "accept": "application/json"},
            follow_redirects=False,
        ) as response:
            status = response.status_code
            if status in {401, 403}:
                raise BootstrapError("the API did not accept the sign-in")
            if status >= 500:
                raise BootstrapError("the API was unavailable")
            if status != 200:
                # The grant works; only the document is not where it was expected.
                return Outcome(None, status)
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > MAXIMUM_DOCUMENT_BYTES:
                    return Outcome(None, status)
                raw.extend(chunk)
    except httpx.HTTPError:
        raise BootstrapError("the API was unavailable") from None
    try:
        return Outcome(len(load_operations(json.loads(raw))), status)
    except (ValueError, UnicodeError, SvpError):
        return Outcome(None, status)


def bootstrap_credential(
    *,
    output_file: Path,
    confirm: Callable[[], None],
    authorize: Callable[[str], str],
    http_client: httpx.Client | None = None,
    clock: Callable[[], float] = time.time,
) -> Outcome:
    """Sign in once and publish the owner-only grant once the API has accepted it."""

    if not output_file.is_absolute() or output_file.is_symlink() or output_file.exists():
        raise BootstrapError("credential output must be a new absolute path")
    # Registration already writes to the service, so consent comes first.
    confirm()
    client = http_client or httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False, trust_env=False
    )
    try:
        try:
            registration = oauth.register(client)
            verifier, challenge = oauth.pkce()
            code = authorize(
                oauth.authorization_url(
                    registration.client_id, state=secrets.token_urlsafe(32), challenge=challenge
                )
            )
            if not isinstance(code, str) or not code or len(code) > 4096:
                raise BootstrapError("authorization response was invalid")
            grant = oauth.exchange(client, registration, code=code, verifier=verifier)
        except SvpError as exc:
            raise BootstrapError(exc.code) from None
        if grant.refresh_token is None:
            raise BootstrapError("the service issued no refresh token")
        expires_at = clock() + grant.expires_in
        # The grant is written only once the REST API has accepted it.
        outcome = _probe(client, grant.access_token)
    finally:
        if http_client is None:
            client.close()
    try:
        create_state_file(
            output_file,
            {
                "version": 1,
                "client_id": registration.client_id,
                "client_secret": registration.client_secret,
                "refresh_token": grant.refresh_token,
                "access_token": grant.access_token,
                "expires_at": expires_at,
            },
        )
    except SvpError:
        raise BootstrapError("could not create the private credential file") from None
    return outcome
