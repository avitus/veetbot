# ADR-0026: The builtin tool roster and the two Milestone 1 tools

- Status: Accepted
- Date: 2026-07-25
- Related: Milestones 1, 4, 5, 6, Sections 8.1 (tool specification), 8.2
  (initial tools), 8.3 (validation), 9.2 (the policy matrix), 11.2 (trust
  labels), 22 (security baseline), 26 (the first assignment), 28 (sandbox
  isolation), ADR-0005 (deterministic policy), ADR-0008 (sandbox
  isolation), ADR-0021 (tool execution pipeline), ADR-0023 (the run
  loop), ADR-0024 (composition root), ADR-0025 (development toolchain)
- Detailed design: `docs/plan/builtin-tools.md`

## Context

Milestone 1's acceptance is one command, `agent run "What is 17
multiplied by 23?"`, and the tool that answers it is specified in five
bullets. Three of the five are classification. The other two say
"Strictly parse supported mathematical expressions" and "Do not use
unrestricted `eval`", which name a boundary without drawing it: no
operator set, no precedence, no numeric type, no precision or rounding
rule, no argument schema, no error vocabulary, and no bound on what a
hostile expression costs.

`system.current_time` is specified in four bullets, two of which are the
whole design. It is called "deterministic", which nothing that reads a
clock is; and it must "accept an explicit timezone", which is a
statement about an argument whose accepted set, default, and failure
behaviour are all unstated. Underneath both is a declaration the runtime
specification makes and does not finish: `Clock.now()` returns a
`datetime`, and no document says whether that datetime is aware or what
zone it is in. Every consumer so far compares two values from the same
clock, so the ambiguity has been harmless. A tool that converts between
zones is the first consumer for which it is not.

Three further problems are structural rather than local.

**The roster contradicts itself.** Section 8.1 ends with seven
namespaced names. Section 8.2 specifies six tools, one of which
(`demo.external_write`) is absent from 8.1, while one of 8.1's
(`artifact.export`) is specified nowhere and assigned to no milestone.
Read as two rosters they disagree; nothing in the plan says which is
authoritative.

**No builtin carries the fields the registry refuses to start
without.** Hard gate 1 of the tool-system specification requires every
registered `ToolSpec` to pass registration validation with
`output_trust` present. Hard gate 3 requires every `NON_IDEMPOTENT` and
`CONDITIONALLY_IDEMPOTENT` builtin to set the effect watermark before
its first outbound operation. Not one builtin in the corpus declares an
idempotency class, a trust label, a risk level, a timeout, an output
ceiling, or a scope. The gates are stated against values that do not
exist.

**One gate has a hole exactly the shape of a sandbox.** Hard gate 2
forbids `output_trust` above `EXTERNAL_UNTRUSTED` for any tool whose
`source` is `mcp`, `device`, or `sandbox`. `sandbox.run_command` is a
builtin: its `source` is `BUILTIN` and its `target_kind` is `sandbox`.
The gate as written does not reach the one builtin that returns bytes
produced by code we did not write.

## Decision

1.  **The builtin roster is eight tools, the union of Section 8.1's
    names and Section 8.2's specifications.** Section 8.1 is read as a
    naming convention illustrated by example rather than a registry
    manifest. That reading is not invented here: the tool-system
    domain partition already reserves `demo` as a builtin domain
    registered at build time, which a document treating 8.1 as the
    complete roster would not have done.
2.  **`artifact.export` is Milestone 6.** It is not a workspace
    operation, so Milestone 4 is wrong; pairing it with the sandbox
    invites the two designs to merge, so Milestone 5 is wrong. It
    belongs with the control tools, because Milestone 6 is the first
    point at which the model rather than the executor decides what
    leaves the run.
3.  **Classification is settled for all eight now, behaviour for
    two.** Every `ToolSpec` field that registration validation reads or
    the 9.2 matrix is keyed by gets a value in this ADR's detailed
    design, including for tools four milestones away. Behaviour is
    specified only for the two Milestone 1 tools. A tool whose
    classification is already fixed cannot surprise the policy engine
    when its design finally arrives.
4.  **`math.calculate` is a hand-written tokenizer and a
    precedence-climbing parser, not an allowlist over `ast.parse`.**
    An allowlist is a subtraction from a grammar that grows with every
    Python release; Python's parse semantics are not the semantics we
    want; and `ast.parse` can exhaust the C stack on nested input
    before any allowlist runs. A closed grammar we can enumerate in a
    test is not otherwise available.
5.  **Every value is a `decimal.Decimal` at fifty significant digits
    with `ROUND_HALF_EVEN`.** The most commonly reported class of "the
    model got the arithmetic wrong" is a tool returning
    `0.30000000000000004` for `0.1 + 0.2`. A calculator that reproduces
    binary floating-point surprise has given up its only advantage over
    the model's own token-level arithmetic.
6.  **The magnitude bound is the decimal context's, not the
    evaluator's.** `Emax` and `Emin` at ten thousand with
    `InvalidOperation`, `DivisionByZero`, `Overflow`, and `Underflow`
    trapped means no call site in the evaluator can forget to check.
    `9**9**9` fails from the exponents before any digit is computed.
7.  **`^` and `**` are the same operator, and `//` and `%` floor.** The
    model writes `**` and a person writes `^`. Flooring rather than
    `Decimal`'s truncation is chosen because the caller's prior is
    Python's, and a residue argument built on a `-1` the caller
    expected to be `1` is confidently wrong with no failure anywhere.
8.  **The function set stops at what `Decimal` implements natively.**
    No trigonometry at Milestone 1: `sin(90)` has two defensible
    answers and a result has no field in which to say which convention
    it used.
9.  **`result` is a JSON string.** A fifty-digit `Decimal` emitted as a
    JSON number is a fifty-digit `Decimal` rounded to seventeen
    significant digits by the transport, which would undo the reason
    for choosing `Decimal`.
10. **Every `math.calculate` failure is `INVALID_ARGUMENTS` with
    `retryable = False`.** The tool reaches no network, opens no file,
    and has no upstream; it is a pure function, so the identical call
    provably produces the identical failure.
11. **Builtin failure messages carry the supported set and never the
    input.** Hard gate 4 requires a static message per `reason_code`,
    and the message table is one table for every tool including MCP
    tools whose failure text is attacker-controlled. A table with one
    interpolating entry has no invariant. The reason code carries the
    diagnosis, the message carries the remedy, and `detail` carries the
    offset for the operator.
12. **`system.current_time` reads the injected `Clock` and nothing
    else.** Section 8.2's determinism claim is relocated to the port,
    where it can be true: the tool is a pure function of `Clock.now()`,
    the timezone argument, and the timezone database.
13. **`Clock.now()` returns an aware `datetime` in UTC**, asserted in
    the port's contract suite. A naive datetime cannot be converted
    without assuming the process's local zone, which would smuggle
    ambient state through a port built to keep it out.
14. **The `timezone` argument accepts IANA names only and defaults to
    `UTC`.** Abbreviations are ambiguous — `IST` names three zones —
    and the server's local zone is not information anyone asked for.
    `tzdata` is a declared runtime dependency, because the failure
    without it is that every name except `UTC` stops resolving in a
    slim image while every developer machine passes.
15. **Hard gate 2 is restated over `target_kind` as well as
    `source`.** One clause closes the case where a builtin executing in
    a sandbox is a conduit for untrusted bytes read as trusted
    narration.
16. **Registration is seven ordered refusals in the composition root's
    freeze phase**, pure and testable with nothing constructed: name
    grammar, reserved domains, domain membership, uniqueness, schema
    validity, field completeness, forced trust.

## Consequences

- Milestone 1's required demonstration becomes implementable and
  testable on the bytes rather than on a substring. The tool call is a
  pure function from `{"expression": "17 * 23"}` to the JSON string
  `"391"` — quoted, per decision 9, not the JSON number `391` — and every
  stage of it — tokenize, parse, evaluate, render — is independently
  assertable.
- Nine hard gates are added, four of which are adversarial rather than
  functional: a property test that no generated expression escapes the
  eight reason codes, timing assertions on `9**9**9` and its
  neighbours, a differential test pinning `//` and `%` against Python's
  integer operators, and the `0.1 + 0.2` regression that is the whole
  argument for `Decimal` in one line.
- The policy engine gains eight fully classified tools to sort, four
  milestones before six of them exist. The 9.2 matrix can be exercised
  at Milestone 4 against real rows rather than fixtures.
- Two clarifications propagate outward. `Clock.now()` acquires an
  aware-UTC contract that every existing consumer already satisfies,
  and hard gate 2 acquires a second field. Neither changes behaviour
  anywhere today; both close a case that would first appear as a
  production surprise.
- `tzdata` joins the runtime dependency set. This is a real addition to
  the image, accepted because the alternative failure is invisible in
  every environment where it would be caught.
- `allow_parallel` is false for every builtin that writes anything,
  which costs nothing at Milestone 1 and removes an ordering bug class
  that would first appear at Milestone 4.
- Five open questions are recorded. The two with the highest reversal
  cost are the flooring semantics for `//` and `%`, which is cheap to
  change now and expensive once a model has learned the tool's
  behaviour, and the fifty-digit precision, which is trivial to raise
  and awkward to lower once results have been rendered.

## Alternatives considered

- **An allowlist over `ast.parse`**: rejected on three grounds — it
  subtracts from a grammar that grows, it inherits Python's parse
  semantics under our arithmetic, and it runs the allowlist after the
  parser has already processed hostile input.
- **A third-party expression library**: rejected. Every candidate is
  either a thin `eval` wrapper, which Section 8.2 forbids by name, or
  brings a numeric model we would have to override anyway. Two hundred
  lines with no dependency and an enumerable grammar is the smaller
  liability, and Section 22's supply-chain posture treats a dependency
  that parses hostile input as a specific cost.
- **`float` for the numeric type**: rejected. It is faster and nothing
  here is performance-bound, and it fails the one test the tool exists
  to pass.
- **Unbounded integers with a `Decimal` fallback**: rejected. A
  promotion rule is a second numeric model, and a second numeric model
  is a second set of edge cases at the boundary between them. One type
  with a magnitude bound is smaller and its failures are one shape.
- **`Decimal`'s truncating `//` and `%`**: rejected. It is what the
  standard library does and it is what a desk calculator does, and it
  is not what the caller expects. Recorded as an open question because
  the argument the other way is real.
- **Interpolating the offending expression into the failure message**:
  rejected, because the message table is shared with tools whose
  failure text is written by a third party. Mitigated by putting the
  supported set into the message and the offset into `detail`.
- **Reading the process's local timezone when none is given**:
  rejected. A tool whose answer depends on which host ran it is a tool
  whose answer is not reproducible, and it converts the deterministic
  harness into a machine that passes locally.
- **Deferring `system.current_time` to a later milestone**, since
  nothing in the demonstration needs it: rejected. It is the smallest
  tool that forces the `Clock` port to be honest, and building it at
  Milestone 1 is what proves the determinism plumbing works before
  anything depends on it.
- **Specifying all eight tools completely now**: rejected as false
  precision. `sandbox.run_command`'s design is the sandbox design, and
  writing it before Section 28's mechanism decision is exercised would
  produce a specification that has to be rewritten. Classification is
  the part that must be settled early, because it is what other
  components read.
