"""The diagnostic cannot mistake missing observations or canned readers for success."""

from pathlib import Path
from typing import Literal

import pytest

from agent_core.evals.general_memory_recall import (
    Observation,
    Seed,
    fuse_ranks,
    load_seeds,
    summarize,
)

ROOT = Path(__file__).resolve().parents[2]


def test_diagnostic_seeds_span_all_families_without_renamed_load_variants() -> None:
    seeds = load_seeds(ROOT / "evals/capability/general-memory-recall-seeds.v1.tsv")
    assert len(seeds) == 400
    assert len({s.id for s in seeds}) == 400
    assert len({s.question for s in seeds}) == 400
    assert {
        family: sum(s.family == family for s in seeds) for family in {s.family for s in seeds}
    } == dict.fromkeys(
        (
            "attributes",
            "preferences",
            "relationships",
            "goals",
            "decisions",
            "routines",
            "temporal",
            "complex",
        ),
        50,
    )


def test_duplicate_information_needs_do_not_inflate_the_seed_census(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.tsv"
    path.write_text("preferences\tdiet\tUser eats plants.\tWhat is for dinner?\n" * 2)
    with pytest.raises(ValueError, match="duplicate"):
        load_seeds(path)


def fixture_rows() -> tuple[tuple[Seed, ...], tuple[Observation, ...]]:
    seed = Seed(
        id="one", family="preferences", subject="diet", statement="Plants", question="Dinner?"
    )
    modes: tuple[Literal["recent", "relevance"], ...] = ("recent", "relevance")
    paths: tuple[Literal["automatic", "deliberate"], ...] = ("automatic", "deliberate")
    rows = tuple(
        Observation(
            seed_id="one",
            mode=mode,
            path=path,
            candidate_ids=("one",),
            returned_ids=(),
            dropped_ids=("one",),
            tokens=1990,
            elapsed_ms=3,
        )
        for mode in modes
        for path in paths
    )
    return (seed,), rows


def test_missing_or_duplicate_observations_cannot_publish_a_partial_success() -> None:
    seeds, rows = fixture_rows()
    for bad in (rows[:-1], (*rows[:-1], rows[0])):
        with pytest.raises(ValueError, match="coverage"):
            summarize(seeds, bad)


def test_candidate_hits_do_not_count_as_rendered_or_answer_success() -> None:
    seeds, rows = fixture_rows()
    report = summarize(seeds, rows)
    assert report["activation_eligible"] is False
    assert report["formation_success"] is None
    assert report["answer_success"] is None
    cells = report["cells"]
    assert isinstance(cells, list)
    assert all(
        cell["candidate_recall"] == 1 and cell["rendered_recall_at_20"] == 0 for cell in cells
    )
    assert all(cell["failures"] == {"context_budget": 1} for cell in cells)


def test_fusion_does_not_count_repeated_derived_keys_as_independent_votes() -> None:
    assert fuse_ranks(("a", "a", "b"), ("b", "c")) == ("b", "a", "c")


async def test_runner_records_real_retrieval_without_telling_it_expected_labels() -> None:
    from agent_core.evals.general_memory_recall_driver import collect

    seeds = (
        Seed(
            id="old",
            family="preferences",
            subject="menu constraint",
            statement="User requested saffron meals.",
            question="user saffron meals",
        ),
    )
    rows = await collect(seeds, noise=161)
    assert len(rows) == 4
    assert all("old" not in row.candidate_ids for row in rows if row.mode == "recent")
    assert all("old" in row.returned_ids for row in rows if row.mode == "relevance")
