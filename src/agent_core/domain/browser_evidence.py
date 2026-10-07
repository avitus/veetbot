"""Closed, positive predicates over bounded browser evidence."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.browser_extraction import BrowserExtractionField, BrowserExtractionRequest
from agent_core.domain.web import is_public_https_url

if TYPE_CHECKING:
    from agent_core.domain.browser import BrowserObservation


class BrowserTextEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["text"]
    # One complete rendered line, with whitespace normalized on both sides.
    text: str = Field(min_length=1, max_length=512, pattern=r"\S")


class BrowserRegionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["region"]
    region_kind: Literal["dialog", "alert", "status", "form", "heading", "main", "section"]
    text: str = Field(min_length=1, max_length=512, pattern=r"\S")


class BrowserLocationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["location"]
    url: str = Field(min_length=1, max_length=4096)

    @field_validator("url")
    @classmethod
    def public_location(cls, value: str) -> str:
        if not is_public_https_url(value):
            raise ValueError("evidence location must be public HTTPS")
        return value


BrowserSimpleEvidence = Annotated[
    BrowserTextEvidence | BrowserRegionEvidence | BrowserLocationEvidence,
    Field(discriminator="kind"),
]


class BrowserRowField(BrowserExtractionField):
    value: str | int | float | bool

    @model_validator(mode="after")
    def typed_value(self) -> BrowserRowField:
        value = self.value
        valid = (
            (self.type == "string" and isinstance(value, str) and 0 < len(value) <= 256)
            or (self.type == "boolean" and type(value) is bool)
            or (self.type == "integer" and type(value) is int and abs(value) <= 2**53 - 1)
            or (
                self.type == "number"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
        )
        if not valid or not self.required:
            raise ValueError("row evidence needs a required, bounded typed value")
        return self


class BrowserRowEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: Literal["row"]
    collection_kind: Literal["table", "list", "form"]
    index: int = Field(default=0, ge=0, le=15)
    fields: list[BrowserRowField] = Field(min_length=1, max_length=8)
    row_limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def unique_fields(self) -> BrowserRowEvidence:
        if len({field.name for field in self.fields}) != len(self.fields):
            raise ValueError("row field names must be unique")
        return self

    def extraction_request(self, revision: str) -> BrowserExtractionRequest:
        return BrowserExtractionRequest(
            expected_revision=revision,
            kind=self.collection_kind,
            index=self.index,
            fields=[
                BrowserExtractionField.model_validate(field.model_dump(exclude={"value"}))
                for field in self.fields
            ],
            row_limit=self.row_limit,
        )


BrowserEvidence = Annotated[
    BrowserTextEvidence | BrowserRegionEvidence | BrowserLocationEvidence | BrowserRowEvidence,
    Field(discriminator="kind"),
]


def evidence_status(
    observation: BrowserObservation,
    evidence: BrowserEvidence,
) -> Literal["satisfied", "not_observed", "ambiguous"]:
    """Match complete positive evidence; missing evidence never proves absence."""
    if observation.interruption is not None:
        return "not_observed"
    if evidence.kind == "row":
        extracted = observation.extraction
        request = evidence.extraction_request(observation.revision)
        if (
            extracted is None
            or extracted.revision != observation.revision
            or extracted.kind != request.kind
            or extracted.index != request.index
            or extracted.fields != request.fields
            or extracted.row_limit != request.row_limit
        ):
            return "not_observed"
        matches = sum(
            row.schema_valid
            and all(
                cell.status == "present" and cell.value == field.value
                for cell, field in zip(row.cells, evidence.fields, strict=True)
            )
            for row in extracted.rows
        )
    elif evidence.kind == "location":
        matches = int(observation.url == evidence.url)
    elif evidence.kind == "region":
        matches = sum(
            region.kind == evidence.region_kind
            and not region.text_truncated
            and " ".join(region.text.split()) == " ".join(evidence.text.split())
            for region in observation.regions
        )
    else:
        lines = observation.text.splitlines()
        coverage = observation.text_coverage
        if coverage is not None and (
            coverage.omitted_text_bytes
            or coverage.node_limit_reached
            or coverage.text_limit_reached
        ):
            # The last retained line may be only the beginning of a longer message.
            lines = lines[:-1]
        if observation.focus is not None:
            if observation.focus.text_offset:
                lines = lines[1:]
            if (
                observation.focus.text_offset + len(observation.text.encode())
                < observation.focus.text_total_bytes
            ):
                lines = lines[:-1]
        matches = sum(" ".join(line.split()) == " ".join(evidence.text.split()) for line in lines)
    return "ambiguous" if matches > 1 else "satisfied" if matches == 1 else "not_observed"
