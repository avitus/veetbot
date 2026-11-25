"""ADR-0355 replay through the ordinary refresh, mailbox, and full model runner."""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.domain.mcp import MCPCallResult
from agent_core.domain.messages import ModelUsage
from agent_core.domain.runs import RunStatus
from tests.gates.test_email_arrival_checks_m26 import (
    NOW,
    _app,
    _caught_up,
    _event_types,
    _limit,
    _mailbox,
    _seed_spend,
    _thread_page,
)
from tests.gates.test_email_runtime_m26 import _assessment_turn


async def _backlog(
    calls: list[str], *, label_board: bool = True, read_clock: FixedClock | None = None
) -> Any:
    base = await _mailbox([], [])
    pending = ["board", *(f"bulk-{n}" for n in range(12))]

    def factory(*args: Any) -> Any:
        client = base(*args)
        original = client.call_tool

        async def call(name: str, arguments: dict[str, Any]) -> MCPCallResult:
            value: dict[str, Any]
            if name == "sync_changes":
                value = {
                    "schema_version": 1,
                    "history_id": "114",
                    "changes": [
                        {
                            "history_id": str(101 + n),
                            "kind": "message_added",
                            "thread_id": item,
                            "message_id": f"{item}-m",
                            "label_ids": [
                                "INBOX",
                                *(["IMPORTANT"] if label_board and item == "board" else []),
                                *(["CATEGORY_PROMOTIONS"] if item != "board" else []),
                            ],
                        }
                        for n, item in enumerate(pending)
                    ],
                    "next_page_token": None,
                    "resync_required": False,
                }
                pending.clear()
            elif name == "get_thread_page":
                item = arguments["thread_id"]
                calls.append(item)
                if read_clock:
                    read_clock.advance(timedelta(seconds=2))
                value = _thread_page(item, NOW - timedelta(minutes=2 if item == "board" else 1))
                message = value["messages"][0]
                message["subject"] = item
                if item == "board":
                    if label_board:
                        message["label_ids"].append("IMPORTANT")
                else:
                    message["from"] = "Offers <news@example.test>"
                    message["body"] = "Our weekly sale."
            else:
                return cast(MCPCallResult, await original(name, arguments))
            return MCPCallResult(content=(json.dumps(value),), structured=value)

        vars(client)["call_tool"] = call
        return client

    return factory


@pytest.mark.parametrize("budget_held", [False, True])
async def test_board_mail_surfaces_before_newer_bulk(budget_held: bool) -> None:
    clock = FixedClock(NOW)
    reads: list[str] = []
    turns = [_assessment_turn() for _ in range(8)]
    async with _app(clock, await _backlog(reads), *turns) as app:
        await _caught_up(app)
        service = app.services.email
        service.triage_mode = "local"
        _limit(app)
        if budget_held:
            await _seed_spend(app, "600", NOW - timedelta(days=29, hours=23, minutes=59))
        check = await service.admit_arrival_check(app.principal)
        assert check is not None, "Budget must still admit discovery"
        run = await app.runs.get(check.run_id)
        assert run.status is RunStatus.COMPLETED
        assert reads[0] == "board"
        inbox = await service.threads(app.principal, view="inbox", limit=100)
        row = next(row for row in inbox["items"] if row["subject"] == "board")
        if budget_held:
            assert row["priority"] == 0 and "budget" in row["reason"].lower()
            assert run.model_call_count == 0
            assert "memory.formation.requested" not in await _event_types(app, run)
            clock.advance(timedelta(minutes=5))
            check = await service.admit_arrival_check(app.principal)
            assert check is not None
            run = await app.runs.get(check.run_id)
        important = await service.threads(app.principal, view="priority")
        assert [item["subject"] for item in important["items"]] == ["board"]
        assert run.model_call_count > 0
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, app.principal, run_id=run.id)
            [visible] = [e for e in events if e.event_type == "email.triage.important"]
            assert visible.payload["elapsed_ms"] == (7 if budget_held else 2) * 60_000
            assert (
                sum(
                    e.event_type == "model.request.started" and e.sequence < visible.sequence
                    for e in events
                )
                == 1
            )
            assessment = await uow.email.get(app.principal, "assessment", row["id"])
            assert assessment and assessment.payload["grounded"]
            assert assessment.payload["supported_evidence"] == [
                "Please approve the board materials."
            ]


async def test_first_page_of_long_arrival_contains_newest_message() -> None:
    from tests.gates.test_email_m26_gmail import Mailbox, invoke

    mailbox = Mailbox()
    first = await invoke(
        mailbox,
        "get_thread_page",
        {"thread_id": "thread-1", "max_messages": 1, "newest_first": True},
    )
    assert first.structured_content and first.structured_content["messages"][0]["id"] == "message-2"
    second = await invoke(
        mailbox,
        "get_thread_page",
        {
            "thread_id": "thread-1",
            "max_messages": 1,
            "page_token": first.structured_content["next_page_token"],
        },
    )
    assert (
        second.structured_content and second.structured_content["messages"][0]["id"] == "message-1"
    )


async def test_work_keep_gets_separate_positive_shadow_decision(tmp_path: Any) -> None:
    from dataclasses import replace

    from agent_core.adapters.judgment import FakeJudgmentProvider
    from agent_core.bootstrap import build
    from agent_core.domain.email import EmailTriageMode
    from agent_core.domain.judgment import NoulAnswer
    from agent_core.domain.messages import FakeModelScript
    from tests.unit.test_email_junk_pilot import pilot_decision, work_factory
    from tests.unit.test_email_junk_standing import standing_settings

    keep = pilot_decision(keep=True)
    positive = keep.model_copy(update={"answers": {"expedite": NoulAnswer(probability=0.97)}})
    provider = FakeJudgmentProvider([keep, positive])
    async with build(
        settings=replace(standing_settings(tmp_path), email_triage_mode=EmailTriageMode.SHADOW),
        judgment_provider_override=provider,
        script=FakeModelScript(turns=[_assessment_turn()]),
        mcp_client_factory=await work_factory(),
    ) as app:
        op = await app.services.email.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(op.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert [set(req.questions) for req in provider.requests] == [{"category"}, {"expedite"}]
        assert run.model_call_count == 1


async def test_receipt_to_important_improves_with_same_arrivals_and_allowance() -> None:
    """Measure collection membership, including the polls for which the item is absent."""
    elapsed = {}
    position = {}
    total_calls = {}
    settled_cost = {}
    for mode in ("off", "local"):
        clock = FixedClock(NOW)
        turn = _assessment_turn().model_copy(
            update={"usage": ModelUsage(provider="fake", model="scripted", cost=Decimal("0.10"))}
        )
        async with _app(clock, await _backlog([]), *[turn for _ in range(24)]) as app:
            await _caught_up(app)
            _limit(app)
            service = app.services.email
            service.triage_mode = mode
            total_calls[mode] = 0
            settled_cost[mode] = Decimal(0)
            for _ in range(8):
                op = await service.admit_arrival_check(app.principal)
                assert op is not None
                run = await app.runs.get(op.run_id)
                assert run.status is RunStatus.COMPLETED, run.failure
                task = await service.get_task(app.principal, run.id)
                assert task.settled_cost is not None
                settled_cost[mode] += task.settled_cost
                async with app.uow_factory() as uow:
                    events = await uow.events.list_after(
                        run.session_id, 0, app.principal, run_id=run.id
                    )
                for event in events:
                    if event.event_type == "model.request.started":
                        total_calls[mode] += 1
                    elif event.event_type == "email.triage.important":
                        assert mode not in elapsed
                        elapsed[mode] = event.payload["elapsed_ms"]
                        position[mode] = total_calls[mode]
                page = await service.threads(app.principal, view="priority")
                assert any(item["subject"] == "board" for item in page["items"]) == (
                    mode in elapsed
                )
                if task.empty_check:
                    break
                clock.advance(timedelta(minutes=5))
            assert task.empty_check, "Both queues must drain before comparing cost"
    assert elapsed == {"off": 1_020_000, "local": 120_000}
    assert position == {"off": 13, "local": 1}
    assert total_calls["off"] == total_calls["local"] == 13
    assert settled_cost["off"] == settled_cost["local"] == Decimal("1.30")


async def test_older_page_preserves_important_until_new_evidence_is_assessed() -> None:
    async with _app(
        FixedClock(NOW), await _backlog([]), *[_assessment_turn() for _ in range(8)]
    ) as app:
        await _caught_up(app)
        service = app.services.email
        service.triage_mode = "local"
        _limit(app)
        op = await service.admit_arrival_check(app.principal)
        run = await app.runs.get(op.run_id)
        first = _thread_page("board", NOW - timedelta(minutes=2))
        first["messages"][0].update(subject="board", label_ids=["IMPORTANT", "INBOX"])
        older = _thread_page("old", NOW - timedelta(days=1))["messages"][0]
        older["thread_id"] = "board"
        first.update(messages=[older, *first["messages"]], newest_first=True)
        updated = await service.import_thread(
            app.principal, "default", first, run.session_id, observed_at=NOW
        )
        assert updated.in_important()
        assert updated.arrival.assessed_at == NOW
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        assert sum(e.event_type == "email.triage.important" for e in events) == 1


async def test_correspondence_expedites_board_without_a_gmail_hint() -> None:
    reads: list[str] = []
    async with _app(
        FixedClock(NOW),
        await _backlog(reads, label_board=False),
        *[_assessment_turn() for _ in range(8)],
    ) as app:
        await _caught_up(
            app, email_address="owner@example.test", verified_addresses=["owner@example.test"]
        )
        service = app.services.email
        service.triage_mode = "local"
        _limit(app)
        session = await app.sessions.create()
        old = _thread_page("board", NOW - timedelta(days=10))
        old["messages"][0]["id"] = "previous-board-mail"
        await service.import_thread(app.principal, "default", old, session)
        sent = _thread_page("previous-reply", NOW - timedelta(days=9))
        sent["messages"][0].update(
            direction="sent",
            label_ids=["SENT"],
            to="colleague@example.test",
            cc="",
            **{"from": "owner@example.test"},
        )
        await service.import_thread(app.principal, "default", sent, session)
        check = await service.admit_arrival_check(app.principal)
        run = await app.runs.get(check.run_id)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert reads[0] == "board"
        visible = await service.threads(app.principal, view="priority")
        assert [row["subject"] for row in visible["items"]] == ["board"]


def test_history_replay_respects_later_removal_and_deletion() -> None:
    from agent_core.domain.email_triage import merge_history_signal

    previous = None
    for kind, revision, labels in [
        ("labels_removed", "102", ["IMPORTANT"]),
        ("message_added", "101", ["INBOX", "IMPORTANT"]),
    ]:
        change = {"kind": kind, "history_id": revision, "sequence": revision}
        previous = merge_history_signal(change, previous, change, labels, NOW)
    assert previous and previous["labels"] == "INBOX" and previous["inbound"] == "1"
    deleted = {"kind": "message_deleted", "history_id": "103", "sequence": "4"}
    gone = merge_history_signal(deleted, previous, deleted, [], NOW)
    replay = merge_history_signal(deleted, gone, change, ["INBOX", "IMPORTANT"], NOW)
    assert not replay["labels"] and replay["inbound"] == "0"


async def test_wait_to_important_includes_provider_read_time() -> None:
    clock = FixedClock(NOW)
    async with _app(
        clock, await _backlog([], read_clock=clock), *[_assessment_turn() for _ in range(8)]
    ) as app:
        await _caught_up(app)
        _limit(app)
        app.services.email.triage_mode = "local"
        check = await app.services.email.admit_arrival_check(app.principal)
        run = await app.runs.get(check.run_id)
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        [placed] = [event for event in events if event.event_type == "email.triage.important"]
        assert 122_000 <= placed.payload["elapsed_ms"] <= 124_000


async def test_foreground_leaves_last_background_dollar_for_expedited_assessment() -> None:
    async with _app(
        FixedClock(NOW), await _backlog([]), *[_assessment_turn() for _ in range(8)]
    ) as app:
        await _caught_up(app)
        _limit(app)
        service = app.services.email
        service.triage_mode = "local"
        await _seed_spend(app, "24", NOW - timedelta(minutes=20))
        op = await service.submit_task(app.principal, kind="refresh")
        run = await app.runs.get(op.run_id)
        task = await service.get_task(app.principal, run.id)
        assert task.scope == "expedited"
        assert run.status is RunStatus.COMPLETED and run.model_call_count == 1
        assert (await service.threads(app.principal, view="priority"))["items"][0][
            "subject"
        ] == "board"


async def test_unlabeled_first_contact_gets_the_oldest_ordinary_slot() -> None:
    reads: list[str] = []
    async with _app(
        FixedClock(NOW),
        await _backlog(reads, label_board=False),
        *[_assessment_turn() for _ in range(8)],
    ) as app:
        await _caught_up(app)
        _limit(app)
        service = app.services.email
        service.triage_mode = "local"
        check = await service.admit_arrival_check(app.principal)
        run = await app.runs.get(check.run_id)
        assert reads[0] == "board"
        assert run.status is RunStatus.COMPLETED, run.failure
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(run.session_id, 0, app.principal)
        [placed] = [e for e in events if e.event_type == "email.triage.important"]
        assert (
            sum(
                e.event_type == "model.request.started" and e.sequence < placed.sequence
                for e in events
            )
            == 1
        )


async def test_provisional_inbox_shows_a_subsequent_provider_delay() -> None:
    from agent_core.application.email import read_value, save_value
    from agent_core.domain.email import EmailAccount

    async with _app(FixedClock(NOW), await _backlog([])) as app:
        await _caught_up(app)
        _limit(app)
        service = app.services.email
        service.triage_mode = "local"
        await _seed_spend(app, "600", NOW - timedelta(hours=2))
        await service.admit_arrival_check(app.principal)
        async with app.uow_factory() as uow:
            account = await read_value(uow.email, app.principal, "account", "default", EmailAccount)
            assert account is not None
            await save_value(
                uow.email,
                app.principal,
                "account",
                "default",
                account.model_copy(update={"status": "unavailable"}),
                NOW,
            )
        page = await service.threads(app.principal, view="inbox")
        row = next(row for row in page["items"] if row["subject"] == "board")
        assert "Gmail" in row["reason"] and "budget" in row["reason"]


# --- review of pull request 268 ---------------------------------------------------


def _mail(name: str, sender: str, check: str, minutes: int) -> Any:
    from agent_core.domain.email import EmailMessage

    return EmailMessage(
        id=name,
        sender=sender,
        sent_at=NOW - timedelta(minutes=minutes),
        labels=["INBOX"],
        sender_check=check,
    )


@pytest.mark.parametrize(("check", "expedited"), [("fail", False), ("pass", True)])
def test_partner_evidence_comes_only_from_mail_that_did_not_fail_its_sender_check(
    check: str, expedited: bool
) -> None:
    from uuid import uuid4

    from agent_core.domain.email import EmailThread, expedite_thread

    partner, stranger = "Partner <partner@example.test>", "Stranger <stranger@example.test>"
    thread = EmailThread(
        id=uuid4(),
        account_id="default",
        provider_thread_id="t",
        subject="s",
        senders=[partner, stranger],
        updated_at=NOW,
        last_accessed_at=NOW,
        messages=[_mail("older", partner, check, 10), _mail("newest", stranger, "pass", 1)],
    )

    assert expedite_thread(thread, {"partner@example.test"}, []) is expedited


async def _imported(app: Any, name: str, session: Any, **message: Any) -> Any:
    page = _thread_page(name, NOW - timedelta(minutes=2))
    page["messages"][0].update(subject=name, **message)
    return await app.services.email.import_thread(
        app.principal, "default", page, session, observed_at=NOW
    )


async def test_owner_important_and_restore_survive_a_deleted_source_session() -> None:
    async with _app(FixedClock(NOW), await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        service.triage_mode = "local"
        session = await app.sessions.create()
        marked = await _imported(app, "marked", session)
        restored = await _imported(app, "restored", session)
        assert marked.arrival and marked.arrival.important_at is None
        await app.services.sessions.delete(app.principal, session)

        await service.feedback(
            app.principal, thread_id=marked.id, target="thread", judgment="important"
        )
        await service.dismiss(app.principal, restored.id, restored.revision)
        await service.feedback(
            app.principal, thread_id=restored.id, target="thread", judgment="important"
        )
        await service.dismiss(app.principal, restored.id, restored.revision, dismissed=False)

        page = await service.threads(app.principal, view="priority")
        assert {row["subject"] for row in page["items"]} == {"marked", "restored"}
        async with app.uow_factory() as uow:
            for thread_id in (marked.id, restored.id):
                stored = await uow.email.get(app.principal, "thread", str(thread_id))
                assert stored and stored.payload["arrival"]["important_at"] is not None


async def test_a_lost_nomination_clears_the_reason_only_triage_wrote() -> None:
    async with _app(FixedClock(NOW), await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        service.triage_mode = "local"
        session = await app.sessions.create()
        hinted = await _imported(app, "board", session, label_ids=["INBOX", "IMPORTANT"])
        assert hinted.arrival and hinted.arrival.expedited
        assert hinted.reason == "Full assessment queued."

        plain = await _imported(app, "board", session, label_ids=["INBOX"])

        assert plain.arrival and not plain.arrival.expedited
        assert plain.reason == "Assessment pending"


async def test_cached_mail_gets_its_arrival_on_the_first_live_observation() -> None:
    async with _app(FixedClock(NOW), await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        service.triage_mode = "local"
        session = await app.sessions.create()
        page = _thread_page("board", NOW - timedelta(minutes=2))
        page["messages"][0].update(subject="board", label_ids=["INBOX", "IMPORTANT"])
        cached = await service.import_thread(app.principal, "default", page, session)
        assert cached.arrival is None

        seen = await service.import_thread(app.principal, "default", page, session, observed_at=NOW)

        assert seen.arrival and seen.arrival.expedited
        assert seen.arrival.received_at == NOW - timedelta(minutes=2)
        assert seen.revision == cached.revision
        assert seen.reason == "Full assessment queued."
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(session, 0, app.principal)
        assert [e.event_type for e in events if e.event_type.startswith("email.triage.")] == [
            "email.triage.discovered"
        ]


async def test_an_unchanged_thread_without_sent_mail_scans_no_learning_history() -> None:
    async with _app(FixedClock(NOW), await _mailbox([], [])) as app:
        await _caught_up(
            app, email_address="owner@example.test", verified_addresses=["owner@example.test"]
        )
        service = app.services.email
        session = await app.sessions.create()
        await _imported(app, "inbound", session)
        scans: list[str] = []
        original = service._learning_state

        async def counted(*args: Any) -> Any:
            scans.append("scan")
            return await original(*args)

        vars(service)["_learning_state"] = counted
        await _imported(app, "inbound", session)

        assert scans == []


async def _refresh_task(app: Any, minutes_ago: int, **fields: Any) -> None:
    from uuid import uuid4

    from agent_core.application.email import save_value
    from agent_core.domain.email import EmailTask

    created = NOW - timedelta(minutes=minutes_ago)
    task = EmailTask(
        id=uuid4(),
        run_id=uuid4(),
        session_id=uuid4(),
        kind="refresh",
        account_ids=["default"],
        created_at=created,
        **fields,
    )
    async with app.uow_factory() as uow:
        await save_value(uow.email, app.principal, "task", str(task.run_id), task, created)


@pytest.mark.parametrize(
    ("fields", "due"),
    [
        # A foreground refresh the budget held to discovery is not a failed check.
        ({"scope": "discovery", "arrival_check": False}, True),
        ({"scope": "expedited", "arrival_check": False}, True),
        ({"scope": "discovery", "arrival_check": True}, False),
        # Tasks from before the origin was kept: only `arrivals` was a server check.
        ({"scope": "arrivals"}, False),
        ({"scope": "new_mail"}, True),
    ],
)
async def test_only_a_failed_server_check_widens_the_wait(
    fields: dict[str, Any], due: bool
) -> None:
    async with _app(FixedClock(NOW), await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        service.triage_mode = "local"
        # Its run is gone, so the refresh counts as failed. One interval has passed.
        await _refresh_task(app, 6, **fields)

        assert await service._arrival_check_due(app.principal) is due


async def test_a_refusal_names_a_retry_time_the_same_gate_would_accept() -> None:
    from uuid import uuid4

    from agent_core.application.email import save_value
    from agent_core.domain.email import EmailTask
    from agent_core.domain.errors import BudgetExceededError

    async with _app(FixedClock(NOW), await _mailbox([], [])) as app:
        await _caught_up(app)
        _limit(app)
        service = app.services.email
        service.triage_mode = "local"
        await _seed_spend(app, "1", NOW - timedelta(minutes=20))
        await _seed_spend(app, "23", NOW - timedelta(days=5))
        # Leaves the rolling window at 09:00 tomorrow.
        await _seed_spend(app, "574", NOW - timedelta(days=30) + timedelta(hours=20))
        live = EmailTask(
            id=uuid4(),
            run_id=uuid4(),
            session_id=uuid4(),
            kind="draft",
            account_ids=["default"],
            created_at=NOW,
            reservation=Decimal("24.5"),
        )
        async with app.uow_factory() as uow:
            await save_value(uow.email, app.principal, "task", str(live.run_id), live, NOW)
            with pytest.raises(BudgetExceededError) as refused:
                await service._check_budget(uow.email, app.principal, Decimal("1"))

        # Tomorrow the rolling window has room, but with nothing spent that day the
        # live reservations alone sit under the arrival cap, which then applies.
        assert refused.value.details["retry_at"] == (NOW + timedelta(hours=1)).isoformat()


async def test_cached_mail_seen_live_times_an_owner_action_in_the_live_session() -> None:
    async with _app(FixedClock(NOW), await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        service.triage_mode = "local"
        historical = await app.sessions.create()
        page = _thread_page("board", NOW - timedelta(minutes=2))
        page["messages"][0].update(subject="board")
        await service.import_thread(app.principal, "default", page, historical)
        await app.services.sessions.delete(app.principal, historical)
        live = await app.sessions.create()

        seen = await service.import_thread(app.principal, "default", page, live, observed_at=NOW)
        await service.feedback(
            app.principal, thread_id=seen.id, target="thread", judgment="important"
        )

        assert seen.source_session_ids[-1] == live
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(live, 0, app.principal)
        assert [e.event_type for e in events if e.event_type.startswith("email.triage.")] == [
            "email.triage.discovered",
            "email.triage.important",
        ]


async def test_a_historical_import_keeps_a_pending_arrival() -> None:
    clock = FixedClock(NOW)
    async with _app(clock, await _mailbox([], [])) as app:
        await _caught_up(app)
        service = app.services.email
        service.triage_mode = "local"
        session = await app.sessions.create()
        pending = await _imported(app, "board", session, label_ids=["INBOX", "IMPORTANT"])
        page = _thread_page("board", NOW - timedelta(minutes=2))
        page["messages"][0].update(subject="board", label_ids=["INBOX", "IMPORTANT"])
        older = _thread_page("old", NOW - timedelta(days=1))["messages"][0]
        older["thread_id"] = "board"
        page.update(messages=[older, *page["messages"]])

        rebuilt = await service.import_thread(app.principal, "default", page, session)

        assert rebuilt.revision == pending.revision + 1
        assert rebuilt.arrival == pending.arrival
        assert rebuilt.reason == "Full assessment queued."
        clock.advance(timedelta(minutes=5))
        again = await service.import_thread(
            app.principal, "default", page, session, observed_at=clock.now()
        )
        assert again.arrival and again.arrival.discovered_at == NOW
        async with app.uow_factory() as uow:
            events = await uow.events.list_after(session, 0, app.principal)
        assert sum(e.event_type == "email.triage.discovered" for e in events) == 1
