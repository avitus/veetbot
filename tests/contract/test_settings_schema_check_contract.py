"""Settings schema check contract: a manifest's settings schema, applied to owner values.

docs/plan/cockpit.md, the domain model. The check answers why values do not
fit the closed settings schema, and nothing when they do.
"""

from __future__ import annotations

from agent_core.adapters.cockpit.runtime import JsonSchemaSettingsCheck

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["place"],
    "properties": {
        "place": {"type": "string", "maxLength": 12},
        "units": {"enum": ["metric", "imperial"]},
    },
}


def test_values_that_fit_have_no_violations() -> None:
    check = JsonSchemaSettingsCheck()
    assert check.violations(SCHEMA, {"place": "Harbor"}) == []
    assert check.violations(SCHEMA, {"place": "Harbor", "units": "metric"}) == []


def test_each_misfit_is_named_and_the_order_is_stable() -> None:
    check = JsonSchemaSettingsCheck()
    for values in (
        {},
        {"place": "x" * 13},
        {"place": "Harbor", "extra": True},
        {"place": "Harbor", "units": "kelvin"},
        {"place": 7},
    ):
        found = check.violations(SCHEMA, values)
        assert found, values
        assert found == sorted(found)
