"""Source identity filters shared by atomic and derived memory recall."""

from collections import defaultdict
from uuid import UUID

from agent_core.domain.memory import MemoryRecord, RecallQuery, Sensitivity
from agent_core.domain.people import PeopleQuery, PeopleRecord, referenced_people
from agent_core.ports.people import PeopleStore


async def identity_scoped_records(
    store: PeopleStore, query: RecallQuery, records: list[MemoryRecord]
) -> list[MemoryRecord]:
    """Batch hard identity and privacy checks, including links hidden from the requested surface."""
    all_links: dict[UUID, list[PeopleRecord]] = defaultdict(list)
    visible: set[UUID] = set()
    if not records:
        return records
    for offset in range(0, len(records), 1000):
        belief_ids = [record.id for record in records[offset : offset + 1000]]
        for ceiling in {Sensitivity.RESTRICTED, query.sensitivity_ceiling}:
            link_query = PeopleQuery(
                tenant_id=query.tenant_id,
                principal_id=query.principal_id,
                kinds=["memory_link", "relationship", "commitment"],
                belief_ids=belief_ids,
                sensitivity_ceiling=ceiling,
                limit=100,
                known_at=query.known_at,
                as_of=query.as_of,
            )
            for _ in range(100):
                page = await store.query(link_query)
                for link in page[:100]:
                    if ceiling is Sensitivity.RESTRICTED:
                        belief_id = getattr(link, "belief_id", None)
                        if isinstance(belief_id, UUID):
                            all_links[belief_id].append(link)
                    if ceiling is query.sensitivity_ceiling:
                        visible.add(link.id)
                if len(page) <= 100:
                    break
                link_query = link_query.model_copy(update={"after": page[99].id})
            else:
                # A bounded scan cannot certify the unscanned identity assignments.
                return []
    result = []
    focal = set(query.people_scope or ())
    for record in records:
        links = all_links.get(record.id, [])
        if links and (
            any(getattr(link, "unresolved", False) or link.id not in visible for link in links)
            or not any(referenced_people(link) & focal for link in links)
        ):
            continue
        result.append(record)
    return result
