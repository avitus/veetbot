# ADR-0036: The executable 106-knob configuration inventory

- Status: Accepted
- Date: 2026-07-31
- Related: Milestone 0, ADR-0024, ADR-0035
- Detailed design: `docs/plan/bootstrap-and-composition.md`

## Context

ADR-0024 places 106 version-controlled configuration knobs in shipped YAML,
but the corpus did not enumerate the paths behind that number. Most values are
fixed directly by their owning specification. Five operational defaults were
named without a numeric value: the global tool-output ceiling, worker lease
duration, and the three required run counters.

A collection of plausible YAML files does not prove the requirement. Without an
inventory, a knob may be omitted, renamed, or set to null while the repository's
tests continue to claim that all 106 exist.

## Decision

`agent_core.config.SHIPPED_KNOB_PATHS` is the executable inventory. It contains
106 file-qualified dotted paths across the six overlayable default documents:
23 policy, 4 model, 26 context, 20 tool, 16 runtime, and 17 memory paths. Static
tests assert that the paths are unique, resolve in shipped YAML, and are non-null.

Schema versions, profile and rule identifiers, conditions, model-catalog data,
and the frozen hardline rules are not knobs and therefore do not enter the count.
They remain validated configuration, but they cannot inflate the declared total.

The initially unspecified ceilings are:

- global tool output: 4 MiB, above the 1 MiB builtin limit while retaining the
  tool system's separate four-times bounded-reader ceiling;
- worker lease: 30 seconds, with heartbeat derived as one third;
- run defaults: 32 steps, 16 model calls, and 32 tool calls.

All five numbers are version-controlled defaults and may be retuned through a
reviewed YAML change when operational evidence exists.

## Consequences

- The 106-knob claim fails closed when a path disappears or becomes null.
- Changing a dotted path requires updating the inventory in the same review.
- The five selected defaults are explicit decisions rather than undocumented
  implementation guesses.
- Hardline protections stay frozen and outside the operator-tunable count.

## Alternatives considered

- **Count every YAML scalar:** rejected because schema versions, descriptions,
  identifiers, and model prices are not tuning knobs and make the number depend
  on document encoding rather than behavior.
- **Keep the count only in prose:** rejected because omissions would remain
  invisible to `make check`.
- **Leave unnamed ceilings absent:** rejected because the runtime and registry
  require concrete values before their first use, and silently choosing them in
  code would defeat versioned configuration.
