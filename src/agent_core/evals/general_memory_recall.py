"""Seeded-retrieval diagnostics for M33, deliberately not activation evidence."""

from __future__ import annotations

import hashlib
from collections import Counter
from math import ceil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FAMILIES = frozenset(
    {
        "attributes",
        "preferences",
        "relationships",
        "goals",
        "decisions",
        "routines",
        "temporal",
        "complex",
    }
)
VERSION = "general-recall-diagnostic@1"


class Seed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: str
    family: str
    subject: str
    statement: str
    question: str


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    seed_id: str
    mode: Literal["recent", "relevance"]
    path: Literal["automatic", "deliberate"]
    candidate_ids: tuple[str, ...]
    returned_ids: tuple[str, ...]
    dropped_ids: tuple[str, ...] = ()
    blocked_ids: tuple[str, ...] = ()
    tokens: int = Field(ge=0, le=2000)
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)


def load_seeds(path: Path) -> tuple[Seed, ...]:
    seeds: list[Seed] = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 4 or any(not part.strip() for part in parts):
            raise ValueError("invalid seed row")
        family, subject, statement, question = parts
        if family not in FAMILIES:
            raise ValueError("unknown family")
        identifier = hashlib.sha256(f"{family}:{subject}".encode()).hexdigest()[:16]
        seeds.append(
            Seed(
                id=identifier,
                family=family,
                subject=subject,
                statement=statement,
                question=question,
            )
        )
    for field in ("id", "statement", "question"):
        if len({getattr(seed, field).casefold() for seed in seeds}) != len(seeds):
            raise ValueError("duplicate information need")
    if not seeds:
        raise ValueError("empty seed corpus")
    return tuple(seeds)


def summarize(seeds: tuple[Seed, ...], rows: tuple[Observation, ...]) -> dict[str, object]:
    expected = {
        (s.id, mode, path)
        for s in seeds
        for mode in ("recent", "relevance")
        for path in ("automatic", "deliberate")
    }
    observed = {(r.seed_id, r.mode, r.path) for r in rows}
    if observed != expected or len(rows) != len(expected):
        raise ValueError("observation coverage differs from the complete experiment")
    families = {seed.id: seed.family for seed in seeds}
    cells: list[dict[str, object]] = []
    for mode in ("recent", "relevance"):
        for path in ("automatic", "deliberate"):
            for family in ("all", *sorted(set(families.values()))):
                group = [
                    r
                    for r in rows
                    if r.mode == mode
                    and r.path == path
                    and (family == "all" or families[r.seed_id] == family)
                ]
                if not group:
                    continue
                failures: Counter[str] = Counter()
                for row in group:
                    if row.seed_id in row.returned_ids[:20]:
                        continue
                    failures[
                        "candidate"
                        if row.seed_id not in row.candidate_ids
                        else "blocked"
                        if row.seed_id in row.blocked_ids
                        else "context_budget"
                        if row.seed_id in row.dropped_ids
                        else "ranking_or_collapse"
                    ] += 1
                times = sorted(r.elapsed_ms for r in group)
                cells.append(
                    {
                        "mode": mode,
                        "path": path,
                        "family": family,
                        "cases": len(group),
                        "candidate_recall": sum(r.seed_id in r.candidate_ids for r in group)
                        / len(group),
                        "rendered_recall_at_20": sum(
                            r.seed_id in r.returned_ids[:20] for r in group
                        )
                        / len(group),
                        "failures": dict(failures),
                        "p50_ms": times[ceil(len(times) * 0.5) - 1],
                        "p95_ms": times[ceil(len(times) * 0.95) - 1],
                        "mean_tokens": sum(r.tokens for r in group) / len(group),
                    }
                )
    return {
        "version": VERSION,
        "activation_eligible": False,
        "formation_success": None,
        "answer_success": None,
        "cases": len(seeds),
        "cells": cells,
    }


def fuse_ranks(*channels: tuple[str, ...], k: int = 60) -> tuple[str, ...]:
    if k < 1:
        raise ValueError("fusion constant must be positive")
    scores: dict[str, float] = {}
    for channel in channels:
        for rank, identifier in enumerate(dict.fromkeys(channel), 1):
            scores[identifier] = scores.get(identifier, 0) + 1 / (k + rank)
    return tuple(sorted(scores, key=lambda identifier: (-scores[identifier], identifier)))
