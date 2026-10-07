"""Reviewed, finite browser recipes with semantic targets and positive completion checks."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.browser import (
    BrowserAction,
    BrowserCondition,
    browser_origin,
    normalize_browser_origin,
)
from agent_core.domain.browser_extraction import BrowserExtractionField


class BrowserWorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["navigate", "observe", "act", "extract"]
    url: str | None = Field(default=None, max_length=4096)
    role: str | None = Field(default=None, min_length=1, max_length=64)
    name: str | None = Field(default=None, min_length=1, max_length=1024)
    action: str | None = None
    value: str | None = Field(default=None, max_length=4096)
    key: str | None = None
    delta_y: int | None = None
    postcondition: BrowserCondition | None = None
    collection_kind: Literal["table", "list", "form"] | None = None
    index: int = Field(default=0, ge=0, le=15)
    fields: list[BrowserExtractionField] = Field(default_factory=list, max_length=8)
    row_limit: int = Field(default=20, ge=1, le=50)

    @model_validator(mode="after")
    def closed_step(self) -> BrowserWorkflowStep:
        permitted = {
            "navigate": {"kind", "url"},
            "observe": {"kind", "postcondition"},
            "act": {"kind", "role", "name", "action", "value", "key", "delta_y", "postcondition"},
            "extract": {"kind", "collection_kind", "index", "fields", "row_limit"},
        }[self.kind]
        if not self.model_fields_set <= permitted:
            raise ValueError("workflow step mixes modes")
        if self.kind == "navigate":
            browser_origin(self.url or "")
        elif self.kind == "act":
            if not self.role or not self.name or self.postcondition is None:
                raise ValueError("actions require a unique semantic target and postcondition")
            BrowserAction.model_validate(
                {
                    "kind": self.action,
                    "expected_revision": "current",
                    "ref": "current",
                    **self.action_values(),
                }
            )
        elif self.kind == "extract" and (self.collection_kind is None or not self.fields):
            raise ValueError("extraction requires a collection and fields")
        return self

    def action_values(self) -> dict[str, str | int]:
        return {
            key: value
            for key in ("value", "key", "delta_y")
            if (value := getattr(self, key)) is not None
        }


class BrowserWorkflowRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: int = Field(ge=1, le=10000, strict=True)
    origin: str
    intents: list[str] = Field(min_length=1, max_length=8)
    steps: list[BrowserWorkflowStep] = Field(min_length=1, max_length=16)
    maximum_seconds: int = Field(default=900, ge=1, le=900, strict=True)

    @field_validator("origin")
    @classmethod
    def normalized_origin(cls, value: str) -> str:
        return normalize_browser_origin(value)

    @model_validator(mode="after")
    def confined_recipe(self) -> BrowserWorkflowRecipe:
        if any(not intent.strip() or len(intent) > 512 for intent in self.intents):
            raise ValueError("workflow intent is empty or too long")
        if self.steps[0].kind != "navigate":
            raise ValueError("workflow starts with a scoped navigation")
        if any(
            step.url is not None and browser_origin(step.url) != self.origin for step in self.steps
        ):
            raise ValueError("workflow navigation leaves its origin")
        return self


class BrowserWorkflowCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    recipes: list[BrowserWorkflowRecipe] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def unique_definitions(self) -> BrowserWorkflowCatalog:
        if len({recipe.id for recipe in self.recipes}) != len(self.recipes):
            raise ValueError("workflow identifiers must be unique")
        return self
