"""Synthetic mail only: reading cleanup must never replace source evidence."""

import time
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
