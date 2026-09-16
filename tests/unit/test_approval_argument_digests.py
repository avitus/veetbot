"""Verifiable redaction for approval arguments.

An approval stores a redacted view of its action arguments, so a long string is
truncated and can no longer be compared for equality by a client that holds the
original. The digest map restores exact verification without publishing the
value. Values redacted for sensitivity carry no digest: a digest of a secret is
a brute-force target, and the owner never needs to verify a credential.
"""

import hashlib

from agent_core.tools.executor import _approval_argument_digests, _approval_argument_view


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_truncated_string_argument_publishes_a_verifiable_digest() -> None:
    body = "b" * 600
    view = _approval_argument_view({"subject": "Re: hello", "body": body})

    assert view["body"] == f"{'b' * 512}…[TRUNCATED]"
    assert _approval_argument_digests({"subject": "Re: hello", "body": body}) == {
        "body": _sha256(body)
    }


def test_short_arguments_need_no_digest() -> None:
    arguments = {"subject": "Re: hello", "body": "short enough to compare directly"}

    assert _approval_argument_view(arguments) == arguments
    assert _approval_argument_digests(arguments) == {}


def test_sensitive_values_are_never_digested() -> None:
    arguments = {"api_key": "s" * 600, "body": "token=value " + "c" * 600}

    assert _approval_argument_digests(arguments) == {}
