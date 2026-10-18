# PostgreSQL retrieval load measurement

The migrated PostgreSQL adapter and shared retriever ran against 100, 1,000,
10,000 and 100,000 competing records, with ten measurements for each automatic
and deliberate path in each sparse, dense and hybrid channel. Every channel used
the relevance reranker, the existing twenty-item and two-thousand-token caps,
and a query matching one older original more strongly than the newer distractors.
The deliberate tool also received a wrong soft type hint.

All 240 samples ranked the older original first and rendered it. Each returned
two items, at most 95 tokens. The table reports the worse p95 of the two paths;
`report.json` retains every sample and separate p50/p95 values. With ten samples,
nearest-rank p95 is the maximum; there is no claim of stable population latency.

| Competing records | Sparse p95 ms | Dense p95 ms | Hybrid p95 ms |
| ---: | ---: | ---: | ---: |
| 100 | 257 | 37 | 58 |
| 1,000 | 107 | 50 | 283 |
| 10,000 | 759 | 97 | 781 |
| 100,000 | 2,079 | 861 | 2,380 |

The complete measurement took 140.135 seconds with no overlapping verification
jobs on this sidecar and no provider calls. The PostgreSQL and pgvector versions,
platform, exact implementation digest and measurement-source digest are retained.
The script creates, guards and later drops a uniquely named clone of an explicitly
disposable migrated database. It never reads the application database.

These are deliberately repeated fabricated distractors, with fixed orthogonal
384-dimensional vectors. Bulk insertion prepares database load; it bypasses normal
formation, backfill and owner change counters. The experiment measures candidate
selection, exact vector search, shared ranking/rendering and trace writes. It does
not measure model quality, embedding latency, index ingestion, lease/preparation
costs, complete worker latency, or production hardware. It is not activation evidence
or a production scaling guarantee. The independent runtime and lifecycle contracts
cover the mechanisms this load fixture bypasses.

Three setup failures remain in `failed-setup-*.json`: a duplicate fixture position,
ambiguous SQL series parameter types, and a missing deliberate-tool run foreign key.
The last retained one automatic measurement before failing. After fixing fixture
setup, all sizes and both paths ran again; partial measurements were not folded
into the completed report. An earlier import-path launch failed before any report
or database access. These were fixture errors, not red regression tests.

To reproduce from the repository root, use a migrated scratch template with no
other connections. Set `DATABASE_URL` to that scratch database and
`VEETBOT_TEST_DATABASE_DISPOSABLE=1`, then run:

```sh
PYTHONPATH=. uv run python evals/observations/general-memory-recall/20261012-postgres-load/measure.py --output /tmp/new-recall-load.json
```

The output must not exist. This command requires the repository's test dependencies
and database-create privileges on the opted-in scratch server. Normal application
and evaluation databases are not accepted as substitutes for explicit opt-in.
