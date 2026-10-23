"""Owner records domain model (Milestone 34)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_core.domain.memory import Sensitivity
from agent_core.domain.records import (
    FUTURE_SKEW,
    RecordCadence,
    RecordComponent,
    RecordEntry,
    RecordNameError,
    RecordPrecision,
    RecordSeries,
    RecordTimeError,
    RecordValueError,
    cadence_interval,
    canonical_value,
    due_since,
    normalize_series_name,
    resolve_observed_at,
    short_value,
    validate_value,
)

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)


def _series(**over: object) -> RecordSeries:
    data: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": "t",
        "principal_id": "p",
        "name": "weight",
        "display_name": "Weight",
        "components": (RecordComponent(name="lbs", unit="lb"),),
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(over)
    return RecordSeries(**data)  # type: ignore[arg-type]


def _entry(**over: object) -> RecordEntry:
    data: dict[str, object] = {
        "id": uuid4(),
        "series_id": uuid4(),
        "tenant_id": "t",
        "principal_id": "p",
        "observed_at": NOW,
        "observed_precision": RecordPrecision.EXACT,
        "values": {"lbs": Decimal("180")},
        "idempotency_key": "k",
        "created_at": NOW,
    }
    data.update(over)
    return RecordEntry(**data)  # type: ignore[arg-type]


def test_normalize_collapses_case_space_and_nfc() -> None:
    assert normalize_series_name("  Blood   Pressure ") == "blood pressure"
    assert normalize_series_name("Café") == normalize_series_name("Café") == "café"


@pytest.mark.parametrize("bad", ["", "   ", "!!!", "x" * 65])
def test_normalize_refuses(bad: str) -> None:
    with pytest.raises(RecordNameError):
        normalize_series_name(bad)


def test_normalize_accepts_64() -> None:
    assert normalize_series_name("x" * 64) == "x" * 64


def test_validate_value_quantizes_and_bounds() -> None:
    assert canonical_value(validate_value(Decimal("128"))) == "128.000000"
    assert short_value(validate_value(Decimal("82.400"))) == "82.4"
    assert short_value(validate_value(Decimal("1E+2"))) == "100"
    assert short_value(validate_value(Decimal("0"))) == "0"
    assert short_value(validate_value(Decimal("-0.5"))) == "-0.5"
    assert canonical_value(validate_value(Decimal("-0"))) == "0.000000"
    assert short_value(validate_value(Decimal("-0"))) == "0"
    assert short_value(Decimal("-0")) == "0"
    assert validate_value(Decimal("999999999999.999999")) == Decimal("999999999999.999999")
    for bad in (
        "NaN",
        "Infinity",
        "-Infinity",
        "1234567890123",
        "0.1234567",
        "1E+12",
        "1E+23",
        "1E+30",
        "1E+999999",
        "1E-30",
    ):
        with pytest.raises(RecordValueError):
            validate_value(Decimal(bad))


def test_resolve_observed_at_rules() -> None:
    assert resolve_observed_at(None, RecordPrecision.EXACT, NOW) == NOW
    assert resolve_observed_at("2026-10-22T07:40:00-07:00", RecordPrecision.EXACT, NOW) == datetime(
        2026, 10, 22, 14, 40, tzinfo=UTC
    )
    day = resolve_observed_at("2026-10-21T23:00:00-07:00", RecordPrecision.DAY, NOW)
    assert day == datetime(2026, 10, 21, 19, 0, tzinfo=UTC)
    assert day.utcoffset() == timedelta(0)
    with pytest.raises(RecordTimeError, match="UTC offset"):
        resolve_observed_at("2026-10-22T07:40:00", RecordPrecision.EXACT, NOW)
    with pytest.raises(RecordTimeError, match="future"):
        resolve_observed_at("2026-10-22T15:06:00+00:00", RecordPrecision.EXACT, NOW)
    assert resolve_observed_at("2026-10-22T15:04:00+00:00", RecordPrecision.EXACT, NOW).minute == 4
    with pytest.raises(RecordTimeError):
        resolve_observed_at("not a time", RecordPrecision.EXACT, NOW)
    assert timedelta(minutes=5) == FUTURE_SKEW


def test_cadence_intervals() -> None:
    assert [cadence_interval(c).days for c in RecordCadence] == [1, 7, 30]


def test_due_since() -> None:
    now = datetime(2026, 10, 22, 8, 0, tzinfo=UTC)
    assert due_since(None, None, now) is None
    assert due_since(None, now, now) is None
    assert due_since(RecordCadence.DAILY, None, now) == now
    last = datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
    assert due_since(RecordCadence.DAILY, last, now) == datetime(2026, 10, 21, 7, 0, tzinfo=UTC)
    assert due_since(RecordCadence.WEEKLY, last, now) is None
    edge = datetime(2026, 10, 21, 8, 0, tzinfo=UTC)
    assert due_since(RecordCadence.DAILY, edge, now) == now


def test_series_requires_unique_components_and_one_to_four() -> None:
    with pytest.raises(ValidationError):
        _series(components=(RecordComponent(name="a"), RecordComponent(name="a")))
    with pytest.raises(ValidationError):
        _series(components=())
    five = tuple(RecordComponent(name=f"c{i}") for i in range(5))
    with pytest.raises(ValidationError):
        _series(components=five)
    four = _series(components=five[:4])
    assert len(four.components) == 4


def test_series_defaults_and_sensitivity_floor() -> None:
    s = _series()
    assert s.sensitivity is Sensitivity.SENSITIVE
    assert s.cadence is None
    assert s.revision == 1
    with pytest.raises(ValidationError):
        _series(sensitivity=Sensitivity.PUBLIC)
    assert _series(sensitivity=Sensitivity.INTERNAL).sensitivity is Sensitivity.INTERNAL
    with pytest.raises(ValidationError):
        s.revision = 2


def test_series_name_and_display_bounds() -> None:
    with pytest.raises(ValidationError):
        _series(display_name="")
    with pytest.raises(ValidationError):
        _series(display_name="x" * 65)


def test_component_validation() -> None:
    assert RecordComponent(name="sys_1", unit="  mmHg ").unit == "mmHg"
    assert RecordComponent(name="a", unit="   ").unit is None
    for bad in ("Sys", "1a", "", "a" * 33, "a-b"):
        with pytest.raises(ValidationError):
            RecordComponent(name=bad)
    with pytest.raises(ValidationError):
        RecordComponent(name="a", unit="x" * 17)
    assert RecordComponent(name="a" * 32).name == "a" * 32


def test_entry_quantizes_values_and_trims_note() -> None:
    e = _entry(values={"lbs": Decimal("1E+2")}, note="  after run  ")
    assert canonical_value(e.values["lbs"]) == "100.000000"
    assert e.note == "after run"
    assert e.note_withheld is False
    assert e.source_session_id is None


def test_entry_refusals() -> None:
    bad_overrides: tuple[dict[str, object], ...] = (
        {"values": {}},
        {"values": {"lbs": Decimal("NaN")}},
        {"values": {"lbs": Decimal("0.1234567")}},
        {"values": {"lbs": Decimal("1E+30")}},
        {"values": {"Bad": Decimal("1")}},
        {"values": {f"c{i}": Decimal(i) for i in range(5)}},
        {"note": "x" * 201},
        {"observed_at": datetime(2026, 10, 22, 15, 0)},
        {"idempotency_key": ""},
    )
    for bad in bad_overrides:
        with pytest.raises(ValidationError):
            _entry(**bad)


def test_entry_observed_at_is_utc() -> None:
    from datetime import timezone

    e = _entry(observed_at=datetime(2026, 10, 22, 8, 0, tzinfo=timezone(timedelta(hours=-7))))
    assert e.observed_at == datetime(2026, 10, 22, 15, 0, tzinfo=UTC)
    assert e.observed_at.utcoffset() == timedelta(0)
