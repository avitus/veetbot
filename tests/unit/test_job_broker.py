"""Jobs use only their approved access, and models run only on explicit calls."""

from __future__ import annotations

from typing import Any

import pytest

from agent_core.application.job_broker import JobBroker
from agent_core.domain.jobs import JobAccess


class Services:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def http(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("http", kwargs))
        return {"status": 200, "json": {"count": 3}}

    async def model(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("model", kwargs))
        return {"text": "Three reviews", "cost": "0.02"}

    async def feed(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("feed", kwargs))
        return {"feed_id": kwargs["feed_id"], "preview": kwargs["preview"]}

    async def log(self, **kwargs: Any) -> None:
        self.calls.append(("log", kwargs))

    def broker(self, access: JobAccess, *, trial: bool = False) -> JobBroker:
        return JobBroker(
            access, http=self.http, model=self.model, feed=self.feed, log=self.log, trial=trial
        )


async def test_code_path_has_no_implicit_model_and_binds_approved_credential() -> None:
    services = Services()
    broker = services.broker(JobAccess({}, {"api.github.com": "github-reader"}))
    result = await broker.dispatch(
        "http.request", {"method": "GET", "url": "https://api.github.com/repos/example"}, "read"
    )
    assert result == {"status": "succeeded", "result": {"status": 200, "json": {"count": 3}}}
    assert [name for name, _ in services.calls] == ["http"]
    assert services.calls[0][1]["credential"] == "github-reader"
    assert services.calls[0][1]["maximum_response_bytes"] == 48 * 1024


async def test_mixed_job_explicit_model_calls_consume_the_grant_budget() -> None:
    services = Services()
    broker = services.broker(JobAccess({}, {}, model_cost="0.04"))
    for expected_remaining in ("0.04", "0.02"):
        reply = await broker.dispatch("model.complete", {"prompt": "Summarize"}, "model")
        assert reply["result"] == {"text": "Three reviews"}
        assert str(services.calls[-1][1]["maximum_cost"]) == expected_remaining
    reply = await broker.dispatch("model.complete", {"prompt": "Again"}, "third")
    assert reply["reason_code"] == "job.model_budget_exceeded"
    assert len(services.calls) == 2


@pytest.mark.parametrize(
    ("call", "arguments", "reason"),
    [
        ("http.request", {"method": "GET", "url": "https://other.example/a"}, "host_refused"),
        ("http.request", {"method": "GET", "url": "http://api.github.com/a"}, "host_refused"),
        ("http.request", {"method": "GET", "url": "https://api.github.com:444/a"}, "host_refused"),
        ("http.request", {"method": "GET", "url": "https://user@api.github.com/a"}, "host_refused"),
        ("http.request", {"method": "GET", "url": "https://127.0.0.1/a"}, "host_refused"),
        ("model.complete", {"prompt": "Hello"}, "model_budget_exceeded"),
        ("feed.publish", {"feed_id": "agent.unapproved", "data": {}}, "feed_refused"),
        ("workspace.read", {}, "undeclared_call"),
    ],
)
async def test_refused_access_never_reaches_a_service(
    call: str, arguments: dict[str, Any], reason: str
) -> None:
    services = Services()
    broker = services.broker(JobAccess({}, {"api.github.com": None}))
    reply = await broker.dispatch(call, arguments, "refused")
    assert reply["reason_code"] == f"job.{reason}"
    assert services.calls == []


async def test_live_trial_publishes_to_preview_and_never_expands_access() -> None:
    services = Services()
    broker = services.broker(JobAccess({}, {}, feeds=frozenset({"agent.reviews"})), trial=True)
    reply = await broker.dispatch(
        "feed.publish",
        {"feed_id": "agent.reviews", "data": {"count": 3}, "expected_interval_seconds": 300},
        "publish",
    )
    assert reply["result"]["preview"] is True
    assert services.calls[0][1]["preview"] is True
    refused = await broker.dispatch("http.request", {"url": "https://api.github.com"}, "read")
    assert refused["reason_code"] == "job.host_refused"
    assert len(services.calls) == 1


async def test_request_limit_stops_dispatch_even_if_job_catches_the_error() -> None:
    services = Services()
    broker = services.broker(JobAccess({}, {"api.github.com": None}, maximum_requests=1))
    arguments = {"method": "GET", "url": "https://api.github.com"}
    assert (await broker.dispatch("http.request", arguments, "one"))["status"] == "succeeded"
    assert (await broker.dispatch("http.request", arguments, "two"))[
        "reason_code"
    ] == "job.request_limit"
    assert (await broker.dispatch("http.request", arguments, "three"))[
        "reason_code"
    ] == "job.request_limit"
    assert len(services.calls) == 1
    with pytest.raises(ValueError, match=r"job\.request_limit"):
        broker.check_result()


async def test_log_lines_are_bounded_and_failure_does_not_echo_service_secrets() -> None:
    services = Services()
    broker = services.broker(JobAccess({}, {"api.github.com": None}))
    assert (await broker.dispatch("log", {"message": "Finished"}, "log"))["status"] == "succeeded"
    assert services.calls == [("log", {"message": "Finished", "call_id": "log"})]
    reply = await broker.dispatch("log", {"message": "x" * 2001}, "long")
    assert reply["reason_code"] == "job.invalid_arguments"

    async def failed(**kwargs: Any) -> dict[str, Any]:
        raise OSError("Canary credential must never reach the job")

    broker.http = failed
    reply = await broker.dispatch("http.request", {"url": "https://api.github.com"}, "read")
    assert reply == {
        "status": "failed",
        "reason_code": "job.service_unavailable",
        "retryable": True,
    }


async def test_failed_model_cannot_reuse_its_allowance_or_report_success() -> None:
    from agent_core.domain.jobs import JobBrokerError, JobExecutionError

    services = Services()
    broker = services.broker(JobAccess({}, {}, model_cost="0.04"))
    attempts = 0

    async def failed(**kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        # The adapter has persisted partial billed usage before reporting failure.
        raise JobBrokerError("job.model_failed")

    broker.model = failed
    first = await broker.dispatch("model.complete", {"prompt": "Summarize"}, "first")
    assert first["reason_code"] == "job.model_failed"
    second = await broker.dispatch("model.complete", {"prompt": "Try again"}, "second")
    assert second["reason_code"] == "job.model_failed"
    assert attempts == 1
    with pytest.raises(JobExecutionError, match=r"job\.model_failed"):
        broker.check_result()
