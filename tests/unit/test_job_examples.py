"""The shipped examples use exactly the operations their task requires."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def example(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "samples" / "jobs" / name / "main.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_github_collector_fetches_structured_data_without_a_model() -> None:
    calls: list[Any] = []
    feeds: list[Any] = []

    def request(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return {
            "status": 200,
            "json": {
                "data": {
                    "repository": {
                        "pullRequests": {
                            "nodes": [
                                {
                                    "number": 3,
                                    "title": "Improve counter",
                                    "url": "https://github.com/example/repo/pull/3",
                                    "isDraft": False,
                                    "reviewDecision": "APPROVED",
                                    "updatedAt": "2026-10-29T00:00:00Z",
                                    "commits": {
                                        "nodes": [
                                            {"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}
                                        ]
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
        }

    def publish(*args: Any, **kwargs: Any) -> dict[str, Any]:
        feeds.append((args, kwargs))
        return {}

    result = example("github-prs").main(
        {"repositories": ["example/repo"]},
        SimpleNamespace(
            http=SimpleNamespace(request=request), feed=SimpleNamespace(publish=publish)
        ),
    )
    assert len(calls) == 1 and len(feeds) == 1
    assert result["count"] == 1
    assert feeds[0][0][1]["pull_requests"][0]["checks"] == "SUCCESS"


def test_mixed_example_requests_one_completion_then_formats_with_code() -> None:
    prompts: list[str] = []

    def complete(prompt: str, **kwargs: Any) -> str:
        prompts.append(prompt)
        return "- Review the change\n- Check the migration"

    result = example("review-summary").main(
        {"url": "https://example.com/reviews"},
        SimpleNamespace(
            http=SimpleNamespace(request=lambda *args: {"status": 200, "json": {"pending": 2}}),
            model=SimpleNamespace(complete=complete),
        ),
    )
    assert len(prompts) == 1
    assert result["items"] == ["Review the change", "Check the migration"]
