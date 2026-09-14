"""Bounded polling of signed receipts and provider history; never dispatches calls."""

import asyncio
import logging

from agent_core.application.calling import CallService

logger = logging.getLogger(__name__)


class CallWorker:
    def __init__(self, service: CallService) -> None:
        self.service = service
        self.stopped = asyncio.Event()

    def stop(self) -> None:
        self.stopped.set()

    async def run_forever(self) -> None:
        while not self.stopped.is_set():
            try:
                await self.service.reconcile()
            except Exception:
                # Exceptions can include provider bodies or credentials; emit only a fixed code.
                logger.warning("calling.reconciliation_failed")
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=10)
            except TimeoutError:
                continue
