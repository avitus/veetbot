"""The persona prefix row: zero-byte emptiness, placement, trust, rendering."""

from datetime import UTC, datetime
from uuid import UUID

from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import SystemMessage, TextPart
from agent_core.domain.persona import (
    PersonaDocument,
    PersonaEntry,
    PersonaEntrySource,
    render_persona,
)
from agent_core.domain.policies import TrustLevel
from tests.contract.support import agent

NOW = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)


def test_empty_persona_renders_zero_bytes() -> None:
    bare = build_prefix(agent(), [])
    explicit = build_prefix(agent(), [], persona="")
    assert prefix_bytes(bare, []) == prefix_bytes(explicit, [])
    assert len(bare) == len(explicit) == 3


def test_persona_renders_at_index_two_as_trusted_unenveloped_system_text() -> None:
    prefix = build_prefix(agent(), [], persona="User values direct answers.")
    assert len(prefix) == 4
    row = prefix[2]
    assert isinstance(row, SystemMessage)
    assert row.trust is TrustLevel.TRUSTED_CONFIGURATION
    part = row.content[0]
    assert isinstance(part, TextPart)
    assert part.text == "User values direct answers."
    assert "<untrusted" not in part.text
    tools_row = prefix[3]
    assert isinstance(tools_row, SystemMessage)
    tools_part = tools_row.content[0]
    assert isinstance(tools_part, TextPart)
    assert tools_part.text.startswith("Declared tools")


def test_render_persona_orders_entries_and_honors_the_ceiling() -> None:
    document = PersonaDocument(
        tenant_id="tenant-a",
        principal_id="principal-a",
        version=1,
        entries=(
            PersonaEntry(text="First truth.", source=PersonaEntrySource.USER_EDIT),
            PersonaEntry(
                text="Sensitive truth.",
                source=PersonaEntrySource.USER_EDIT,
                sensitivity=Sensitivity.RESTRICTED,
            ),
            PersonaEntry(
                text="Affirmed truth.",
                source=PersonaEntrySource.AFFIRMATION,
                source_belief_id=UUID("00000000-0000-0000-0000-000000000501"),
            ),
        ),
        source=PersonaEntrySource.USER_EDIT,
        created_at=NOW,
    )
    assert render_persona(document, ceiling=Sensitivity.RESTRICTED) == (
        "First truth.\nSensitive truth.\nAffirmed truth."
    )
    assert render_persona(document, ceiling=Sensitivity.INTERNAL) == (
        "First truth.\nAffirmed truth."
    )
    assert document.affirmed_belief_ids == (UUID("00000000-0000-0000-0000-000000000501"),)
    empty = PersonaDocument.empty("tenant-a", "principal-a", NOW)
    assert render_persona(empty, ceiling=Sensitivity.RESTRICTED) == ""
