"""Memories that bulk mail already formed leave through source exclusion (ADR-0116).

The census identifies the affected threads; the existing exclusion path erases
their derived memories and blocks re-formation. The operator command applies it
in one pass, previewing first and changing nothing without ``--confirm``.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from typer.testing import CliRunner

from agent_core.bootstrap import build
from agent_core.cli.main import app
from agent_core.domain.email import EmailRecord
from agent_core.domain.email_semantics import thread_source_key
from agent_core.domain.messages import FakeModelScript
from agent_core.domain.runs import RunStatus
from tests.contract.memory_fixtures import browse_query
from tests.gates.test_email_m18 import _email_settings
from tests.unit.test_email_refresh_bulk_formation import _mail, _people_assessment


async def test_exclude_bulk_sources_previews_then_erases_only_census_threads() -> None:
    settings = replace(
        _email_settings(),
        email_mode_enabled=True,
        people_enabled=True,
        email_unsubscribe_enabled=True,
    )
    async with build(
        settings=settings,
        script=FakeModelScript(turns=[_people_assessment(bulk=False)]),
        mcp_client_factory=await _mail(census=False),
    ) as app_:
        principal = app_.principal
        operation = await app_.services.email.submit_task(principal, kind="refresh")
        run = await app_.runs.get(operation.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        query = browse_query(tenant_id=principal.tenant_id, principal_id=principal.principal_id)
        async with app_.uow_factory() as uow:
            assert len(await uow.memories.browse(query)) == 1
            [thread] = await uow.email.list(principal, "thread")
            # The sender is recognized as bulk only after the memory formed, as in production.
            stamp = datetime(2026, 9, 22, tzinfo=UTC)
            await uow.email.put(
                EmailRecord(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    kind="subscription_thread",
                    key=thread_source_key(
                        str(thread.payload["account_id"]), str(thread.payload["provider_thread_id"])
                    ),
                    revision=1,
                    payload={"subscription_id": "census-row"},
                    created_at=stamp,
                    updated_at=stamp,
                ),
                expected_revision=0,
            )

        preview = await app_.services.email.exclude_bulk_sources(principal, confirm=False)
        assert preview.confirmed is False
        assert [str(item.thread_id) for item in preview.candidates] == [thread.key]
        assert preview.excluded == []
        async with app_.uow_factory() as uow:
            assert len(await uow.memories.browse(query)) == 1, "a preview changes nothing"

        applied = await app_.services.email.exclude_bulk_sources(principal, confirm=True)
        assert applied.confirmed is True
        assert [str(item.thread_id) for item in applied.excluded] == [thread.key]
        assert applied.excluded[0].status in {"erased", "cleanup_pending"}
        async with app_.uow_factory() as uow:
            assert await uow.memories.browse(query) == []
            assert await uow.email.get(principal, "thread", thread.key) is None
            [tombstone] = await uow.email.list(principal, "excluded_source")
            assert tombstone.payload["thread_id"] == thread.key

        # Idempotent: the excluded thread is gone, so a second pass finds nothing.
        again = await app_.services.email.exclude_bulk_sources(principal, confirm=True)
        assert again.candidates == [] and again.excluded == []


def test_exclude_bulk_is_an_operator_command_that_requires_confirmation() -> None:
    result = CliRunner().invoke(app, ["email", "exclude-bulk", "--help"])
    assert result.exit_code == 0, result.output
    assert "--confirm" in result.output
