from agent_core.adapters.persistence.memory import InMemoryPolicyProfileRepository
from agent_core.domain.policies import PolicyProfileRecord
from tests.contract.support import NOW


async def test_policy_profile_repository_records_content_addressed_audit_data() -> None:
    repository = InMemoryPolicyProfileRepository()
    record = PolicyProfileRecord(
        policy_version="default@profile+hline",
        profile_name="default",
        profile_sha256="a" * 64,
        hardline_sha256="b" * 64,
        rule_count=22,
        loaded_at=NOW,
        loaded_by="contract",
    )
    assert await repository.record(record) == record
    assert await repository.get(record.policy_version) == record
