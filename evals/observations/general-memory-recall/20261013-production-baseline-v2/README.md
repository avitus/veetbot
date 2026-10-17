# Production-reader baseline, second attempt

The complete ten-scenario development baseline ran through automatic and deliberate
recall on scratch PostgreSQL with the owner-selected Astra/high chat reader and
Sol memory formation. It completed all twenty observations: six answers passed.
This sparse control is a latency and cost baseline, not confirmation or activation
evidence. It used no held-out scenarios.

The seventy-three completed provider calls reported USD 1.19266700, with no held
usage. The preceding failed attempt remains unchanged in
`../20261013-production-baseline-v1/`; its USD 2.19152 uncertain reservation is still
charged against the owner's aggregate USD 100 allowance. Combined reported and
held accounting is USD 4.13898575. The final confirmation is capped at USD 92.

`original-records.tar.gz` preserves the complete original run, observations,
provider journal and ten paired formation banks byte for byte. `report.json`
records the archive and individual file hashes. The original development run
retains its implementation digest and is not relabeled as a clean-build confirmation.
The archive can be extracted into a new scratch directory for inspection.

The baseline measured p95 elapsed recall/reader latency of 21670.706385 ms and
maximum attributed per-observation cost of USD 0.11150100. The preregistered rule,
fixed before this attempt, sets confirmation ceilings at 1.25 times those values:
27088.382981 ms and USD 0.13937625. A simple cost extrapolation is approximately
USD 231 for all 4800 observations; actual usage may differ. The original USD 100
cap governed that attempt. After the first interrupted confirmation, the owner
explicitly approved USD 300 in aggregate, including all earlier charges, for one
replacement after final review and CI (ADR-0199). A capped or otherwise incomplete
run cannot authorize activation.

## Retention container

`retained-records.tar.gz` contains the byte-identical `original-records.tar.gz`
and `report.json` described above. Extract the container into a new scratch
directory before checking the original per-file and archive hashes. The container
SHA-256 is `fd3ac99c3f6b6fbd855897db7a9a7e9af739eaa22a931af6d2fe985c484a0659`.
