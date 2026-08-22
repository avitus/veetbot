"""Apple Push Notification service transport adapter."""

from __future__ import annotations

import base64
import json
import stat
from collections.abc import Mapping
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from agent_core.domain.devices import PushEnvironment, PushProvider, PushTarget
from agent_core.domain.notifications import DeliveryOutcome, PushMessage, PushOutcome
from agent_core.ports.determinism import Clock

_APNS_HOSTS = {
    PushEnvironment.SANDBOX: "https://api.sandbox.push.apple.com:443",
    PushEnvironment.PRODUCTION: "https://api.push.apple.com:443",
}
_PROVIDER_TOKEN_REFRESH = timedelta(minutes=20)
_UNREGISTERED_REASONS = {"Unregistered", "BadDeviceToken", "DeviceTokenNotForTopic"}
_CLIENT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class APNsPushTransport:
    """Deliver content-free messages through APNs over HTTP/2."""

    def __init__(
        self,
        *,
        key_file: Path,
        key_id: str,
        team_id: str,
        topic: str,
        clock: Clock,
        clients: Mapping[PushEnvironment, httpx.AsyncClient] | None = None,
    ) -> None:
        if not all(value.strip() for value in (key_id, team_id, topic)):
            raise ValueError("APNs identifiers cannot be blank")
        self._private_key = _load_private_key(key_file)
        self._key_id = key_id
        self._team_id = team_id
        self._topic = topic
        self._clock = clock
        self._provider_token: tuple[str, datetime] | None = None
        if clients is None:
            self._clients = {
                environment: httpx.AsyncClient(
                    base_url=host,
                    http2=True,
                    timeout=_CLIENT_TIMEOUT,
                )
                for environment, host in _APNS_HOSTS.items()
            }
        else:
            self._clients = dict(clients)

    async def deliver(self, target: PushTarget, message: PushMessage) -> PushOutcome:
        if target.provider is not PushProvider.APNS or target.environment is None:
            raise ValueError("APNs transport requires an APNs target")
        client = self._clients.get(target.environment)
        if client is None:
            raise ValueError(f"APNs client is unavailable for {target.environment.value}")
        headers = {
            "authorization": f"Bearer {self._token()}",
            "apns-topic": self._topic,
            "apns-push-type": "alert",
            "apns-priority": "10" if message.priority >= 10 else "5",
            "apns-collapse-id": _collapse_id(message.dedupe_key),
        }
        if message.expires_at is not None:
            headers["apns-expiration"] = str(int(message.expires_at.timestamp()))
        payload = {
            "aps": {"alert": {"title": message.payload.title}},
            "veetbot": message.payload.model_dump(mode="json"),
        }
        try:
            response = await client.post(
                f"/3/device/{target.token.get_secret_value()}",
                headers=headers,
                json=payload,
            )
        except httpx.HTTPError:
            return PushOutcome(
                outcome=DeliveryOutcome.RETRY,
                provider_reason="NetworkError",
            )
        reason = _response_reason(response)
        provider_id = response.headers.get("apns-id")
        if 200 <= response.status_code < 300:
            return PushOutcome(
                outcome=DeliveryOutcome.DELIVERED,
                provider_id=provider_id,
            )
        if response.status_code == 410 or (
            response.status_code == 400 and reason in _UNREGISTERED_REASONS
        ):
            outcome = DeliveryOutcome.UNREGISTERED
        elif (
            response.status_code == 429
            or response.status_code >= 500
            or reason == "ExpiredProviderToken"
        ):
            outcome = DeliveryOutcome.RETRY
            if reason == "ExpiredProviderToken":
                self._provider_token = None
        else:
            outcome = DeliveryOutcome.REJECTED
        return PushOutcome(
            outcome=outcome,
            provider_reason=reason,
            provider_id=provider_id,
        )

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()

    def _token(self) -> str:
        now = self._clock.now()
        if self._provider_token is not None:
            token, issued_at = self._provider_token
            if issued_at <= now < issued_at + _PROVIDER_TOKEN_REFRESH:
                return token
        encoded_header = _base64url(
            json.dumps(
                {"alg": "ES256", "kid": self._key_id},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        )
        encoded_claims = _base64url(
            json.dumps(
                {"iss": self._team_id, "iat": int(now.timestamp())},
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
        )
        signing_input = f"{encoded_header}.{encoded_claims}"
        der_signature = self._private_key.sign(
            signing_input.encode("ascii"),
            ec.ECDSA(hashes.SHA256()),
        )
        r, s = decode_dss_signature(der_signature)
        raw_signature = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        token = f"{signing_input}.{_base64url(raw_signature)}"
        self._provider_token = (token, now)
        return token


def _load_private_key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("APNs key file must be a regular file with mode 0600")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError("APNs key file must have mode 0600")
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError("APNs key file is unavailable") from exc
    try:
        loaded = serialization.load_pem_private_key(payload, password=None)
    except (TypeError, ValueError) as exc:
        raise ValueError("APNs key file is not a valid private key") from exc
    if not isinstance(loaded, ec.EllipticCurvePrivateKey) or not isinstance(
        loaded.curve, ec.SECP256R1
    ):
        raise ValueError("APNs key file must contain a P-256 private key")
    return loaded


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _collapse_id(dedupe_key: str) -> str:
    encoded = dedupe_key.encode("utf-8")
    if len(encoded) <= 64 and dedupe_key.isascii():
        return dedupe_key
    return sha256(encoded).hexdigest()


def _response_reason(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"HTTP{response.status_code}"
    if isinstance(payload, dict):
        reason = payload.get("reason")
        if isinstance(reason, str) and 0 < len(reason) <= 128:
            return reason
    return f"HTTP{response.status_code}"
