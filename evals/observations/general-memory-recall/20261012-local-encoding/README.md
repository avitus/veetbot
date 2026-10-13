# Local query-encoding diagnostic

These eight complete runs use the same 400 development seeds, 1,000 project-ticket
distractors, fixed clock and implementation identity. They exercise the real
indexing, query preparation, candidate selection, ranking and rendering code with
20-item/2,000-token limits. Every file remains ineligible for release activation.
The corpus is pooled statements; formation and reader behavior are not measured.

| Query encoding / channel | Automatic rendered recall | Deliberate rendered recall |
| --- | ---: | ---: |
| JSON query / hybrid | 93.75% | 92.25% |
| Natural query / sparse | 68.75% | 68.75% |
| Natural query / dense | 95.50% | 95.50% |
| Natural query / hybrid | 96.25% | 96.25% |

All eight families reach at least 90% rendered recall in the final hybrid rows.
Its remaining fifteen misses per path comprise four candidate failures and eleven
ranking failures. No required fact in the top twenty is lost to the token budget.
This is a development result, not the independent held-out quality gate.

Each observation reports actual candidate, dense, ranked and rendered positions,
returned identifiers, context tokens and elapsed time. Query preparation is inside
the measured recall interval; model loading and source indexing are outside it.
Developer-machine p95 recall times are 126.75 ms and 127.54 ms for the final hybrid
paths. These in-memory timings do not establish PostgreSQL production latency.
The sequential eight-run comparison took 208.88 seconds wall time, with no overlap;
the slowest whole run was the 34.95-second JSON/automatic control.

The fixed model/tokenizer hashes and CPU parameters are in `configuration.json`.
Use operator-installed regular files named `model.onnx` and `tokenizer.json` that
match those hashes. The adapter does not download assets. Reproduce one arm with:

```sh
uv run python -m agent_core.evals.general_memory_local --configuration evals/observations/general-memory-recall/20261012-local-encoding/configuration.json --model-directory /absolute/path/to/verified-model --output test-results/memory/local-hybrid-automatic.json --channel hybrid --path automatic --encoding natural@2
```

The remaining arms change `--channel` (`sparse`, `dense`, `hybrid`), `--path`
(`automatic`, `deliberate`) or `--encoding` (`json@1`, `natural@2`). Use a fresh
output path: the runner refuses overwrites, registers the inputs before work,
and preserves partial observations on failure. JSON encoding is a frozen local
comparison only; production uses natural task text and retains the full structured
query in cache identity and visibility checks. The sparse arm is unchanged by
query-embedding encoding.

The earlier preliminary local pilot used different generic dashboard distractors
and reported 95.25% candidate recall. This controlled comparison uses the same
project-ticket distractors as the checked-in sparse diagnostic; it does not
present the preliminary pilot as a directly paired result. The original OpenAI
pilot and candidate-selection observations remain untouched.
