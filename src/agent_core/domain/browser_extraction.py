"""Provider-neutral contracts for deterministic visible-content extraction."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BrowserExtractionField(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    column: int = Field(ge=0, le=15)
    type: Literal["string", "integer", "number", "boolean"]
    required: bool = True


class BrowserExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    expected_revision: str = Field(min_length=1, max_length=128)
    kind: Literal["table", "list", "form"]
    index: int = Field(default=0, ge=0, le=15)
    fields: list[BrowserExtractionField] = Field(min_length=1, max_length=8)
    row_limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def unique_fields(self) -> BrowserExtractionRequest:
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("extraction field names must be unique")
        return self


class BrowserExtractedCell(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    field: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
    column: int = Field(ge=0, le=15)
    ref: str = Field(min_length=1, max_length=128)
    text: str = Field(max_length=256)
    value: str | int | float | bool | None
    status: Literal["present", "missing", "invalid", "truncated"]


class BrowserExtractedRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: str = Field(min_length=1, max_length=128)
    cells: tuple[BrowserExtractedCell, ...] = Field(min_length=1, max_length=8)
    schema_valid: bool


class BrowserExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    revision: str = Field(min_length=1, max_length=128)
    kind: Literal["table", "list", "form"]
    index: int = Field(ge=0, le=15)
    status: Literal["extracted", "not_found", "unsupported_structure"]
    source_ref: str | None = Field(default=None, max_length=128)
    source_name: str = Field(default="", max_length=256)
    fields: list[BrowserExtractionField] = Field(min_length=1, max_length=8)
    rows: tuple[BrowserExtractedRow, ...] = Field(default=(), max_length=50)
    scope: Literal["main_document"] = "main_document"
    source_nodes: int = Field(ge=0, le=8192)
    source_scan_limit_reached: bool
    row_nodes: int = Field(ge=0, le=4096)
    row_scan_limit_reached: bool
    row_limit: int = Field(ge=1, le=50)
    omitted_rows: int = Field(default=0, ge=0, le=4096)
    byte_limit_reached: bool = False

    @model_validator(mode="after")
    def records_match_schema(self) -> BrowserExtractionResult:
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("extraction field names must be unique")
        if len(self.rows) > self.row_limit or (self.status != "extracted" and self.rows):
            raise ValueError("extraction rows contradict their envelope")
        if (self.status == "not_found") != (self.source_ref is None):
            raise ValueError("extraction source contradicts its status")
        refs = [row.ref for row in self.rows]
        if len(set(refs)) != len(refs):
            raise ValueError("extraction row references must be unique")
        for row in self.rows:
            if len(row.cells) != len(self.fields):
                raise ValueError("extraction cells must match fields")
            valid = True
            for cell, field in zip(row.cells, self.fields, strict=True):
                if (cell.field, cell.column) != (field.name, field.column):
                    raise ValueError("extraction cell does not match its field")
                if cell.status != "present":
                    if cell.value is not None:
                        raise ValueError("unavailable cell has a value")
                    valid &= cell.status == "missing" and not field.required
                    continue
                value = cell.value
                accepted = (
                    (field.type == "string" and isinstance(value, str))
                    or (field.type == "boolean" and type(value) is bool)
                    or (field.type == "integer" and type(value) is int and abs(value) <= 2**53 - 1)
                    or (
                        field.type == "number"
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(value)
                    )
                )
                if not accepted:
                    raise ValueError("extraction value does not match its field type")
            if row.schema_valid != valid:
                raise ValueError("extraction schema validity is inconsistent")
        return self
