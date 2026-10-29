"""The per-execution broker for approved job access (ADR-0268)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from agent_core.domain.jobs import JobAccess, JobBrokerError, JobExecutionError
from agent_core.domain.web import is_public_https_url


class JobBroker:
    def __init__(
        self,
        access: JobAccess,
        *,
        http: Callable[..., Awaitable[dict[str, Any]]],
        model: Callable[..., Awaitable[dict[str, Any]]],
        feed: Callable[..., Awaitable[dict[str, Any]]],
        log: Callable[..., Awaitable[None]],
        trial: bool = False,
    ) -> None:
        self.access = access
        self.http = http
        self.model = model
        self.feed = feed
        self.log = log
        self.trial = trial
        self._requests = 0
        self._cost = Decimal("0")
        self._limit_failure: str | None = None

    def check_result(self) -> None:
        """A program cannot turn a limit failure into success by catching it."""
        if self._limit_failure is not None:
            raise JobExecutionError(self._limit_failure)

    def _refused(self, reason: str, *, limit: bool = False) -> dict[str, Any]:
        if limit:
            self._limit_failure = f"job.{reason}"
        return {"status": "denied", "reason_code": f"job.{reason}", "retryable": False}

    async def dispatch(self, call: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        if self._limit_failure is not None:
            return self._refused(self._limit_failure.removeprefix("job."))
        try:
            result = await self._dispatch(call, arguments, call_id)
        except JobBrokerError as exc:
            if exc.reason in {
                "job.request_limit",
                "job.response_limit",
                "job.model_budget_exceeded",
            }:
                self._limit_failure = exc.reason
            return {"status": "failed", "reason_code": exc.reason, "retryable": exc.retryable}
        except (OSError, TimeoutError):
            # Upstream exceptions may name credentials, URLs or prompt content.
            return {"status": "failed", "reason_code": "job.service_unavailable", "retryable": True}
        return result

    async def _dispatch(self, call: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
        if call == "http.request":
            url = arguments.get("url")
            if not isinstance(url, str) or not is_public_https_url(url):
                return self._refused("host_refused")
            host = urlsplit(url).hostname
            if host is None or host not in self.access.hosts:
                return self._refused("host_refused")
            method = arguments.get("method", "GET")
            if method not in {"GET", "POST"} or set(arguments) - {"url", "method", "json"}:
                return self._refused("invalid_arguments")
            if self._requests >= self.access.maximum_requests:
                return self._refused("request_limit", limit=True)
            self._requests += 1
            result = await self.http(
                method=method,
                url=url,
                json=arguments.get("json"),
                credential=self.access.hosts[host],
                maximum_response_bytes=self.access.maximum_response_bytes,
                call_id=call_id,
            )
        elif call == "model.complete":
            remaining = Decimal(self.access.model_cost) - self._cost
            if remaining <= 0:
                return self._refused("model_budget_exceeded", limit=True)
            prompt = arguments.get("prompt")
            maximum = arguments.get("max_output_tokens", 1024)
            if (
                not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt.encode()) > 48 * 1024
                or type(maximum) is not int
                or not 1 <= maximum <= 8192
                or set(arguments) - {"prompt", "max_output_tokens"}
            ):
                return self._refused("invalid_arguments")
            completion = await self.model(
                prompt=prompt,
                max_output_tokens=maximum,
                maximum_cost=remaining,
                call_id=call_id,
            )
            cost = Decimal(completion["cost"])
            if not cost.is_finite() or cost < 0:
                return self._refused("model_accounting_invalid", limit=True)
            self._cost += cost
            if self._cost > Decimal(self.access.model_cost):
                return self._refused("model_budget_exceeded", limit=True)
            result = {"text": completion["text"]}
        elif call == "feed.publish":
            feed_id = arguments.get("feed_id")
            if not isinstance(feed_id, str) or feed_id not in self.access.feeds:
                return self._refused("feed_refused")
            interval = arguments.get("expected_interval_seconds")
            if (
                type(interval) is not int
                or interval <= 0
                or "data" not in arguments
                or set(arguments) - {"feed_id", "data", "expected_interval_seconds", "schema"}
            ):
                return self._refused("invalid_arguments")
            result = await self.feed(**arguments, preview=self.trial, call_id=call_id)
        elif call == "log":
            message = arguments.get("message")
            if (
                not isinstance(message, str)
                or len(message.encode()) > 2000
                or set(arguments) != {"message"}
            ):
                return self._refused("invalid_arguments")
            await self.log(message=message, call_id=call_id)
            result = {}
        else:
            return self._refused("undeclared_call")
        return {"status": "succeeded", "result": result}
