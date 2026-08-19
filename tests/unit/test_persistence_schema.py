"""Static checks for query-critical persistence indexes."""

from typing import cast

from sqlalchemy import Table

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
    assert forbidden.isdisjoint(table.columns)
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
    assert forbidden.isdisjoint(table.columns)
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
    assert forbidden.isdisjoint(table.columns)
    assert {constraint.name for constraint in table.constraints} >= {
        "ck_browser_authentications_status_closed",
        "ck_browser_authentications_time_window",
    }
