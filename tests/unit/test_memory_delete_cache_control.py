"""DELETE /v1/memories/{id} answers `Cache-Control: private, no-store`.

Guards docs/plan/memory-read-api-and-browser.md "Writes", which says of the
delete and the review route: "Both responses carry `Cache-Control: private,
no-store`."
"""

from __future__ import annotations

from agent_core.bootstrap import build
from agent_core.domain.memory import Sensitivity
from tests.gates.test_memory_read_api_m17 import _client, _enabled_settings
from tests.gates.test_memory_write_api_adr0117 import WRITER, _flagged, _seed_with_session


async def test_memory_delete_response_is_private_no_store() -> None:
    async with build(
        settings=_enabled_settings(), storage="memory", sequential_ids=True, principal=WRITER
    ) as composition:
        doomed = _flagged(belief_id=1, position=1, sensitivity=Sensitivity.SENSITIVE)
        await _seed_with_session(composition, [doomed])
        async with _client(composition) as client:
            deleted = await client.delete(
                f"/v1/memories/{doomed.id}",
                params={"ceiling": "restricted"},
                headers={"Idempotency-Key": "delete-1"},
            )
            assert deleted.status_code == 204, deleted.text
            assert deleted.headers.get("cache-control") == "private, no-store"
