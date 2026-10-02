"""Synthetic mail only: reading cleanup must never replace source evidence."""

from typing import cast

from agent_core.application.email_reader import reading_body
from tests.gates.test_email_experience_m26 import email_client, seed_mail


def test_forwarded_newsletter_keeps_article_attribution_and_embeds_links() -> None:
    source = """A thoughtful read.

---------- Forwarded message ---------
From: Journal <journal@example.com>
Date: Tuesday
Subject: A long article title
 continued on another line
To: <reader@example.com>

\u034f \u2007 \u00ad\u034f
Forwarded this email? Subscribe for more
<https://example.com/subscribe>
The article title
<https://example.com/article?token=keep>
By Jane Writer

The first paragraph contains a source
<https://example.com/evidence>
and continues with its original argument.

COMMENT
<https://example.com/comments>
Unsubscribe
<https://example.com/unsubscribe>
"""
    result = reading_body(source)
    assert "A thoughtful read." in result
    assert "Forwarded from Journal" in result
    assert "By Jane Writer" in result
    assert "[The article title](https://example.com/article?token=keep)" in result
    assert "[The first paragraph contains a source](https://example.com/evidence)" in result
    assert "and continues with its original argument." in result
    for noise in (
        "Subject:",
        "To:",
        "Date:",
        "\u034f",
        "\u00ad",
        "Subscribe",
        "COMMENT",
        "/unsubscribe",
    ):
        assert noise not in result


def test_html_retains_structure_and_safe_links_without_active_content() -> None:
    source = """<div style="display:none">Invisible preheader</div>
    <h1>Research &amp; evidence</h1><p>The <a href="https://example.com/report?a=1&amp;b=2">report</a>
    matters.</p><script>alert('bad')</script><style>bad css</style>
    <img src="https://example.com/pixel"><p>Second paragraph.</p>
    <a href="javascript:alert(1)">Unsafe action</a>"""
    result = reading_body(source)
    assert "# Research & evidence" in result
    assert "[report](https://example.com/report?a=1&b=2)" in result
    assert "Second paragraph." in result
    assert "Unsafe action" in result
    for noise in ("Invisible", "javascript:", "<img", "pixel", "bad css", "alert("):
        assert noise not in result


def test_conservative_cleanup_preserves_substantive_disclosures_and_correspondence() -> None:
    source = """Hi Alex,

Please unsubscribe me from the event, not the research updates.

Disclosure: I own shares in this company. The investment has material risks.

Best,
Jamie

On Tuesday, Alex wrote:
> What are the risks?
"""
    result = reading_body(source)
    assert result == source.strip()


def test_no_blind_tail_deletion_or_remote_link_resolution() -> None:
    source = """First paragraph.

Unsubscribe
<https://example.com/leave>

An important postscript after the footer.

https://example.com/very/long/path?opaque=keep
"""
    result = reading_body(source)
    assert "An important postscript after the footer." in result
    assert "[example.com](https://example.com/very/long/path?opaque=keep)" in result
    assert reading_body("") == ""


def test_malformed_forward_header_does_not_consume_message() -> None:
    source = "---------- Forwarded message ---------\nPlease keep this paragraph.\nAnd this one."
    assert "Please keep this paragraph." in reading_body(source)
    assert "And this one." in reading_body(source)


def test_article_title_and_byline_are_separate_from_newsletter_chrome() -> None:
    source = """An essay on research.

Research and discovery
<https://example.com/article>
An essay on research.
By Jane Writer
PAID

The author's argument stays exactly as written.

© 2026 Example Journal
123 Main Street, San Francisco, CA 94104
Unsubscribe
<https://example.com/leave>
"""
    result = reading_body(source, subject="Fwd: Research and discovery")
    assert "## [Research and discovery](https://example.com/article)" in result
    assert "\n\nBy Jane Writer\n\n" in result
    assert "The author's argument stays exactly as written." in result
    assert "PAID" not in result
    assert "123 Main" not in result
    assert "©" not in result


def test_explicit_advertisement_is_removed_but_editorial_sponsorship_is_preserved() -> None:
    source = """<p>Article opening.</p><div class="advertisement"><p>Buy today!</p></div>
    <p>Disclosure: the author received funding from Example.</p><p>Article conclusion.</p>"""
    result = reading_body(source)
    assert "Buy today" not in result
    assert "Disclosure: the author received funding from Example." in result
    assert "Article conclusion." in result


def test_bounded_plain_ad_and_stock_disclaimer_leave_the_article_intact() -> None:
    source = """Opening paragraph.

------------------------------
Advertisement
Buy our new product today.
<https://example.com/ad>
------------------------------

Closing paragraph.

This email and any attachments are confidential and intended solely for the named recipient.

An important postscript.
"""
    result = reading_body(source)
    assert "Buy our" not in result
    assert "confidential" not in result
    assert "Opening paragraph." in result
    assert "Closing paragraph." in result
    assert "An important postscript." in result


async def test_thread_projects_reader_without_changing_source() -> None:
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        response = await client.get(f"/v1/email/threads/{thread.id}")
        assert response.status_code == 200
        message = response.json()["messages"][0]
        assert message["body"] == thread.messages[0].body
        assert message.get("reader_body") == "Please review the board materials."
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            messages = cast(list[dict[str, object]], record.payload["messages"])
            assert "reader_body" not in messages[0]


async def test_expired_source_never_reappears_in_reader_projection() -> None:
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            payload = {**record.payload, "last_accessed_at": "2024-01-01T00:00:00Z"}
            await uow.email.put(
                record.model_copy(update={"revision": 2, "payload": payload}), expected_revision=1
            )
        response = await client.get(f"/v1/email/threads/{thread.id}")
        assert response.status_code == 200
        for message in response.json()["messages"]:
            assert message["body"] == ""
            assert message["reader_body"] == ""


def test_sender_text_cannot_author_a_link_an_image_or_markup() -> None:
    """Only links the reader builds, from a checked destination, are links."""
    source = """Please [Verify account](https://attacker.example/login) today.

![pixel](https://tracker.example/open.png)

Call <tel:+15551234567> or <b>act now</b>.

A pre-escaped \\[bracket](https://attacker.example/escaped) changes nothing.
"""
    result = reading_body(source)
    assert r"\[Verify account\](https://attacker.example/login)" in result
    assert r"!\[pixel\](https://tracker.example/open.png)" in result
    assert r"\<tel:+15551234567>" in result
    assert r"\\\[bracket\](https://attacker.example/escaped)" in result
    assert "[Verify account](" not in result
    assert "<tel:" not in result.replace(r"\<tel:", "")


def test_html_text_cannot_author_a_link_but_an_anchor_still_can() -> None:
    source = (
        "<p>Please [Verify account](https://attacker.example/login) today.</p>"
        '<p>Read the <a href="https://example.com/report">[1] report</a>.</p>'
        "<p>Call &lt;tel:+15551234567&gt;.</p>"
    )
    result = reading_body(source)
    assert r"\[Verify account\](https://attacker.example/login)" in result
    assert r"[\[1\] report](https://example.com/report)" in result
    assert r"\<tel:+15551234567>" in result
