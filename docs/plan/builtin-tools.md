---
title: Builtin Tools
status: design
canonical: true
---

# Builtin tools

Milestone 1's required demonstration is `17 * 23`, and the tool that
computes it is specified in five bullets, two of which say what not to
do. That is enough to start an argument about parsing and not enough to
write a test. The same is true of `system.current_time`, whose four
bullets include the word "deterministic" and the instruction to accept a
timezone, which are the two hardest things about it and are stated as
though they were the easy ones.

This document specifies both, completely. It gives every other builtin
the classification fields that registration validation requires before
the process will start, and it specifies the four Milestone 4 tools —
the three `workspace.` ones and `demo.external_write` — as completely as
the first two, on a later pass than the one that classified them.

The distinction it draws throughout is between **classification** and
**behaviour**. A tool's classification — what it may touch, how risky
it is, whether repeating it is safe, what trust its output carries —
is what policy sorts on, what the 9.2 matrix is keyed by, and what hard
gate 1 in [tool-system.md](tool-system.md) refuses to start without. A
tool's behaviour is what it does with its arguments. The eight builtins
get their classification here, all of it, at once. Six of them get their
behaviour here as well: the two Milestone 1 tools, because Milestone 1
cannot be written without them, and the four Milestone 4 tools, which
were a pointer on the first pass and are designed here on a later one.
The remaining two are Milestone 6's, and a pointer is a smaller debt
than it sounds like: a tool whose classification is fixed cannot
surprise the policy engine later.

## What this document is responsible for

It is responsible for seven things.

1.  **The roster.** Which tools are builtin, what each is called, and
    which milestone ships it. Section 8.1 and Section 8.2 disagree about
    this in two places, and the disagreement is resolved below rather
    than split.
2.  **The classification table.** Every `ToolSpec` field, for every
    builtin, with a value. Registration validation reads eight of those
    fields and the policy engine reads three; none of them currently has
    a value anywhere in the corpus.
3.  **`math.calculate`, completely.** The expression language as a
    grammar, the numeric type, the precision and rounding rule, the
    operator set with its associativity, the function set with its
    domains, the bounds that make a hostile expression a failure rather
    than an outage, the error vocabulary mapped onto `ToolFailureKind`,
    and both JSON schemas.
4.  **`system.current_time`, completely.** What it reads, why that makes
    it deterministic, which timezone names it accepts, what it returns,
    and the contract it forces onto the `Clock` port — which
    [runtime-loop.md](runtime-loop.md) declares as returning `datetime`
    without saying whether that datetime is aware.
5.  **The four Milestone 4 tools, completely.** What each does with the
    `WorkspaceHandle` the containment rule already lives on, the
    encoding and binary-file rules, the checksum and what it is taken
    over, the listing's limit and its ordering, the provenance record
    that decides what trust a read carries back, and the record
    `demo.external_write` leaves behind.
6.  **The registration check.** What happens at startup, in what order,
    and what a builtin has to get wrong for the process to refuse to
    start.
7.  **The failure-message rule for builtins.** Hard gate 4 requires a
    static message per `reason_code`. For a builtin whose arguments the
    model wrote, a static message that omits what went wrong leaves the
    model unable to correct itself, and the fix is not to echo the
    input.

It is not responsible for the pipeline those tools run inside, which is
[tool-system.md](tool-system.md), or for the approval behaviour of
`demo.external_write`, which is
[policy-and-approvals.md](policy-and-approvals.md), or for the sandbox
`sandbox.run_command` executes in, which is Section 28, ADR-0008, and
[sandbox-isolation.md](sandbox-isolation.md).

## The roster

Section 8.1 ends with the instruction "Use namespaced names:" followed
by seven names. Section 8.2 is titled "Initial tools" and specifies six,
five of which appear in that list of seven. `demo.external_write` is
specified in 8.2 and absent from 8.1. `artifact.export` appears in 8.1
and is specified nowhere.

Read as two rosters these contradict. Read as what they are — a naming
convention illustrated by example, and a list of the tools the early
milestones build — they do not. The 8.1 fence demonstrates the dotted
form; it is not a registry manifest, and treating it as one is what
makes `demo.external_write` look like an error. The evidence for that
reading is in [tool-system.md](tool-system.md), whose domain partition
table already reserves `demo` and `delegate` as builtin domains
registered at build time. A document that had understood 8.1 as the
complete roster would not have made room for a domain 8.1 omits.

The roster is therefore the union, and it is eight tools:

```text
name                  domain     milestone  ships as
--------------------  ---------  ---------  ----------------------
math.calculate        math       1          full design, here
system.current_time   system     1          full design, here
workspace.read_text   workspace  4          full design, here
workspace.write_text  workspace  4          full design, here
workspace.list_files  workspace  4          full design, here
demo.external_write   demo       4          full design, here
sandbox.run_command   sandbox    6          classification, here
artifact.export       artifact   6          classification, here
```

The milestone column is taken from the plan wherever the plan states it.
Milestone 1's implement list names `math.calculate` and
`system.current_time`. Milestone 4's implement list names the three
`workspace.` tools and `demo.external_write`. Section 8.2 says to add
`sandbox.run_command` "only after the sandbox milestone", which is
Milestone 6 — Milestone 5 is the HTTP API and SSE.
`artifact.export` is the one assignment the plan does not
make, and it is placed at Milestone 6 below with its reason.

### The roster is not the corpus's tool census

Eight is the number of tools *this document* designs. It is not the
number of tools the model can call, and the gap is wide enough to
state here rather than leave a reader to assemble.

Ten more model-callable tools are declared at build time by other
specifications:

```text
tool                          kind        declared by
----------------------------  ----------  -------------------
conversation.ask_user         control     tool-system
delegate.run                  control     tool-system
context.update_working_state  control     context-engine
skill.load                    control     skills
skill.manage                  capability  skills
memory.remember               capability  memory-formation
memory.search                 capability  memory-retrieval
memory.recall_episodes        capability  memory-retrieval
knowledge.ingest              capability  knowledge-documents
knowledge.search              capability  knowledge-documents
```

Eighteen model-callable tools in total, and this document's roster is
eight of them. The rule that keeps both numbers right is
[knowledge-documents.md](knowledge-documents.md)'s, and it is repeated
here because a reader who finds it only there has already been
confused: *"Subject specifications declare their own tools ... so this
costs `builtin-tools.md` nothing, and the roster's count is
unchanged."* A tool belongs to the document that designs the subject
it acts on. The roster is what is left over — the tools with no
subject document of their own.

Two consequences follow, and both read wrong if they are not said.

**The classification table below is complete for the eight and for
nothing else.** Of the other ten, only `skill.manage` is fully
classified, in [skills.md](skills.md), which gives it six fields;
`skill.load` carries three. The three remaining control tools inherit
`side_effect: NONE` and `target_kind: in_process` from the
registration constraint on their kind and declare nothing else. Of the
five memory and knowledge tools, `memory.search`,
`memory.recall_episodes`, and `knowledge.search` declare an
`output_trust`, the two `knowledge.` tools declare `required_scopes`,
and `memory.remember` declares neither. That is a gap in the corpus
rather than a division of labour, because `ToolSpec` has no optional
fields and registration step 6 below refuses a spec that is missing
one. Whoever builds a tool on that list supplies its classification
with it, in the document that owns it.

**The registration check below runs over the registry, not over this
roster.** Its subject is the checked-in builtin specs, which at freeze
is all eighteen. Step 3, domain membership, already passes for every
one of them: [tool-system.md](tool-system.md)'s partition table lists
`delegate`, `conversation`, `context`, `skill`, and `memory` as
builtin domains registered at build time, and `knowledge` beside them.
It is step 6 that has nothing to read for eight of the eighteen.

### Why `artifact.export` is Milestone 6

Nothing in the plan assigns it. Three placements are defensible and one
is correct.

Milestone 4 is wrong because artifact export is not a workspace
operation — it moves bytes from a workspace into the artifact store,
and the artifact store is what large outputs are already being written
to by the truncation path in
[tool-system.md](tool-system.md). Adding a
model-callable entry point to that store before the store's own
retention, scoping, and cross-tenant rules are exercised puts the model
in front of a component that has only ever been driven by the executor.

Milestone 5 is wrong for a narrower reason, and it is the more
tempting placement of the two, because Milestone 5 is the HTTP API and
the artifact routes a client reads land there. A route a client calls
and a tool the model calls are different surfaces with different
callers, different authorization, and different failure modes, and
building the second because the first exists is how a tool acquires a
shape borrowed from HTTP. The narrower objection is decisive on its
own: the tool takes a workspace path, and no workspace exists until
Milestone 6.

Sharing a milestone with `sandbox.run_command` carries its own hazard,
and it is worth naming so that the two designs are not allowed to
merge. Exporting a file is not a property of having run a command.
`artifact.export` takes a workspace path, is `IDEMPOTENT`, and runs
`in_process`; `sandbox.run_command` is none of those. They share a
milestone and nothing else.

Milestone 6 is right because Milestone 6 is where the model gains
control tools and the programmatic bridge — the first point at which
the model is deciding what leaves the run, rather than the executor
deciding what to keep. `artifact.export` is that decision made explicit,
and it belongs with the others.

This is a judgment call on a question the plan leaves open, and it is
recorded as such.

## The classification table

Every field of the completed `ToolSpec`, for all eight of the roster —
the ten tools above are their own documents' to classify. The table is
split across four fences for width, not for meaning; together they are
one row per tool.

Classification proper — the three fields the 9.2 policy matrix is
keyed by:

```text
name                  side_effect      risk    idempotency
--------------------  ---------------  ------  -----------------
math.calculate        NONE             LOW     READ_ONLY
system.current_time   NONE             LOW     READ_ONLY
workspace.read_text   WORKSPACE_READ   LOW     READ_ONLY
workspace.list_files  WORKSPACE_READ   LOW     READ_ONLY
workspace.write_text  WORKSPACE_WRITE  MEDIUM  IDEMPOTENT
artifact.export       WORKSPACE_READ   LOW     IDEMPOTENT
sandbox.run_command   CODE_EXECUTION   HIGH    NON_IDEMPOTENT
demo.external_write   EXTERNAL_WRITE   HIGH    NON_IDEMPOTENT
```

Trust and scopes:

```text
name                  output_trust        required_scopes
--------------------  ------------------  ------------------
math.calculate        INTERNAL_TOOL       (none)
system.current_time   INTERNAL_TOOL       (none)
workspace.read_text   INTERNAL_TOOL       workspace.read
workspace.list_files  INTERNAL_TOOL       workspace.read
workspace.write_text  INTERNAL_TOOL       workspace.write
artifact.export       INTERNAL_TOOL       artifact.write
sandbox.run_command   EXTERNAL_UNTRUSTED  sandbox.execute
demo.external_write   INTERNAL_TOOL       demo.write
```

Limits:

```text
name                  timeout_s  max_output_bytes  allow_parallel
--------------------  ---------  ----------------  --------------
math.calculate        2          4096              yes
system.current_time   2          4096              yes
workspace.read_text   10         1048576           yes
workspace.list_files  10         262144            yes
workspace.write_text  10         4096              no
artifact.export       30         4096              no
sandbox.run_command   300        1048576           no
demo.external_write   10         4096              no
```

Kind, source, and execution target:

```text
name                  kind        source   target_kind
--------------------  ----------  -------  -----------
math.calculate        CAPABILITY  BUILTIN  in_process
system.current_time   CAPABILITY  BUILTIN  in_process
workspace.read_text   CAPABILITY  BUILTIN  in_process
workspace.list_files  CAPABILITY  BUILTIN  in_process
workspace.write_text  CAPABILITY  BUILTIN  in_process
artifact.export       CAPABILITY  BUILTIN  in_process
sandbox.run_command   CAPABILITY  BUILTIN  sandbox
demo.external_write   CAPABILITY  BUILTIN  in_process
```

`server_id` is `None` for all eight, because it is set only when
`source` is `MCP`. `deprecated` is `False` for all eight. `description`,
`input_schema`, and `output_schema` are per-tool and are given below for
the two that ship at Milestone 1; for the other six they are part of the
design their milestone owes.

Six of these values need their reasoning stated, because a reader would
otherwise have to guess whether they were considered.

1.  **`sandbox.run_command` is the only builtin whose `output_trust` is
    `EXTERNAL_UNTRUSTED`, and it is forced rather than chosen.** Hard
    gate 2 requires that no tool with `source` in `{mcp, device,
    sandbox}` carry trust above `EXTERNAL_UNTRUSTED`. Its `source` is
    `BUILTIN`, so the gate as written does not reach it — but its
    `target_kind` is `sandbox`, and what the gate is protecting against
    is code we did not write producing bytes the model reads as
    narration. That is exactly what a shell command in a sandbox is.
    The gate is therefore restated below over `target_kind` as well as
    `source`.
2.  **`workspace.read_text` is declared `INTERNAL_TOOL` and will often
    have to lower itself.** A workspace can contain a file the sandbox
    downloaded, and reading that file returns third-party bytes.
    `ToolResult.output_trust` exists for this and
    [tool-system.md](tool-system.md) uses this precise example. The
    constraint on the Milestone 4 design is stated below rather than
    left to be noticed.
3.  **`workspace.write_text` is `IDEMPOTENT`, not
    `CONDITIONALLY_IDEMPOTENT`.** Writing the same bytes to the same
    path twice leaves the workspace in the same state, and the tool
    returns metadata and a checksum rather than an append offset.
    Nothing about the second call depends on whether the first
    happened, which is the test the class is for.
4.  **`artifact.export` is `IDEMPOTENT` on the same grounds and gets a
    condition anyway.** Exporting the same path twice within a run must
    return the same `ArtifactRef` rather than creating a second one.
    That is a requirement on the Milestone 6 design, not an observation
    about it, and it is what makes the class honest.
5.  **`allow_parallel` is `yes` only for the four read-only tools.** The
    two Milestone 1 tools are pure; the two workspace readers observe a
    filesystem that nothing in the same step is writing, because a step
    containing a writer is not a parallel batch. Every tool that writes
    anything is serialized, which costs nothing at Milestone 1 and
    removes a class of ordering bug that would otherwise first appear
    at Milestone 4.
6.  **The two Milestone 1 tools have no `required_scopes`.** They touch
    nothing, and a scope that gates arithmetic is a scope that will be
    granted to everyone, which teaches the scope system to be ignored.
    Section 8.2's "No approval" is a consequence of `side_effect =
    NONE` and `risk = LOW` under the 9.2 matrix, not a separate
    declaration, and it is not restated on the `ToolSpec`.

### Versioning

Every builtin registers at `version = "1.0.0"` and versions
independently. The version is serialized into the byte-stable prefix
along with the rest of the tool definition, which means a version bump
invalidates every provider-side prefix cache for every session that had
the tool enabled.

That cost is the reason for the rule: **a builtin's version changes only
when its `input_schema`, `output_schema`, or documented semantics
change.** A bug fix that makes the tool do what it already said it did
is not a version change. A new optional argument is a minor bump. A
removed argument, a changed default, or a changed output field is a
major bump, and a major bump of a Milestone 1 tool is a decision to
invalidate every cached prefix in the fleet, which is a thing to do
deliberately rather than in passing.

## `math.calculate`

Section 8.2 gives it five bullets. Three are classification and are in
the table above. The remaining two — "Strictly parse supported
mathematical expressions" and "Do not use unrestricted `eval`" — name
a boundary without drawing it. Everything below draws it.

### Why a hand-written parser and not an `ast` allowlist

The obvious implementation is `ast.parse(expr, mode="eval")` followed by
a walk that rejects any node type not on an allowlist. It is about
thirty lines, it is what most projects do, and it is rejected here for
three reasons.

The first is that an allowlist over someone else's grammar is a
subtraction from an open set. Python's expression grammar is a moving
target: comprehensions, walrus, f-strings, and starred expressions all
arrived after the language was stable, and each one arrived as a node
type an existing allowlist did not mention. Subtracting from a set that
grows means the tool's real input language is defined by whatever
Python's parser accepts this release, minus whatever we happened to
think of. The property we want — that the accepted language is a
closed set we can enumerate in a test — is not available that way.

The second is that Python's semantics are not the semantics we want.
`/` is float division, `**` is right-associative over machine floats,
integer literals accept underscores, `1j` is a complex number, and
`-7 // 2` is `-4` while `Decimal(-7) // Decimal(2)` is `-3`. An
allowlisted `ast` walk that then evaluates with `Decimal` operands
inherits Python's *parse* and our *arithmetic*, and the seams between
them are where the wrong answers live.

The third is that `ast.parse` is not a safe front end for hostile input
in the first place. Deeply nested expressions can exhaust the C stack
during parsing, before any allowlist runs. A depth bound applied after
parsing is a bound applied after the thing it was supposed to prevent.

A precedence-climbing parser over a hand-written tokenizer is roughly
two hundred lines, has no dependency, accepts exactly the grammar
printed below, and enforces its depth bound while parsing. That is the
implementation.

### The grammar

```text
expression := term (("+" | "-") term)*
term       := unary (("*" | "/" | "//" | "%") unary)*
unary      := ("+" | "-") unary | power
power      := primary [("^" | "**") unary]
primary    := NUMBER | NAME | call | "(" expression ")"
call       := NAME "(" [expression ("," expression)*] ")"
NUMBER     := digit+ ["." digit+] [("e" | "E") ["+" | "-"] digit+]
NAME       := [a-z][a-z0-9_]*
```

Whitespace between tokens is insignificant and any run of it is
discarded. Nothing else is accepted: no assignment, no comparison, no
string, no list, no attribute access, no underscore digit separators, no
leading `.` on a number, no hexadecimal, and no trailing operator. An
input that does not derive from `expression` and consume every token
fails with `syntax`.

`NAME` resolves to a constant if the next token is not `(`, and to a
function if it is. There is no other namespace, no user-defined name,
and no assignment, so a name is either in the fixed table below or it is
`unknown_name`.

### Precedence and associativity

Tightest binding first:

```text
level  operators             associativity  note
-----  --------------------  -------------  -------------------
1      ( )                   n/a            grouping and calls
2      ^  **                 right          2^3^2 is 2^(3^2)
3      unary +  unary -      right          -2^2 is -(2^2)
4      *  /  //  %           left
5      binary +  binary -    left
```

`^` and `**` are the same operator. The model writes `**` because it
writes Python; a person writes `^` because they write a calculator.
Accepting both and mapping them to one node costs one line in the
tokenizer and removes a failure mode that would look, to whoever hit
it, like the tool being broken.

`-2^2` evaluating to `-4` is the convention every calculator and every
programming language with a `^` operator uses, and the grammar produces
it because `unary` sits above `power` on the left while `power`'s
right operand is a full `unary`. That last part is what makes `2^-1`
parse.

### Numbers, precision, and rounding

Every value is a `decimal.Decimal`. Not a float, and not an `int` with a
promotion rule.

The reason is that the single most commonly reported class of "the model
got the arithmetic wrong" is not the model's arithmetic at all: it is
`0.1 + 0.2` returning `0.30000000000000004` from a tool the model
trusted. A calculator tool exists to be more reliable than the model's
own token-level arithmetic. One that reproduces binary floating-point
surprise has given up the only advantage it had.

Evaluation runs inside an explicit `decimal.localcontext()`:

```text
prec     50
rounding ROUND_HALF_EVEN
Emax     10000
Emin     -10000
traps    InvalidOperation, DivisionByZero, Overflow, Underflow
capitals 0
```

Three properties follow, and each of them is doing work.

1.  **`prec = 50` is well past double precision and well short of
    unbounded.** Fifty significant digits answers every question a
    calculator tool is actually asked, and it bounds the cost of
    `exp` and `ln`, which are iterative and whose cost grows with
    precision.
2.  **`Emax` and `Emin` at ten thousand make the magnitude bound part
    of the arithmetic rather than a hand-written check.** There is no
    place in the evaluator that tests whether a result got too big;
    the context raises `Overflow`, the trap converts it to an
    exception, and the exception maps to `result_out_of_range`. A
    check that lives in the arithmetic cannot be forgotten at one call
    site.
3.  **Trapping `Underflow` as well as `Overflow` is deliberate.**
    Silently returning zero for a result that is merely very small is
    the same category of lie as silently returning infinity for one
    that is very large, and a calculator that quietly rounds an answer
    to zero is worse than one that says it cannot represent it.

Rounding is `ROUND_HALF_EVEN`, which is the IEEE 754 default and the
`decimal` module's default. It is not the rounding rule a person means
by "round half up", and the difference shows in `round(2.5)` returning
`2`. That is stated in the tool's `description` so the model can say so
rather than being surprised by it.

### Rendering the result

A `Decimal` has more than one string form and the tool must pick one,
because the rendered string is what the model reads and what a test
asserts on.

The rule has two branches:

1.  **If the value's adjusted exponent is between -25 and 25**, render
    with `format(value, "f")` — positional notation, no exponent —
    and then strip a trailing run of zeros after a decimal point, and
    the decimal point itself if nothing follows it. `17 * 23` renders
    as `391`, not `3.91E+2`. `1/4` renders as `0.25`, not
    `0.250000...`.
2.  **Otherwise**, render with `to_sci_string`, giving one digit before
    the point and an explicit exponent.

The band is where positional notation stays readable. Outside it,
positional notation is a page of zeros and the exponent is the
information.

Exactness is reported alongside the value. The local context's `Inexact`
flag is cleared before evaluation and read afterward; `result_exact` is
`True` when it was never set. `1/3` at fifty digits is a very good
approximation and the tool says so rather than implying the fifty digits
are the answer.

### Operators

```text
operator  meaning
--------  ----------------------------------------------------
+  -  *   as in decimal arithmetic
/         true division; division by zero is a failure
//        floor division: floor(a / b), exact
%         a - b * floor(a / b); sign follows the divisor
^  **     exponentiation, right-associative
```

`//` and `%` need their own paragraph because there are two defensible
answers and picking the wrong one is silently wrong rather than loudly
wrong.

`Decimal`'s built-in `//` truncates toward zero, so
`Decimal(-7) // Decimal(2)` is `-3` and `Decimal(-7) % Decimal(2)` is
`-1`. Python's `int` operators floor, so `-7 // 2` is `-4` and
`-7 % 2` is `1`. The caller here is a language model whose prior for
both operators is Python's, and whose reasoning about a modulus is
almost always a positive-residue argument. **The tool implements the
flooring form**, computed explicitly rather than by calling `Decimal`'s
operators, and says so in its `description`.

The cost of being wrong in the other direction is a model that computes
a correct residue argument on top of a `-1` it expected to be `1`,
which produces a confidently wrong answer with no failure anywhere. The
cost of this choice is that someone reading the implementation has to
notice the operators are not `Decimal`'s. A comment covers that.

`//` and `%` with a zero divisor are `division_by_zero`, the same as
`/`.

### Functions and constants

```text
function       arity  domain
-------------  -----  ---------------------------------------
abs(x)         1      any
ceil(x)        1      any
floor(x)       1      any
round(x)       1      any
round(x, n)    2      n integral, -50 <= n <= 50
sqrt(x)        1      x >= 0
ln(x)          1      x > 0
log10(x)       1      x > 0
exp(x)         1      result within the magnitude bound
min(a, ...)    1+     any
max(a, ...)    1+     any
```

Every one of these is either trivial or is a method `decimal.Decimal`
already provides at context precision. `sqrt`, `ln`, `log10`, and `exp`
are `Decimal.sqrt`, `Decimal.ln`, `Decimal.log10`, and `Decimal.exp`;
they are correctly rounded and they respect the context, which is the
whole reason the function set stops where it does.

Constants, carried as 60-significant-digit literals and rounded to the
context precision on use:

```text
pi  3.14159265358979323846264338327950288419716939937510582097494
e   2.71828182845904523536028747135266249775724709369995957496696
```

Ten more digits than the context needs, so that the fiftieth digit is
correct after rounding rather than correct by luck.

**There is no trigonometry at Milestone 1.** Not because it is hard —
a Taylor series over `Decimal` is a short function — but because
`sin(90)` has two defensible answers and neither of them can be
signalled. A tool that silently reads its argument as radians when the
caller meant degrees returns `0.8939...` where the caller expected `1`,
and nothing in the result says which convention was used. Adding
trigonometry means adding a units argument or two families of function
names, and that is a decision to make when something needs it rather
than a decision to guess now. It is recorded as an open question.

### Bounds

A calculator that accepts arbitrary expressions is a denial-of-service
surface, and `9**9**9` is its canonical form. Four bounds close it, and
three of the four are enforced by the context above rather than by the
evaluator.

```text
bound                   value   enforced at        failure
----------------------  ------  -----------------  --------------------
expression length       1024    schema, then       expression_too_long
                                the tokenizer
token count             512     tokenizing         expression_too_long
nesting depth           32      parsing            expression_too_deep
result magnitude        1e10000 the decimal        result_out_of_range
                                context
```

The depth bound counts grammar recursion, not parentheses: a call
argument, a parenthesized group, and a unary chain each add one. It is
checked as the parser descends, so `((((...))))` fails at depth 33
rather than after building a tree.

`9**9**9` fails on the magnitude bound. Right-associativity makes it
`9^(9^9)`, the inner power is `387420489`, and raising nine to that
overflows `Emax` on the first operation that would produce the value.
The context raises, the trap fires, and the tool returns
`result_out_of_range` in microseconds. Nothing large is ever allocated,
because the overflow is detected from the exponents before the digits
are computed.

The expression-length bound appears twice on purpose. `maxLength` in
the input schema means an oversized expression is rejected at pipeline
step 4, before the tool is entered at all. The tokenizer checks it
again because a tool that depends on its caller having validated is a
tool that is wrong the first time it is called from anywhere else.

### The error vocabulary

Every failure is `ToolFailureKind.INVALID_ARGUMENTS` with
`retryable = False`. There is no other kind, because there is nothing
this tool can fail at that is not a property of the expression it was
given: it reaches no network, opens no file, and has no upstream.
`retryable = False` is exact rather than conservative — the tool is a
pure function, so the identical call produces the identical failure,
and a retry is provably useless. The model reformulating the expression
is a different call, not a retry.

Reason codes, each prefixed `tool.invalid_arguments.`:

```text
syntax
  The expression could not be parsed.
unknown_name
  Unknown function or constant. Functions: abs, ceil, exp,
  floor, ln, log10, max, min, round, sqrt. Constants: pi, e.
arity
  Wrong number of arguments for that function.
domain
  An argument is outside the function's domain.
division_by_zero
  Division by zero.
result_out_of_range
  The result is too large or too small to represent.
expression_too_long
  The expression is longer than 1024 characters.
expression_too_deep
  The expression is nested more than 32 levels deep.
```

The second line of each entry is the model-facing `message` that hard
gate 4 requires to be checked in. The `detail` field carries what the
operator needs and the model does not get: for `syntax`, the character
offset and the unexpected token; for `domain`, which argument and which
function.

That asymmetry is a real cost and it is worth naming. A model that gets
"The expression could not be parsed." and no offset has to re-derive
where it went wrong from an expression it wrote. The alternative is to
interpolate the offending input into the message, and the reason not to
is not squeamishness about this tool — it is that the message table
is one table for every tool, including MCP tools whose failure text is
attacker-controlled. A table with one interpolating entry is a table
whose invariant is "static, except where it isn't", which is not an
invariant.

The mitigation is in the messages themselves: `unknown_name` carries
the whole supported set, and the reason codes are specific enough
— `syntax` against `arity` against `domain` against `unknown_name` —
that the model can tell a typo from a misunderstanding without being
told where. That is the design rule for builtin failure messages
generally: **make the reason code carry the diagnosis and the message
carry the remedy, and never make either carry the input.**

### The schemas

Input:

```json
{
  "type": "object",
  "properties": {
    "expression": {
      "type": "string",
      "minLength": 1,
      "maxLength": 1024,
      "description": "A mathematical expression, e.g. 17 * 23"
    }
  },
  "required": ["expression"],
  "additionalProperties": false
}
```

Output:

```json
{
  "type": "object",
  "properties": {
    "result": {"type": "string"},
    "result_exact": {"type": "boolean"},
    "expression": {"type": "string"}
  },
  "required": ["result", "result_exact", "expression"],
  "additionalProperties": false
}
```

`result` is a **string**, and that is the load-bearing choice in the
output schema. A JSON number is a double by the time it has been through
`json.dumps` and the provider's serializer and the model's tokenizer,
and a fifty-digit `Decimal` emitted as a JSON number is a fifty-digit
`Decimal` that has been rounded to seventeen significant digits by the
transport. Choosing `Decimal` and then emitting a float would undo the
entire reason for choosing it.

`expression` echoes the input verbatim. It is not a canonicalized form.
The model reads its own expression back and can see that the tool
understood the same string it sent, which is the cheapest possible
guard against an argument that got mangled upstream.

### What the model reads

`content` is a single text part carrying the rendered result and
nothing else:

```text
391
```

Not `17 * 23 = 391`, and not `The result is 391.` The model already has
the expression; a tool that restates it is spending prefix-adjacent
tokens on something the model wrote three hundred tokens ago. When
`result_exact` is `False`, and only then, the rendering is followed by
a second line:

```text
0.33333333333333333333333333333333333333333333333333
rounded to 50 significant digits
```

### The demonstration

Milestone 1's required flow is:

```text
run created
model requests math.calculate
tool executes
```

with the input `"What is 17 multiplied by 23?"`. The fake model provider
emits a tool call with `{"expression": "17 * 23"}`, the tokenizer
produces five tokens, the parser produces one multiplication node, the
evaluator produces `Decimal("391")`, the renderer produces `391`, and
`result_exact` is `True`. Every step of that is a pure function, which
is why the acceptance test asserts on the bytes rather than on a
substring.

## `system.current_time`

Section 8.2 gives it four bullets: read-only, deterministic, no
approval, accept an explicit timezone. The first and third are in the
classification table. The other two are the whole design.

### Why it is deterministic

It is not. Nothing that reads a clock is.

What is true is narrower and more useful: **the tool is a pure function
of `Clock.now()`, the `timezone` argument, and the timezone database.**
It does not call `datetime.now()`, `time.time()`, or anything else that
reads ambient state. It receives a `Clock` at construction — the same
`Clock` [runtime-loop.md](runtime-loop.md) declares and
[bootstrap-and-composition.md](bootstrap-and-composition.md) constructs
in the determinism phase, before anything else exists — and asks it
for the time.

Under the deterministic harness the injected `Clock` is fixed or
scripted, and the tool's output is byte-stable across runs. That is the
property Section 8.2's "Deterministic" was reaching for, and it is a
property of the port rather than of the tool. An implementation that
called `datetime.now(UTC)` would satisfy every one of Section 8.2's
four bullets as written and be untestable.

ADR-0024 already requires a static check that no module outside
`adapters/determinism.py` reads ambient time or generates an
identifier. The builtin tool package is inside that check's scope, and
this tool is the reason the check has to reach further than the
runtime.

### What `Clock.now()` returns

`runtime-loop.md` declares `def now(self) -> datetime: ...` and does
not say whether the datetime is aware, or in what zone. Every consumer
so far has been comparing it to another value from the same clock, so
the ambiguity has not mattered. It matters here, because this tool
converts.

**`Clock.now()` returns an aware `datetime` whose `tzinfo` is UTC.** A
naive datetime cannot be converted to another zone without an
assumption, and the assumption would be the process's local zone —
ambient state, arriving through the back door of a port that exists to
keep ambient state out. An implementation returning a naive datetime is
a contract violation, asserted in the `Clock` contract suite rather
than discovered here.

This is a clarification of an existing declaration, not a new
requirement. It is recorded as such.

### Timezones

The `timezone` argument is an **IANA timezone name**, and it defaults to
`"UTC"`.

Defaulting to UTC rather than to the process's local zone is the same
decision as the one above, arriving from the other side. A tool whose
answer depends on which host it ran on is a tool whose answer is not
reproducible, and "the server's local time" is not information the
model asked for.

Names are resolved with `zoneinfo.ZoneInfo`. The accepted set is
therefore exactly the contents of the timezone database, which is a
real, versioned, enumerable set rather than a list this document would
have to maintain. `UTC`, `America/New_York`, and `Etc/GMT+5` are names;
`EST`, `+05:30`, and `local` are not.

Rejecting the abbreviations is deliberate: `IST` names three different
zones and `CST` names four, and a tool that picks one has answered a
different question than the one it was asked. Rejecting fixed offsets
is a narrower call — `+05:30` is unambiguous — and it is made for
consistency of the accepted set rather than for safety. It is recorded
as an open question.

`tzdata` is a declared runtime dependency, not an assumed one. A slim
container image has no `/usr/share/zoneinfo`, and the failure mode
without the package is that every name except `UTC` stops resolving in
production while every test passes on a developer machine.

An unresolvable name is `INVALID_ARGUMENTS`, not `NOT_FOUND`.
`ToolFailureKind` documents `NOT_FOUND` as "the target, not the tool",
and a timezone name is an argument rather than a target.

### The schemas

Input:

```json
{
  "type": "object",
  "properties": {
    "timezone": {
      "type": "string",
      "maxLength": 64,
      "default": "UTC",
      "description": "IANA name, e.g. UTC or America/New_York"
    }
  },
  "required": [],
  "additionalProperties": false
}
```

Output:

```json
{
  "type": "object",
  "properties": {
    "iso8601": {"type": "string"},
    "timezone": {"type": "string"},
    "utc_offset": {"type": "string"},
    "unix_seconds": {"type": "integer"},
    "weekday": {"type": "string"}
  },
  "required": ["iso8601", "timezone", "utc_offset",
               "unix_seconds", "weekday"],
  "additionalProperties": false
}
```

A representative result:

```json
{
  "iso8601": "2026-07-25T09:03:11.482913-04:00",
  "timezone": "America/New_York",
  "utc_offset": "-04:00",
  "unix_seconds": 1785157391,
  "weekday": "Saturday"
}
```

Four rules govern the rendering.

1.  **`iso8601` is RFC 3339 with microsecond precision and an explicit
    numeric offset, always.** Never `Z`, even for UTC. Both forms are
    correct and both appear in the wild; picking one makes the string
    byte-stable under a fixed clock, which is what the deterministic
    harness asserts on.
2.  **`unix_seconds` is an integer, truncated toward negative
    infinity.** It is the one field that is genuinely a number, it is
    exactly representable as a double for the next quarter of a
    million years, and a fractional epoch would reintroduce the float
    problem `math.calculate` went to some trouble to avoid. The
    microseconds are in `iso8601` for anyone who needs them.
3.  **`utc_offset` is redundant and is included anyway.** It is already
    the tail of `iso8601`, and making the model parse a string to
    answer "how far ahead is this" is how off-by-one-hour errors get
    made during a transition week.
4.  **`weekday` is the English full name.** "What day is it" is one of
    the two questions this tool exists to answer, and deriving a
    weekday from an ISO string is a calculation the model should not be
    asked to do. English rather than localized, because the field is
    for the model and the model's prompt is in English; a localized
    surface is a rendering concern.

The timezone database version is deliberately not in the output. It
would change the result bytes on a dependency update, which would break
byte-stability for a fact that belongs in a startup log line rather
than in every tool result.

### What the model reads

`content` is a single text part with the ISO string, the zone name, and
the weekday, on one line:

```text
2026-07-25T09:03:11.482913-04:00 (America/New_York, Saturday)
```

### Failures

```text
unknown_timezone
  Unknown timezone. Provide an IANA name such as UTC,
  America/New_York, or Europe/London.
```

`INVALID_ARGUMENTS`, `retryable = False`, prefixed
`tool.invalid_arguments.` like the others. It is the only failure the
tool has: the clock cannot fail, the conversion cannot fail once the
zone resolves, and the output cannot exceed its byte limit.

## The three `workspace.` tools

Section 8.2 gives the three of them nine bullets. Six are
classification and are in the table above. The other three — "Reject
absolute paths and path traversal", "Return file metadata and
checksum", and "Enforce result limits" — name three algorithms in
eleven words.

One of the three is already written, and it is the one that mattered
most. [sandbox-isolation.md](sandbox-isolation.md) specifies
`WorkspaceHandle.resolve` as a five-step containment rule, states for
each step why it rejects rather than normalizes, and declares a
property gate over it at Milestone 4 — this milestone, because these
are the tools that first call it. Nothing below restates that
algorithm. What is below is the half that document does not own: what
these three tools do with the handle, and what they hand back.

### None of the three resolves a path

All three take a `path` argument. None of them joins it, normalizes
it, compares it against a root, or opens anything. Each passes the
string it was given to `WorkspaceHandle.read`, `write`, or `listdir`
and lets the handle resolve it on the execution service's side of the
boundary, which is where [sandbox-isolation.md](sandbox-isolation.md)
puts the check so that it cannot race a filesystem the tool does not
own.

This is a prohibition rather than a convention because the alternative
is three implementations of one rule. The structural gate below
asserts it by inspection: the three modules import no `os`, `os.path`,
`pathlib`, `shutil`, or `glob`, call no `open`, and reach the
filesystem only through `ToolExecutionContext.workspace`. A traversal
test that passes because two of three tools got it right is precisely
the failure being designed out, and it is not a hypothetical — three
tools written by three people over one sprint is the ordinary case.

`WorkspaceEscape` is not caught. It propagates past the tool body and
the pipeline maps it to `ToolValidationError`, the class step 4 of the
execution pipeline already raises, so a rejected path is a failed
invocation with a validation shape rather than a tool-authored failure
that each of the three would word differently. Milestone 4's
acceptance criterion *"Path traversal is rejected"* is therefore one
behaviour with one message across all three tools; harness case 19
exercises them, and `gate.sandbox.workspace_containment` exercises the
function underneath them.

### The workspace is a cache, and `write_text` says so

[sandbox-isolation.md](sandbox-isolation.md) holds the workspace for a
worker's lease rather than for a run's logical lifetime. A run that
pauses for an approval and resumes on another worker gets an empty
one, and that document already requires `sandbox.run_command`'s
description to tell the model that files worth keeping should be
exported.

`workspace.write_text`'s description carries the same sentence, for
the same reason and with more force, because writing a file is the
operation whose entire point is that something persists. A model that
writes `notes.md`, requests an approval, and reads `notes.md` back
after the resume gets `no_such_path`, and the only place that outcome
can be prevented is the description it read before it wrote.

### Text, encoding, and what makes a file binary

All three are text tools. Two say so in their names and the third
inherits the consequence, which is that none of them is a path by
which arbitrary bytes enter or leave a workspace. Moving bytes is
`artifact.export`'s job and running code that produces them is
`sandbox.run_command`'s, both at Milestone 6, and both governed in
ways a text reader is not.

**The encoding is UTF-8 and it is not an argument.** No `encoding`
parameter, no BOM handling, no `latin-1` fallback, and no detection
library. An encoding argument makes the tool's output depend on a
guess the model made about a file it has not read, and the failure
mode of a wrong guess is not an error — it is mojibake that the model
then reasons about confidently.

**Reading decodes strictly.** A file that does not decode as UTF-8 is
`not_text`. The tool does not fall back, does not substitute
replacement characters, and does not return the prefix that decoded. A
partial decode is the worst of the three available outcomes because it
looks like content, so nothing downstream treats it as a failure.

**A NUL byte is binary even when it decodes.** U+0000 is valid UTF-8
and appears in essentially no text file, which makes its presence the
cheapest binary detector available, and it is checked on the bytes
before the decode rather than on the decoded string. A file containing
one is `not_text` with the same message as a file that fails to
decode, because the difference between them is not one the model can
act on differently.

That is the entire binary rule: NUL, then strict decode. There is no
extension list, no magic-number table, and no heuristic over the first
few kilobytes. Each of those is a second definition of "binary" that
would then have to agree with this one, and the two-part test already
rejects every file a text tool has any business refusing.

Reading is incremental, and the two checks run as the bytes arrive: an
incremental decoder that carries a partial multi-byte sequence across
chunk boundaries, and a NUL scan per chunk. Reading the whole file
into memory before checking either would make the size ceiling depend
on the check rather than the other way around.

**Writing encodes strictly and normalizes nothing.** The argument is
a JSON string, so it is already text. It is encoded UTF-8 with no BOM,
no line-ending translation, and no inserted trailing newline. A tool
that appends a newline the model did not write is a tool whose
checksum is not the checksum of what the model sent.

### The size ceiling is the pipeline's, not this document's

`workspace.read_text` declares `maximum_output_bytes` of 1,048,576 and
enforces no separate limit of its own. A file between that and the
hard ceiling is excerpted head-and-tail and artifactized by step 12 of
the execution pipeline; a file past the hard ceiling is cancelled with
`OUTPUT_TOO_LARGE` and its partial output artifactized. Both are
[tool-system.md](tool-system.md)'s rules, already written, already
gated, and already applied to every other tool.

Building a second ceiling here would produce two truncation shapes for
one problem, and the tool's would be the one without the elision
marker, the artifact reference, or the trust-envelope rule. The reader
therefore has no `too_large` reason code, which is a deliberate
absence rather than an oversight.

`workspace.write_text` bounds its input the other way, in the schema:
`content` has a `maxLength` of 1,048,576, matching the reader's
ceiling so that a write and a read of the same file are symmetric.
Over-long content is a schema failure at step 4 and never reaches the
tool.

### The checksum

Section 8.2 requires `workspace.write_text` to "Return file metadata
and checksum" and does not say of what.

**The checksum is the lowercase hex SHA-256 of the encoded bytes,
prefixed `sha256:`** — of the bytes written, not of the string
argument as the model sent it and not of the file re-read afterwards.
Three things make that the right one.

1.  **SHA-256 is already the corpus's hash.** `FileChange.sha256`, the
    artifact store's content addressing, and
    `gate.sandbox.artifact_checksum` all use it. A second algorithm
    here would be a second thing to reconcile the moment a workspace
    file becomes an artifact, which is exactly what `artifact.export`
    does at Milestone 6.
2.  **The prefix is not decoration.** A bare sixty-four-character hex
    string is indistinguishable from the next algorithm's, and the
    migration that adds a second one is the migration that discovers
    nothing recorded which was which.
3.  **Hashing the encoded bytes rather than re-reading makes the value
    independent of the filesystem.** A re-read returns the same value
    in every case that works and a confusing one in the cases that do
    not, and the cases that do not are the ones somebody will be
    debugging.

`workspace.read_text` returns the same field, computed the same way
over the bytes it read. Equality between a write's checksum and a
later read's is then a meaningful comparison, and it is the one a
model doing anything careful will make.

### Provenance, and the trust a read carries back

The classification table declares `workspace.read_text` as
`INTERNAL_TOOL` and notes that the tool will often have to lower
itself. This is that rule, stated as an algorithm, because it decides
whether the context engine wraps what comes back in an untrusted
envelope.

**Provenance is a property of the workspace, and the handle answers
it.** [sandbox-isolation.md](sandbox-isolation.md) adds one method and
one enum to `WorkspaceHandle` for this: `write` records the resolved
path as `TOOL_WRITTEN` in the same operation that writes the bytes,
and `provenance` reports it. Recording it there rather than in the
tool is what makes it survive the tool's process, and asking the port
rather than a repository is what keeps `ToolExecutionContext` free of
the database session it deliberately does not carry.

`workspace.read_text` returns `output_trust = INTERNAL_TOOL` for a
path whose provenance is `TOOL_WRITTEN` and `EXTERNAL_UNTRUSTED` for
every other value. `ToolResult.output_trust` may only lower and the
executor clamps, so the tool cannot get this wrong in the direction
that matters.

Four consequences are worth stating, because each is a place where a
reasonable implementer would otherwise decide something else.

1.  **Writes establish provenance; reads do not.** Reading a file is
    not evidence about who wrote it. If reads established it, the
    first read would launder the file for every read after it.
2.  **Provenance does not survive a resume**, because the workspace
    does not either. A run that wrote a file before an approval and
    reads it afterwards gets `no_such_path` from an empty workspace,
    not `INTERNAL_TOOL` from a record that outlived its volume.
3.  **A sandbox write never establishes it.** The enum's third value
    exists at Milestone 6 precisely so that the Milestone 6
    implementer cannot quietly decide otherwise: bytes produced by
    code we did not write are the case `EXTERNAL_UNTRUSTED` was
    defined for, and a file is not laundered by having been written
    through a handle.
4.  **A parent run's writes do not establish a child run's reads.**
    Each run answers for itself, for the same reason each run exports
    its own trajectory: "who produced these bytes" has one honest
    answer per run.

At Milestone 4 the record is nearly always empty and the answer is
nearly always `EXTERNAL_UNTRUSTED`, because no sandbox exists yet and
nothing else fills a workspace. That is the correct default, and it is
the argument for building the rule now rather than at the milestone
that makes it interesting: by Milestone 6 it is already exercised.

`workspace.list_files` returns names and metadata, never contents, and
it follows the same rule for a reason that is easy to miss. A filename
is attacker-controlled whenever the file is — an archive extracted
into a workspace can create
`URGENT_instructions_for_the_assistant.md`, and a listing that renders
that as trusted platform text is a prompt injection with a very small
payload. A listing is `INTERNAL_TOOL` only when every entry it returns
is `TOOL_WRITTEN`, and `EXTERNAL_UNTRUSTED` otherwise.

### The listing: limit, ordering, and what an entry carries

Section 8.2's "Enforce result limits" is the third algorithm.

**The limit is one thousand entries and it is a constant, not an
argument.** A limit the model sets is a limit the model raises. The
real bound is the tool's `maximum_output_bytes` of 262,144, which a
thousand entries at a hundred bytes each already approaches; the entry
cap exists so that the overflow is a clean `truncated` flag rather
than the pipeline's excerpt machinery cutting a JSON document in half.

**Ordering is bytewise ascending on the full relative path.** Not
modification time, not size, not directories first, and not locale
collation. Bytewise because it is the only order that is identical on
every host and under every locale, which is what makes a listing
reproducible in the deterministic harness and what the property gate
asserts.

**Truncation drops the tail of that order and never the middle**, so a
truncated listing is always a prefix of the untruncated one. A model
that receives a thousand entries and `truncated: true` knows exactly
which part of the workspace it has seen and can ask for a
subdirectory, which is not true of a listing sampled or elided in the
middle.

An entry carries a relative path, whether it is a file or a directory,
and a size. Nothing else.

No modification time: it is ambient state, it changes the result bytes
under a fixed clock, and it is the field most likely to be used for a
decision the model should be making from content. No checksum, because
hashing every file in a listing turns a directory read into a full
read of the directory, which is the cost the tool exists to avoid.

A symlink is neither followed nor listed. Inside a workspace it is a
second name for something the listing already contains under the real
one, and pointing outward it is an escape `resolve` rejects at read
time anyway. Resolving link targets is where containment gets hard,
and a directory listing is not the place to be doing containment.

### The schemas

`workspace.read_text` input and output:

```json
{
  "type": "object",
  "properties": {
    "path": {"type": "string", "maxLength": 4096}
  },
  "required": ["path"],
  "additionalProperties": false
}
```

```json
{
  "type": "object",
  "properties": {
    "path": {"type": "string"},
    "content": {"type": "string"},
    "byte_count": {"type": "integer"},
    "checksum": {"type": "string"}
  },
  "required": ["path", "content", "byte_count", "checksum"],
  "additionalProperties": false
}
```

`workspace.write_text` input and output:

```json
{
  "type": "object",
  "properties": {
    "path": {"type": "string", "maxLength": 4096},
    "content": {"type": "string", "maxLength": 1048576}
  },
  "required": ["path", "content"],
  "additionalProperties": false
}
```

```json
{
  "type": "object",
  "properties": {
    "path": {"type": "string"},
    "byte_count": {"type": "integer"},
    "checksum": {"type": "string"},
    "created": {"type": "boolean"}
  },
  "required": ["path", "byte_count", "checksum", "created"],
  "additionalProperties": false
}
```

`workspace.list_files` input and output:

```json
{
  "type": "object",
  "properties": {
    "path": {"type": "string", "maxLength": 4096, "default": ""},
    "recursive": {"type": "boolean", "default": false}
  },
  "required": [],
  "additionalProperties": false
}
```

```json
{
  "type": "object",
  "properties": {
    "path": {"type": "string"},
    "entries": {
      "type": "array",
      "maxItems": 1000,
      "items": {
        "type": "object",
        "properties": {
          "path": {"type": "string"},
          "kind": {"enum": ["file", "directory"]},
          "byte_count": {"type": "integer"}
        },
        "required": ["path", "kind", "byte_count"],
        "additionalProperties": false
      }
    },
    "truncated": {"type": "boolean"}
  },
  "required": ["path", "entries", "truncated"],
  "additionalProperties": false
}
```

`created` on the write result is `true` when the path did not exist
before the call. It is the one field that distinguishes two calls the
`IDEMPOTENT` classification says are otherwise interchangeable, and it
is reported rather than suppressed because a model that meant to
create and instead overwrote should be able to tell.

### Failures

Four reason codes across the three tools, each with its static
model-facing message:

```text
tool.not_found.no_such_path
  No such path in the workspace.
tool.invalid_arguments.not_text
  Not a UTF-8 text file. This tool reads text only.
tool.invalid_arguments.not_a_file
  That path is a directory. Use workspace.list_files.
tool.invalid_arguments.not_a_directory
  That path is a file. Use workspace.read_text.
```

`no_such_path` is `NOT_FOUND`, which the vocabulary documents as "the
target, not the tool", and a path is the target. It is `retryable =
False`: the workspace does not change between a call and its retry
unless something else wrote to it, and something else writing to it is
a different call rather than a retry of this one.

The other three are `INVALID_ARGUMENTS`, also non-retryable. A binary
file is arguably not an argument problem, and `OUTPUT_INVALID` is the
tempting alternative, but `OUTPUT_INVALID` is the tool blaming its own
output for the caller's choice of file. The path is the argument, and
the argument named something this tool does not read.

The two directory codes name the tool to use instead. That is the same
tradeoff `math.calculate` makes in the other direction: the message
carries the remedy and never the input, and a sibling tool's name is
remedy rather than input.

## `demo.external_write`

Section 8.2 gives it three bullets: always requires approval, records
what would have been written, does not call an actual external
service. The first belongs to
[policy-and-approvals.md](policy-and-approvals.md), which already uses
this tool in its worked denial example. The other two are the design,
and it is as small as the roster note promised.

### It has no destination

The `destination` argument is a string, and it is never resolved,
parsed, connected to, or checked against anything. It is recorded
verbatim, bounded at 256 characters by the schema, and that is the
whole of its treatment.

A demo tool that validated destinations would be a demo tool with a
URL parser in it, and a URL parser reachable by a model that has just
been told this operation requires approval is a strange thing to have
built for a fixture. The tool's purpose is to be denied and approved,
not to be correct about addresses.

### The record is the result

There is no side table. The tool returns a `structured` result, and
step 13 of the execution pipeline persists it on the
`tool_invocations` row where every other tool's result already goes.
The record is therefore durable, tenant-scoped, queryable, and covered
by the retention the invocation row already has, none of which a new
table would acquire for free.

```json
{
  "type": "object",
  "properties": {
    "destination": {"type": "string", "maxLength": 256},
    "content": {"type": "string", "maxLength": 4096}
  },
  "required": ["destination", "content"],
  "additionalProperties": false
}
```

```json
{
  "type": "object",
  "properties": {
    "recorded": {"const": true},
    "destination": {"type": "string"},
    "byte_count": {"type": "integer"},
    "checksum": {"type": "string"}
  },
  "required": ["recorded", "destination", "byte_count", "checksum"],
  "additionalProperties": false
}
```

`checksum` is the same `sha256:` form the workspace tools use, over
the UTF-8 encoding of `content`. The content itself is recorded on the
row and is not returned to the model, which already has it.

`recorded` is a constant rather than a status field. There is no
second value it could take, and a field that is always `true` is
easier to reason about than one that is a status whose failure branch
does not exist.

The result carries no timestamp. The invocation row carries the
timing, and a second one inside the payload would be a second thing to
keep consistent with it.

### Its body has no failures

The body reaches nothing that can fail: no network, no filesystem, no
clock, no parser. Two things upstream of it can still stop a call, and
neither is a body failure. Schema validation rejects an invocation that
omits `destination` or `content`, or that exceeds either length limit,
before the tool runs at all. Policy can deny it, and a denial is not a
`ToolResult` — it is a `PolicyDecision` rendered by the pipeline, which
is the whole reason this tool is the approvals fixture. Past schema
validation and policy, there is no way for the call not to succeed. A test that pauses on `demo.external_write` and
resumes is testing the approval path and nothing else, because there
is nothing else in the tool for the test to be accidentally
exercising.

## The two tools this document does not design

Each has its classification above, which is what registration validation
and the policy engine need. Both are Milestone 6; the four Milestone 4
tools this section deferred on its first pass — the three `workspace.`
ones and `demo.external_write` — are designed above. What the remaining
two still owe:

1.  **`sandbox.run_command`, at Milestone 6.** Everything about it is
    Section 28, ADR-0008, and
    [sandbox-isolation.md](sandbox-isolation.md), which gives the tool
    its full `ToolSpec`. The one thing fixed here is that its
    `output_trust` is `EXTERNAL_UNTRUSTED` and cannot be raised.
2.  **`artifact.export`, at Milestone 6.** The argument shape, the
    `ArtifactRef` it returns, the size ceiling, and the
    same-path-same-run identity that makes its `IDEMPOTENT`
    classification true.

## Registration, and the startup check

Builtins register at build time, in the composition root's freeze phase,
before any adapter that could call one exists. Registration is a pure
function of the checked-in specs: no I/O, no configuration lookup, and
no ordering dependency, which is what lets the whole of it run inside a
static test with nothing constructed.

The order is fixed and each step refuses rather than warns.

1.  **Name grammar.** Every name matches the registry pattern and is at
    most 96 characters. The longest name the corpus declares is
    `context.update_working_state`, at 28.
2.  **Reserved domains.** No builtin's domain is `mcp` or `device`.
    This is hard gate 10 in [tool-system.md](tool-system.md) and it is
    checked here because here is where a builtin is introduced.
3.  **Domain membership.** Every builtin's domain appears in the
    partition table's builtin rows. A builtin in an unlisted domain is
    a startup error rather than a new domain, because domains are how
    the reserved set is defined and an implicit one defeats it.
4.  **Uniqueness.** No two registered specs share a name.
5.  **Schema validity.** Both schemas compile under the declared JSON
    Schema dialect, contain no remote `$ref`, and set
    `additionalProperties: false` at every object level.
6.  **Field completeness.** `output_trust` is present, `risk`,
    `side_effect`, and `idempotency` are enum members rather than
    strings, and `maximum_output_bytes` is under the global ceiling.
7.  **Forced trust.** No spec whose `source` is in `{MCP, DEVICE}` or
    whose `target_kind` is `sandbox` or `device` declares
    `output_trust` above `EXTERNAL_UNTRUSTED`.

Step 7 is hard gate 2 widened, and the widening is the point.
[tool-system.md](tool-system.md) states the gate over `source`, which
catches MCP and device tools. `sandbox.run_command` has `source =
BUILTIN` and `target_kind = sandbox`, so the gate as written does not
reach it, and the thing the gate protects against — bytes produced by
code we did not write being read as trusted narration — is precisely
what a shell command returns. Stating the gate over both fields costs
one clause and closes the one case where a builtin can be a conduit.

## Hard gates

These fail the build.

1.  All seven registration steps pass for the checked-in builtin specs,
    asserted as a static test with nothing else constructed. **M1.**
2.  A test registers a builtin whose domain is `mcp` and expects a startup
    error. **M1.**
3.  `math.calculate` on `17 * 23` returns exactly `391` with
    `result_exact` true, asserted on the rendered bytes. **M1.**
4.  A property test over generated expressions asserts that every input
    either parses and evaluates within the bounds, or returns one of the
    eight reason codes — and never raises, never returns a non-`Decimal`,
    and never runs longer than the declared timeout. **M1.**
5.  `9**9**9`, `9^9^9`, a 1025-character expression, and a 33-deep nesting
    each return their specific reason code in under fifty milliseconds.
    **M1.**
6.  A differential test asserts `//` and `%` agree with Python's `int`
    operators on every combination of signs, which is the flooring
    semantics this document chose over `Decimal`'s. **M1.**
7.  `0.1 + 0.2` returns exactly `0.3`. This is the regression test for the
    entire reason the numeric type is `Decimal`. **M1.**
8.  `system.current_time` under a fixed `Clock` returns identical bytes
    across two invocations, and the module containing it does not appear
    in the ambient-time static check's allowed set. **M1.**
9.  Every `reason_code` this document declares has an entry in the
    checked-in outcome message table, and every entry's message contains
    no interpolation. **M1.**
10. The three `workspace.` modules import no `os`, `os.path`,
    `pathlib`, `shutil`, or `glob`, call no `open`, and name no
    attribute of `ToolExecutionContext` other than `workspace` when
    they touch a file. Asserted over the import graph and the call
    graph, in the same walk the import-boundary check already
    performs. **M4.**
11. `workspace.read_text` over a file whose bytes are `61 00 62`
    returns `not_text`, and over a file whose bytes are a valid UTF-8
    sequence split across the reader's chunk boundary returns the
    decoded text. The second half is the one that regresses. **M4.**
12. `workspace.write_text` called twice with identical `path` and
    `content` returns the identical `checksum` and `byte_count` both
    times, `created` true then false, and leaves one file. **M4.**
13. A property test over generated workspaces asserts that a listing
    is bytewise ascending on `path`, that a truncated listing is a
    prefix of the untruncated one, and that no entry names a symlink.
    **M4.**
14. A run writes `a.md` through `workspace.write_text` and reads both
    `a.md` and a `b.md` placed in the workspace by the fixture. The
    first read returns `output_trust = INTERNAL_TOOL`, the second
    `EXTERNAL_UNTRUSTED`, and a `list_files` that returns both is
    `EXTERNAL_UNTRUSTED`. **M4.**
15. An approved `demo.external_write` persists its record as the
    `structured` result on `tool_invocations` and writes to no other
    table, asserted by a row count over the schema before and after.
    **M4.**

## Conflicts this document resolves

1.  **Section 8.1's name list and Section 8.2's tool list disagree in
    two places.** 8.1 names seven; 8.2 specifies six, one of which
    (`demo.external_write`) is not in 8.1, and one of 8.1's
    (`artifact.export`) is specified nowhere. Resolved by reading 8.1
    as a naming convention illustrated by example rather than a
    registry manifest — the reading
    [tool-system.md](tool-system.md)'s domain partition already
    assumes, since it reserves the `demo` domain 8.1 omits. The roster
    is the union, eight tools.
2.  **`artifact.export` has no milestone.** Resolved to Milestone 6,
    with the alternatives and the reason stated rather than assumed.
3.  **Section 8.2 calls `system.current_time` deterministic, and
    nothing that reads a clock is.** Resolved by making the tool a
    pure function of the injected `Clock`, which relocates the
    determinism claim to the port where it can be true.
4.  **`Clock.now()`'s return type does not say whether it is aware.**
    Resolved: aware, UTC, asserted in the port's contract suite. The
    ambiguity was harmless until a consumer converted between zones,
    and this is that consumer.
5.  **Hard gate 1 requires `output_trust` on every registered spec and
    no builtin declares one.** Resolved by the classification table,
    which gives all eight every field the gate reads.
6.  **Hard gate 2 is stated over `source` and `sandbox.run_command`
    evades it.** Resolved by restating the gate over `target_kind` as
    well.
7.  **Hard gate 4 requires a static message per `reason_code`, and a
    static message cannot say where a syntax error was.** Resolved by
    a rule rather than an exception: the reason code carries the
    diagnosis, the message carries the remedy and the supported set,
    and neither carries the input. The table keeps its invariant.
8.  **The roster reads as the corpus's tool census and is not.** Eight
    is what this document designs; eighteen model-callable tools are
    declared at build time across the corpus, and ten of them belong
    to other specifications. Resolved by naming those ten here,
    together with the rule that keeps the roster's count correct —
    [knowledge-documents.md](knowledge-documents.md)'s, which had
    written it down in the one place a reader of the roster would not
    look.

## Decisions

1.  **The builtin roster is eight tools**, the union of Section 8.1's
    illustrative names and Section 8.2's specified set, across six
    domains, all of which the tool-system partition table already
    lists.
2.  **Classification is fixed for all eight now; behaviour is fixed for
    two.** Every `ToolSpec` field that registration validation reads or
    the 9.2 matrix is keyed by has a value in this document, including
    for tools whose design is four milestones away. A tool whose
    classification is settled cannot surprise the policy engine when it
    finally arrives.
3.  **`artifact.export` is Milestone 6**, with the model's first
    control tools, because it is the first point at which the model
    rather than the executor decides what leaves the run.
4.  **`math.calculate` uses a hand-written tokenizer and a
    precedence-climbing parser**, not an allowlist over `ast.parse`.
    An allowlist is a subtraction from a grammar that grows, Python's
    parse semantics are not the ones we want, and `ast.parse` can
    exhaust the stack before any allowlist runs.
5.  **Every value is a `decimal.Decimal` at fifty significant digits
    with `ROUND_HALF_EVEN`.** A calculator tool that reproduces binary
    floating-point surprise has given up its only advantage over the
    model's own arithmetic.
6.  **The magnitude bound is the decimal context's, not the
    evaluator's.** `Emax` and `Emin` at ten thousand with `Overflow`
    and `Underflow` trapped means there is no place in the evaluator
    that can forget to check.
7.  **`^` and `**` are the same operator.** The model writes one and a
    person writes the other, and mapping both to one node costs a line.
8.  **`//` and `%` use Python's flooring semantics, not `Decimal`'s
    truncating ones**, implemented explicitly. The caller's prior is
    Python's, and being wrong here is silently wrong.
9.  **The function set stops at what `Decimal` implements natively**,
    plus the trivial ones. There is no trigonometry at Milestone 1,
    because degrees against radians is a units decision that cannot be
    signalled in a result.
10. **`result` is a JSON string, not a JSON number.** Emitting a
    fifty-digit `Decimal` as a double would undo the reason for
    choosing `Decimal`.
11. **`result_exact` is reported from the context's `Inexact` flag.**
    It is free, and it stops fifty digits of an approximation from
    reading as an answer.
12. **Every `math.calculate` failure is `INVALID_ARGUMENTS` with
    `retryable = False`**, exactly, because the tool is a pure function
    and the identical call provably produces the identical failure.
13. **Builtin failure messages carry the supported set and never the
    input.** The reason code carries the diagnosis; `detail` carries
    the offset and the operator sees it; the model gets a static
    message that is still actionable. The message table keeps one rule
    for every tool including the hostile ones.
14. **`system.current_time` reads the injected `Clock` and nothing
    else**, which is what makes Section 8.2's determinism claim true
    and testable.
15. **`Clock.now()` returns an aware UTC `datetime`**, asserted in the
    port's contract suite. Naive would smuggle the process's local zone
    in through a port built to keep ambient state out.
16. **The `timezone` argument accepts IANA names only and defaults to
    `UTC`.** Abbreviations are ambiguous, offsets are excluded for
    consistency, and the server's local zone is not information anyone
    asked for. `tzdata` is a declared dependency.
17. **`iso8601` always carries a numeric offset, never `Z`**, so the
    string is byte-stable under a fixed clock.
18. **Builtins version independently from `1.0.0`, and a version bump
    invalidates every cached prefix.** Bumps are for schema and
    semantic changes, never for bug fixes that make a tool do what it
    already said.
19. **`allow_parallel` is true only for the four read-only builtins.**
    Serializing every writer costs nothing at Milestone 1 and removes
    an ordering bug that would first appear at Milestone 4.
20. **Registration is seven ordered refusals in the freeze phase**,
    pure and testable with nothing else constructed, and hard gate 2 is
    restated over `target_kind` so `sandbox.run_command` cannot evade
    it.
21. **No `workspace.` tool resolves a path.** All three hand the
    caller's string to `WorkspaceHandle` and let the execution service
    resolve it, which keeps one containment rule instead of three and
    keeps the check on the side of the boundary that owns the volume.
    The prohibition is structural, not advisory.
22. **`WorkspaceEscape` is not caught**, so a rejected path becomes
    `ToolValidationError` — the class step 4 of the pipeline already
    raises — and Milestone 4's *"Path traversal is rejected"* is one
    behaviour with one message across all three tools.
23. **`workspace.write_text`'s description says the workspace is a
    cache.** The model is the only party that can avoid losing a file
    to a resume, and the description is the only thing it reads before
    it writes.
24. **Encoding is UTF-8, strict, and not an argument.** A NUL byte
    anywhere in the read decides binary before decoding starts, and
    decoding runs through an incremental decoder so a character split
    across a chunk boundary is not read as a malformed one. Writing
    normalizes nothing: no newline translation, no trailing newline,
    no BOM.
25. **There is no `too_large` reason code.** A large file is a large
    result, and the pipeline's excerpt-and-artifactize step already
    owns that. A second ceiling in the tool would be a second
    truncation policy to keep in agreement with the first.
26. **The checksum is lowercase hex SHA-256 over the encoded bytes,
    prefixed `sha256:`.** The prefix makes the algorithm legible in a
    stored result, and hashing the bytes rather than the string means
    a reader and a writer agree without either knowing the other's
    string representation.
27. **Provenance lives on `WorkspaceHandle`.** `write` records
    `TOOL_WRITTEN` in the same operation that writes the bytes, and
    `provenance` reports it. A repository lookup was the alternative
    and is not available: `ToolExecutionContext` deliberately carries
    no database session.
28. **A listing is capped at 1000 entries, ordered bytewise ascending
    on the relative path, and truncated from the tail**, so a
    truncated listing is a prefix of the full one and two listings of
    an unchanged workspace are byte-identical.
29. **`workspace.list_files` is `INTERNAL_TOOL` only when every entry
    is `TOOL_WRITTEN`.** A filename is attacker-controlled whenever
    the file is, and a listing rendered as trusted platform text is a
    prompt injection with a very small payload.
30. **`demo.external_write` has no destination, no side table, and no
    failures.** Its record is the `structured` result the pipeline
    already persists on `tool_invocations`, which is what makes it a
    faithful rehearsal of the approval path without becoming a second
    thing to operate.

## Open questions for review

1.  **Whether `//` and `%` should match Python or `Decimal`.** This
    document chose Python's flooring form because the caller is a model
    that writes Python. Someone using the tool as a desk calculator
    would expect truncation. Reversal is a one-line change plus the
    differential test, and the description string.
2.  **Whether trigonometry should arrive with a units argument or with
    two families of names.** `sin(x, "degrees")` keeps the namespace
    small; `sin` and `sind` match what calculators do. Neither is
    needed until something asks for it, and guessing now fixes the
    wrong one in a `ToolSpec` version.
3.  **Whether fixed-offset timezone strings like `+05:30` should be
    accepted.** They are unambiguous, which is the argument for, and
    they make the accepted set two sets rather than one, which is the
    argument against. Cheap to add later; awkward to remove.
4.  **Whether `artifact.export` at Milestone 6 is right.** The
    alternative with the strongest case is Milestone 4, alongside the
    workspace tools it reads from. This document placed it with the
    control tools instead, on the argument that it is the model
    deciding what leaves the run.
5.  **Whether fifty significant digits is the right precision.** It is
    comfortably past anything a calculator tool is asked and it bounds
    the cost of `exp` and `ln`. Raising it is a one-line change with a
    cost nobody would notice; lowering it is not, once results have
    been rendered.
6.  **Whether the 1000-entry listing cap should be an argument.** A
    constant keeps the tool's cost bounded without the model having to
    reason about it, and a prefix is a usable answer. The argument
    against is that a model looking for one file in a large workspace
    has no way to page. Adding `limit` later is compatible; adding it
    now invents paging state nothing has asked for.
7.  **Whether provenance should descend from a parent run to a child
    run.** This document says no, on the same reasoning that keeps
    trajectory exports flat. The case for yes is that a parent that
    writes a file and delegates its analysis currently gets the
    analysis back as untrusted, which is correct in the general case
    and pedantic in that one.
8.  **Whether `demo.external_write` should be registered outside
    development.** It exists to exercise approvals, and the approval
    path is exactly the thing worth exercising in production. Leaving
    it registered means a production registry contains a tool that
    does nothing, which is either honest or confusing depending on who
    is reading the catalog.
9.  **Whether the ten non-roster tools should be classified here.**
    This document says no: a classification belongs beside the design
    that justifies it, and pulling ten rows in would make the word
    roster mean two things. The argument for yes is that registration
    validates one set and a reviewer checking the 9.2 matrix against
    reality would have one table to read rather than six. Reversal
    moves rows and changes no values.
