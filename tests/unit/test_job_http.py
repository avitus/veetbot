"""ADR-0268 HTTP reads pin checked addresses and keep credentials in the broker."""

from __future__ import annotations

import httpx
import pytest

from agent_core.adapters.jobs.http import JobHTTP
from agent_core.domain.credentials import CredentialRef, SecretValue

CANARY = "github-job-test-canary"


class Credentials:
    async def resolve(self, reference: CredentialRef) -> SecretValue:
        assert reference.name == "github-reader"
        return SecretValue(CANARY)


async def test_one_resolution_pins_dial_but_preserves_tls_hostname_and_credential() -> None:
    resolved: list[str] = []
    sent: list[httpx.Request] = []

    async def resolve(host: str) -> tuple[str, ...]:
        resolved.append(host)
        return ("140.82.112.6",)

    def respond(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"items": [1, 2], "echo": CANARY})

    broker = JobHTTP(Credentials(), resolver=resolve, transport=httpx.MockTransport(respond))
    result = await broker.request(
        method="POST",
        url="https://api.github.com/graphql",
        json={"query": "query { viewer { login } }"},
        credential="github-reader",
        maximum_response_bytes=1024,
        call_id="read",
    )
    assert resolved == ["api.github.com"]
    [request] = sent
    assert request.url.host == "140.82.112.6"
    assert request.headers["host"] == "api.github.com"
    assert request.extensions["sni_hostname"] == "api.github.com"
    assert request.headers["authorization"] == f"Bearer {CANARY}"
    assert result["status"] == 200
    assert result["json"]["items"] == [1, 2]
    assert CANARY not in str(result)


@pytest.mark.parametrize(
    "addresses", [("127.0.0.1",), ("169.254.169.254",), ("140.82.112.6", "::1"), ()]
)
async def test_private_or_mixed_dns_answer_never_dials(addresses: tuple[str, ...]) -> None:
    async def resolve(host: str) -> tuple[str, ...]:
        return addresses

    def respond(request: httpx.Request) -> httpx.Response:
        pytest.fail("A private address reached the HTTP transport.")

    broker = JobHTTP(Credentials(), resolver=resolve, transport=httpx.MockTransport(respond))
    with pytest.raises(ValueError, match=r"job\.address_refused"):
        await broker.request(
            method="GET",
            url="https://api.github.com",
            json=None,
            credential=None,
            maximum_response_bytes=1024,
            call_id="read",
        )


@pytest.mark.parametrize(
    ("url", "credential", "operator_hosts"),
    [
        ("https://other.example", "github-reader", None),
        ("https://api.github.com", "other-credential", None),
        ("https://api.github.com", None, frozenset({"other.example"})),
        ("http://api.github.com", None, None),
    ],
)
async def test_host_credential_and_operator_restrictions_precede_dns(
    url: str, credential: str | None, operator_hosts: frozenset[str] | None
) -> None:
    async def resolve(host: str) -> tuple[str, ...]:
        pytest.fail("A refused destination reached DNS.")

    broker = JobHTTP(Credentials(), resolver=resolve, operator_hosts=operator_hosts)
    with pytest.raises(ValueError, match=r"job\.(host|credential)_refused"):
        await broker.request(
            method="GET",
            url=url,
            json=None,
            credential=credential,
            maximum_response_bytes=1024,
            call_id="read",
        )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"Location": "https://elsewhere.example"}),
        httpx.Response(200, content=b"x" * 1025),
    ],
)
async def test_redirects_and_oversized_bodies_are_refused(response: httpx.Response) -> None:
    seen = 0

    async def resolve(host: str) -> tuple[str, ...]:
        return ("140.82.112.6",)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen += 1
        return response

    broker = JobHTTP(Credentials(), resolver=resolve, transport=httpx.MockTransport(respond))
    with pytest.raises(ValueError, match=r"job\.(redirect_refused|response_limit)"):
        await broker.request(
            method="GET",
            url="https://api.github.com",
            json=None,
            credential=None,
            maximum_response_bytes=1024,
            call_id="read",
        )
    assert seen == 1


async def test_github_reader_refuses_write_requests_and_graphql_mutations() -> None:
    async def resolve(host: str) -> tuple[str, ...]:
        pytest.fail("A read-only credential must not authorize this write.")

    broker = JobHTTP(Credentials(), resolver=resolve)
    for path, body in [
        ("/repos/example/issues", {}),
        ("/graphql", {"query": "mutation { addComment { id } }"}),
    ]:
        with pytest.raises(ValueError, match=r"job\.credential_refused"):
            await broker.request(
                method="POST",
                url="https://api.github.com" + path,
                json=body,
                credential="github-reader",
                maximum_response_bytes=1024,
                call_id="write",
            )


async def test_github_reader_token_explicitly_requests_only_read_permissions() -> None:
    import json
    from datetime import timedelta

    from agent_core.adapters.cockpit.github import GitHubAppTokens
    from agent_core.adapters.determinism import FixedClock
    from tests.contract.fake_github import app_private_key
    from tests.contract.support import NOW

    bodies = []

    def respond(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            201, json={"token": "test-reader", "expires_at": (NOW + timedelta(hours=1)).isoformat()}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        tokens = GitHubAppTokens(
            client,
            app_id=1,
            installation_id=2,
            private_key_pem=app_private_key(),
            clock=FixedClock(NOW),
            read_only=True,
        )
        assert await tokens.token() == "test-reader"
    assert bodies == [
        {
            "permissions": dict.fromkeys(
                ("metadata", "pull_requests", "checks", "statuses", "contents"), "read"
            )
        }
    ]


@pytest.mark.parametrize(
    "encoding", ["base64", "base64url", "hex", "percent", "mixed-hex", "mixed-percent"]
)
async def test_encoded_credential_echo_is_refused_before_returning_to_job(encoding: str) -> None:
    """A response must not hand a reversible credential encoding to sandbox code."""
    import base64

    hexadecimal = CANARY.encode().hex()
    # Deliberately mix letter case within a single credential encoding.
    mixed = hexadecimal.upper().replace("D", "d", 1)
    encoded = {
        "mixed-hex": mixed,
        "mixed-percent": "".join("%" + mixed[i : i + 2] for i in range(0, len(mixed), 2)),
        "base64": base64.b64encode(CANARY.encode()).decode(),
        "base64url": base64.urlsafe_b64encode(CANARY.encode()).decode().rstrip("="),
        "hex": CANARY.encode().hex(),
        "percent": "".join(f"%{byte:02X}" for byte in CANARY.encode()),
    }[encoding]

    async def resolve(host: str) -> tuple[str, ...]:
        return ("140.82.112.6",)

    broker = JobHTTP(
        Credentials(),
        resolver=resolve,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={encoded: [encoded]})
        ),
    )
    with pytest.raises(ValueError, match=r"job\.credential_echo_refused"):
        await broker.request(
            method="GET",
            url="https://api.github.com",
            json=None,
            credential="github-reader",
            maximum_response_bytes=1024,
            call_id="encoded-echo",
        )
