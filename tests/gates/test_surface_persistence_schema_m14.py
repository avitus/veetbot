"""Milestone 14/25 surface schema trust-boundary gates."""

from __future__ import annotations

from sqlalchemy import CheckConstraint, UniqueConstraint

from agent_core.adapters.persistence.sqlalchemy_models import Base


def test_surface_tables_and_whatsapp_window_are_declared() -> None:
    tables = Base.metadata.tables
    expected = {
        "surface_pairing_codes",
        "surface_pairings",
        "surface_sender_lockouts",
        "surface_sessions",
        "surface_inbound_receipts",
        "surface_replies",
    }

    assert expected <= tables.keys(), "surface persistence tables are not implemented"
    assert "last_inbound_at" in tables["surface_sessions"].c
    for name in expected - {"surface_sender_lockouts", "surface_inbound_receipts"}:
        assert "tenant_id" in tables[name].c


def test_surface_schema_encodes_live_uniqueness_and_content_free_receipts() -> None:
    tables = Base.metadata.tables
    pairings = tables["surface_pairings"]
    sessions = tables["surface_sessions"]
    receipts = tables["surface_inbound_receipts"]
    replies = tables["surface_replies"]

    pairing_indexes = {
        str(index.name): str(index.dialect_options["postgresql"]["where"])
        for index in pairings.indexes
    }
    session_indexes = {
        str(index.name): str(index.dialect_options["postgresql"]["where"])
        for index in sessions.indexes
    }
    assert pairing_indexes["uq_surface_pairings_live_sender"] == "revoked_at IS NULL"
    assert session_indexes["uq_surface_sessions_live_key"] == "rotated_at IS NULL"
    assert list(receipts.primary_key.columns.keys()) == ["surface_id", "external_update_id"]
    assert set(receipts.c.keys()) == {
        "surface_id",
        "external_update_id",
        "received_at",
        "disposition",
        "session_id",
        "run_id",
        "reason_code",
    }
    assert any(
        isinstance(constraint, UniqueConstraint) and list(constraint.columns.keys()) == ["run_id"]
        for constraint in replies.constraints
    )


def test_surface_erasure_rules_and_whatsapp_device_routing_are_declared() -> None:
    tables = Base.metadata.tables
    sessions = tables["surface_sessions"]
    receipts = tables["surface_inbound_receipts"]
    replies = tables["surface_replies"]
    devices = tables["devices"]

    assert next(iter(sessions.c.session_id.foreign_keys)).ondelete == "CASCADE"
    assert next(iter(receipts.c.session_id.foreign_keys)).ondelete == "SET NULL"
    assert next(iter(receipts.c.run_id.foreign_keys)).ondelete == "SET NULL"
    assert next(iter(replies.c.run_id.foreign_keys)).ondelete == "CASCADE"
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in devices.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert "whatsapp" in checks["ck_devices_device_push_provider_closed"]
    assert "whatsapp" in checks["ck_devices_device_surface_routing"]
