"""Milestone 8 skill substrate hard gates."""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Sequence
from pathlib import Path
from uuid import UUID

import pytest
import yaml
import zstandard
from hypothesis import given, settings
from hypothesis import strategies as st

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.mcp.scripted import ScriptedMCPClientFactory
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.adapters.skills.memory import InMemorySkillRepository
from agent_core.adapters.skills.stores import InMemorySkillPackageStore
from agent_core.application.session_service import SessionService
from agent_core.bootstrap import _memory_uow_repositories, build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.rendering import build_prefix, envelope_items
from agent_core.domain.errors import ConflictError, NotFoundError, SkillValidationError
from agent_core.domain.mcp import (
    MCPDiscovery,
    MCPRemotePrompt,
    MCPServerConfig,
    MCPTransport,
    ScriptedMCPServer,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    TextPart,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.skills import (
    CatalogEntry,
    LoadedSkillBody,
    SkillManifest,
    SkillPackage,
    SkillPackageMember,
    SkillRef,
    SkillRevision,
    SkillSource,
)
from agent_core.skills.catalog import SkillCatalogService
from agent_core.skills.package import (
    MAX_PACKAGE_BYTES,
    SkillPackageValidator,
    package_from_directory,
    read_archive_member,
)
from agent_core.tools.skill_load import LegacySkillLoadTool, SkillLoadTool
from tests.contract.support import NOW, agent, memory_stack, principal

ROOT = Path(__file__).resolve().parents[2]


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/skill-m8",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
    )


def _package(
    name: str,
    body: str,
    *,
    version: str = "1.0.0",
    description: str = "Skill.",
    required_tools: tuple[str, ...] = (),
    extra_members: tuple[SkillPackageMember, ...] = (),
) -> SkillPackage:
    metadata = yaml.safe_dump(
        {
            "name": name,
            "version": version,
            "description": description,
            "required_tools": list(required_tools),
        },
        sort_keys=False,
    )
    markdown = f"---\n{metadata}---\n{body}".encode()
    return SkillPackage(
        directory_name=name,
        members=(SkillPackageMember(path="SKILL.md", data=markdown), *extra_members),
    )


async def _catalog_stack(
    packages: list[SkillPackage],
    *,
    mcp_entries: list[CatalogEntry] | None = None,
    maximum_entries: int = 20,
    maximum_loaded: int = 2,
    maximum_body_tokens: int = 6_000,
) -> tuple[
    SkillCatalogService,
    InMemorySkillRepository,
    InMemorySkillPackageStore,
    MemoryUnitOfWorkFactory,
]:
    clock, sessions, runs, events = await memory_stack()
    store = InMemorySkillPackageStore()
    repository = InMemorySkillRepository(
        store,
        SkillPackageValidator(ConservativeTokenEstimator()),
        clock,
        SequenceIdFactory(),
    )
    for package in packages:
        await repository.install("tenant-a", package, SkillSource.OPERATOR, None, None)
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            skills=repository,
            clock=clock,
        )
    )

    async def prompts(_session_id: UUID, _principal: object) -> list[CatalogEntry]:
        return list(mcp_entries or [])

    catalogs = SkillCatalogService(
        factory,
        store,
        ConservativeTokenEstimator(),
        mcp_prompts=prompts,
        maximum_entries=maximum_entries,
        maximum_loaded=maximum_loaded,
        maximum_body_tokens=maximum_body_tokens,
    )
    return catalogs, repository, store, factory


def _text(items: Sequence[object]) -> str:
    return "\n".join(
        part.text
        for item in items
        for part in getattr(item, "content", [])
        if isinstance(part, TextPart)
    )


async def test_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    packages = [
        _package(f"s{index}", f"BODY_SENTINEL_{index}", description=f"D{index}")
        for index in range(5)
    ]
    catalogs, _repository, store, _factory = await _catalog_stack(packages)
    archive_reads = 0
    original_archive_bytes = store.archive_bytes

    async def counted_archive_bytes(key: str) -> bytes:
        nonlocal archive_reads
        archive_reads += 1
        return await original_archive_bytes(key)

    monkeypatch.setattr(store, "archive_bytes", counted_archive_bytes)
    configured = agent().model_copy(
        update={"enabled_skills": [f"s{index}@1" for index in range(5)]}
    )
    catalog = await catalogs.open(UUID(int=501), configured, principal())
    assert archive_reads == 0
    prefix = build_prefix(configured, [], [entry.metadata for entry in catalog.entries])
    prefix_text = _text(prefix)
    assert all(f"D{index}" in prefix_text for index in range(5))
    assert all(f"BODY_SENTINEL_{index}" not in prefix_text for index in range(5))

    body, _missing = await catalogs.load(UUID(int=501), principal(), "s0", None, (), frozenset())
    assert archive_reads == 1
    rendered_body = _text(
        envelope_items(
            [
                UserMessage(
                    content=[TextPart(text=body.content)],
                    trust=body.trust,
                )
            ]
        )
    )
    assert "BODY_SENTINEL_0" in rendered_body
    assert "BODY_SENTINEL_0" not in prefix_text


async def test_evicted_catalog_reopens_from_recorded_session_pins() -> None:
    async with build(
        settings=_settings(),
        sequential_ids=True,
        enabled_skills=["stable@1"],
        skill_packages=((_package("stable", "recorded body"), SkillSource.OPERATOR),),
    ) as composition:
        composition.skill_catalogs._cache_capacity = 1
        first_session = await composition.sessions.create()
        await composition.sessions.create()
        with pytest.raises(NotFoundError, match="not open"):
            composition.skill_catalogs.current(first_session)
        body, _missing = await composition.skill_catalogs.load(
            first_session,
            composition.principal,
            "stable",
            None,
            (),
            frozenset(),
        )
    assert body.content == "recorded body"


async def test_catalog_pinned() -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="one"), ScriptedTurn(text="two")])
    async with build(
        settings=_settings(),
        script=script,
        sequential_ids=True,
        enabled_skills=["base@1"],
        skill_packages=((_package("base", "original"), SkillSource.OPERATOR),),
    ) as composition:
        session_id = await composition.sessions.create()
        original = composition.skill_catalogs.current(session_id)
        async with composition.uow_factory() as uow:
            await uow.skills.install(
                composition.principal.tenant_id,
                _package("new", "new body"),
                SkillSource.OPERATOR,
                None,
                None,
            )
            await uow.skills.archive(composition.principal.tenant_id, "base", 1)
        assert composition.skill_catalogs.current(session_id) == original
        with pytest.raises(NotFoundError, match="pinned catalog"):
            await composition.skill_catalogs.load(
                session_id,
                composition.principal,
                "new",
                None,
                (),
                frozenset(),
            )
        loaded, _missing = await composition.skill_catalogs.load(
            session_id,
            composition.principal,
            "base",
            None,
            (),
            frozenset(),
        )
        assert loaded.content == "original"
        first = await composition.runs.submit("first", session_id)
        await composition.runs.wait_terminal(first)
        second = await composition.runs.submit("second", session_id)
        await composition.runs.wait_terminal(second)
        async with composition.uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, composition.principal)
    hashes = [
        event.payload["prefix_sha256"]
        for event in events
        if event.event_type == "model.request.started"
    ]
    assert len(hashes) == 2
    assert len(set(hashes)) == 1


async def test_no_tool_from_skill() -> None:
    for path in (ROOT / "src" / "agent_core" / "skills").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"register", "register_dynamic"}
        ]
    package = _package(
        "manifest",
        "Do not register anything.",
        extra_members=(SkillPackageMember(path="tools/manifest.json", data=b"{}"),),
    )
    script = FakeModelScript(turns=[ScriptedTurn(text="done")])
    async with build(
        settings=_settings(),
        script=script,
        skill_packages=((package, SkillSource.OPERATOR),),
        enabled_skills=["manifest"],
        enabled_tools=["skill.load"],
    ) as composition:
        session_id = await composition.sessions.create()
        run_id = await composition.runs.submit("inspect", session_id)
        await composition.runs.wait_terminal(run_id)
        async with composition.uow_factory() as uow:
            plan_event = await uow.events.latest_before(
                session_id, (1 << 63) - 1, "context.plan.created", composition.principal
            )
    assert plan_event is not None
    assert plan_event.payload["plan"]["tool_names"] == ["skill.load"]


def test_untrusted_body() -> None:
    entries = [
        CatalogEntry(
            manifest=SkillManifest(
                name=name,
                version="1.0.0",
                description="Remote procedure.",
            ),
            revision=revision,
            content_sha256=hashlib.sha256(body.encode()).hexdigest(),
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
            source=source,
            ephemeral_body=body if source is SkillSource.MCP else None,
        )
        for name, revision, body, source in (
            ("agent-body", 1, "AGENT_UNTRUSTED", SkillSource.AGENT),
            ("mcp-body", 0, "MCP_UNTRUSTED", SkillSource.MCP),
        )
    ]
    rendered = _text(build_prefix(agent(), [], [entry.metadata for entry in entries]))
    assert rendered.count('trust="external_untrusted"') == 2
    assert "AGENT_UNTRUSTED" not in rendered
    assert "MCP_UNTRUSTED" not in rendered
    body_items = [
        UserMessage(
            content=[TextPart(text=entry.ephemeral_body or "AGENT_UNTRUSTED")],
            trust=entry.trust,
        )
        for entry in entries
    ]
    body_text = _text(envelope_items(body_items))
    assert body_text.count('trust="external_untrusted"') == 2


async def test_revision_pinned() -> None:
    catalogs, repository, store, _factory = await _catalog_stack(
        [_package("stable", "revision one")]
    )
    configured = agent().model_copy(update={"enabled_skills": ["stable@1"]})
    session_id = UUID(int=502)
    pinned = await catalogs.open(session_id, configured, principal())
    await repository.install(
        "tenant-a",
        _package("stable", "revision two", version="1.1.0"),
        SkillSource.OPERATOR,
        1,
        None,
    )
    await repository.install(
        "tenant-a",
        _package("stable", "revision three", version="1.2.0"),
        SkillSource.OPERATOR,
        2,
        None,
    )
    loaded, _missing = await catalogs.load(session_id, principal(), "stable", None, (), frozenset())
    assert loaded.content == "revision one"
    assert loaded.content_sha256 == pinned.entries[0].content_sha256
    key = pinned.entries[0].package_key
    assert key is not None
    store._objects[key] = b"corrupted"
    with pytest.raises(ConflictError, match="archive hash"):
        await catalogs.load(session_id, principal(), "stable", "SKILL.md", (), frozenset())


async def test_missing_tool_loads() -> None:
    package = _package(
        "needs",
        "Use the available tools.",
        required_tools=("math.calculate", "system.current_time", "absent.tool"),
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="skill.load", arguments={"name": "needs"})]
            ),
            ScriptedTurn(tool_calls=[ScriptedToolCall(name="absent.tool", arguments={})]),
            ScriptedTurn(text="continued"),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        skill_packages=((package, SkillSource.OPERATOR),),
        enabled_skills=["needs"],
        enabled_tools=["skill.load", "math.calculate", "system.current_time"],
    ) as composition:
        run_id = await composition.runs.submit("load then call")
        completed = await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)
        async with composition.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
    assert completed.final_message == "continued"
    assert checkpoint is not None and checkpoint.loaded_skills[0].name == "needs"
    missing_result = next(
        event
        for event in events
        if event.event_type == "tool.call.completed" and event.payload.get("name") == "skill.load"
    )
    assert "absent.tool" in str(missing_result.payload)
    loaded_invocation = next(item for item in invocations if item.tool_name == "skill.load")
    assert loaded_invocation.structured_result is not None
    assert loaded_invocation.structured_result["skill_update"]["notes"] == ["skill.tool.missing"]
    denied = next(event for event in events if event.event_type == "tool.call.denied")
    assert denied.payload["reason_code"] == "policy.matrix.unknown_tool"


def test_skill_load_model_contract_prohibits_guessed_names() -> None:
    assert SkillLoadTool.spec.description == (
        "Load or unload an exact skill name listed in Available skill metadata; "
        "never guess names or use this tool to discover capabilities."
    )
    assert SkillLoadTool.spec.input_schema["properties"]["name"]["description"] == (
        "Exact name from Available skill metadata."
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"name": "web-research"},
        {"name": "web-research", "unload": True},
    ],
)
async def test_missing_skill_load_lists_the_pinned_catalog_for_recovery(
    arguments: dict[str, object],
) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(tool_calls=[ScriptedToolCall(name="skill.load", arguments=arguments)]),
            ScriptedTurn(text="I will use a declared capability directly."),
        ]
    )
    async with build(
        settings=_settings(),
        script=script,
        skill_packages=((_package("available", "Use declared tools."), SkillSource.OPERATOR),),
        enabled_skills=["available"],
        enabled_tools=["skill.load"],
    ) as composition:
        run_id = await composition.runs.submit("Research a current topic.")
        await composition.runs.wait_terminal(run_id)
        events = await composition.runs.events(run_id)

    failed = next(event for event in events if event.event_type == "tool.call.failed")
    assert failed.payload["reason_code"] == "tool.skill.not_in_catalog"
    content = failed.payload["result_item"]["content"]
    assert "Choose only a name from the attached available-skill data" in content[0]["text"]
    assert content[1]["text"] == "Available skill names: available"
    assert '"remediation":"modify_arguments"' in content[0]["text"]


async def test_skill_load_survives_builtin_minor_upgrade() -> None:
    """A session pinned to skill.load@1.0.0 keeps the capability after the 1.1.0 bump.

    A session keeps the exact tool version it was shown, so the registry must
    retain compatible builtin history exactly as it does for memory.remember.
    """

    script = FakeModelScript(turns=[ScriptedTurn(text="ready")])
    async with build(
        settings=_settings(),
        script=script,
        skill_packages=((_package("available", "Use declared tools."), SkillSource.OPERATOR),),
        enabled_skills=["available"],
        enabled_tools=["skill.load"],
    ) as composition:
        registry = composition.tool_pipeline._registry
        current = registry.get("skill.load")
        legacy = registry.get("skill.load", "1.0.0")

    assert current.spec.version == "1.1.0"
    assert legacy.spec.version == "1.0.0"
    assert legacy.spec == LegacySkillLoadTool.spec
    assert (
        legacy.spec.description == "Load or unload content from the session-pinned skill catalog."
    )
    assert legacy.spec.input_schema["properties"]["name"] == {
        "type": "string",
        "minLength": 1,
        "maxLength": 64,
    }


async def test_catalog_capped() -> None:
    packages = [_package(f"s{index:02d}", f"body {index}") for index in range(40)]
    remote = [
        CatalogEntry(
            manifest=SkillManifest(
                name=f"mcp-prompt-{index}",
                version="0.0.0+mcp",
                description="P",
            ),
            revision=0,
            content_sha256=hashlib.sha256(f"prompt {index}".encode()).hexdigest(),
            trust=TrustLevel.EXTERNAL_UNTRUSTED,
            source=SkillSource.MCP,
            ephemeral_body=f"prompt {index}",
        )
        for index in range(2)
    ]
    catalogs, _repository, _store, factory = await _catalog_stack(
        packages, mcp_entries=remote, maximum_entries=20
    )
    configured = agent().model_copy(
        update={"enabled_skills": [f"s{index:02d}" for index in range(40)]}
    )
    services = SessionService(
        factory,
        FixedClock(NOW),
        SequenceIdFactory(),
        principal(),
        configured,
        catalogs=catalogs,
    )
    session_id = await services.create()
    first = catalogs.current(session_id)
    second = await catalogs.open(session_id, configured, principal())
    assert first.model_dump_json() == second.model_dump_json()
    assert len(first.entries) == 20
    assert [entry.manifest.name for entry in first.entries] == [
        f"s{index:02d}" for index in range(20)
    ]
    async with factory() as uow:
        events = await uow.events.list_after(session_id, 0, principal())
    opened = next(event for event in events if event.event_type == "session.created")
    assert opened.payload["dropped_skills"] == [
        *[f"s{index:02d}" for index in range(20, 40)],
        "mcp-prompt-0",
        "mcp-prompt-1",
    ]


async def test_catalog_deduplicates_and_caps_refs_before_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalogs, repository, _store, _factory = await _catalog_stack(
        [_package("one", "one"), _package("two", "two")],
        maximum_entries=1,
    )
    resolved: list[str] = []
    original_resolve = repository.resolve

    async def counted_resolve(tenant_id: str, ref: SkillRef) -> SkillRevision:
        resolved.append(str(ref))
        return await original_resolve(tenant_id, ref)

    monkeypatch.setattr(repository, "resolve", counted_resolve)
    configured = agent().model_copy(update={"enabled_skills": ["one", "one@1", "two"]})

    catalog = await catalogs.open(UUID(int=504), configured, principal())

    assert [entry.manifest.name for entry in catalog.entries] == ["one"]
    assert catalog.dropped_names == ("one", "two")
    assert resolved == ["one"]


@settings(max_examples=100)
@given(
    directory=st.text(min_size=0, max_size=70),
    name=st.text(min_size=0, max_size=70),
    version=st.text(min_size=0, max_size=20),
    description=st.text(min_size=0, max_size=520),
    tools=st.lists(st.text(min_size=0, max_size=30), max_size=12),
    body=st.text(min_size=0, max_size=12_000),
    member_path=st.sampled_from(("note.txt", "../escape", "/absolute", "a/./b")),
    symlink=st.booleans(),
)
def test_validation_total(
    directory: str,
    name: str,
    version: str,
    description: str,
    tools: list[str],
    body: str,
    member_path: str,
    symlink: bool,
) -> None:
    metadata = yaml.safe_dump(
        {
            "name": name,
            "version": version,
            "description": description,
            "required_tools": tools,
        },
        sort_keys=False,
    )
    package = SkillPackage(
        directory_name=directory,
        members=(
            SkillPackageMember(
                path="SKILL.md",
                data=f"---\n{metadata}---\n{body}".encode(),
            ),
            SkillPackageMember(
                path=member_path,
                data=b"member",
                kind="symlink" if symlink else "file",
            ),
        ),
    )
    validator = SkillPackageValidator(ConservativeTokenEstimator())
    try:
        validated = validator.validate(package)
    except SkillValidationError as exc:
        assert exc.rule
    else:
        assert validated.manifest.name == directory
        assert hashlib.sha256(validated.archive).hexdigest() == validated.content_sha256


def test_package_directory_count_applies_only_to_members(tmp_path: Path) -> None:
    root = tmp_path / "directory-count"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: directory-count\nversion: 1.0.0\n"
        "description: Directory count.\nrequired_tools: []\n---\nbody",
        encoding="utf-8",
    )
    for index in range(63):
        (root / f"member-{index}.txt").write_text("member", encoding="utf-8")
    for index in range(100):
        (root / f"empty-{index}").mkdir()

    package = package_from_directory(root)

    assert len(package.members) == 64


def test_archive_declared_size_is_rejected_before_decompression() -> None:
    oversized = zstandard.ZstdCompressor().compress(b"x" * (MAX_PACKAGE_BYTES * 4 + 1))

    with pytest.raises(SkillValidationError, match="expanded archive"):
        read_archive_member(oversized, "SKILL.md")


async def test_body_cap() -> None:
    catalogs, _repository, _store, _factory = await _catalog_stack(
        [_package(name, name) for name in ("one", "two", "three")]
    )
    configured = agent().model_copy(update={"enabled_skills": ["one", "two", "three"]})
    session_id = UUID(int=503)
    await catalogs.open(session_id, configured, principal())
    loaded: tuple[LoadedSkillBody, ...] = ()
    for name in ("one", "two"):
        body, _missing = await catalogs.load(
            session_id, principal(), name, None, loaded, frozenset()
        )
        loaded = (*loaded, body)
    with pytest.raises(ConflictError, match="one, two"):
        await catalogs.load(session_id, principal(), "three", None, loaded, frozenset())
    loaded = tuple(body for body in loaded if body.name != "one")
    replacement, _missing = await catalogs.load(
        session_id, principal(), "three", None, loaded, frozenset()
    )
    assert replacement.name == "three"
    loaded = (*loaded, replacement)
    assert len(loaded) <= 2
    assert sum(body.tokens for body in loaded) <= catalogs.maximum_body_tokens

    estimator = ConservativeTokenEstimator()
    token_limit = sum(estimator.estimate_text(name, "skill-validation") for name in ("one", "two"))
    token_catalogs, _repository, _store, _factory = await _catalog_stack(
        [_package(name, name) for name in ("one", "two", "three")],
        maximum_loaded=3,
        maximum_body_tokens=token_limit,
    )
    token_session_id = UUID(int=505)
    await token_catalogs.open(token_session_id, configured, principal())
    token_loaded: tuple[LoadedSkillBody, ...] = ()
    for name in ("one", "two"):
        body, _missing = await token_catalogs.load(
            token_session_id,
            principal(),
            name,
            None,
            token_loaded,
            frozenset(),
        )
        token_loaded = (*token_loaded, body)
    with pytest.raises(ConflictError, match="token cap"):
        await token_catalogs.load(
            token_session_id,
            principal(),
            "three",
            None,
            token_loaded,
            frozenset(),
        )


async def test_mcp_read_only() -> None:
    catalogs, repository, _store, _factory = await _catalog_stack([])
    del catalogs
    with pytest.raises(ConflictError, match="read-only"):
        await repository.install(
            "tenant-a", _package("remote", "body"), SkillSource.MCP, None, None
        )
    prompt_body = "REMOTE_PROMPT_BYTES"
    server = MCPServerConfig(
        tenant_id="local",
        server_id="prompts",
        transport=MCPTransport.STDIO,
        endpoint="/fixture/prompts",
        operator_configured=True,
    )
    scripted = ScriptedMCPServer(
        name="prompts",
        discovery=MCPDiscovery(prompts=(MCPRemotePrompt(name="review", body=prompt_body),)),
    )
    async with build(
        settings=_settings(),
        mcp_servers=(server,),
        mcp_client_factory=ScriptedMCPClientFactory({"prompts": scripted}),
        sequential_ids=True,
    ) as composition:
        session_id = await composition.sessions.create()
        catalog = composition.skill_catalogs.current(session_id)
        entry = next(item for item in catalog.entries if item.source is SkillSource.MCP)
        async with composition.uow_factory() as uow:
            rows = await uow.skills.list_active("local", 100)
    assert rows == []
    assert entry.revision == 0
    assert entry.content_sha256 == hashlib.sha256(prompt_body.encode()).hexdigest()
