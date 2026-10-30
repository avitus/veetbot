"""Offline query/index composition requires the complete local release tuple."""

import json
from pathlib import Path
from typing import Any

import pytest

from agent_core.config import ConfigurationError
from agent_core.memory.semantic_deployment import (
    IndexProfiles,
    active_index_dependencies,
    compose_indexing,
)
from tests.contract.test_local_semantic_preparation import CONFIG, LOCAL
from tests.unit.test_semantic_index_composition import configured_index, release_files


async def local_options(tmp_path: Path) -> tuple[dict[str, Any], list[str], list[str], Path]:
    kwargs, provider, credentials, _, evidence = await configured_index(tmp_path)
    document = {
        "schema_version": 1,
        "configurations": [
            {
                "embedding": CONFIG.model_dump(mode="json"),
                "local": LOCAL.model_dump(mode="json"),
                "model_directory": str(tmp_path / "model"),
                "reader_sha256": "a" * 64,
            }
        ],
    }
    path = kwargs["settings"].config_dir / "memory" / "semantic-index.yaml"
    path.write_text(json.dumps(document))
    kwargs["configuration"].update(
        {"policy_version": "policy@1", "memory": {"retrieval": {"semantic_enabled": True}}}
    )
    profile = IndexProfiles.model_validate(document).configurations[0]
    deps = active_index_dependencies(
        profile,
        configuration=kwargs["configuration"],
        formation=kwargs["formation"],
        reader=kwargs["reader"],
    )
    release_files(tmp_path, deps)
    lifecycle: list[str] = []

    class Local:
        async def embed(self, request: Any) -> Any:
            return await provider.embed(request)

        async def close(self) -> None:
            lifecycle.append("closed")

    def load(path: Path, config: Any) -> Local:
        assert path == tmp_path / "model" and config == LOCAL
        lifecycle.append("loaded")
        return Local()

    kwargs["local_provider_factory"] = load
    return kwargs, credentials, lifecycle, evidence


@pytest.mark.parametrize("change", ["remote", "query_disabled", "unattended"])
async def test_bounded_admission_never_loads_out_of_scope_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from agent_core.memory.bounded_recall_admission import BoundedAdmission

    kwargs, credentials, lifecycle, _ = await local_options(tmp_path)
    monkeypatch.setattr(
        "agent_core.memory.semantic_deployment.bind_index_manifest",
        lambda *args, **kwargs: BoundedAdmission(lambda: True),
    )
    if change == "remote":
        path = kwargs["settings"].config_dir / "memory" / "semantic-index.yaml"
        from tests.contract.semantic_indexing_cases import INDEX_CONFIG

        remote = INDEX_CONFIG.model_copy(
            update={
                "space": INDEX_CONFIG.space.model_copy(update={"provider": "openai"}),
                "residency_provider": "openai",
            }
        )
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "configurations": [
                        {
                            "credential_ref": "openai",
                            "embedding": remote.model_dump(mode="json"),
                        }
                    ],
                }
            )
        )
    elif change == "query_disabled":
        kwargs["configuration"]["memory"]["retrieval"]["semantic_enabled"] = False
    else:
        from dataclasses import replace

        kwargs["settings"] = replace(kwargs["settings"], memory_reconsolidation_enabled=True)
    with pytest.raises(ConfigurationError):
        await compose_indexing(**kwargs)
    assert credentials == [] and lifecycle == []


async def test_admitted_local_tuple_loads_without_credentials_and_closes(tmp_path: Path) -> None:
    kwargs, credentials, lifecycle, evidence = await local_options(tmp_path)
    services = await compose_indexing(**kwargs)
    assert services is not None and services.queries is not None
    assert credentials == [] and lifecycle == ["loaded"]
    evidence.unlink()
    assert (await services.index.run_slice()).reason == "disabled"
    await services.close()
    assert lifecycle == ["loaded", "closed"]


@pytest.mark.parametrize("changed", ["evidence", "reader", "space", "credentials"])
async def test_invalid_local_release_never_loads_model(tmp_path: Path, changed: str) -> None:
    kwargs, credentials, lifecycle, evidence = await local_options(tmp_path)
    if changed == "evidence":
        evidence.write_text("{}")
    else:
        path = kwargs["settings"].config_dir / "memory" / "semantic-index.yaml"
        document = json.loads(path.read_text())
        profile = document["configurations"][0]
        if changed == "reader":
            profile.pop("reader_sha256")
        elif changed == "space":
            profile["embedding"]["space"]["dimensions"] = 6
        else:
            profile["credential_ref"] = "openai"
        path.write_text(json.dumps(document))
    with pytest.raises(ConfigurationError, match="Semantic index"):
        await compose_indexing(**kwargs)
    assert not credentials and not lifecycle


async def test_source_recovery_binds_same_release_and_requires_local_reader(tmp_path: Path) -> None:
    kwargs, credentials, lifecycle, _evidence = await local_options(tmp_path)
    kwargs["configuration"]["memory"]["retrieval"]["source_recovery_enabled"] = True
    path = kwargs["settings"].config_dir / "memory" / "semantic-index.yaml"
    profile = IndexProfiles.model_validate(json.loads(path.read_text())).configurations[0]
    release_files(
        tmp_path,
        active_index_dependencies(
            profile,
            configuration=kwargs["configuration"],
            formation=kwargs["formation"],
            reader=kwargs["reader"],
        ),
    )
    services = await compose_indexing(**kwargs)
    assert services is not None
    assert getattr(services, "source_recovery", None) is not None, (
        "Passing local release must compose its explicitly enabled source channel"
    )
    await services.close()
    assert not credentials
    lifecycle.clear()
    kwargs["configuration"]["memory"]["retrieval"]["semantic_enabled"] = False
    release_files(
        tmp_path,
        active_index_dependencies(
            profile,
            configuration=kwargs["configuration"],
            formation=kwargs["formation"],
            reader=kwargs["reader"],
        ),
    )
    with pytest.raises(ConfigurationError, match="Semantic index"):
        await compose_indexing(**kwargs)
    assert lifecycle == [], "Source recall must not load without runtime reader authority"


@pytest.mark.parametrize("expected", [False, True])
async def test_bootstrap_distinguishes_local_model_unavailability_from_factory_bugs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected: bool,
) -> None:
    import agent_core.bootstrap as bootstrap
    from agent_core.domain.embeddings import EmbeddingError
    from tests.contract.support import principal

    kwargs, credentials, _, _ = await local_options(tmp_path)
    monkeypatch.setattr(
        "agent_core.memory.semantic_deployment.bind_index_manifest",
        lambda *args, **kwargs: lambda: True,
    )

    def unavailable(*args: Any, **options: Any) -> Any:
        if expected:
            raise EmbeddingError("Model unavailable")
        raise RuntimeError("Unexpected factory implementation failure")

    monkeypatch.setattr(bootstrap, "local_embedding_provider", unavailable)

    async def start() -> None:
        async with bootstrap.build(
            settings=kwargs["settings"],
            storage="memory",
            principal=principal(),
            clock=kwargs["clock"],
            credential_resolver=kwargs["credentials"],
        ) as app:
            assert await app.readiness_probe()
            assert app.semantic_services is None
            assert app.executor._prepare_recall is None

    if expected:
        await start()
    else:
        with pytest.raises(RuntimeError, match="Unexpected factory implementation failure"):
            await start()
    assert not credentials
