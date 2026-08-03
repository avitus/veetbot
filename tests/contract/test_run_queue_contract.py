import inspect

from agent_core.adapters.persistence.queue import PostgresRunQueue


def test_run_queue_exposes_claim_heartbeat_release_and_reclaim() -> None:
    for name in ("enqueue", "claim", "heartbeat", "release", "reclaim_expired"):
        assert inspect.iscoroutinefunction(getattr(PostgresRunQueue, name))
