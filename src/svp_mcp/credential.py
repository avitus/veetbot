"""The grant as one private file: read, refreshed and replaced under an exclusive lock."""

from __future__ import annotations

import fcntl
import json
import math
import os
import secrets
import stat
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import httpx

from svp_mcp import oauth
from svp_mcp.constants import EXPIRY_SKEW_SECONDS, MAXIMUM_STATE_BYTES
from svp_mcp.errors import SvpError

_KEYS = frozenset(
    {"version", "client_id", "client_secret", "refresh_token", "access_token", "expires_at"}
)


def _validated(state: object) -> dict[str, Any]:
    if not isinstance(state, dict) or set(state) != _KEYS:
        raise SvpError("svp.credential_invalid")
    expires_at = state["expires_at"]
    if (
        isinstance(state["version"], bool)
        or state["version"] != 1
        or any(
            not isinstance(state[key], str) or not state[key]
            for key in ("client_id", "client_secret", "refresh_token")
        )
        or not isinstance(state["access_token"], str)
        or isinstance(expires_at, bool)
        or not isinstance(expires_at, int | float)
        or not math.isfinite(expires_at)
        or expires_at < 0
    ):
        raise SvpError("svp.credential_invalid")
    return dict(state)


def read_state(path: Path) -> dict[str, Any]:
    """Read the owner-only state file without following a link."""

    if not path.is_absolute():
        raise SvpError("svp.credential_invalid")
    try:
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_mode & 0o077
                or metadata.st_size > MAXIMUM_STATE_BYTES
            ):
                raise SvpError("svp.credential_invalid")
            raw = stream.read(MAXIMUM_STATE_BYTES + 1)
        return _validated(json.loads(raw))
    except (OSError, ValueError, UnicodeError):
        raise SvpError("svp.credential_invalid") from None


def _write(descriptor: int, state: Mapping[str, Any]) -> None:
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(json.dumps(dict(state), sort_keys=True).encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def create_state_file(path: Path, state: Mapping[str, Any]) -> None:
    """Publish a new owner-only state file; an existing path is never replaced."""

    document = _validated(dict(state))
    if not path.is_absolute():
        raise SvpError("svp.credential_invalid")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise SvpError("svp.credential_invalid") from None
    try:
        _write(descriptor, document)
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise


class CredentialStore:
    """Hand out a live access token, refreshing at use and keeping any rotation durable."""

    def __init__(
        self,
        path: Path,
        *,
        http: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path
        self._http = http or httpx.Client(
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False
        )
        self._owns_http = http is None
        self._clock = clock
        self._guard = threading.Lock()
        self._token = ""
        self._expires_at = 0.0

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def _usable(self, token: str, expires_at: float, rejected: str | None) -> bool:
        return (
            bool(token) and token != rejected and expires_at - self._clock() > EXPIRY_SKEW_SECONDS
        )

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        # Another server process may hold the same grant; a rotation is only
        # safe while every holder waits here and re-reads the file afterwards.
        if not self._path.is_absolute():
            raise SvpError("svp.credential_invalid")
        lock = self._path.with_name(self._path.name + ".lock")
        try:
            descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        except OSError:
            raise SvpError("svp.credential_invalid") from None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _refreshed(self, state: dict[str, Any]) -> dict[str, Any]:
        # The replacement file is reserved before the refresh is spent: a
        # rotated token that could not be saved would end the grant.
        temporary = self._path.with_name(f"{self._path.name}.{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except OSError:
            raise SvpError("svp.credential_invalid") from None
        try:
            try:
                grant = oauth.refresh(
                    self._http,
                    oauth.Client(state["client_id"], state["client_secret"]),
                    state["refresh_token"],
                )
            except BaseException:
                os.close(descriptor)
                raise
            updated = {
                **state,
                "access_token": grant.access_token,
                "expires_at": self._clock() + grant.expires_in,
                "refresh_token": grant.refresh_token or state["refresh_token"],
            }
            _write(descriptor, updated)
            os.replace(temporary, self._path)
            directory = os.open(self._path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            with suppress(OSError):
                os.unlink(temporary)
            raise SvpError("svp.credential_invalid") from None
        except BaseException:
            with suppress(OSError):
                os.unlink(temporary)
            raise
        return updated

    def access_token(self, *, rejected: str | None = None) -> str:
        """Return a token good for at least the skew, never the one just rejected."""

        with self._guard:
            if not self._usable(self._token, self._expires_at, rejected):
                with self._exclusive():
                    state = read_state(self._path)
                    if not self._usable(state["access_token"], state["expires_at"], rejected):
                        state = self._refreshed(state)
                self._token = state["access_token"]
                self._expires_at = float(state["expires_at"])
            return self._token
