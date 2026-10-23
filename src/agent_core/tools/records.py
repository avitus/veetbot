"""Owner records tools: named numeric series the owner reports and later asks about."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from agent_core.application.authorization import require_scope
from agent_core.application.records import RecordsRefusal, RecordsService
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import TextPart
from agent_core.domain.policies import IdempotencyClass, RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.records import (
    RecordCadence,
    RecordComponent,
    RecordEntry,
    RecordPrecision,
    RecordSeries,
    RecordValueError,
    canonical_value,
    due_since,
    ordered_values,
    short_value,
    validate_value,
)
from agent_core.domain.tools import (
    ToolExecutionContext,
    ToolFailure,
    ToolFailureKind,
    ToolResult,
    ToolSpec,
)
from agent_core.ports.records import Bucket, ComponentSummary, Order, SeriesSummary

WITHHELD_NOTE = "[note withheld]"
_NUMBER_TEXT = re.compile(r"^[+-]?\d+(\.\d+)?([eE][+-]?\d+)?$")
_NAME_SCHEMA = {"type": "string", "minLength": 1, "maxLength": 64}
_COMPONENT_NAME = {"type": "string", "pattern": "^[a-z][a-z0-9_]{0,31}$"}
_OUTPUT_SCHEMA: dict[str, Any] = {"type": "object"}
_NOT_MEMORY = (
    " Records are the owner's own measurements and are not memory; they are never recalled "
    "unprompted."
)
_PERMISSION_CODES = frozenset(
    {"records.secret_detected", "records.name_rejected", "records.untrusted_turn"}
)


def parse_decimal_values(raw: Mapping[str, object]) -> dict[str, Decimal]:
    """Parse JSON numbers as exact decimals so ``82.4`` never passes through a binary float."""

    parsed: dict[str, Decimal] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            raise RecordValueError("value must be a number")
        try:
            if isinstance(value, int):
                number = Decimal(value)
            elif isinstance(value, float):
                number = Decimal(repr(value))
            elif isinstance(value, str) and _NUMBER_TEXT.fullmatch(value.strip()):
                number = Decimal(value.strip())
            else:
                raise RecordValueError("value must be a number")
        except InvalidOperation as exc:
            raise RecordValueError("value must be a number") from exc
        parsed[key] = validate_value(number)
    return parsed


def _failure(kind: ToolFailureKind, code: str, detail: str) -> ToolResult:
    return ToolResult(
        ok=False,
        content=[],
        failure=ToolFailure(kind=kind, reason_code=code, detail=detail, retryable=False),
    )


def _refusal(exc: RecordsRefusal) -> ToolResult:
    if exc.code == "records.series_unknown":
        kind = ToolFailureKind.NOT_FOUND
    elif exc.code in _PERMISSION_CODES:
        kind = ToolFailureKind.PERMISSION
    else:
        kind = ToolFailureKind.INVALID_ARGUMENTS
    return _failure(kind, exc.code, exc.detail)


def _invalid() -> ToolResult:
    return _failure(
        ToolFailureKind.INVALID_ARGUMENTS,
        "records.invalid_arguments",
        "an argument has an invalid shape or value",
    )


def _stamp(value: datetime) -> str:
    return value.isoformat()


def _values(values: Mapping[str, Decimal]) -> dict[str, str]:
    return {key: canonical_value(value) for key, value in values.items()}


def _components(series: RecordSeries) -> list[dict[str, Any]]:
    return [{"name": c.name, "unit": c.unit} for c in series.components]


def _summary_body(summary: SeriesSummary, now: datetime) -> dict[str, Any]:
    series = summary.series
    due = due_since(series.cadence, summary.last_observed_at, now)
    return {
        "series_id": str(series.id),
        "display_name": series.display_name,
        "components": _components(series),
        "sensitivity": series.sensitivity.value,
        "cadence": series.cadence.value if series.cadence else None,
        "entry_count": summary.entry_count,
        "first_observed_at": _stamp(summary.first_observed_at)
        if summary.first_observed_at
        else None,
        "last_observed_at": _stamp(summary.last_observed_at) if summary.last_observed_at else None,
        "due": due is not None,
        "due_since": _stamp(due) if due else None,
    }


def _in_order(series: RecordSeries, keyed: Mapping[str, Any]) -> dict[str, Any]:
    """Any per-component mapping in the series' declared component order."""

    return {c.name: keyed[c.name] for c in series.components if c.name in keyed}


def _entry_body(entry: RecordEntry, series: RecordSeries) -> dict[str, Any]:
    return {
        "entry_id": str(entry.id),
        "observed_at": _stamp(entry.observed_at),
        "precision": entry.observed_precision.value,
        "values": _values(ordered_values(series, entry.values)),
        "note": WITHHELD_NOTE if entry.note_withheld else entry.note,
    }


def _component_summary(summary: ComponentSummary) -> dict[str, Any]:
    return {
        "count": summary.count,
        "min": canonical_value(summary.minimum),
        "max": canonical_value(summary.maximum),
        "mean": canonical_value(summary.mean),
        "first": {
            "value": canonical_value(summary.first.value),
            "observed_at": _stamp(summary.first.observed_at),
        },
        "last": {
            "value": canonical_value(summary.last.value),
            "observed_at": _stamp(summary.last.observed_at),
        },
    }


def _spec(
    name: str,
    description: str,
    schema: dict[str, Any],
    *,
    risk: RiskLevel,
    idempotency: IdempotencyClass,
    scope: str,
    maximum_output_bytes: int,
    allow_parallel: bool,
    side_effect: SideEffectClass = SideEffectClass.NONE,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        version="1.0.0",
        description=description + _NOT_MEMORY,
        input_schema=schema,
        output_schema=_OUTPUT_SCHEMA,
        side_effect=side_effect,
        risk=risk,
        idempotency=idempotency,
        required_scopes={scope},
        timeout_seconds=10,
        maximum_output_bytes=maximum_output_bytes,
        allow_parallel=allow_parallel,
        output_trust=TrustLevel.INTERNAL_TOOL,
    )


class _RecordsTool:
    spec: ToolSpec
    _scope: str

    def __init__(self, service: RecordsService, zone: ZoneInfo) -> None:
        self._service = service
        self._zone = zone

    async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        require_scope(context.principal, self._scope)
        try:
            return await self._run(arguments, context)
        except RecordsRefusal as exc:
            return _refusal(exc)
        except ValueError as exc:
            if isinstance(exc, RecordValueError):
                return _failure(
                    ToolFailureKind.INVALID_ARGUMENTS, "records.value_out_of_range", str(exc)
                )
            return _invalid()

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        raise NotImplementedError


class RecordsListTool(_RecordsTool):
    _scope = "records.read"
    spec = _spec(
        "records.list",
        "List the owner's record series with their components, units, entry counts, "
        "latest observation and whether a recurring series is due. It takes no arguments.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        scope="records.read",
        maximum_output_bytes=16384,
        allow_parallel=True,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        summaries = await self._service.list_series(context.principal)
        now = self._service.now()
        rows = sorted(summaries, key=lambda s: s.series.name)
        body = [_summary_body(row, now) for row in rows]
        return ToolResult(
            ok=True,
            content=[TextPart(text=f"{len(body)} series.")],
            structured={"series": body, "count": len(body)},
        )


class RecordsCreateSeriesTool(_RecordsTool):
    _scope = "records.write"
    spec = _spec(
        "records.create_series",
        "Declare a named series of one to four numeric components with optional units, "
        "sensitivity and a cadence. This needs the owner's approval, and a series must "
        "exist before records.append can add to it. Components cannot change afterwards.",
        {
            "type": "object",
            "properties": {
                "series": _NAME_SCHEMA,
                "components": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": _COMPONENT_NAME,
                            "unit": {"type": "string", "minLength": 1, "maxLength": 16},
                        },
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
                "sensitivity": {"type": "string", "enum": ["sensitive", "restricted"]},
                "cadence": {"type": "string", "enum": [item.value for item in RecordCadence]},
            },
            "required": ["series", "components"],
            "additionalProperties": False,
        },
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.IDEMPOTENT,
        scope="records.write",
        maximum_output_bytes=4096,
        allow_parallel=False,
        side_effect=SideEffectClass.EXTERNAL_WRITE,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        components = [RecordComponent(**dict(item)) for item in arguments["components"]]
        sensitivity = arguments.get("sensitivity")
        cadence = arguments.get("cadence")
        series, created = await self._service.create_series(
            context.principal,
            display_name=str(arguments["series"]),
            components=components,
            sensitivity=Sensitivity(sensitivity) if sensitivity else None,
            cadence=RecordCadence(cadence) if cadence else None,
            session_id=context.session_id,
            run_id=context.run_id,
        )
        sentence = (
            f"Created series '{series.display_name}'."
            if created
            else f"Series '{series.display_name}' already exists."
        )
        summaries = await self._service.list_series(context.principal)
        summary = next(s for s in summaries if s.series.id == series.id)
        body = _summary_body(summary, self._service.now())
        return ToolResult(
            ok=True,
            content=[TextPart(text=sentence)],
            structured={**body, "created": created},
        )


class RecordsAppendTool(_RecordsTool):
    _scope = "records.write"
    spec = _spec(
        "records.append",
        "Add one entry to an existing series with a value for every declared component, "
        "an optional observed time and precision, and an optional note. Values are the "
        "owner's own words and the series must already exist. In a turn that read "
        "external content the tool refuses; ask the owner to state the value in chat.",
        {
            "type": "object",
            "properties": {
                "series": _NAME_SCHEMA,
                "values": {
                    "type": "object",
                    "minProperties": 1,
                    "maxProperties": 4,
                    "propertyNames": {"pattern": "^[a-z][a-z0-9_]{0,31}$"},
                    "additionalProperties": {"type": "number"},
                },
                "observed_at": {"type": "string", "format": "date-time"},
                "precision": {"type": "string", "enum": [item.value for item in RecordPrecision]},
                "note": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            "required": ["series", "values"],
            "additionalProperties": False,
        },
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.CONDITIONALLY_IDEMPOTENT,
        scope="records.write",
        maximum_output_bytes=4096,
        allow_parallel=False,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        values = parse_decimal_values(arguments["values"])
        mark: Callable[[], Awaitable[None]] = context.mark_effect_sent
        await mark()
        result = await self._service.append(
            context.principal,
            series_name=str(arguments["series"]),
            values=values,
            observed_at=arguments.get("observed_at"),
            precision=RecordPrecision(arguments.get("precision", RecordPrecision.EXACT.value)),
            note=arguments.get("note"),
            idempotency_key=context.idempotency_key,
            session_id=context.session_id,
            run_id=context.run_id,
            origin_trust=context.origin_trust,
        )
        entry, series = result.entry, result.series
        values = ordered_values(series, entry.values)
        units = {c.name: c.unit or "" for c in series.components}
        rendered = ", ".join(
            f"{key} {short_value(value)}{' ' + units[key] if units[key] else ''}"
            for key, value in values.items()
        )
        observed = (
            entry.observed_at.astimezone(self._zone).date().isoformat()
            if entry.observed_precision is RecordPrecision.DAY
            else _stamp(entry.observed_at)
        )
        return ToolResult(
            ok=True,
            content=[
                TextPart(text=f"Recorded {rendered} for '{series.display_name}' at {observed}.")
            ],
            structured={
                "entry_id": str(entry.id),
                "series": series.display_name,
                "values": _values(values),
                "observed_at": _stamp(entry.observed_at),
                "precision": entry.observed_precision.value,
                "entry_count": result.entry_count,
                "replayed": result.replayed,
                "note_withheld": entry.note_withheld,
            },
        )


class RecordsQueryTool(_RecordsTool):
    _scope = "records.read"
    spec = _spec(
        "records.query",
        "Read entries of one series in a half-open time window, newest first by default, "
        "with a per-component summary over the whole window rather than the page. A bucket "
        "of day, week or month replaces the entries with per-bucket aggregates.",
        {
            "type": "object",
            "properties": {
                "series": _NAME_SCHEMA,
                "since": {"type": "string", "format": "date-time"},
                "until": {"type": "string", "format": "date-time"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "order": {"type": "string", "enum": [item.value for item in Order]},
                "bucket": {"type": "string", "enum": [item.value for item in Bucket]},
            },
            "required": ["series"],
            "additionalProperties": False,
        },
        risk=RiskLevel.LOW,
        idempotency=IdempotencyClass.READ_ONLY,
        scope="records.read",
        maximum_output_bytes=262144,
        allow_parallel=True,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        bucket = arguments.get("bucket")
        result = await self._service.query(
            context.principal,
            series_name=str(arguments["series"]),
            since=arguments.get("since"),
            until=arguments.get("until"),
            limit=int(arguments.get("limit", 100)),
            order=Order(arguments.get("order", Order.DESC.value)),
            bucket=Bucket(bucket) if bucket else None,
            zone=self._zone,
        )
        window = {
            "since": _stamp(result.window.since) if result.window.since else None,
            "until": _stamp(result.window.until),
        }
        name = result.series.display_name
        if result.buckets is not None:
            rows = [
                {
                    "start": _stamp(row.start),
                    "end": _stamp(row.end),
                    **{
                        key: {
                            "count": stats.count,
                            "min": canonical_value(stats.minimum),
                            "max": canonical_value(stats.maximum),
                            "mean": canonical_value(stats.mean),
                        }
                        for key, stats in _in_order(result.series, row.components).items()
                    },
                }
                for row in result.buckets
            ]
            return ToolResult(
                ok=True,
                content=[TextPart(text=f"{len(rows)} buckets in window.")],
                structured={"series": name, "window": window, "bucket": bucket, "buckets": rows},
            )
        return ToolResult(
            ok=True,
            content=[TextPart(text=f"{len(result.entries)} entries in window.")],
            structured={
                "series": name,
                "window": window,
                "entries": [_entry_body(entry, result.series) for entry in result.entries],
                "summary": {
                    k: _component_summary(v)
                    for k, v in _in_order(result.series, result.summary).items()
                },
            },
        )


class RecordsDeleteTool(_RecordsTool):
    _scope = "records.write"
    spec = _spec(
        "records.delete",
        "Remove one entry by id, or a whole series with all its entries. This asks the "
        "owner every time. An entry already gone succeeds with zero removed, while an "
        "unknown series is refused.",
        {
            "type": "object",
            "properties": {
                "series": _NAME_SCHEMA,
                "entry_id": {"type": "string", "format": "uuid"},
            },
            "required": ["series"],
            "additionalProperties": False,
        },
        risk=RiskLevel.MEDIUM,
        idempotency=IdempotencyClass.IDEMPOTENT,
        scope="records.write",
        maximum_output_bytes=4096,
        allow_parallel=False,
        side_effect=SideEffectClass.EXTERNAL_DELETE,
    )

    async def _run(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult:
        raw_entry = arguments.get("entry_id")
        result = await self._service.delete(
            context.principal,
            series_name=str(arguments["series"]),
            entry_id=UUID(str(raw_entry)) if raw_entry is not None else None,
            session_id=context.session_id,
            run_id=context.run_id,
        )
        name = result.series.display_name
        sentence = (
            f"Removed series '{name}' and {result.entries_removed} entries."
            if result.series_removed
            else f"Removed {result.entries_removed} entries from '{name}'."
        )
        return ToolResult(
            ok=True,
            content=[TextPart(text=sentence)],
            structured={
                "series": name,
                "entries_removed": result.entries_removed,
                "series_removed": result.series_removed,
            },
        )
