"""Shared scoped email-store behavior on the deterministic and PostgreSQL tiers."""

from datetime import timedelta

import pytest

from agent_core.domain.email import EmailRecord
from agent_core.domain.errors import ConflictError
from agent_core.ports.email import EmailStore
from tests.contract.support import NOW, PRINCIPAL_ID, TENANT, memory_uow_factory, principal


def record(key: str = "a", *, revision: int = 1) -> EmailRecord:
    return EmailRecord(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        kind="draft",
        key=key,
        revision=revision,
        payload={"body": "First draft", "nested": {"items": ["one"]}},
        created_at=NOW,
        updated_at=NOW + timedelta(seconds=revision),
    )


async def assert_email_store_contract(store: EmailStore) -> None:
    """Every backend runs this same mutation, scoping, cursor and aliasing contract."""
    owner = principal()
    foreign_owner = owner.model_copy(update={"principal_id": "other"})
    foreign_tenant = owner.model_copy(update={"tenant_id": "other"})
    assert await store.get(owner, "draft", "a") is None
    first = record()
    stored = await store.put(first, expected_revision=0)
    first.payload["body"] = "Changed caller object"
    stored.payload["body"] = "Changed returned object"
    fetched = await store.get(owner, "draft", "a")
    assert fetched is not None
    assert fetched.payload["body"] == "First draft"
    nested = fetched.payload["nested"]
    assert isinstance(nested, dict)
    nested["items"].append("Changed")
    reread = await store.get(owner, "draft", "a")
    assert reread is not None
    assert reread.payload["nested"] == {"items": ["one"]}
    assert await store.get(foreign_owner, "draft", "a") is None
    assert await store.get(foreign_tenant, "draft", "a") is None
    assert await store.get(owner, "thread", "a") is None
    assert await store.list(foreign_owner, "draft") == []
    assert await store.list(foreign_tenant, "draft") == []
    for expected, revision in ((0, 1), (0, 2), (1, 3), (2, 3), (-1, 1)):
        with pytest.raises(ConflictError):
            await store.put(record(revision=revision), expected_revision=expected)
    changed = await store.put(record(revision=2), expected_revision=1)
    assert changed.revision == 2
    await store.put(record("c"), expected_revision=0)
    await store.put(record("b"), expected_revision=0)
    assert [item.key for item in await store.list(owner, "draft", limit=2)] == ["a", "b"]
    assert [item.key for item in await store.list(owner, "draft", after="b", limit=2)] == ["c"]
    listed = await store.list(owner, "draft", limit=1)
    listed[0].payload["body"] = "Changed list result"
    unchanged = await store.get(owner, "draft", "a")
    assert unchanged is not None
    assert unchanged.payload["body"] == "First draft"
    for boundary in (0, -1, 1001):
        with pytest.raises(ValueError):
            await store.list(owner, "draft", limit=boundary)
    for actor, expected in ((owner, 1), (foreign_owner, 2), (foreign_tenant, 2)):
        with pytest.raises(ConflictError):
            await store.delete(actor, "draft", "a", expected_revision=expected)
    await store.delete(owner, "draft", "a", expected_revision=2)
    assert await store.get(owner, "draft", "a") is None
    with pytest.raises(ConflictError):
        await store.delete(owner, "draft", "a", expected_revision=2)


async def test_email_store_is_available_and_passes_shared_contract() -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        store = getattr(uow, "email", None)
        assert store is not None, "the ordinary unit of work must expose the email store"
        await assert_email_store_contract(store)


async def test_memory_email_lock_serializes_and_releases_after_error() -> None:
    import asyncio

    from agent_core.adapters.persistence.email import InMemoryEmailStore

    store = InMemoryEmailStore()
    entered = asyncio.Event()

    async def contender() -> None:
        async with store.lock(principal()):
            entered.set()

    class IntentionalFailureError(Exception):
        pass

    task = None
    try:
        with pytest.raises(IntentionalFailureError):
            async with store.lock(principal()):
                task = asyncio.create_task(contender())
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(entered.wait(), timeout=0.05)
                other = principal().model_copy(update={"principal_id": "other"})
                async with asyncio.timeout(1):
                    async with store.lock(other):
                        pass
                raise IntentionalFailureError
        assert task is not None
        await asyncio.wait_for(task, timeout=1)
        assert entered.is_set()
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def assert_task_admission_query_preserves_unsettled_reservations(store: EmailStore) -> None:
    for key, age, settled in (("old", 31, "1"), ("pending", 40, None), ("recent", 1, "1")):
        await store.put(
            record(key).model_copy(
                update={
                    "kind": "task",
                    "payload": {
                        "created_at": (NOW - timedelta(days=age)).isoformat(),
                        "settled_cost": settled,
                    },
                }
            ),
            expected_revision=0,
        )
    rows = await store.list_tasks(principal(), created_since=NOW - timedelta(days=30), limit=1)
    assert [row.key for row in rows] == ["pending"]
    rows = await store.list_tasks(
        principal(), created_since=NOW - timedelta(days=30), after="pending"
    )
    assert [row.key for row in rows] == ["recent"]
    assert [row.key for row in await store.list_tasks(principal())] == ["pending"]
    assert (
        await store.list_tasks(
            principal().model_copy(update={"principal_id": "foreign"}),
            created_since=NOW - timedelta(days=30),
        )
        == []
    )
    assert await store.get(principal(), "task", "old") is not None


async def test_task_admission_query_preserves_unsettled_reservations() -> None:
    from agent_core.adapters.persistence.email import InMemoryEmailStore

    await assert_task_admission_query_preserves_unsettled_reservations(InMemoryEmailStore())


async def email_import_window_contract(store: EmailStore) -> None:
    for key, days, account in [
        ("late", 2, "work"),
        ("early", 0, "work"),
        ("legacy", 1, "work"),
        ("excluded", 1, "work"),
        ("foreign", 1, "home"),
    ]:
        await store.put(
            record(key).model_copy(
                update={
                    "kind": "semantic_source",
                    "payload": {
                        "account_id": account,
                        "evidence_at": (NOW + timedelta(days=days)).isoformat(),
                        **({} if key == "legacy" else {"excluded": key == "excluded"}),
                    },
                }
            ),
            expected_revision=0,
        )
    read = getattr(store, "list_semantic_window", None)
    assert read is not None, "historical email imports need a bounded chronological source reader"
    options = {"account_ids": ["work"], "since": NOW, "until": NOW + timedelta(days=3), "limit": 1}
    first = await read(principal(), **options)
    assert [row.key for row in first] == ["early"]
    second = await read(principal(), after=(NOW, "early"), **options)
    assert [row.key for row in second] == ["legacy"]
    third = await read(principal(), after=(NOW + timedelta(days=1), "legacy"), **options)
    assert [row.key for row in third] == ["late"]
    assert await read(principal().model_copy(update={"principal_id": "foreign"}), **options) == []


async def test_email_import_pages_are_scoped_and_chronological() -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        await email_import_window_contract(uow.email)


def thread_record(key: str) -> EmailRecord:
    return record(key).model_copy(
        update={
            "kind": "thread",
            "payload": {
                "id": key,
                "subject": f"Subject {key}",
                "senders": ["sender@example.test"],
                "messages": [{"id": f"{key}-1", "body": "Private body"}],
            },
        }
    )


async def thread_summary_contract(store: EmailStore) -> None:
    """Listing reads every thread field except message content, in key-ordered pages."""
    for key in ("summary-c", "summary-a", "summary-b"):
        await store.put(thread_record(key), expected_revision=0)
    await store.put(record("summary-draft"), expected_revision=0)
    read = getattr(store, "list_thread_summaries", None)
    assert read is not None, "the inbox list needs a thread reader that omits message content"
    first = await read(principal(), limit=2)
    assert [row.key for row in first] == ["summary-a", "summary-b"]
    assert [row.key for row in await read(principal(), after="summary-b")] == ["summary-c"]
    expected = thread_record("summary-a")
    assert first[0] == expected.model_copy(
        update={"payload": {k: v for k, v in expected.payload.items() if k != "messages"}}
    )
    first[0].payload["senders"].append("changed@example.test")
    [again] = await read(principal(), limit=1)
    assert again.payload["senders"] == ["sender@example.test"]
    stored = await store.get(principal(), "thread", "summary-a")
    assert stored is not None and stored.payload["messages"] == expected.payload["messages"]
    for owner in (
        principal().model_copy(update={"principal_id": "foreign"}),
        principal().model_copy(update={"tenant_id": "foreign"}),
    ):
        assert await read(owner) == []
    for boundary in (0, -1, 1001):
        with pytest.raises(ValueError):
            await read(principal(), limit=boundary)


async def test_thread_summaries_omit_message_content() -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        await thread_summary_contract(uow.email)


async def semantic_source_timestamp_contract(store: EmailStore) -> None:
    """Both stores validate active sources before any insert or revision change."""
    from datetime import timezone

    for index, timestamp in enumerate(
        ["2026-01-01T00:00:00", "invalid", None, 123, "2026-02-30T00:00:00Z"]
    ):
        source = record(f"invalid-{index}").model_copy(
            update={
                "kind": "semantic_source",
                "payload": {"account_id": "work", "evidence_at": timestamp},
            }
        )
        with pytest.raises(ValueError, match="invalid retained email source"):
            await store.put(source, expected_revision=0)
        assert await store.get(principal(), "semantic_source", source.key) is None
    source = record("aware-source").model_copy(
        update={
            "kind": "semantic_source",
            "payload": {
                "account_id": "work",
                "evidence_at": NOW.astimezone(timezone(timedelta(hours=5))).isoformat(),
            },
        }
    )
    await store.put(source, expected_revision=0)
    with pytest.raises(ValueError, match="invalid retained email source"):
        await store.put(
            source.model_copy(
                update={
                    "revision": 2,
                    "payload": {**source.payload, "evidence_at": "2026-01-01T00:00:00"},
                }
            ),
            expected_revision=1,
        )
    retained = await store.get(principal(), "semantic_source", source.key)
    assert retained is not None and retained.revision == 1
    # Legacy exclusion receipts remain writable so validation cannot block erasure.
    await store.put(
        record("excluded-legacy").model_copy(
            update={"kind": "semantic_source", "payload": {"account_id": "work", "excluded": True}}
        ),
        expected_revision=0,
    )
    page = await store.list_semantic_window(
        principal(), account_ids=["work"], since=NOW, until=NOW + timedelta(seconds=1), limit=10
    )
    assert source.key in {row.key for row in page}


async def test_memory_semantic_source_timestamps_are_validated_before_writes() -> None:
    _, factory = await memory_uow_factory()
    async with factory() as uow:
        await semantic_source_timestamp_contract(uow.email)
