"""Structured-log redaction tests."""

from agent_core.observability.logging import CONTENT_PREVIEW_CHARS, redact_sensitive


def test_redacts_sensitive_keys_and_provider_values() -> None:
    provider_value = "sk-" + ("x" * 24)
    event = redact_sensitive(
        None,
        "info",
        {
            "event": "provider.call",
            "api_key": "synthetic-value-for-test",
            "provider_value": provider_value,
        },
    )
    assert event["api_key"] == "[REDACTED]"
    assert event["provider_value"] == "[REDACTED]"
    assert provider_value not in repr(event)


def test_redaction_recurses_and_removes_embedded_provider_keys() -> None:
    provider_value = "sk-ant-" + ("y" * 24)
    event = redact_sensitive(
        None,
        "info",
        {
            "nested": {
                "authorization": "Bearer synthetic-test-value",
                "items": [f"provider returned {provider_value} in an error"],
            }
        },
    )
    assert event["nested"] == {
        "authorization": "[REDACTED]",
        "items": ["provider returned [REDACTED] in an error"],
    }
    assert provider_value not in repr(event)


def test_content_is_bounded_and_reports_original_length() -> None:
    content = "a" * (CONTENT_PREVIEW_CHARS + 50)
    event = redact_sensitive(None, "info", {"event": "tool.returned", "content": content})
    rendered = event["content"]
    assert isinstance(rendered, dict)
    assert rendered == {"preview": "a" * CONTENT_PREVIEW_CHARS, "length": len(content)}


def test_content_preview_is_sanitized_before_truncation() -> None:
    provider_value = "sk-" + ("z" * 24)
    original = {"password": "synthetic-password", "result": provider_value}
    event = redact_sensitive(
        None,
        "info",
        {"content": original},
    )
    assert provider_value not in repr(event)
    assert "synthetic-password" not in repr(event)
    assert event["content"]["length"] == len(str(original))
