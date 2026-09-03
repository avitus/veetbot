"""Static checks for query-critical persistence indexes."""

from pathlib import Path
from typing import cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from agent_core.adapters.persistence.sqlalchemy_models import Base, EvalScenarioAttemptCostRow

ROOT = Path(__file__).resolve().parents[2]


def test_capability_attempt_costs_index_the_scenario_run_foreign_key() -> None:
    table = cast(Table, EvalScenarioAttemptCostRow.__table__)
    indexed_columns = {tuple(column.name for column in index.columns) for index in table.indexes}

    assert ("scenario_run_id",) in indexed_columns


def test_process_events_index_diagnostic_scope_and_reverse_chronology() -> None:
    table = Base.metadata.tables["process_events"]
    index = next(
        candidate
        for candidate in table.indexes
        if candidate.name == "ix_process_events_diagnostics_scope"
    )
    expressions = " ".join(str(expression) for expression in index.expressions)

    assert "event_type" in expressions
    assert "tenant_id" in expressions
    assert "principal_id" in expressions
    assert "session_id" in expressions
    assert "created_at DESC" in expressions
    assert "id DESC" in expressions


def test_browser_profiles_schema_contains_metadata_only() -> None:
    table = Base.metadata.tables["browser_profiles"]

    assert set(table.columns.keys()) == {
        "id",
        "tenant_id",
        "principal_id",
        "provider_name",
        "provider_ref",
        "allowed_origins",
        "status",
        "generation",
        "encryption_key_version",
        "created_at",
        "updated_at",
        "last_used_at",
    }
    forbidden = {"cookies", "tokens", "storage_state", "credential", "material", "blob"}
    assert forbidden.isdisjoint(table.columns.keys())
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_browser_profiles_generation_nonnegative",
        "ck_browser_profiles_status_closed",
        "ck_browser_profiles_binding_consistent",
    }


def test_browser_grants_schema_contains_exact_authority_without_material() -> None:
    table = Base.metadata.tables["browser_grants"]

    assert set(table.columns.keys()) == {
        "id",
        "tenant_id",
        "principal_id",
        "profile_id",
        "profile_generation",
        "agent_version",
        "policy_version",
        "allowed_origins",
        "action_kinds",
        "element_roles",
        "element_names",
        "purpose",
        "starts_at",
        "expires_at",
        "approved_by",
        "revoked_at",
        "created_at",
        "updated_at",
    }
    forbidden = {"cookies", "tokens", "storage_state", "credential", "material", "blob"}
    assert forbidden.isdisjoint(table.columns.keys())
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_browser_grants_generation_nonnegative",
        "ck_browser_grants_time_window",
        "ck_browser_grants_action_kinds_nonempty",
    }


def test_browser_authentications_schema_is_secret_free() -> None:
    table = Base.metadata.tables["browser_authentications"]

    assert set(table.columns.keys()) == {
        "id",
        "tenant_id",
        "principal_id",
        "profile_id",
        "status",
        "expires_at",
        "created_at",
        "updated_at",
    }
    forbidden = {
        "capability",
        "launch_url",
        "cookies",
        "tokens",
        "storage_state",
        "credential",
        "material",
        "blob",
    }
    assert forbidden.isdisjoint(table.columns.keys())
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_browser_authentications_status_closed",
        "ck_browser_authentications_time_window",
    }


def test_schedule_tables_encode_identity_erasure_and_query_constraints() -> None:
    tables = Base.metadata.tables
    assert {
        "schedules",
        "schedule_revisions",
        "schedule_occurrences",
        "schedule_idempotency_keys",
    } <= tables.keys()

    schedules = tables["schedules"]
    assert {tuple(column.name for column in index.columns) for index in schedules.indexes} >= {
        ("state", "next_fire_at"),
        ("tenant_id", "principal_id", "updated_at", "id"),
    }

    revisions = tables["schedule_revisions"]
    assert tuple(column.name for column in revisions.primary_key.columns) == (
        "schedule_id",
        "revision",
    )

    occurrences = tables["schedule_occurrences"]
    assert "links_erased_at" in occurrences.columns
    assert {tuple(column.name for column in index.columns) for index in occurrences.indexes} >= {
        ("schedule_id", "nominal_fire_at"),
        ("run_id",),
    }
    run_indexes = [
        index
        for index in occurrences.indexes
        if tuple(column.name for column in index.columns) == ("run_id",)
    ]
    assert len(run_indexes) == 1
    assert run_indexes[0].unique is True
    assert str(run_indexes[0].dialect_options["postgresql"]["where"]) == "run_id IS NOT NULL"
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in occurrences.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("schedule_id", "nominal_fire_at") in unique_columns
    foreign_keys = {
        foreign_key.parent.name: foreign_key.ondelete for foreign_key in occurrences.foreign_keys
    }
    assert foreign_keys["session_id"] == "RESTRICT"
    assert foreign_keys["run_id"] == "RESTRICT"
    assert all(
        foreign_key.deferrable and foreign_key.initially == "DEFERRED"
        for foreign_key in occurrences.foreign_keys
        if foreign_key.parent.name in {"session_id", "run_id"}
    )
    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in occurrences.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "links_erased_at" in check_sql

    idempotency = tables["schedule_idempotency_keys"]
    assert tuple(column.name for column in idempotency.primary_key.columns) == (
        "tenant_id",
        "principal_id",
        "key",
    )


def test_notification_tables_encode_routing_identity_and_query_constraints() -> None:
    tables = Base.metadata.tables
    assert {
        "devices",
        "device_registration_idempotency_keys",
        "notification_outbox",
        "notification_deliveries",
    } <= tables.keys()

    device_idempotency = tables["device_registration_idempotency_keys"]
    assert tuple(column.name for column in device_idempotency.primary_key.columns) == (
        "tenant_id",
        "principal_id",
        "key",
    )
    assert set(device_idempotency.columns.keys()) == {
        "tenant_id",
        "principal_id",
        "key",
        "request_hash",
        "response",
        "created_at",
    }

    devices = tables["devices"]
    assert {tuple(column.name for column in index.columns) for index in devices.indexes} >= {
        ("tenant_id", "principal_id", "created_at", "id"),
        ("push_provider", "push_token"),
    }
    token_indexes = [
        index
        for index in devices.indexes
        if tuple(column.name for column in index.columns) == ("push_provider", "push_token")
    ]
    assert len(token_indexes) == 1
    assert token_indexes[0].unique is True
    assert str(token_indexes[0].dialect_options["postgresql"]["where"]) == (
        "push_token IS NOT NULL AND status = 'active'"
    )
    device_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in devices.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("tenant_id", "principal_id", "client_device_id") in device_unique_columns
    device_checks = " ".join(
        str(constraint.sqltext)
        for constraint in devices.constraints
        if isinstance(constraint, CheckConstraint)
    )
    for required in (
        "push_provider",
        "push_token",
        "push_environment",
        "muted_kinds",
        "capabilities",
    ):
        assert required in device_checks

    outbox = tables["notification_outbox"]
    assert {tuple(column.name for column in index.columns) for index in outbox.indexes} >= {
        ("status", "next_attempt_at"),
        ("tenant_id", "principal_id", "created_at", "id"),
    }
    outbox_foreign_keys = {foreign_key.parent.name for foreign_key in outbox.foreign_keys}
    assert outbox_foreign_keys.isdisjoint(
        {"session_id", "run_id", "approval_id", "question_id", "schedule_id", "occurrence_id"}
    )

    deliveries = tables["notification_deliveries"]
    assert {
        foreign_key.parent.name: (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in deliveries.foreign_keys
    } == {
        "notification_id": ("notification_outbox.id", "RESTRICT"),
        "device_id": ("devices.id", "RESTRICT"),
    }
    delivery_unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in deliveries.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("notification_id", "device_id", "attempt") in delivery_unique_columns


def _device_ondelete(table: Table) -> str | None:
    return next(
        foreign_key.ondelete
        for foreign_key in table.foreign_keys
        if foreign_key.parent.name == "device_id"
    )


def test_device_channel_tables_encode_ownership_idempotency_and_erasure() -> None:
    tables = Base.metadata.tables
    assert {
        "device_invocations",
        "device_ingest_receipts",
        "device_triage_sessions",
    } <= tables.keys()

    assert "capabilities" in tables["devices"].columns

    invocations = tables["device_invocations"]
    assert tuple(column.name for column in invocations.primary_key.columns) == ("id",)
    assert {
        foreign_key.parent.name: (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in invocations.foreign_keys
    } == {
        "device_id": ("devices.id", "CASCADE"),
        "run_id": ("runs.id", "CASCADE"),
    }
    invocation_checks = " ".join(
        str(constraint.sqltext)
        for constraint in invocations.constraints
        if isinstance(constraint, CheckConstraint)
    )
    for status in ("pending", "sent", "cancelled", "failed", "expired"):
        assert f"'{status}'" in invocation_checks
    assert {tuple(column.name for column in index.columns) for index in invocations.indexes} >= {
        ("device_id", "status", "created_at")
    }

    receipts = tables["device_ingest_receipts"]
    assert tuple(column.name for column in receipts.primary_key.columns) == (
        "device_id",
        "channel",
        "digest",
    )
    assert {
        foreign_key.parent.name: (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in receipts.foreign_keys
    } == {
        "device_id": ("devices.id", "CASCADE"),
        "session_id": ("sessions.id", "SET NULL"),
        "run_id": ("runs.id", "SET NULL"),
    }
    forbidden = {"sender", "body", "content", "text", "message"}
    assert forbidden.isdisjoint(receipts.columns.keys())
    assert {"received_at", "accepted_at"} <= set(receipts.columns.keys())

    triage = tables["device_triage_sessions"]
    assert tuple(column.name for column in triage.primary_key.columns) == ("device_id", "channel")
    assert {
        foreign_key.parent.name: (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in triage.foreign_keys
    } == {
        "device_id": ("devices.id", "CASCADE"),
        "session_id": ("sessions.id", "CASCADE"),
    }

    # Device deletion is a bare DELETE on devices with no dependent-table purge
    # (PostgresDeviceRegistry.delete), so every device-scoped row here must
    # CASCADE or deletion fails permanently the moment one row exists. The
    # RESTRICT on notification_deliveries.device_id is the deliberate contrast:
    # that table is the delivery audit trail and pins the device on purpose.
    assert {
        table_name: _device_ondelete(tables[table_name])
        for table_name in (
            "device_invocations",
            "device_ingest_receipts",
            "device_triage_sessions",
            "notification_deliveries",
        )
    } == {
        "device_invocations": "CASCADE",
        "device_ingest_receipts": "CASCADE",
        "device_triage_sessions": "CASCADE",
        "notification_deliveries": "RESTRICT",
    }

    migration = next(
        (ROOT / "migrations" / "versions").glob("*_add_milestone_24_device_channel.py")
    )
    migration_sql = migration.read_text(encoding="utf-8")
    for table in ("device_invocations", "device_ingest_receipts", "device_triage_sessions"):
        assert f'_tenant_policy("{table}")' in migration_sql
    assert '"device_invocation"' in migration_sql


def test_delegation_table_encodes_trust_boundaries() -> None:
    tables = Base.metadata.tables
    assert "delegations" in tables

    delegations = tables["delegations"]
    assert tuple(column.name for column in delegations.primary_key.columns) == ("id",)
    assert "links_erased_at" in delegations.columns
    unique_columns = {
        tuple(column.name for column in constraint.columns)
        for constraint in delegations.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert ("invocation_id",) in unique_columns
    assert {
        foreign_key.parent.name: (foreign_key.target_fullname, foreign_key.ondelete)
        for foreign_key in delegations.foreign_keys
    } == {
        "parent_run_id": ("runs.id", "CASCADE"),
        "parent_session_id": ("sessions.id", "CASCADE"),
        "invocation_id": ("tool_invocations.id", "CASCADE"),
    }
    check_sql = " ".join(
        str(constraint.sqltext)
        for constraint in delegations.constraints
        if isinstance(constraint, CheckConstraint)
    )
    assert "PENDING" in check_sql
    assert "JOINED" in check_sql
    assert "depth" in check_sql
    assert {tuple(column.name for column in index.columns) for index in delegations.indexes} >= {
        ("parent_run_id",),
        ("tenant_id", "principal_id", "created_at", "id"),
    }

    runs = tables["runs"]
    sibling_indexes = [
        index
        for index in runs.indexes
        if tuple(column.name for column in index.columns) == ("parent_run_id", "run_kind")
    ]
    assert len(sibling_indexes) == 1
    assert (
        str(sibling_indexes[0].dialect_options["postgresql"]["where"])
        == "parent_run_id IS NOT NULL"
    )

    migration = next((ROOT / "migrations" / "versions").glob("*_add_milestone_13_delegations.py"))
    migration_sql = migration.read_text(encoding="utf-8")
    assert "ALTER TABLE delegations ENABLE ROW LEVEL SECURITY" in migration_sql
    assert "ALTER TABLE delegations FORCE ROW LEVEL SECURITY" in migration_sql
    assert "delegations_tenant_isolation" in migration_sql


def test_persona_tables_encode_versioning_and_open_nomination_uniqueness() -> None:
    documents = Base.metadata.tables["persona_documents"]
    assert {column.name for column in documents.primary_key.columns} == {
        "tenant_id",
        "principal_id",
        "version",
    }
    assert set(documents.columns.keys()) == {
        "tenant_id",
        "principal_id",
        "version",
        "entries",
        "source",
        "source_nomination_id",
        "created_at",
    }

    nominations = Base.metadata.tables["persona_nominations"]
    open_unique = next(
        index for index in nominations.indexes if index.name == "ix_persona_nominations_open"
    )
    assert open_unique.unique
    assert [column.name for column in open_unique.columns] == [
        "tenant_id",
        "principal_id",
        "belief_id",
    ]
    assert "state = 'nominated'" in str(open_unique.dialect_options["postgresql"]["where"]).replace(
        '"', ""
    )
