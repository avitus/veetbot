"""Closed, revalidated request vocabulary at both ends of the private socket."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from final_cut.plan import Change, Handle, Shot, Strict


class Select(Strict):
    operation: Literal["select"]


class Inventory(Strict):
    operation: Literal["inventory"]


class Excerpt(Strict):
    operation: Literal["excerpt"]
    grant: Handle
    shot: Shot
    analyze: bool = True


class Recommend(Strict):
    operation: Literal["recommend"]
    grant: Handle
    evidence: Annotated[list[Handle], Field(min_length=1, max_length=24)]
    seconds: Annotated[int, Field(ge=1, le=600)]
    brief: Annotated[str, Field(max_length=1200)] = ""


class Cut(Strict):
    operation: Literal["cut"]
    grant: Handle
    shots: Annotated[list[Shot], Field(min_length=1, max_length=64)]


class Revision(Strict):
    operation: Literal["revise"]
    grant: Handle
    version: Handle
    changes: Annotated[list[Change], Field(min_length=1, max_length=64)]


class Handoff(Strict):
    operation: Literal["deliver", "reconcile"]
    grant: Handle
    version: Handle


Request = Annotated[
    Select | Inventory | Excerpt | Recommend | Cut | Revision | Handoff,
    Field(discriminator="operation"),
]
REQUEST = TypeAdapter[Request](Request)
