"""Content-free model span attributes."""

from __future__ import annotations

from agent_core.domain.messages import ProviderMetadata


def span_provider_attributes(metadata: ProviderMetadata | None) -> dict[str, object]:
    """Return the second and only non-persistence view of provider metadata."""

    if metadata is None:
        return {}
    return {
        f"model.provider.{key}": value
        for key, value in metadata.model_dump(mode="python", exclude_none=True).items()
    }
