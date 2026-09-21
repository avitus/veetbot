"""The immutable base every owner-scoped email value shares."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class EmailValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
