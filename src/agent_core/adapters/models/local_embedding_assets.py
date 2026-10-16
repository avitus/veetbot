"""Bounded local model reads without importing native inference libraries."""

import hashlib
import os
import stat
from pathlib import Path

from agent_core.domain.embeddings import EmbeddingError
from agent_core.domain.local_embeddings import LocalEmbeddingConfig


def verified_file(path: Path, expected: str, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= limit:
            raise EmbeddingError("local embedding file is not a bounded regular file")
        content = source.read(limit + 1)
    if len(content) > limit or hashlib.sha256(content).hexdigest() != expected:
        raise EmbeddingError("local embedding file verification failed")
    return content


def read_local_assets(root: Path, config: LocalEmbeddingConfig) -> tuple[bytes, bytes]:
    checked = LocalEmbeddingConfig.model_validate(config.model_dump())
    return (
        verified_file(root / "model.onnx", checked.model_sha256, 128 * 1024 * 1024),
        verified_file(root / "tokenizer.json", checked.tokenizer_sha256, 8 * 1024 * 1024),
    )
