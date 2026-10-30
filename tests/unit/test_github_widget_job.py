"""The repair collector uses authenticated JSON and the existing widget feed shape."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from tests.unit.test_job_examples import example


def page(
    nodes: list[dict[str, Any]], *, after: str | None = None, total: int | None = None
) -> dict[str, Any]:
    """Build a GitHub GraphQL page with explicit pagination metadata."""
    return {
        "status": 200,
        "json": {
            "data": {
                "repository": {
                    "pullRequests": {
                        "totalCount": len(nodes) if total is None else total,
                        "nodes": nodes,
                        "pageInfo": {"hasNextPage": after is not None, "endCursor": after},
                    }
                }
            }
        },
    }


PULL: dict[str, Any] = {
    "number": 3,
    "title": "Repair counter",
    "author": {"login": "example"},
    "isDraft": False,
    "mergeable": "MERGEABLE",
    "latestOpinionatedReviews": {
        "nodes": [{"state": "APPROVED"}],
        "pageInfo": {"hasNextPage": False},
    },
    "reviewRequests": {"totalCount": 0},
    "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
}


def execute(
    responses: list[dict[str, Any]], feed_calls: list[Any] | None = None
) -> tuple[Any, list[Any], list[Any]]:
    """Run the sample collector against queued replies and capture feed writes."""
    calls: list[Any] = []
    feeds: list[Any] = [] if feed_calls is None else feed_calls

    def request(method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Record authenticated GraphQL variables and return the next fixture."""
        assert method == "POST" and url == "https://api.github.com/graphql"
        calls.append(kwargs["json"]["variables"])
        return responses.pop(0)

    result = example("github-widget").main(
        {"repositories": ["example/one", "example/two"], "refresh_seconds": 900},
        SimpleNamespace(
            http=SimpleNamespace(request=request),
            feed=SimpleNamespace(publish=lambda *a, **kw: feeds.append((a, kw))),
        ),
    )
    return result, calls, feeds


def test_widget_job_pages_authenticated_results_and_publishes_verified_zero() -> None:
    """Publish a complete validated snapshot including verified empty repositories."""
    second = {**deepcopy(PULL), "number": 4, "mergeable": "UNKNOWN"}
    _, calls, feeds = execute(
        [page([PULL], after="page2", total=2), page([second], total=2), page([])]
    )
    assert calls[1]["after"] == "page2"
    assert len(feeds) == 1
    args, kwargs = feeds[0]
    assert args[0] == "agent.github-pull-requests"
    assert kwargs == {"expected_interval_seconds": 900}
    first, empty = args[1]["repositories"]
    assert first["totalOpen"] == 2 and first["status"] == "ok"
    assert first["pullRequests"][0] == {
        "number": 3,
        "title": "Repair counter",
        "author": "example",
        "draft": False,
        "review": "approved",
        "checks": "passing",
        "merge": "mergeable",
    }
    assert first["pullRequests"][1]["merge"] == "unknown"
    assert empty["status"] == "ok" and empty["totalOpen"] == 0
    assert empty["pullRequests"] == []
    schema = json.loads(
        (Path(__file__).parents[1] / "fixtures/cockpit/github_widget_feed.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(args[1])


@pytest.mark.parametrize(
    "response",
    [
        {"status": 403, "json": {}},
        {"status": 200, "json": {"errors": [{"message": "Denied"}]}},
        {"status": 200, "json": {"data": {"repository": None}}},
    ],
)
def test_inaccessible_repository_never_becomes_a_verified_zero(response: dict[str, Any]) -> None:
    """Reject inaccessible repositories without publishing a misleading snapshot."""
    feeds: list[Any] = []
    with pytest.raises(ValueError):
        execute([page([PULL]), response], feeds)
    assert feeds == []


def test_incomplete_reviews_and_missing_checks_stay_unknown() -> None:
    """Preserve unknown status when review or check evidence is incomplete."""
    pull = deepcopy(PULL)
    pull["latestOpinionatedReviews"]["pageInfo"]["hasNextPage"] = True
    pull["commits"]["nodes"] = []
    _, _, feeds = execute([page([pull]), page([])])
    actual = feeds[0][0][1]["repositories"][0]["pullRequests"][0]
    assert actual["review"] == actual["checks"] == "unknown"


def test_repeated_page_cursor_refuses_a_partial_snapshot() -> None:
    """Reach the cursor guard with a stable total rather than failing the count check."""
    feeds: list[Any] = []
    with pytest.raises(ValueError, match="invalid pagination cursor"):
        execute(
            [page([PULL], after="same", total=1), page([], after="same", total=1)],
            feed_calls=feeds,
        )
    assert feeds == []


def test_count_mismatch_refuses_a_partial_snapshot() -> None:
    """Refuse to publish when page contents disagree with the reported total."""
    feeds: list[Any] = []
    with pytest.raises(ValueError, match="pagination"):
        execute([page([PULL], total=2), page([])], feeds)
    assert feeds == []


def test_large_snapshot_caps_details_but_preserves_verified_count() -> None:
    """Read every page before limiting details to the existing feed schema."""
    pulls = [{**deepcopy(PULL), "number": number} for number in range(1, 302)]
    responses = [
        page(
            pulls[offset : offset + 10], after=str(offset + 10) if offset < 300 else None, total=301
        )
        for offset in range(0, 301, 10)
    ]
    _, calls, feeds = execute([*responses, page([])])
    assert len(calls) == 32
    snapshot = feeds[0][0][1]
    schema = json.loads(
        (Path(__file__).parents[1] / "fixtures/cockpit/github_widget_feed.schema.json").read_text()
    )
    Draft202012Validator(schema).validate(snapshot)
    repository = snapshot["repositories"][0]
    assert repository["totalOpen"] == 301
    assert len(repository["pullRequests"]) == 300
    assert [pull["number"] for pull in repository["pullRequests"]] == list(range(1, 301))


def test_over_budget_repository_set_stops_before_request_51() -> None:
    """Keep the previous snapshot and direct the owner to reduce oversized inputs."""
    responses = [
        page(
            [{**deepcopy(PULL), "number": offset + index + 1} for index in range(10)],
            after=str(offset + 10) if offset < 490 else None,
            total=500,
        )
        for offset in range(0, 500, 10)
    ] + [page([])]
    feeds: list[Any] = []
    with pytest.raises(ValueError, match="50-page budget; reduce the selected repositories"):
        execute(responses, feeds)
    assert len(responses) == 1
    assert feeds == []
