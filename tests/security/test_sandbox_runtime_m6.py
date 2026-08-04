"""Milestone 6 isolation gates against a real container runtime, never the fake."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from uuid import UUID

import pytest

from agent_core.adapters.determinism import RandomIdFactory, SystemClock
from agent_core.adapters.execution.docker import (
    DockerExecutionEnvironment,
    resolve_local_image_digest,
)
from agent_core.domain.execution import (
    BridgeEndpoint,
    EgressDestination,
    EgressMode,
    EgressPolicy,
    EnvironmentHandle,
    EnvironmentSpec,
    ExecutionCommand,
    ExecutionResult,
    KillReason,
    ResourceLimits,
)
from agent_core.execution.environment import TIER_ZERO_NAMES, build_sandbox_environment
from agent_core.tools.bridge import ProgrammaticBridgeSession

pytestmark = [pytest.mark.integration, pytest.mark.sandbox]


def _limits(**overrides: int) -> ResourceLimits:
    values = {
        "cpu_millicores": 500,
        "memory_bytes": 128 * 1024 * 1024,
        "pids_max": 64,
        "workspace_bytes": 2 * 1024 * 1024,
        "inodes_max": 1000,
        "wall_clock_seconds": 15,
    }
    values.update(overrides)
    return ResourceLimits(**values)


@pytest.fixture
async def runtime() -> AsyncIterator[tuple[DockerExecutionEnvironment, str]]:
    adapter = DockerExecutionEnvironment(
        SystemClock(), RandomIdFactory(), hard_cap_seconds=20, reaper_grace_seconds=0
    )
    digest = await resolve_local_image_digest("agent-core-sandbox:dev")
    try:
        yield adapter, digest
    finally:
        for environment_id, state in tuple(adapter._states.items()):
            handle = EnvironmentHandle(
                environment_id=environment_id,
                tenant_id=state.specification.tenant_id,
                run_id=state.specification.run_id,
                lease_epoch=state.specification.lease_epoch,
                created_at=SystemClock().now(),
                expires_at=SystemClock().now(),
            )
            await adapter.destroy(handle)


@asynccontextmanager
async def _environment(
    runtime: tuple[DockerExecutionEnvironment, str],
    *,
    run_id: int,
    tenant_id: str = "tenant-a",
    limits: ResourceLimits | None = None,
    egress: EgressPolicy | None = None,
    parent: dict[str, str] | None = None,
) -> AsyncIterator[tuple[DockerExecutionEnvironment, EnvironmentHandle]]:
    adapter, digest = runtime
    handle = await adapter.provision(
        EnvironmentSpec(
            tenant_id=tenant_id,
            run_id=UUID(int=run_id),
            lease_epoch=1,
            image_digest=digest,
            limits=limits or _limits(),
            egress=egress or EgressPolicy(),
            environment=build_sandbox_environment(parent or {}),
        )
    )
    try:
        yield adapter, handle
    finally:
        await adapter.destroy(handle)


async def _execute(
    adapter: DockerExecutionEnvironment,
    handle: EnvironmentHandle,
    script: str,
    *,
    timeout: int = 8,
    maximum_output: int = 256 * 1024,
) -> ExecutionResult:
    return await adapter.execute(
        handle,
        ExecutionCommand(
            ("python", "-c", script),
            PurePosixPath("."),
            timeout,
            None,
            maximum_output,
        ),
    )


async def test_no_credential_reaches(
    runtime: tuple[DockerExecutionEnvironment, str],
) -> None:
    secrets = {
        "OPENAI_API_KEY": "synthetic-provider-value-7d951",
        "AGENT_DATABASE_URL": "synthetic-database-value-0be44",
        "AWS_SECRET_ACCESS_KEY": "synthetic-cloud-value-18c12",
        "PRIVATE_SERVICE_TOKEN": "synthetic-pattern-value-19d42",
    }
    script = r"""
import json,os,pathlib
observed={'environment':dict(os.environ)}
for candidate in ('/proc/1/environ','/etc/hostname','/etc/resolv.conf'):
  try: observed[candidate]=pathlib.Path(candidate).read_bytes().decode('utf-8','replace')
  except OSError as exc: observed[candidate]=type(exc).__name__
print(json.dumps(observed,sort_keys=True))
"""
    async with _environment(runtime, run_id=100, parent=secrets) as (adapter, handle):
        result = await _execute(adapter, handle, script)
    rendered = result.stdout.decode("utf-8", errors="replace")
    assert result.exit_code == 0
    assert not set(secrets) & set(json.loads(rendered)["environment"])
    assert all(value not in rendered for value in secrets.values())
    assert all(name not in json.loads(rendered)["environment"] for name in TIER_ZERO_NAMES)


async def test_network_denied(runtime: tuple[DockerExecutionEnvironment, str]) -> None:
    script = r"""
import json,socket
out={}
targets={'public':('1.1.1.1',53),
         'dns':('example.com',80),
         'metadata':('169.254.169.254',80)}
for name,target in targets.items():
  try:
    s=socket.create_connection(target,0.5); s.close(); out[name]=True
  except OSError: out[name]=False
print(json.dumps(out,sort_keys=True))
"""
    async with _environment(runtime, run_id=101) as (adapter, handle):
        result = await _execute(adapter, handle, script)
        logs = await adapter.egress_log(handle)
    assert json.loads(result.stdout) == {"dns": False, "metadata": False, "public": False}
    assert logs == ()


async def test_programmatic_bridge_runs_inside_sandbox(
    runtime: tuple[DockerExecutionEnvironment, str],
) -> None:
    observed: list[tuple[str, dict[str, object], str]] = []

    async def dispatch(call: str, arguments: dict[str, object], call_id: str) -> dict[str, object]:
        observed.append((call, arguments, call_id))
        return {"status": "succeeded", "result": {"value": 42}}

    source = r"""
import json,os,socket
client=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
client.connect(os.environ['AGENT_TOOL_BRIDGE_SOCKET'])
request={'call':'math.calculate','arguments':{'expression':'6*7'},'ordinal':0}
client.sendall(json.dumps(request).encode()+b'\n')
print(client.makefile('rb').readline().decode().strip())
"""
    script_hash = hashlib.sha256(source.encode()).hexdigest()
    session = ProgrammaticBridgeSession(
        script_hash=script_hash,
        token="test",
        dispatch=dispatch,
    )
    endpoint = BridgeEndpoint(PurePosixPath("/workspace/.agent/test-bridge.sock"), session.token)
    async with _environment(runtime, run_id=103) as (adapter, handle):
        result = await adapter.execute_with_bridge(
            handle,
            ExecutionCommand(("python", "-c", source), PurePosixPath("."), 8, None, 4096),
            endpoint,
            session,
        )
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "status": "succeeded",
        "result": {"value": 42},
    }
    assert [(name, arguments) for name, arguments, _call_id in observed] == [
        ("math.calculate", {"expression": "6*7"})
    ]


async def test_egress_allowlisted(runtime: tuple[DockerExecutionEnvironment, str]) -> None:
    policy = EgressPolicy(
        EgressMode.ALLOWLIST,
        (
            EgressDestination("example.com", frozenset({80})),
            EgressDestination("localtest.me", frozenset({80})),
        ),
    )
    script = r"""
import json,socket,urllib.error,urllib.request
out={}
for name,url in {
 'allowed':'http://example.com:80',
 'other':'http://example.org:80',
 'private':'http://169.254.169.254:80',
 'private_name':'http://localtest.me:80',
 'port':'http://example.com:81',
}.items():
  try: out[name]=urllib.request.urlopen(url,timeout=5).status
  except Exception as exc: out[name]=type(exc).__name__
try:
  raw=socket.create_connection(('example.com',80),0.5); raw.close(); out['unproxied']=True
except OSError: out['unproxied']=False
print(json.dumps(out,sort_keys=True))
"""
    async with _environment(runtime, run_id=102, egress=policy) as (adapter, handle):
        await asyncio.sleep(0.25)
        result = await _execute(adapter, handle, script, timeout=12)
        logs = await adapter.egress_log(handle)
    outcomes = json.loads(result.stdout)
    assert outcomes["allowed"] == 200
    assert outcomes["unproxied"] is False
    assert all(outcomes[name] != 200 for name in ("other", "private", "private_name", "port"))
    reasons = {(entry.get("host"), entry.get("port")): entry.get("reason") for entry in logs}
    assert reasons[("example.com", 80)] == "allowed"
    assert reasons[("example.org", 80)] == "destination_miss"
    assert reasons[("169.254.169.254", 80)] == "destination_miss"
    assert reasons[("localtest.me", 80)] == "private_address"
    assert reasons[("example.com", 81)] == "port_miss"


async def test_limits_enforced(runtime: tuple[DockerExecutionEnvironment, str]) -> None:
    cases = (
        (
            _limits(wall_clock_seconds=2),
            "import time; print('partial',flush=True); time.sleep(30)",
            KillReason.TIMEOUT,
            2,
        ),
        (
            _limits(memory_bytes=64 * 1024 * 1024),
            (
                "print('partial',flush=True); x=bytearray(256*1024*1024); "
                "[x.__setitem__(i,1) for i in range(0,len(x),4096)]; print(len(x))"
            ),
            KillReason.MEMORY,
            8,
        ),
        (
            _limits(pids_max=16),
            (
                "import os,time; print('partial',flush=True); "
                "[(os.fork(),time.sleep(.01)) for _ in range(100)]"
            ),
            KillReason.PIDS,
            8,
        ),
        (
            _limits(workspace_bytes=4096),
            "print('partial',flush=True); open('full','wb').write(b'x'*8192)",
            KillReason.DISK,
            8,
        ),
        (
            _limits(inodes_max=5),
            "print('partial',flush=True); [open(f'f{i}','w').close() for i in range(10)]",
            KillReason.DISK,
            8,
        ),
    )
    for index, (limits, script, reason, timeout) in enumerate(cases, 110):
        async with _environment(runtime, run_id=index, limits=limits) as (adapter, handle):
            result = await _execute(adapter, handle, script, timeout=timeout)
        assert result.killed_by is reason
        assert b"partial" in result.stdout
        if reason is KillReason.TIMEOUT:
            assert result.timed_out is True
    async with _environment(runtime, run_id=119) as (adapter, handle):
        result = await _execute(
            adapter,
            handle,
            (
                "import sys,time; print('x'*3000,flush=True); "
                "print('y'*3000,file=sys.stderr,flush=True); time.sleep(5)"
            ),
            maximum_output=4096,
        )
        follow_up = await _execute(adapter, handle, "print('restarted')")
        cancelled = asyncio.create_task(
            _execute(
                adapter,
                handle,
                (
                    "import os,pathlib,time; "
                    "pathlib.Path('cancel.pid').write_text(str(os.getpid())); time.sleep(30)"
                ),
                timeout=12,
            )
        )
        workspace = adapter.workspace(handle)
        for _attempt in range(50):
            try:
                cancelled_pid = int((await workspace.read("cancel.pid")).decode())
                break
            except FileNotFoundError:
                await asyncio.sleep(0.05)
        else:
            raise AssertionError("cancelled command did not start")
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        after_cancel = await _execute(
            adapter,
            handle,
            f"import pathlib; print(pathlib.Path('/proc/{cancelled_pid}').exists())",
        )
    assert result.killed_by is KillReason.OUTPUT_LIMIT
    assert len(result.stdout) + len(result.stderr) <= 4096
    assert result.stdout_truncated or result.stderr_truncated
    assert follow_up.exit_code == 0
    assert after_cancel.exit_code == 0
    assert after_cancel.stdout.strip() == b"False"


async def test_escape_denied(runtime: tuple[DockerExecutionEnvironment, str]) -> None:
    script = r"""
import json,os,pathlib
status=pathlib.Path('/proc/self/status').read_text()
cap=[line for line in status.splitlines() if line.startswith('CapEff:')][0].split()[1]
checks={'docker_socket':pathlib.Path('/var/run/docker.sock').exists(),
        'host_pid':pathlib.Path('/proc/2').exists(),
        'capabilities':cap,
        'module_write':None,
        'proc_mem':None}
for key,path in [('module_write','/sys/module'),('proc_mem','/proc/1/mem')]:
  try:
    with open(path,'wb') as target: target.write(b'x')
    checks[key]=True
  except OSError: checks[key]=False
print(json.dumps(checks,sort_keys=True))
"""
    async with _environment(runtime, run_id=120) as (adapter, handle):
        result = await _execute(adapter, handle, script)
    checks = json.loads(result.stdout)
    assert checks == {
        "capabilities": "0000000000000000",
        "docker_socket": False,
        "host_pid": False,
        "module_write": False,
        "proc_mem": False,
    }


async def test_workspace_isolated(runtime: tuple[DockerExecutionEnvironment, str]) -> None:
    adapter, digest = runtime

    async def provision(tenant: str, run: int) -> EnvironmentHandle:
        return await adapter.provision(
            EnvironmentSpec(
                tenant,
                UUID(int=run),
                1,
                digest,
                _limits(),
                EgressPolicy(),
                build_sandbox_environment({}),
            )
        )

    for first_tenant, second_tenant, offset in (
        ("tenant-a", "tenant-b", 130),
        ("tenant-a", "tenant-a", 140),
    ):
        first, second = await asyncio.gather(
            provision(first_tenant, offset), provision(second_tenant, offset + 1)
        )
        try:
            await adapter.workspace(first).write("secret.txt", b"tenant-private")
            result = await _execute(
                adapter,
                second,
                (
                    "import json,pathlib; "
                    "print(json.dumps({'exists':pathlib.Path('/workspace/secret.txt').exists(),"
                    "'roots':[str(p) for p in "
                    "pathlib.Path('/proc').glob('*/root/workspace/secret.txt')]}))"
                ),
            )
            observed = json.loads(result.stdout)
            assert observed == {"exists": False, "roots": []}
        finally:
            await asyncio.gather(adapter.destroy(first), adapter.destroy(second))


async def test_no_orphans(runtime: tuple[DockerExecutionEnvironment, str]) -> None:
    adapter, digest = runtime
    run_id = UUID(int=150)
    first = await adapter.provision(
        EnvironmentSpec(
            "tenant-a",
            run_id,
            1,
            digest,
            _limits(),
            EgressPolicy(),
            build_sandbox_environment({}),
        )
    )
    second = await adapter.provision(
        EnvironmentSpec(
            "tenant-a",
            run_id,
            2,
            digest,
            _limits(),
            EgressPolicy(),
            build_sandbox_environment({}),
        )
    )
    assert await adapter.reap(frozenset({(run_id, 2)})) == 1
    assert first.environment_id not in adapter.live_environment_ids()
    assert second.environment_id in adapter.live_environment_ids()
    assert await adapter.reap(frozenset()) == 1
    assert adapter.live_environment_ids() == frozenset()

    orphan = await adapter.provision(
        EnvironmentSpec(
            "tenant-a",
            UUID(int=151),
            3,
            digest,
            _limits(),
            EgressPolicy(),
            build_sandbox_environment({}),
        )
    )
    adapter._states.pop(orphan.environment_id)
    replacement_process = DockerExecutionEnvironment(
        SystemClock(), RandomIdFactory(), hard_cap_seconds=20, reaper_grace_seconds=0
    )
    assert await replacement_process.reap(frozenset()) == 1
