"""Content-free OpenTelemetry metrics for the policy advisory layer."""

from __future__ import annotations

import logging

from opentelemetry import metrics
from opentelemetry.metrics import Meter

logger = logging.getLogger(__name__)


class AdvisoryMetrics:
    """Escalation and abstention counts, from the first day the layer runs.

    The escalation rate is escalations over consulted actions, counted in
    observe mode as well as enforce. The disagreement rate is read from
    resolved approvals whose reason is the advisory escalation, not from here.
    """

    def __init__(self, meter: Meter | None = None) -> None:
        active_meter = meter or metrics.get_meter("agent_core.policy")
        self._consulted = active_meter.create_counter(
            "agent.policy.advisory.consulted", unit="{action}"
        )
        self._abstained = active_meter.create_counter(
            "agent.policy.advisory.abstained", unit="{action}"
        )
        self._latency = active_meter.create_histogram("agent.policy.advisory.latency", unit="s")

    def consulted(
        self, *, tool: str, verdict: str, enforced: bool, signals: tuple[str, ...], seconds: float
    ) -> None:
        attributes: dict[str, str | bool] = {
            "tool": tool,
            "verdict": verdict,
            "enforced": enforced,
        }
        self._consulted.add(1, attributes)
        self._latency.record(seconds, {"tool": tool})
        # One line per consultation, abstentions included: a host with no metrics
        # exporter still has to answer whether the advisor is running.
        logger.info(
            "policy_advisory_consulted",
            extra={
                "tool": tool,
                "verdict": verdict,
                "enforced": enforced,
                "signals": signals,
                "seconds": round(seconds, 3),
            },
        )

    def abstained(self, *, tool: str, cause: str) -> None:
        self._abstained.add(1, {"tool": tool, "cause": cause})
        logger.info("policy_advisory_abstained", extra={"tool": tool, "cause": cause})
