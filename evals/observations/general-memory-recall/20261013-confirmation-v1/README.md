# Interrupted first confirmation

This attempt is failed and incomplete: 34 of the required 4,800 observations.
It is not activation evidence and supplies no passed quality gate. Its original
source was `c6cdc3f124c5440416be2445faed99d70bac40ec`; observations and captures
retain that identity without relabeling.

The operator sent SIGINT on October 13 at 22:10:23 UTC after hosted integration
verification exposed a missing People-store binding in the M32 PostgreSQL test
fixture. The three original assertions reproduced the failure; adding the same
binding already supplied by production bootstrap made them pass unchanged.
ADR-0199 fingerprints tests as well as runtime inputs. The required fixture
correction therefore prevents this attempt from certifying the final release,
even though production source is unchanged. Cancellation stopped further spend
rather than continuing a collection that cannot bind to the repaired release.

The terminal runner recorded `CancelledError`, 62 settled provider calls,
USD 1.31466675 reported cost and no new uncertain reservations. Prior attempts
remain charged at USD 4.13898575, including their retained uncertainty; aggregate
reported and held cost is USD 5.45365250 under the original USD 100 authorization.
The owner subsequently approved one replacement after final review and CI and
raised the aggregate cap to USD 300, including all earlier charges. Its USD 290
allocation leaves USD 4.54634750 unallocated. No replacement was launched as part
of this retention; the interrupted attempt will never be resumed.

`original-records.tar.gz` preserves every original file, including all observations,
formation banks, preregistration, reviewed corpus, input capture, source identity,
provider journal and failed run status. `report.json` preserves the retention
manifest and original remote archive name. The published archive is byte-identical,
with SHA-256 `1844a070ac745501ed5ab217b27599634a926ba02fe9350983730ffe156efe14`.
All fifteen file hashes were independently verified after copying the archive.
Protected partial checkpoints and operator diagnostics remain retained separately.

No partial collection, unsettled prior reservation or interrupted attempt is
reclassified as success. Independent difficult-failure review, complete comparative
evidence, final hosted review/CI and production delivery remain required.

## Retention container

`retained-records.tar.gz` contains the byte-identical `original-records.tar.gz`
and `report.json` described above. Extract the container into a new scratch
directory before checking the original per-file and archive hashes. The container
SHA-256 is `874331db3b26f2fa778145a9c4238e4a9a9491fc55d18e29b46d64a9037df064`.
