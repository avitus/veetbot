# Native PostgreSQL recall load measurement

The completed fifth attempt retained 480 samples across 100, 1,000, 10,000 and
100,000 competing synthetic records, three retrieval channels and both automatic
and deliberate recall. Each group contains ten cold-query-cache and ten warm-cache
samples. Cold means the application's prepared-query cache was invalidated; the
database and operating-system caches were not flushed.

Every sample ranked the older target first and rendered it, using at most two items
and 95 tokens. These are repeated measurements of one synthetic information need,
not 480 independent answer-quality scenarios. Release activation remains disabled.

The actual verified CPU encoder and leased indexing worker materialized both
current and historical vectors. The historical-indexing configuration was enabled;
the script required exactly twice the original-record count in stored vectors.
The 100,000-record increment indexed 180,000 additional vectors in 3,659.94 seconds.
Peak process RSS was 485,068 KiB. Complete measured duration was 4,328.94 seconds;
the enclosing remote command took 5,649.50 seconds, including time outside the
instrumented measurement. No remote provider calls or model charges were incurred.

| Channel | Automatic p95 at 100,000 records | Deliberate p95 |
| --- | --- | --- |
| Sparse | 2,161.66 ms | 2,130.36 ms |
| Dense | 438.21 ms | 437.81 ms |
| Hybrid | 2,419.09 ms | 2,712.52 ms |

Each p95 combines the twenty cold/warm samples for that path and channel. Retrieval
measurements include native query preparation when uncached; they exclude answer
reading and memory formation. The isolated harness supplies admission directly,
so it does not certify production startup admission or the complete release gate.

## Original inputs and retention

Source commit: `0a310373fec0f0974e0f47b88ba334ca809d2fc6`. The original report
retains that identity and implementation digest. PostgreSQL 16.15, pgvector 0.8.7,
the encoder tuple and asset hashes are recorded in the archived report. Source cleanliness
and the implementation digest were checked before and after measurement.

`original-records.tar.gz` preserves the original report and executed measurement
script without formatting or rewriting either. Extract the archive into a new
scratch directory to inspect the report. SHA-256 values:

- Archive: `d25cbedd2d3c2ad601e3c1a7b5fd755fa0909b11b4b7a4508790b385f829bedf`.
- Original report: `9db36e9e1676cd9952864b555b1f6670a3b52295516f666547df87d2bca22ba7`.
- Executed script: `144dc1a3bb565a0f652e7adc84c24c935bd4ac3e0cadb0c868209e00c82de736`.

Earlier setup failures and the interrupted fourth, current-only indexing attempt
remain retained in protected operator storage. Their diagnostics are not promoted
to successful historical-indexing measurements. No incomplete attempt supplies
activation evidence, and this completed load test does not replace the separate
400-scenario, 4,800-observation confirmation or independent failure review.
