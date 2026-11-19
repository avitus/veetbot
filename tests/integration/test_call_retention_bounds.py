"""Exercise real retention, rollback and retry with an unrelated owner history."""

import asyncio
import os
import resource
import sys
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, event, text
from sqlalchemy.orm import QueryContext

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.persistence.calls import PostgresCallStore
from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.sqlalchemy_models import EventRow
from agent_core.application.calling import CallService
from agent_core.bootstrap import build, build_call_worker
from agent_core.domain.calls import CallRecord
from tests.contract.support import NOW, principal
from tests.contract.test_bland_client_contract import CALL_ID
from tests.contract.test_call_source_erasure_contract import assert_call_source_erasure
from tests.gates.test_call_m27 import call_configuration
from tests.integration.m2_support import database_settings
from tests.integration.role_probe import release_probe_role
from tests.integration.test_call_role_privileges_m27 import (
    CALL_WORKER_TABLE_PRIVILEGES,
    worker_settings,
)


async def test_retention_skips_unrelated_history_and_retries_atomically(tmp_path: Path) -> None:
    settings = database_settings()
    # Set for the incident-scale measurement in the disposable database only.
    scale = os.environ.get("VEETBOT_RETENTION_SCALE") == "1"
    later = 2048 if scale else 257
    sessions, events = (19_425, 1_403_944 - later - 6) if scale else (12, 300)
    engine = create_engine(settings.database_url)
    owner = principal()
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("""
                CREATE TEMP TABLE background ON COMMIT DROP AS
                SELECT i, gen_random_uuid() AS sid FROM generate_series(1, :n) i
            """),
                {"n": sessions},
            )
            await conn.execute(
                text("""
                INSERT INTO sessions (id, tenant_id, principal_id, agent_id, agent_version,
                    status, created_at, updated_at)
                SELECT sid, :tenant, :owner, gen_random_uuid(), '1', 'active', now(), now()
                FROM background
            """),
                {"tenant": owner.tenant_id, "owner": owner.principal_id},
            )
            await conn.execute(
                text("""
                INSERT INTO events (session_id, sequence, event_type, actor_type, payload,
                    payload_schema_version, created_at)
                SELECT sid, n, 'background', 'runtime',
                    jsonb_build_object('text', repeat(md5(n::text), 16)), 1, now()
                FROM generate_series(1, CAST(:events AS integer)) n
                JOIN background ON i = 1 + (n - 1) % :sessions
            """),
                {"events": events, "sessions": sessions},
            )
    finally:
        await engine.dispose()

    clock = FixedClock(NOW + timedelta(days=31))
    async with build(settings=settings, storage="postgres", principal=owner, clock=clock) as app:
        service = CallService(app.uow_factory, clock, owner, call_configuration())
        async with app.uow_factory() as uow:
            await uow.calls.put(
                CallRecord(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    kind="call",
                    key=CALL_ID,
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                    payload={"status": "completed", "created_at": NOW.isoformat()},
                ),
                expected_revision=0,
            )

        async def retry() -> None:
            def bounded_load(target: EventRow, context: QueryContext) -> None:
                assert target.event_type != "background", "materialized unrelated event"
                # Two 64-row pages can overlap while the cursor advances.
                assert len(context.session.identity_map) <= 140, "retained prior payload pages"

            # Exercise more than one flush page in a single affected session.
            async with engine.begin() as conn:
                await conn.execute(
                    text("""
                    INSERT INTO events (session_id, run_id, sequence, event_type, actor_type,
                        payload, payload_schema_version, created_at)
                    SELECT e.session_id, e.run_id, n+5, 'assistant.message.completed', 'runtime',
                        jsonb_build_object('content', 'retained paraphrase'), 1, now()
                    FROM events e CROSS JOIN generate_series(1,:n) n
                    WHERE e.sequence=5 AND e.event_type='tool.call.proposed'
                """),
                    {"n": later},
                )
                await conn.execute(
                    text("""
                    INSERT INTO events (session_id, sequence, event_type, actor_type, payload,
                        payload_schema_version, created_at)
                    SELECT e.session_id, :seq, 'memory.belief.created', 'runtime',
                        jsonb_build_object('belief', jsonb_build_object('id', 'source-belief',
                        'authority','inferred','source_session_id',e.session_id::text,
                        'source_event_ids',jsonb_build_array(3), 'statement','original belief')),
                        1,now() FROM events e WHERE sequence=5 AND event_type='tool.call.proposed'
                """),
                    {"seq": later + 6},
                )
                await conn.execute(
                    text("""
                    INSERT INTO recall_traces (id, tenant_id, principal_id, session_id, trace,
                        created_at, operator_fields_expire_at)
                    SELECT gen_random_uuid(), s.tenant_id, s.principal_id, s.id,
                        '{"beliefs":[{"belief_id":"source-belief"}],"rendered":"original belief"}',
                        now(), now() FROM sessions s JOIN events e ON e.session_id=s.id
                        WHERE e.sequence=5 AND e.event_type='tool.call.proposed'
                """)
                )
                await conn.execute(
                    text("""
                    UPDATE events SET run_id=(SELECT run_id FROM events
                        WHERE event_type='tool.call.proposed' AND sequence=5),
                        payload='{"content":"cross-session paraphrase"}',
                        event_type='assistant.message.completed'
                    WHERE id=(SELECT min(id) FROM events WHERE event_type='background')
                """)
                )
                await conn.execute(
                    text("""
                    UPDATE events SET payload=payload || jsonb_build_object('belief',
                        jsonb_build_object('authority','inferred','id',1,
                        'source_session_id',session_id::text,'source_event_ids',jsonb_build_array(3)))
                    WHERE event_type='assistant.message.completed' AND sequence=3
                """)
                )
                await conn.execute(
                    text("""
                    INSERT INTO recall_traces (id, tenant_id, principal_id, session_id, trace,
                        created_at, operator_fields_expire_at)
                    SELECT gen_random_uuid(), t.tenant_id,t.principal_id,t.session_id,
                        '{"beliefs":[{"belief_id":"1"}],"rendered":"keep numeric lookalike"}',
                        now(),now() FROM recall_traces t CROSS JOIN generate_series(1,70)
                """)
                )
                # A second owner in this tenant and a second tenant read the same call ID.
                await conn.execute(
                    text("""
                    WITH foreign_sessions AS (
                        INSERT INTO sessions (id, tenant_id, principal_id, agent_id, agent_version,
                            status, created_at, updated_at)
                        SELECT gen_random_uuid(), t, p, gen_random_uuid(),'1','active',now(),now()
                        FROM (VALUES (:tenant, 'foreign'), ('foreign', :owner)) AS pairs(t,p)
                        RETURNING id)
                    INSERT INTO events (session_id, sequence, event_type, actor_type, payload,
                        payload_schema_version, created_at)
                    SELECT id, 1, 'foreign.read', 'runtime', jsonb_build_object('name',
                        'mcp.bland_read.get_call', 'source', 'bland', 'call_id', CAST(:key AS text),
                        'transcript','foreign text'), 1, now() FROM foreign_sessions
                """),
                    {"tenant": owner.tenant_id, "owner": owner.principal_id, "key": CALL_ID},
                )

                # Run and session FKs are independent. An inconsistent historical
                # event must never enroll a different principal's run in the erasure.
                await conn.execute(
                    text("""
                    WITH foreign_run AS (
                        INSERT INTO runs (id,session_id,tenant_id,agent_id,agent_version,
                            status,limits,usage,created_at,updated_at,final_message)
                        SELECT gen_random_uuid(),id,tenant_id,agent_id,agent_version,
                            'COMPLETED','{}','{}',now(),now(),'foreign final'
                        FROM sessions WHERE principal_id='foreign' RETURNING id)
                    UPDATE events SET run_id=(SELECT id FROM foreign_run)
                    WHERE event_type='memory.belief.created'
                """)
                )

            membership_reads = 0

            def count_membership(
                _conn: Any, _cursor: Any, statement: str, _params: Any, _context: Any, _many: bool
            ) -> None:
                nonlocal membership_reads
                if "SELECT belief_id FROM pg_temp.call_beliefs WHERE" in statement:
                    membership_reads += 1

            original = PostgresCallStore.put
            with pytest.MonkeyPatch.context() as patch:

                async def interrupt(
                    self: PostgresCallStore, record: CallRecord, *, expected_revision: int
                ) -> CallRecord:
                    if record.payload.get("erased"):
                        raise InterruptedError("before tombstone")
                    return await original(self, record, expected_revision=expected_revision)

                patch.setattr(PostgresCallStore, "put", interrupt)
                event.listen(Engine, "before_cursor_execute", count_membership)
                event.listen(EventRow, "load", bounded_load)
                try:
                    with pytest.raises(InterruptedError, match="before tombstone"):
                        await service.delete(owner, CALL_ID)
                finally:
                    event.remove(Engine, "before_cursor_execute", count_membership)
                    event.remove(EventRow, "load", bounded_load)
            assert membership_reads <= 2
            async with app.uow_factory() as uow:
                retained = await uow.calls.get(owner, "call", CALL_ID)
                assert retained and retained.payload["status"] == "completed"
            if scale:
                async with release_probe_role(
                    "retention_benchmark", CALL_WORKER_TABLE_PRIVILEGES
                ) as url:
                    for mode, status in (("crash", 73), ("retry", 0)):
                        process = await asyncio.create_subprocess_exec(
                            sys.executable,
                            "-m",
                            "tests.integration.test_call_retention_bounds",
                            mode,
                            str(tmp_path),
                            env={**os.environ, "DATABASE_URL": url},
                        )
                        assert await process.wait() == status
                        if mode == "crash":
                            await assert_rollback()
            else:
                await assert_rollback()
                assert await service.expire() == 1
            async with engine.connect() as conn:
                assert not await conn.scalar(
                    text("""
                    SELECT count(*) FROM events WHERE payload->>'content'='cross-session paraphrase'
                """)
                )
                assert (
                    await conn.scalar(
                        text("""
                    SELECT count(*) FROM recall_traces
                    WHERE trace->>'rendered'='keep numeric lookalike'
                """)
                    )
                    == 70
                )
                assert (
                    await conn.scalar(
                        text("SELECT count(*) FROM runs WHERE final_message='foreign final'")
                    )
                    == 1
                )
                assert not await conn.scalar(
                    text("""SELECT count(*) FROM events
                    WHERE event_type='assistant.message.completed'
                    AND payload->>'content'='retained paraphrase'""")
                )
                assert (
                    await conn.scalar(
                        text("""SELECT count(*) FROM events WHERE
                    event_type='foreign.read' AND payload->>'transcript'='foreign text'""")
                    )
                    == 2
                )
                assert (
                    await conn.scalar(
                        text("""SELECT count(*) FROM recall_traces WHERE
                    trace->>'rendered'='' AND trace->'beliefs'='[]'::jsonb""")
                    )
                    == 1
                )
                assert (
                    await conn.scalar(
                        text("""SELECT payload->'belief'->>'statement' FROM events
                    WHERE event_type='memory.belief.created'""")
                    )
                    == "[email source erased]"
                )
            async with app.uow_factory() as uow:
                erased = await uow.calls.get(owner, "call", CALL_ID)
                assert erased and erased.payload["erased"]
            assert await service.expire() == 0

        async def assert_rollback() -> None:
            async with engine.connect() as conn:
                assert (
                    await conn.scalar(
                        text("""SELECT count(*) FROM events
                    WHERE event_type='assistant.message.completed'
                    AND payload->>'content'='retained paraphrase'""")
                    )
                    == later
                )
                assert await conn.scalar(text("SELECT count(*) FROM checkpoints")) == 1
                assert (
                    await conn.scalar(
                        text("""SELECT count(*) FROM call_records
                    WHERE kind='call' AND payload->>'status'='completed'""")
                    )
                    == 1
                )

        try:
            await assert_call_source_erasure(app.uow_factory, retry)
        finally:
            await engine.dispose()


async def _measure_worker() -> None:
    settings = worker_settings(Path(sys.argv[2]), database_settings().database_url)
    if sys.argv[1] == "crash":
        original = PostgresCallStore.put

        async def crash(
            store: PostgresCallStore, record: CallRecord, *, expected_revision: int
        ) -> CallRecord:
            if record.payload.get("erased"):
                os._exit(73)  # Disconnect before the worker can commit the tombstone.
            return await original(store, record, expected_revision=expected_revision)

        pytest.MonkeyPatch().setattr(PostgresCallStore, "put", crash)
    async with build_call_worker(
        settings=settings, clock=FixedClock(NOW + timedelta(days=31))
    ) as app:
        assert app.worker is not None
        started = time.monotonic()
        assert await app.worker.service.expire() == 1
        print(
            f"Cold worker expiry {time.monotonic() - started:.2f}s, "
            f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss} KiB",
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(_measure_worker())
