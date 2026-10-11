"""Optional synthetic-only embedding ablation; never a production memory adapter.

Only the checked-in seed file is admitted. The caller explicitly supplies a
spend ceiling and output path. No answer model, owner database, retries or live
activation is reachable. Results measure candidate discovery before rendering.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from decimal import Decimal
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import httpx

from agent_core.evals.general_memory_recall import fuse_ranks, load_seeds

MODEL = "text-embedding-3-small"
DIMENSIONS = 256
# Verified 2026-10-11 at developers.openai.com/api/docs/models/text-embedding-3-small.
USD_PER_TOKEN = Decimal("0.02") / Decimal(1_000_000)
SEEDS = Path("evals/capability/general-memory-recall-seeds.v1.tsv")


def decode_vectors(
    body: dict[str, Any], *, count: int, dimensions: int
) -> tuple[tuple[float, ...], ...]:
    try:
        rows = body["data"]
        usage = body["usage"]["total_tokens"]
        if type(usage) is not int or usage < 0 or len(rows) != count:
            raise ValueError("invalid embedding batch accounting")
        indexes = [row["index"] for row in rows]
        if any(type(index) is not int for index in indexes) or sorted(indexes) != list(
            range(count)
        ):
            raise ValueError("invalid embedding batch ordering")
        vectors: list[tuple[float, ...]] = []
        for row in sorted(rows, key=lambda row: row["index"]):
            vector = tuple(float(value) for value in row["embedding"])
            if len(vector) != dimensions or not all(math.isfinite(v) for v in vector):
                raise ValueError("invalid embedding vector")
            norm = math.sqrt(sum(v * v for v in vector))
            if not math.isfinite(norm) or norm <= 0:
                raise ValueError("invalid embedding norm")
            vectors.append(tuple(v / norm for v in vector))
        return tuple(vectors)
    except (KeyError, TypeError, OverflowError) as exc:
        raise ValueError("invalid embedding response shape") from exc


async def embed(
    texts: tuple[str, ...], *, api_key: str, ceiling: Decimal, client: httpx.AsyncClient
) -> tuple[tuple[tuple[float, ...], ...], int, tuple[float, ...]]:
    # UTF-8 byte counts conservatively bound text tokens. Reserve the whole job
    # before the first send; failures do not release this uncertain reservation.
    upper_tokens = sum(len(text.encode()) + 1 for text in texts)
    if not ceiling.is_finite() or ceiling <= 0 or upper_tokens * USD_PER_TOKEN > ceiling:
        raise ValueError("embedding experiment exceeds its explicit spend ceiling")
    if not texts or any(not text or len(text.encode()) > 2048 for text in texts):
        raise ValueError("invalid bounded experiment inputs")
    vectors: list[tuple[float, ...]] = []
    tokens = 0
    times: list[float] = []
    async with asyncio.timeout(120):
        for offset in range(0, len(texts), 64):
            batch = texts[offset : offset + 64]
            started = perf_counter()
            response = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": MODEL,
                    "dimensions": DIMENSIONS,
                    "encoding_format": "float",
                    "input": list(batch),
                },
                timeout=30,
            )
            if response.status_code != 200:
                raise ValueError(
                    f"embedding provider status {response.status_code}; response withheld"
                )
            payload = response.json()
            if payload.get("model") != MODEL:
                raise ValueError("embedding model differs from the frozen experiment")
            vectors.extend(decode_vectors(payload, count=len(batch), dimensions=DIMENSIONS))
            tokens += payload["usage"]["total_tokens"]
            if tokens > upper_tokens:
                raise ValueError("embedding usage exceeds conservative reservation")
            times.append((perf_counter() - started) * 1000)
    return tuple(vectors), tokens, tuple(times)


async def compare(sparse_path: Path, output: Path, ceiling: Decimal) -> None:
    sparse = json.loads(sparse_path.read_text())
    seeds = load_seeds(SEEDS)
    digest = hashlib.sha256(SEEDS.read_bytes()).hexdigest()
    if sparse["seed_sha256"] != digest or sparse["version"] != "general-recall-diagnostic@1":
        raise ValueError("sparse evidence does not match the frozen seeds")
    noise = sparse["noise"]
    if type(noise) is not int or not 0 <= noise <= 10000:
        raise ValueError("invalid sparse load")
    ids = tuple(s.id for s in seeds) + tuple(
        str(uuid5(NAMESPACE_URL, f"general-noise:{i}")) for i in range(noise)
    )
    texts = tuple(f"{s.subject}: {s.statement}" for s in seeds) + tuple(
        f"Project ticket {i}: User requested project ticket {i} for the next review."
        for i in range(noise)
    )
    inputs = (*texts, *(s.question for s in seeds))
    manifest = {
        "experiment": "general-semantic-ablation@1",
        "activation_eligible": False,
        "seed_sha256": digest,
        "sparse_sha256": hashlib.sha256(sparse_path.read_bytes()).hexdigest(),
        "model": MODEL,
        "dimensions": DIMENSIONS,
        "cost_ceiling_usd": str(ceiling),
        "conservative_reservation_usd": str(
            sum(len(t.encode()) + 1 for t in inputs) * USD_PER_TOKEN
        ),
        "batch_size": 64,
        "timeout_seconds": 120,
        "input_count": len(inputs),
        "noise": noise,
        "inputs_sha256": hashlib.sha256(json.dumps(inputs).encode()).hexdigest(),
        "status": "registered",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise ValueError("refusing to overwrite an earlier experiment")
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    try:
        async with httpx.AsyncClient(follow_redirects=False) as client:
            vectors, tokens, times = await embed(
                inputs, api_key=os.environ["VEETBOT_OPENAI_KEY"], ceiling=ceiling, client=client
            )
        documents, queries = vectors[: len(ids)], vectors[len(ids) :]
        rows = []
        for seed, query in zip(seeds, queries, strict=True):
            dense = tuple(
                ids[i]
                for i in sorted(
                    range(len(ids)),
                    key=lambda i: (
                        -sum(a * b for a, b in zip(documents[i], query, strict=True)),
                        ids[i],
                    ),
                )[:160]
            )
            for path in ("automatic", "deliberate"):
                observed = next(
                    r
                    for r in sparse["observations"]
                    if (r["seed_id"], r["mode"], r["path"]) == (seed.id, "relevance", path)
                )
                fused = fuse_ranks(tuple(observed["candidate_ids"]), dense)
                rows.append(
                    {
                        "seed_id": seed.id,
                        "family": seed.family,
                        "path": path,
                        "sparse_candidate_rank": _rank(seed.id, observed["candidate_ids"]),
                        "dense_rank": _rank(seed.id, dense),
                        "fusion_rank": _rank(seed.id, fused),
                        "dense_top20": dense[:20],
                        "fusion_top20": fused[:20],
                    }
                )
        manifest.update(
            {
                "status": "completed",
                "used_tokens": tokens,
                "actual_cost_usd": str(tokens * USD_PER_TOKEN),
                "batch_latencies_ms": times,
                "vectors_sha256": hashlib.sha256(json.dumps(vectors).encode()).hexdigest(),
                "observations": rows,
                "limits": (
                    "Candidate discovery only, no rendering/formation/answer/holdout/safety "
                    "certification; batched provider timings are not online query latency."
                ),
            }
        )
    except BaseException:
        manifest.update({"status": "failed", "cost_reservation_retained": True})
        output.write_text(json.dumps(manifest, indent=2) + "\n")
        raise
    output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({k: v for k, v in manifest.items() if k != "observations"}, indent=2))


def _rank(identifier: str, ids: tuple[str, ...] | list[str]) -> int | None:
    return ids.index(identifier) + 1 if identifier in ids else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sparse", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cost-ceiling-usd", type=Decimal, required=True)
    args = parser.parse_args()
    asyncio.run(compare(args.sparse, args.output, args.cost_ceiling_usd))


if __name__ == "__main__":
    main()
