"""JSON Schema check contract: a widget's settings and a feed's data against their schemas.

docs/plan/cockpit.md, the domain model and assistant feeds. The check answers
why an instance does not fit a schema, and why a schema the assistant wrote is
not one; it answers nothing when they are fine, and it never reaches outside
the schema it was given.
"""

from __future__ import annotations

from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck

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
    check = Draft202012SchemaCheck()
    assert check.violations(SCHEMA, {"place": "Harbor"}) == []
    assert check.violations(SCHEMA, {"place": "Harbor", "units": "metric"}) == []
    assert check.violations({"type": "array", "items": {"type": "number"}}, [1, 2.5]) == []


def test_each_misfit_is_named_and_the_order_is_stable() -> None:
    check = Draft202012SchemaCheck()
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
    assert check.violations({"type": "array"}, {"not": "a list"})


def test_a_valid_schema_has_no_schema_violations() -> None:
    check = Draft202012SchemaCheck()
    assert check.schema_violations(SCHEMA) == []
    assert check.schema_violations({"$defs": {"n": {"type": "number"}}, "$ref": "#/$defs/n"}) == []


def test_a_malformed_schema_is_named() -> None:
    check = Draft202012SchemaCheck()
    malformed: tuple[dict[str, object], ...] = ({"type": 7}, {"properties": []}, {"minLength": -1})
    for schema in malformed:
        assert check.schema_violations(schema), schema


def test_a_schema_that_refers_outside_itself_is_refused() -> None:
    check = Draft202012SchemaCheck()
    for schema in (
        {"$ref": "https://example.test/schema.json"},
        {"properties": {"a": {"$ref": "other.json#/x"}}},
        {"$dynamicRef": "https://example.test/x"},
    ):
        assert check.schema_violations(schema), schema
        assert check.violations(schema, {"a": 1}), schema
