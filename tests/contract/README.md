# Contract tests

Each port added under `agent_core.ports` must add its shared adapter contract
here. The Milestone 0 structural gate rejects a port without one. A port with
multiple shipped implementations must enumerate them in its contract module and
run the shared invariants against every subject. The memory-candidate extractor
contract is the reference pattern: its subject list comes from the authoritative
production-package census, not a second list maintained by the test.
