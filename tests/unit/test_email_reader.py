"""Synthetic mail only: reading cleanup must never replace source evidence."""

import time
from typing import cast

import pytest

from agent_core.application.email_reader import reading_body
from tests.gates.test_email_experience_m26 import email_client, seed_mail


def test_unclosed_table_keeps_cell_prose_without_a_markdown_grid() -> None:
    """An incomplete table has no reliable rectangular structure."""
    result = reading_body(
        "<table><tr><th>Asset</th><th>Value</th></tr><tr><td>Cash</td><td>42</td></tr>"
    )
    assert all(value in result for value in ("Asset", "Value", "Cash", "42"))
    assert "|" not in result
    assert "---" not in result


@pytest.mark.parametrize("with_image", [False, True])
async def test_html_fallback_does_not_restore_styles_as_missing_plain_prose(
    with_image: bool,
) -> None:
    """A retained tag-stripped HTML fallback is not an independent MIME alternative."""
    import base64

    from gmail_mcp.client import GmailClient

    html = (
        "<html><head><style>#outlook a { padding:0; }</style>"
        "<!--[if mso]><xml><o:PixelsPerInch>96</o:PixelsPerInch></xml><![endif]-->"
        "</head><body><h1>Account login</h1><p>A new login was recorded.</p>"
        "<div hidden>Hidden preheader</div><p>Manage your "
        '<a href="https://example.com/security">activity</a>.</p>'
        + (
            '<img src="https://images.example/logo.png" alt="Logo">'
            if with_image
            else '<img src="https://images.example/pixel.png" width="1" height="1">'
        )
        + "</body></html>"
    )
    original, _ = GmailClient._payload_content(
        {
            "mimeType": "text/html",
            "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
        }
    )
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            payload = dict(record.payload)
            messages = [dict(item) for item in cast(list[dict[str, object]], payload["messages"])]
            messages[0].update(body=original, reader_html=html)
            payload["messages"] = messages
            await uow.email.put(
                record.model_copy(update={"revision": 2, "payload": payload}), expected_revision=1
            )
        message = (await client.get(f"/v1/email/threads/{thread.id}")).json()["messages"][0]
        assert message["body"] == original
        for rendered in [message["reader_body"], message["reader_image_body"]]:
            if rendered is None:
                continue
            assert "padding" not in rendered
            assert "Hidden preheader" not in rendered
            assert "96" not in rendered
            assert rendered.count("A new login was recorded.") == 1
            assert "[activity](https://example.com/security)" in rendered
        assert (message["reader_image_body"] is not None) == with_image


def test_html_list_paragraph_starts_beside_its_bullet() -> None:
    result = reading_body(
        "<ul><li><p><strong>Application:</strong> Example browser</p></li>"
        "<li> <div><p>Time: Today</p></div></li></ul>"
    )
    assert "- **Application:** Example browser" in result
    assert "- Time: Today" in result
    assert "\n-\n" not in result


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
    # Every word stays; the sign-off's own line break now survives rendering.
    assert result == source.strip().replace("Best,\n", "Best,\\\n")


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


# The plain-text part Gmail generates: an anchor becomes `text <url>`, a detected
# phone number gains its own target, and every line wraps by its full width.
GMAIL_SIGNATURE = """Riley Example
Partner, Example <https://www.example.com/>
650.555.0100
riley@example.com
Bio <https://www.example.com/team/riley-example>|LinkedIn
<https://www.linkedin.example/in/rileyexample/>|X <https://x.example/riley>|
20VC <https://www.example.com/podcasts#20VC>"""

READER_SIGNATURE = (
    "Riley Example\\\n"
    "Partner, [Example](https://www.example.com/)\\\n"
    "650.555.0100\\\n"
    "riley@example.com\\\n"
    "[Bio](https://www.example.com/team/riley-example) | "
    "[LinkedIn](https://www.linkedin.example/in/rileyexample/) | "
    "[X](https://x.example/riley) | "
    "[20VC](https://www.example.com/podcasts#20VC)"
)


def test_gmail_signature_links_its_labels_and_keeps_its_lines() -> None:
    assert reading_body(GMAIL_SIGNATURE) == READER_SIGNATURE


def test_link_target_links_the_word_before_it() -> None:
    result = reading_body("See the report <https://example.com/report> for more.")
    assert result == "See the [report](https://example.com/report) for more."


def test_link_target_without_a_word_before_it_names_its_host() -> None:
    result = reading_body("Source: <https://example.com/a>\n\nBefore, <https://example.com/b>")
    assert result == (
        "Source: [example.com](https://example.com/a)\n\n"
        "Before, [example.com](https://example.com/b)"
    )


def test_address_shaped_label_names_only_its_own_destination() -> None:
    result = reading_body(
        "pay.example <https://attacker.example/login>\n\nwww.example.com <https://www.example.com/>"
    )
    assert "[pay.example]" not in result
    assert "pay.example [attacker.example](https://attacker.example/login)" in result
    assert "[www.example.com](https://www.example.com/)" in result


def test_sender_text_around_a_link_target_stays_inert() -> None:
    result = reading_body(
        "[Verify](https://attacker.example/a) now <https://example.com/b> <tel:1>"
    )
    assert r"\[Verify\](https://attacker.example/a)" in result
    assert "[now](https://example.com/b)" in result
    assert r"\<tel:1>" in result


def test_gmail_phone_target_is_removed_without_touching_other_angle_text() -> None:
    assert reading_body("650.555.0100 <(650)%20555-0100>") == "650.555.0100"
    assert reading_body("+1 650 555 0100 <+1%20650%20555%200100>") == "+1 650 555 0100"
    assert reading_body("Size 12 <34> today") == r"Size 12 \<34> today"


def test_gmail_links_and_phone_targets_are_cleaned_inside_quotes() -> None:
    source = ">> Partner, Example <https://www.example.com/>\n>> 650.555.0100 <(650)%20555-0100>"
    assert reading_body(source) == (
        ">> Partner, [Example](https://www.example.com/)\\\n>> 650.555.0100"
    )


def test_wrapped_prose_flows_and_lines_the_writer_ended_stay() -> None:
    source = """As if they were one combined fund? We would start with the first capital
call and include all the cash flows.
Short closing line.

>>> Monday fine
>>> Yes to method
>>>
>>> On Fri, Oct 30, 2026 at 5:35 PM Riley Example With A Long Name <riley@example.com>
>>> wrote:
"""
    assert reading_body(source) == (
        "As if they were one combined fund? We would start with the first capital "
        "call and include all the cash flows.\\\n"
        "Short closing line.\n\n"
        ">>> Monday fine\\\n"
        ">>> Yes to method\n"
        ">>>\n"
        ">>> On Fri, Oct 30, 2026 at 5:35 PM Riley Example With A Long Name "
        "\\<riley@example.com> wrote:"
    )


def test_a_line_break_is_never_written_where_the_paragraph_ends() -> None:
    """A hard break before a new block would show the reader a stray backslash."""
    source = """Intro line
- first
- second
After the list

A line
> a quote
Line before a table
| a | b |
|---|---|
| 1 | 2 |

A title
---
Body line"""
    assert "\\\n" not in reading_body(source)


def test_signature_delimiter_does_not_turn_the_sign_off_into_a_heading() -> None:
    result = reading_body("Thanks,\nJamie\n--\nJamie Example")
    assert result == "Thanks,\\\nJamie\n\n--\\\nJamie Example"


def test_code_fences_keep_their_lines_untouched() -> None:
    source = "```\nshort\nlines\n```"
    assert reading_body(source) == source


def test_repeated_signature_is_shown_once_however_it_wrapped() -> None:
    rewrapped = """Riley Example
Partner, Example <https://www.example.com/>
650.555.0100 <(650)%20555-0100>
riley@example.com
Bio <https://www.example.com/team/riley-example>|LinkedIn
<https://www.linkedin.example/in/rileyexample/>|X
<https://x.example/riley>|20VC
<https://www.example.com/podcasts#20VC>"""
    quoted = "\n".join(f"> {line}" for line in rewrapped.split("\n"))
    deeper = "\n".join(f">> {line}" for line in rewrapped.split("\n"))
    source = (
        f"Latest note.\n\n{GMAIL_SIGNATURE}\n\nOn Friday Sam wrote:\n\n> A reply.\n>\n{quoted}\n>\n"
        f"> On Friday Riley wrote:\n>\n>> The first question.\n>>\n{deeper}\n"
    )
    result = reading_body(source)
    assert result.count("Riley Example") == 1
    assert READER_SIGNATURE in result
    assert "> A reply." in result
    assert ">> The first question." in result
    # What is left stays one quote, so the client can fold the whole chain.
    assert "\n\n>" not in result.split("On Friday Sam wrote:")[1].strip()


def test_short_repeats_and_differing_blocks_stay() -> None:
    source = """Yes.

Best,
Jamie

> Yes.
>
> Best,
> Jamie
>
> One
> Two
> Three
>
> One
> Two
> Four
"""
    result = reading_body(source)
    assert result.count("Yes.") == 2
    assert result.count("Jamie") == 2
    assert result.count("One") == 2


def test_a_link_target_on_its_own_line_inside_a_quote_labels_the_line_before() -> None:
    source = "> The article title\n> <https://example.com/article>\n> and it continues here."
    assert reading_body(source) == (
        "> [The article title](https://example.com/article)\n> and it continues here."
    )


def test_gmail_promotional_control_goes_with_its_inline_target() -> None:
    result = reading_body(
        "Article text.\n\nUnsubscribe <https://example.com/leave>\n\nA postscript."
    )
    assert result == "Article text.\n\nA postscript."


def test_cleanup_stays_quick_on_hostile_lines() -> None:
    """Runs on every thread read, so sender text must not make it quadratic."""
    started = time.monotonic()
    reading_body("a" * 50_000 + "! <https://example.com/x>")
    reading_body("a" + " " * 50_000 + "b")
    reading_body("\n".join(["word " * 16] * 2_000))
    assert time.monotonic() - started < 5


def test_cleanup_stays_quick_on_a_long_wrapped_paragraph() -> None:
    """Rejoining must not copy the growing paragraph for every wrapped line."""
    lines = [f"word{index} " + "wrapped " * 9 + "sentence" for index in range(200_000)]
    started = time.monotonic()
    result = reading_body("\n".join(lines))
    assert time.monotonic() - started < 5
    assert result == " ".join(lines)


async def test_images_are_projected_in_place_but_never_fetched_on_thread_read() -> None:
    """Reading a thread exposes inert image positions without issuing network requests."""
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            payload = dict(record.payload)
            messages = [dict(item) for item in cast(list[dict[str, object]], payload["messages"])]
            messages[0]["reader_html"] = (
                '<p>Before.</p><img src="https://images.example/photo.png" alt="The garden">'
                '<p>After.</p><img width="1" height="1" src="https://images.example/pixel">'
                '<img src="http://localhost/private"><script>bad()</script>'
            )
            payload["messages"] = messages
            await uow.email.put(
                record.model_copy(update={"revision": 2, "payload": payload}), expected_revision=1
            )
        response = await client.get(f"/v1/email/threads/{thread.id}")
        message = response.json()["messages"][0]
        assert message["body"] == thread.messages[0].body
        assert "reader_html" not in message
        rich = message.get("reader_image_body", "")
        assert rich.index("Before.") < rich.index("![The garden]") < rich.index("After.")
        assert "pixel" not in rich and "localhost" not in rich and "bad()" not in rich


async def test_image_endpoint_checks_retention_identity_scope_failure_and_explicit_retry() -> None:
    """Image access enforces owner, scope and retention boundaries and explicit retries."""
    from uuid import uuid4

    import pytest

    from agent_core.domain.errors import (
        AuthorizationError,
        EmailImageUnavailableError,
        NotFoundError,
    )

    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        service = app.services.email
        calls: list[str] = []

        async def read(url: str) -> bytes:
            calls.append(url)
            if len(calls) == 1:
                raise EmailImageUnavailableError("Image could not be loaded.")
            return b"jpeg-fixture"

        service.read_image = read
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            payload = dict(record.payload)
            messages = [dict(item) for item in cast(list[dict[str, object]], payload["messages"])]
            messages[0]["reader_html"] = '<p>Photo.</p><img src="https://images.example/photo.png">'
            payload["messages"] = messages
            await uow.email.put(
                record.model_copy(update={"revision": 2, "payload": payload}), expected_revision=1
            )
        route = f"/v1/email/threads/{thread.id}/images"
        await client.get(f"/v1/email/threads/{thread.id}")
        assert calls == []
        assert (
            await client.post(route, json={"message_id": "missing", "index": 0})
        ).status_code == 400
        assert (await client.post(route, json={"message_id": "m1", "index": 1})).status_code == 400
        assert (await client.post(route, json={"message_id": "m1", "index": -1})).status_code == 400
        assert (
            await client.post(
                route, json={"message_id": "m1", "index": 0, "url": "https://other.example"}
            )
        ).status_code == 400
        assert (
            await client.post(
                f"/v1/email/threads/{uuid4()}/images", json={"message_id": "m1", "index": 0}
            )
        ).status_code == 404
        assert calls == []
        with pytest.raises(AuthorizationError):
            await service.image(
                app.principal.model_copy(update={"scopes": set()}), thread.id, "m1", 0
            )
        with pytest.raises(NotFoundError):
            await service.image(
                app.principal.model_copy(update={"principal_id": "another-owner"}),
                thread.id,
                "m1",
                0,
            )
        assert calls == []
        failure = await client.post(route, json={"message_id": "m1", "index": 0})
        assert failure.status_code == 502, failure.text
        assert len(calls) == 1
        response = await client.post(route, json={"message_id": "m1", "index": 0})
        assert response.status_code == 200 and response.content == b"jpeg-fixture"
        assert response.headers["cache-control"] == "private, no-store"
        assert calls == ["https://images.example/photo.png"] * 2
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            payload = {**record.payload, "last_accessed_at": "2024-01-01T00:00:00Z"}
            await uow.email.put(
                record.model_copy(update={"revision": record.revision + 1, "payload": payload}),
                expected_revision=record.revision,
            )
        assert (await client.post(route, json={"message_id": "m1", "index": 0})).status_code == 400
        message = (await client.get(f"/v1/email/threads/{thread.id}")).json()["messages"][0]
        assert message["reader_image_body"] is None
        assert len(calls) == 2


def test_image_projection_rejects_sender_markdown_hidden_and_nonpublic_sources() -> None:
    """Sender markup cannot forge image controls or expose hidden and unsafe sources."""
    urls: list[str] = []
    source = """<p>![Forged](veetbot-email-image:0)</p>
    <div hidden><img src="https://images.example/hidden"></div>
    <img style="width:1px" src="https://images.example/pixel">
    <img src="cid:attachment"><img src="data:image/png;base64,abc">
    <img src="http://images.example/insecure"><img src="https://127.0.0.1/private">
    <img src="https://CREDENTIALS@images.example/private">
    <img src="https://images.example/photo.png" alt="[Garden]">
    <p>After.</p>""".replace("CREDENTIALS", "name:" + "password")
    rich = reading_body(source, image_urls=urls)
    assert urls == ["https://images.example/photo.png"]
    assert r"!\[Forged\](veetbot-email-image:0)" in rich
    assert r"![\[Garden\]](veetbot-email-image:0)" in rich
    assert rich.endswith("After.")


def test_image_projection_caps_references_without_losing_prose() -> None:
    """The image budget bounds references while preserving surrounding message text."""
    urls: list[str] = []
    rich = reading_body(
        "<p>Before.</p>" + '<img src="https://images.example/photo.png">' * 30 + "<p>After.</p>",
        image_urls=urls,
    )
    assert len(urls) == 20
    assert rich.count("veetbot-email-image:") == 20
    assert rich.startswith("Before.") and rich.endswith("After.")


async def test_adding_image_source_preserves_revision_and_learning_content() -> None:
    """Presentation HTML must not change source identity or become learning evidence."""
    from uuid import uuid4

    from tests.gates.test_email_learning_m26 import observation, prepare

    async with email_client() as (app, client):
        service = await prepare(app)
        source = observation()
        session = uuid4()
        original = await service.import_thread(app.principal, "work", source, session)
        html = '<p>Photo.</p><img src="https://images.example/photo.png">'
        source["messages"][0]["reader_html"] = html
        updated = await service.import_thread(app.principal, "work", source, session)
        assert updated.id == original.id and updated.revision == original.revision
        assert updated.source_fingerprint == original.source_fingerprint
        assert updated.messages[0].body == original.messages[0].body
        assert updated.messages[0].reader_html == html
        projection = (await client.get(f"/v1/email/threads/{updated.id}")).json()
        assert "veetbot-email-image:0" in projection["messages"][0]["reader_image_body"]


@pytest.mark.parametrize("footer", ["", "<p>Unsubscribe from these updates.</p>"])
async def test_image_only_html_preserves_plain_text_in_mac_projection(footer: str) -> None:
    """Image alternatives must not erase text present only in the plain MIME part."""
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            payload = dict(record.payload)
            messages = [dict(item) for item in cast(list[dict[str, object]], payload["messages"])]
            messages[0]["reader_html"] = (
                '<a href="https://example.com"><img src="https://images.example/photo.png" '
                'alt="The garden"></a>' + footer
            )
            payload["messages"] = messages
            await uow.email.put(
                record.model_copy(update={"revision": 2, "payload": payload}), expected_revision=1
            )
        message = (await client.get(f"/v1/email/threads/{thread.id}")).json()["messages"][0]
        assert message["reader_body"]
        assert message["reader_body"] in message["reader_image_body"]
        assert "![The garden](veetbot-email-image:0)" in message["reader_image_body"]


async def test_image_fetch_concurrency_is_bounded_and_slots_release_after_failure() -> None:
    """Parallel loads share a four-image budget and failed fetches release their slot."""
    import asyncio

    from agent_core.domain.errors import EmailImageUnavailableError

    async with email_client() as (app, _):
        thread, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            payload = dict(record.payload)
            messages = [dict(item) for item in cast(list[dict[str, object]], payload["messages"])]
            messages[0]["reader_html"] = '<img src="https://images.example/photo.png">'
            payload["messages"] = messages
            await uow.email.put(
                record.model_copy(update={"revision": 2, "payload": payload}), expected_revision=1
            )
        active = peak = calls = 0

        async def read(url: str) -> bytes:
            nonlocal active, peak, calls
            active += 1
            calls += 1
            fail = calls == 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.05)
                if fail:
                    raise EmailImageUnavailableError("Synthetic failure")
                return b"jpeg-fixture"
            finally:
                active -= 1

        app.services.email.read_image = read
        results = await asyncio.wait_for(
            asyncio.gather(
                *(app.services.email.image(app.principal, thread.id, "m1", 0) for _ in range(12)),
                return_exceptions=True,
            ),
            timeout=5,
        )
        assert peak <= 4
        assert calls == 12 and active == 0
        assert sum(isinstance(result, EmailImageUnavailableError) for result in results) == 1
        assert results.count(b"jpeg-fixture") == 11


def test_image_projection_merges_only_plain_paragraphs_missing_from_html() -> None:
    """Visible words match across markup, wrapping and link URLs without duplicate prose."""
    from agent_core.application.email_reader import merge_image_body

    plain = "Already here with [a link](https://plain.example).\n\nMissing detail."
    rich = (
        "**Already here** with [a link](https://html.example).\n\n![Photo](veetbot-email-image:0)"
    )
    html_text = "**Already here** with [a link](https://html.example)."
    result = merge_image_body(plain, rich, html_text)
    assert result == "Missing detail.\n\n" + rich
    assert merge_image_body("Already here\nwith a link.", rich, html_text) == rich
    assert merge_image_body("", rich, html_text) == rich


def test_image_merge_stays_quick_on_unclosed_link_delimiters() -> None:
    """Unclosed sender link syntax must not cause repeated scans of the remaining body."""
    from agent_core.application.email_reader import merge_image_body

    hostile = r"\](" * 16384
    started = time.monotonic()
    assert merge_image_body("Keep this text.", hostile, hostile).startswith("Keep this text.")
    assert time.monotonic() - started < 1


def test_images_inside_links_keep_surrounding_text_linked() -> None:
    """An image splits an anchor into linked text segments and standalone image markers."""
    urls: list[str] = []
    result = reading_body(
        '<a href="https://example.com/article">Before'
        '<img src="https://images.example/one.png" alt="One">Middle'
        '<img src="https://images.example/two.png" alt="Two">After</a>',
        image_urls=urls,
    )
    assert result.split("\n\n") == [
        "[Before](https://example.com/article)",
        "![One](veetbot-email-image:0)",
        "[Middle](https://example.com/article)",
        "![Two](veetbot-email-image:1)",
        "[After](https://example.com/article)",
    ]
    assert len(urls) == 2


@pytest.mark.parametrize("style", ["width:100px;height:1px", "height:100px;width:2px"])
def test_adjacent_dimensions_exclude_tiny_images(style: str) -> None:
    """Either inline dimension can exclude an image even after another declaration."""
    urls: list[str] = []
    reading_body(f'<img style="{style}" src="https://images.example/pixel">', image_urls=urls)
    assert urls == []


async def test_queued_image_rechecks_erasure_before_fetching() -> None:
    """An image erased while waiting for admission must never reach the external host."""
    import asyncio

    queued = asyncio.Event()

    class WaitingSlot(asyncio.Semaphore):
        async def __aenter__(self) -> None:
            queued.set()
            return await super().__aenter__()

    async with email_client() as (app, _):
        thread, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            payload = dict(record.payload)
            messages = [dict(item) for item in cast(list[dict[str, object]], payload["messages"])]
            messages[0]["reader_html"] = '<img src="https://images.example/photo.png">'
            payload["messages"] = messages
            await uow.email.put(
                record.model_copy(update={"revision": 2, "payload": payload}), expected_revision=1
            )
        calls: list[str] = []

        async def read(url: str) -> bytes:
            calls.append(url)
            return b"jpeg"

        service = app.services.email
        service.read_image = read
        service._image_slots = WaitingSlot(0)
        task = asyncio.create_task(service.image(app.principal, thread.id, "m1", 0))
        try:
            await asyncio.wait_for(queued.wait(), timeout=2)
            async with app.uow_factory() as uow:
                record = await uow.email.get(app.principal, "thread", str(thread.id))
                assert record is not None
                payload = dict(record.payload)
                messages = [
                    dict(item) for item in cast(list[dict[str, object]], payload["messages"])
                ]
                messages[0]["reader_html"] = ""
                payload["messages"] = messages
                await uow.email.put(
                    record.model_copy(update={"revision": record.revision + 1, "payload": payload}),
                    expected_revision=record.revision,
                )
            service._image_slots.release()
            with pytest.raises(ValueError):
                await asyncio.wait_for(task, timeout=2)
            assert calls == []
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("plain", [False, True])
async def test_flash_email_preserves_table_and_paragraphs_without_images(plain: bool) -> None:
    """Synthetic financial mail preserves its HTML structure through the scoped API."""
    import base64

    from gmail_mcp.client import GmailClient

    html = (
        "<p>Quarterly results.</p><p>Please review the figures.</p>"
        "<table><tr><th>Metric</th><th>Actual</th></tr>"
        "<tr><td>Revenue</td><td>$42</td></tr></table><p>Thank you.</p>"
    )
    part = {
        "mimeType": "text/html",
        "body": {"data": base64.urlsafe_b64encode(html.encode()).decode()},
    }
    original = (
        "Quarterly results. Please review the figures. Metric Actual Revenue $42 Thank you."
        if plain
        else GmailClient._payload_content(part)[0]
    )
    async with email_client() as (app, client):
        thread, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            record = await uow.email.get(app.principal, "thread", str(thread.id))
            assert record is not None
            payload = dict(record.payload)
            messages = [dict(item) for item in cast(list[dict[str, object]], payload["messages"])]
            messages[0].update(body=original, reader_html=GmailClient._reader_html(part))
            payload["messages"] = messages
            await uow.email.put(
                record.model_copy(update={"revision": 2, "payload": payload}), expected_revision=1
            )
        message = (await client.get(f"/v1/email/threads/{thread.id}")).json()["messages"][0]
        assert message["body"] == original
        assert "Quarterly results.\n\nPlease review the figures." in message["reader_body"]
        assert "| Metric | Actual |\n| --- | --- |\n| Revenue | $42 |" in message["reader_body"]
        assert message["reader_body"].count("Quarterly results.") == 1
        assert message["reader_image_body"] is None


def test_html_data_table_inside_layout_preserves_safe_cells() -> None:
    """Verify html data table inside layout preserves safe cells."""
    result = reading_body(
        '<table role="presentation"><tr><td><p>Intro.</p>'
        "<table><tr><td>Metric</td><td>Value</td></tr>"
        "<tr><td><strong>Revenue</strong></td><td>One | two<br>Three</td></tr>"
        "</table><p>Closing.</p></td></tr></table>"
    )
    assert "| Metric | Value |\n| --- | --- |" in result
    assert r"| **Revenue** | One \| two Three |" in result
    assert "Intro." in result and "Closing." in result


def test_html_row_spans_keep_all_text_without_inventing_columns() -> None:
    """Verify html row spans keep all text without inventing columns."""
    result = reading_body(
        '<table><tr><td rowspan="2">Summary</td></tr><tr><td>Revenue</td><td>$42</td></tr></table>'
    )
    assert "Summary" in result and "Revenue" in result and "$42" in result
    assert "| --- |" not in result


def test_html_column_spans_preserve_financial_table_grid() -> None:
    result = reading_body(
        '<table><tr><th>Metric</th><th colspan="2">Quarter</th></tr>'
        "<tr><td>Revenue</td><td>Actual</td><td>Plan</td></tr>"
        '<tr><td>Total</td><td colspan="2">$42</td></tr></table>'
    )
    assert "| Metric | Quarter |  |\n| --- | --- | --- |" in result
    assert "| Revenue | Actual | Plan |" in result
    assert "| Total | $42 |  |" in result
