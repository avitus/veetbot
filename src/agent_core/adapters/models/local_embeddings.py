"""Offline CPU embedding adapter; files are installed and verified before use."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

import numpy as np
import onnxruntime as ort  # type: ignore[import-untyped]
from onnxruntime.capi import (  # type: ignore[import-untyped]
    onnxruntime_pybind11_state as ort_errors,
)
from tokenizers import Tokenizer

from agent_core.adapters.models.local_embedding_assets import read_local_assets
from agent_core.domain.embeddings import EmbeddingError, EmbeddingRequest, EmbeddingResponse
from agent_core.domain.local_embeddings import LocalEmbeddingConfig


class LocalEmbeddingProvider:
    def __init__(self, root: Path, config: LocalEmbeddingConfig) -> None:
        self._config = LocalEmbeddingConfig.model_validate(config.model_dump())
        self._closed = False
        self._work: asyncio.Future[EmbeddingResponse] | None = None
        try:
            model, tokenizer = read_local_assets(root, config)
        except OSError:
            raise EmbeddingError("local embedding assets could not be read") from None
        try:
            self._tokenizer = Tokenizer.from_str(tokenizer.decode("utf-8"))
        except Exception as error:
            # The tokenizer's Rust deserializer exposes malformed JSON as the
            # base Exception type. Do not disguise other Python implementation bugs.
            if type(error) is not Exception and not isinstance(error, UnicodeError):
                raise
            raise EmbeddingError("local embedding tokenizer could not be loaded") from None
        self._tokenizer.no_truncation()
        self._tokenizer.no_padding()
        pad_id = self._tokenizer.token_to_id("[PAD]")
        if pad_id is None:
            raise EmbeddingError("local embedding padding token is unavailable")
        self._pad_id = pad_id
        ort.disable_telemetry_events()
        options = ort.SessionOptions()
        options.intra_op_num_threads = config.threads
        options.inter_op_num_threads = 1
        options.enable_profiling = False
        try:
            # Bytes, rather than a path, prevent model-relative external-data reads.
            self._session = ort.InferenceSession(
                model, sess_options=options, providers=["CPUExecutionProvider"]
            )
        except (
            ort_errors.EPFail,
            ort_errors.Fail,
            ort_errors.InvalidArgument,
            ort_errors.InvalidGraph,
            ort_errors.InvalidProtobuf,
            ort_errors.NoSuchFile,
            ort_errors.NotImplemented,
            ort_errors.RuntimeException,
        ):
            raise EmbeddingError("local embedding model could not be loaded") from None
        if {item.name for item in self._session.get_inputs()} != {
            "input_ids",
            "attention_mask",
            "token_type_ids",
        }:
            raise EmbeddingError("local embedding input schema mismatch")
        outputs = self._session.get_outputs()
        if len(outputs) != 1 or outputs[0].name != "last_hidden_state":
            raise EmbeddingError("local embedding output schema mismatch")
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-embedding")

    def _encode(self, request: EmbeddingRequest) -> EmbeddingResponse:
        try:
            # Tokenize on this same single worker rather than starting the
            # tokenizer library's separate process-wide parallel batch pool.
            encoded = [self._tokenizer.encode(text) for text in request.texts]
            if any(len(item.ids) > self._config.maximum_tokens for item in encoded):
                raise EmbeddingError("local embedding token capacity exceeded")
            width = max(len(item.ids) for item in encoded)
            inputs = {
                "input_ids": np.asarray(
                    [item.ids + [self._pad_id] * (width - len(item.ids)) for item in encoded],
                    dtype=np.int64,
                ),
                "attention_mask": np.asarray(
                    [item.attention_mask + [0] * (width - len(item.ids)) for item in encoded],
                    dtype=np.int64,
                ),
                "token_type_ids": np.asarray(
                    [item.type_ids + [0] * (width - len(item.ids)) for item in encoded],
                    dtype=np.int64,
                ),
            }
            tokens = int(inputs["attention_mask"].sum())
            if not 0 < tokens <= request.maximum_tokens:
                raise EmbeddingError("local embedding input usage exceeds admission")
            hidden = np.asarray(self._session.run(["last_hidden_state"], inputs)[0])
            if not np.issubdtype(hidden.dtype, np.floating) or hidden.shape != (
                *inputs["input_ids"].shape,
                self._config.dimensions,
            ):
                raise EmbeddingError("local embedding output dimensions mismatch")
            mask = inputs["attention_mask"][..., np.newaxis]
            mean = (hidden * mask).sum(axis=1) / np.maximum(mask.sum(axis=1), 1)
            response = EmbeddingResponse(
                request_digest=request.digest,
                vectors=tuple(tuple(float(value) for value in row) for row in mean),
                input_tokens=tokens,
            )
            vectors = response.validate_for(request)
            return response.model_copy(update={"vectors": vectors})
        except EmbeddingError:
            raise
        except Exception:
            raise EmbeddingError("local embedding inference failed") from None

    async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        # No queue, retry, download, credential or network transport exists here.
        if self._closed or (self._work is not None and not self._work.done()):
            raise EmbeddingError("local embedding capacity unavailable")
        try:
            checked = EmbeddingRequest.model_validate(request.model_dump())
        except ValueError:
            raise EmbeddingError("invalid local embedding request") from None
        if checked.space != self._config.space:
            raise EmbeddingError("local embedding space mismatch")
        self._work = asyncio.get_running_loop().run_in_executor(
            self._executor, self._encode, checked
        )
        # Native kernels cannot be cancelled. A cancelled caller must not free
        # the slot and start overlapping work, or publish the discarded result.
        self._work.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(self._work)

    async def close(self) -> None:
        self._closed = True
        if self._work is not None:
            with suppress(EmbeddingError):
                await asyncio.shield(self._work)
        self._executor.shutdown(wait=False, cancel_futures=True)
