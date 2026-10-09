"""The isolated launcher uses Veetbot's credential, with fabricated secrets only."""

import io
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from scripts import review_owner_memory as launcher
from tests.unit.test_owner_memory_fixture import packet


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VEETBOT_OPENAI_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)


@pytest.mark.parametrize(
    "canonical,alias,expected",
    [
        ("configured-key", None, "configured-key"),
        ("configured-key", "invalid-alias", "configured-key"),
        (None, "compatibility-key", "compatibility-key"),
        ("  ", "compatibility-key", "compatibility-key"),
        (" configured-key ", "invalid-alias", "configured-key"),
    ],
)
def test_launcher_uses_canonical_veetbot_credential(
    monkeypatch: pytest.MonkeyPatch,
    canonical: str | None,
    alias: str | None,
    expected: str,
) -> None:
    value = packet()
    value = value.model_copy(
        update={
            "model": value.model.model_copy(update={"provider": "openai"}),
            "residency_provider": "openai",
        }
    )
    monkeypatch.delenv("VEETBOT_OPENAI_KEY", raising=False)
    if canonical:
        monkeypatch.setenv("VEETBOT_OPENAI_KEY", canonical)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    if alias:
        monkeypatch.setenv("OPENAI_API_KEY", alias)
    monkeypatch.setattr("sys.argv", ["review_owner_memory.py"])
    monkeypatch.setattr(
        "sys.stdin", SimpleNamespace(buffer=io.BytesIO(value.model_dump_json().encode()))
    )

    def access(url: str, **kwargs: Any) -> httpx.Response:
        assert url == "https://api.openai.com/v1/models/test-model"
        assert kwargs["headers"] == {"Authorization": "Bearer " + expected}
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", access)

    def provider(**kwargs: Any) -> Any:
        assert kwargs["api_key"] == expected, (
            "canonical credential must override the compatibility alias"
        )
        raise SystemExit(0)

    monkeypatch.setattr(launcher, "OpenAIResponsesProvider", provider)
    with pytest.raises(SystemExit) as exc:
        launcher.main()
    assert exc.value.code == 0


@pytest.mark.parametrize("status", [401, 404, 503, "timeout"])
def test_unusable_credential_refuses_before_review_or_budget(
    monkeypatch: pytest.MonkeyPatch,
    status: int | str,
) -> None:
    value = packet()
    value = value.model_copy(
        update={
            "model": value.model.model_copy(update={"provider": "openai"}),
            "residency_provider": "openai",
        }
    )
    monkeypatch.setenv("VEETBOT_OPENAI_KEY", "fake-key")
    monkeypatch.setattr("sys.argv", ["review_owner_memory.py"])
    monkeypatch.setattr(
        "sys.stdin", SimpleNamespace(buffer=io.BytesIO(value.model_dump_json().encode()))
    )

    def refused(url: str, **kwargs: Any) -> httpx.Response:
        if status == "timeout":
            raise httpx.ReadTimeout("PRIVATE_SENTINEL")
        assert isinstance(status, int)
        return httpx.Response(
            status,
            request=httpx.Request("GET", url),
            json={"error": {"message": "PRIVATE_SENTINEL"}},
        )

    def forbidden(**kwargs: Any) -> Any:
        raise AssertionError("no review provider may start with unusable credentials")

    monkeypatch.setattr(httpx, "get", refused)
    monkeypatch.setattr(launcher, "OpenAIResponsesProvider", forbidden)
    with pytest.raises(SystemExit) as exc:
        launcher.main()
    assert "model access" in str(exc.value)
    assert "PRIVATE_SENTINEL" not in str(exc.value)
