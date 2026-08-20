import inspect

from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from agent_core.scheduling.worker import ScheduleWorker


def test_worker_services_expose_graceful_stop_and_forever_loop() -> None:
    for service in (DurableWorker, MaintenanceWorker, ScheduleWorker):
        assert callable(service.stop)
        assert inspect.iscoroutinefunction(service.run_once)
        assert inspect.iscoroutinefunction(service.run_forever)
