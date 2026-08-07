from pathlib import Path
from uuid import UUID

from agent_core.adapters.execution.local_workspace import LocalWorkspaceFactory


async def test_workspace_factory_returns_a_stable_run_scoped_handle(tmp_path: Path) -> None:
    factory = LocalWorkspaceFactory(tmp_path)
    run_id = UUID(int=1)
    first = factory.for_run("tenant-a", run_id)
    second = factory.for_run("tenant-a", run_id)
    other = factory.for_run("tenant-a", UUID(int=2))
    hostile = factory.for_run("../outside", run_id)
    assert first is second
    assert first is not other
    assert first is not hostile
    await hostile.write("probe.txt", b"probe")
    assert await hostile.read("probe.txt") == b"probe"
    assert not (tmp_path.parent / "outside" / str(run_id) / "probe.txt").exists()
    assert not (tmp_path.parent / "outside").exists()


async def test_workspace_factory_fences_reclaimed_run_leases(tmp_path: Path) -> None:
    factory = LocalWorkspaceFactory(tmp_path)
    run_id = UUID(int=3)
    first = factory.for_run("tenant-a", run_id, lease_epoch=1)
    reclaimed = factory.for_run("tenant-a", run_id, lease_epoch=2)

    await first.write("lease.txt", b"first")
    await reclaimed.write("lease.txt", b"second")

    assert first is not reclaimed
    assert await first.read("lease.txt") == b"first"
    assert await reclaimed.read("lease.txt") == b"second"
