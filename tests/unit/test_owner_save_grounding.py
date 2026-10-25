"""Owner saves after web reads: grounded in the owner's words, never in what the turn read.

Production run 724c26cf refused "Andy requested that pruning schedules for
roses, hydrangeas, hibiscus and azaleas be added" because, after a web
search, only a sixteen-character verbatim span of the owner's message could
lift a statement to USER trust, while run 7e373f3d committed the same save
only because it was proposed in the step before the web result existed.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_core.bootstrap import DEFAULT_AGENT_INSTRUCTIONS
from agent_core.domain.messages import TextPart, ToolCallItem, ToolResultItem, UserMessage
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.tools.executor import _argument_trust, owner_grounded_statement
from agent_core.tools.messages import message_for
from tests.contract.support import NOW

OWNER = (
    "Look up pruning seasons, then remember that pruning schedules for roses, "
    "hydrangeas, hibiscus and azaleas belong on the calendar."
)
WEB = "Prune roses in late winter and hydrangeas after flowering. Feed azaleas in spring."
PARAPHRASE = "Andy wants pruning schedules for roses, hydrangeas, hibiscus and azaleas added."


def _checkpoint(
    *, owner: str = OWNER, web: str | None = WEB, owner_trust: TrustLevel = TrustLevel.USER
) -> RunCheckpoint:
    conversation: list[object] = [UserMessage(content=[TextPart(text=owner)], trust=owner_trust)]
    if web is not None:
        conversation.extend(
            [
                ToolCallItem(
                    call_id="web",
                    item_index=0,
                    name="web.search",
                    arguments={"query": "pruning seasons"},
                    raw_arguments='{"query":"pruning seasons"}',
                ),
                ToolResultItem(
                    call_id="web",
                    content=[TextPart(text=web)],
                    trust=TrustLevel.EXTERNAL_UNTRUSTED,
                ),
            ]
        )
    return RunCheckpoint(
        run_id=UUID(int=724),
        version=1,
        status=RunStatus.RUNNING,
        conversation=conversation,  # type: ignore[arg-type]
        created_at=NOW,
    )


def test_a_paraphrase_of_the_owner_message_is_grounded_after_a_web_read() -> None:
    assert owner_grounded_statement(PARAPHRASE, _checkpoint())
    trust = _argument_trust({"statement": PARAPHRASE}, _checkpoint(), tool_name="memory.remember")
    assert trust == {"statement": TrustLevel.USER}


def test_the_verbatim_span_rule_still_raises() -> None:
    verbatim = (
        "pruning schedules for roses, hydrangeas, hibiscus and azaleas belong on the calendar"
    )
    trust = _argument_trust({"statement": verbatim}, _checkpoint(), tool_name="memory.remember")
    assert trust == {"statement": TrustLevel.USER}


def test_a_statement_carrying_the_web_results_words_is_not_grounded() -> None:
    derived = "Prune roses in late winter and hydrangeas after flowering; hibiscus follows."
    assert not owner_grounded_statement(derived, _checkpoint())
    trust = _argument_trust({"statement": derived}, _checkpoint(), tool_name="memory.remember")
    assert trust == {"statement": TrustLevel.EXTERNAL_UNTRUSTED}


def test_grounding_does_not_depend_on_whether_the_web_result_exists_yet() -> None:
    before = owner_grounded_statement(PARAPHRASE, _checkpoint(web=None))
    after = owner_grounded_statement(PARAPHRASE, _checkpoint())
    assert before is after is True


def test_a_device_ingested_or_surface_message_never_lifts_a_statement() -> None:
    ingested = _checkpoint(owner_trust=TrustLevel.EXTERNAL_UNTRUSTED)
    assert not owner_grounded_statement(PARAPHRASE, ingested)
    assert _argument_trust({"statement": PARAPHRASE}, ingested, tool_name="memory.remember") == {
        "statement": TrustLevel.EXTERNAL_UNTRUSTED
    }


def test_too_much_of_the_models_own_phrasing_is_not_grounded() -> None:
    padded = "Andy apparently intends eventually to organise pruning schedules for roses soon."
    assert not owner_grounded_statement(padded, _checkpoint())


def test_other_tools_keep_the_verbatim_rule_only() -> None:
    trust = _argument_trust({"owner_quote": PARAPHRASE}, _checkpoint(), tool_name="email.context")
    assert trust == {"owner_quote": TrustLevel.EXTERNAL_UNTRUSTED}
    trust = _argument_trust({"statement": PARAPHRASE}, _checkpoint())
    assert trust == {"statement": TrustLevel.EXTERNAL_UNTRUSTED}


@settings(max_examples=60, deadline=None)
@given(
    st.lists(
        st.sampled_from(["winter", "flowering", "feed", "spring", "prune", "mulch", "fertiliser"]),
        min_size=1,
        max_size=4,
    )
)
def test_web_words_in_any_position_are_refused(web_words: list[str]) -> None:
    checkpoint = _checkpoint(web=WEB + " Mulch with fertiliser.")
    statement = f"Andy wants pruning schedules for roses and {' '.join(web_words)} added."
    assert not owner_grounded_statement(statement, checkpoint)


def test_the_closed_refusal_has_corrective_guidance() -> None:
    guidance = message_for("tool.memory.statement_not_owner_grounded")
    assert "owner" in guidance and "artifact" in guidance
    assert "Do not report this refusal" in guidance


def test_agent_instructions_keep_derived_plans_out_of_memory_and_unrequested_failures_quiet() -> (
    None
):
    assert "only what the owner stated" in DEFAULT_AGENT_INSTRUCTIONS
    assert "never in memory" in DEFAULT_AGENT_INSTRUCTIONS
    assert "Never mention a save, or a failed save, the owner did not ask for" in (
        DEFAULT_AGENT_INSTRUCTIONS
    )


@pytest.mark.parametrize("statement", ["", "roses", "the and of"])
def test_statements_below_the_minimum_are_never_grounded(statement: str) -> None:
    assert not owner_grounded_statement(statement, _checkpoint())


QUIET_WEB = "Weather today is mild with light wind."


@pytest.mark.parametrize(
    ("owner", "statement"),
    [
        # An introduced negation reverses what the owner said.
        (
            "Remember that I eat pork every Sunday at lunch.",
            "I do not eat pork every Sunday at lunch.",
        ),
        (
            "Remember that I eat pork every Sunday at lunch.",
            "I never eat pork every Sunday at lunch.",
        ),
        # A changed modal turns a possibility into a commitment, or the reverse.
        (
            "Remember that I may transfer funds to savings next month.",
            "I will transfer funds to savings next month.",
        ),
        (
            "Remember that I must renew the passport before March.",
            "I might renew the passport before March.",
        ),
        # Dropping a negation or a hedge changes the meaning just as much.
        ("Remember that I never eat pork on Sundays.", "I eat pork on Sundays."),
        ("Remember that I might move to Lisbon next year.", "Andy moves to Lisbon next year."),
        ("Remember that I don't drink coffee after noon.", "Andy drinks coffee after noon."),
        ("Remember that I have no siblings in Portugal.", "Andy has siblings in Portugal."),
        # Words that frame a request elsewhere are still the statement's own markers,
        # and in the middle of an owner sentence they are what the owner states.
        (
            "Remember that I forget my sister's birthday every year.",
            "I never forget my sister's birthday every year.",
        ),
        (
            "Remember that I forget my sister's birthday every year.",
            "I do not forget my sister's birthday every year.",
        ),
        (
            "Remember that I never forget my sister's birthday.",
            "I forget my sister's birthday.",
        ),
        # A negation borrowed from another of the owner's sentences is not the owner's
        # statement about this one.
        (
            "Remember that I eat pork every Sunday. I can't stand olives.",
            "I do not eat pork every Sunday.",
        ),
        (
            "Remember that I eat pork every Sunday. I never liked olives.",
            "I never eat pork every Sunday.",
        ),
        # Markers are counted: dropping one of two negations is still a drop.
        (
            "Remember that I do not eat pork and do not eat beef.",
            "I do not eat pork and eat beef.",
        ),
    ],
)
def test_a_statement_that_changes_negation_or_modality_is_not_grounded(
    owner: str, statement: str
) -> None:
    """Content-word overlap is not provenance when the polarity or modality moved."""

    assert not owner_grounded_statement(statement, _checkpoint(owner=owner, web=QUIET_WEB))


@pytest.mark.parametrize(
    ("owner", "statement"),
    [
        ("Remember that I never eat pork on Sundays.", "Andy never eats pork on Sundays."),
        (
            "Remember that I may transfer funds to savings next month.",
            "Andy may transfer funds to savings next month.",
        ),
        (
            "Could you remember that I don't drink coffee after noon? Also find decaf brands.",
            "Andy does not drink coffee after noon.",
        ),
        ("Don't forget that my sister lives in Porto.", "Andy's sister lives in Porto."),
        ("Please, don't forget that my sister lives in Porto.", "Andy's sister lives in Porto."),
        (
            "Remember that I never eat pork. I never drink wine.",
            "I never eat pork and never drink wine.",
        ),
    ],
)
def test_a_statement_that_keeps_negation_and_modality_stays_grounded(
    owner: str, statement: str
) -> None:
    """Request framing ("could you", "don't forget") is not part of what is restated."""

    assert owner_grounded_statement(statement, _checkpoint(owner=owner, web=QUIET_WEB))
