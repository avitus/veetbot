"""Static checks for query-critical persistence indexes."""

from typing import cast

from sqlalchemy import CheckConstraint, Table, UniqueConstraint

from agent_core.adapters.persistence.sqlalchemy_models import Base, EvalScenarioAttemptCostRow


def test_capability_attempt_costs_index_the_scenario_run_foreign_key() -> None:
    table = cast(Table, EvalScenarioAttemptCostRow.__table__)
    indexed_columns = {tuple(column.name for column in index.columns) for index in table.indexes}

    assert ("scenario_run_id",) in indexed_columns


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
