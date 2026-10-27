# ADR-0261: Read a long tool result in sections

- Status: Proposed. Awaiting the owner's approval of the design; nothing is
  implemented.
- Date: 2026-10-27
- Amends: ADR-0260 decision 6 (the capture marker), ADR-0123 (how a deferred
  tool is advertised), and ADR-0137's single admission ceiling (one tool is
  admitted at its own limit)
- Related: ADR-0136, ADR-0233, ADR-0242, ADR-0253
- Scope: one builtin tool, the capture marker, a mark on capture rows and the
  default agent's roster; no gate, milestone, policy profile or compiled
  `policy_version` changes
- Detailed design: `docs/plan/tool-system.md`, `docs/plan/context-engine.md`,
  `docs/plan/builtin-tools.md`, `docs/plan/web-access.md`,
  `docs/plan/sandbox-isolation.md`

## Context

On 2026-10-27 the owner asked Chat to find and analyze Anthropic's S-1.
`web.fetch` acquired about 524 KB of the filing. The tool pipeline admits at
most 4,096 bytes of a result to the model, so the model saw about 3 KB of head
and tail. The whole result went to a capture: a tool-output artifact that
carries the result's trust label, belongs to the tenant and the chat, and is
kept for 30 days. The model hunted for the capture with three sandbox
commands, each of which needed an approval, and then built its financial
summary from search snippets and analysts' write-ups instead of the filing.

ADR-0260 made the marker say that nothing can open the capture, so the hunt
stops. The model still cannot read the filing. That ADR deferred reading a
capture in sections to a design of its own, and this is that design.

Four constraints shape it.

1. **Trust.** Every byte of a fetched page is `external_untrusted`, and the
   tool pipeline never summarizes untrusted output (tool-system decision 10).
   Whatever reads the capture must return the same bytes under the same label.
2. **Policy.** On 2026-10-27 the owner declined two changes: letting sandbox
   commands run without asking after web reads, and a model-written purpose
   line on approval cards. A reader must need neither. After a read, the trust
   overlay must still escalate code, writes and messages. Editing the policy
   YAML would also change the compiled `policy_version`, which
   memory-formation evidence is bound to.
3. **The prompt cache.** ADR-0242 quantized tool-result truncation, so the
   truncated prefix moves at most once per quarter of the tool-result budget.
   A reader that adds large results must keep that property.
4. **The roster.** With every production flag on and a website bound, the tool
   definitions were three tokens under their 9,000-token cap when last
   measured (2026-10-26), and the deferred index is at its 2,000-token cap,
   which already leaves two Gmail writes unoffered (ADR-0253). A new definition
   or index entry would push out another tool.

## Decision

1. **`artifact.read_capture` reads a capture in sections.** It is a new
   builtin in the existing `artifact` domain and takes three arguments.
   `artifact` is the capture's ID, in the form the marker or a truncation
   pointer prints (`artifact:<uuid>`) or as the bare UUID. `section` is a
   whole number from 1, with 1 as the default. `find` is 1 to 200 characters.
   Without `find`, the tool returns that section. With `find`, it searches
   from that section onward. A section past the end returns
   `tool.capture.section_out_of_range`.
2. **The capture alone fixes the sections.** A capture's text is its text
   parts in order, joined by a blank line. A partial capture, cut at the hard
   ceiling and therefore not valid JSON, is read as UTF-8 with invalid bytes
   replaced. Section 1 starts at the beginning of the text. Each section is
   the longest run of whole lines whose rendered result, header included,
   fits 16 KiB; a line longer than a section is cut at a character boundary.
   The next section starts where the previous one ends. The header is one
   line: `artifact:<id> section <n> of <total>, from <tool>`. The same capture
   and arguments therefore always return the same bytes, so a retried or
   replayed read is identical and the identical-call guard (ADR-0136) counts a
   repeated read as a repeat.
3. **Find is a literal search.** Matching is case-insensitive under Unicode
   case folding, with no patterns. The result gives the total number of
   matches and the first 20 from the starting section. Each match shows its
   section number and up to 160 characters around it, with whitespace
   collapsed to single spaces. A search result fits the same 16 KiB.
4. **Only this chat's untrusted captures open.** The reader opens an artifact
   only when all of the following hold:
   - the tool pipeline wrote it as a capture, which a mark on its row records
     along with the producing tool and the scopes that tool required;
   - it belongs to the run's tenant, principal and chat;
   - it has not expired;
   - it is labeled `external_untrusted`;
   - the run's principal still holds the recorded scopes.

   Anything else returns the one refusal `tool.capture.not_found`: another
   chat's capture, an export, an upload, a chart, a capture of a trusted tool,
   a capture made before this change, or a capture whose tool's access was
   revoked. Because the refusal is the same in every case, the model cannot
   learn whether an ID exists elsewhere. Deleting a chat deletes its captures,
   and an expired capture is not found.
5. **Every section is `external_untrusted`, and policy is unchanged.** The
   tool declares `side_effect: NONE`, `LOW` risk, `READ_ONLY`, an `in_process`
   target, no required scopes, `external_untrusted` output trust, no parallel
   execution, a 10-second timeout and 16 KiB of output. The matrix row `none:
   allow` admits it, and the trust overlay never escalates a read. Reading
   therefore needs no approval and no policy edit, and `policy_version` does
   not change. A section enters context inside the same untrusted envelope as
   its fetch did, so a later sandbox command, workspace write or message still
   asks, exactly as it would after the fetch. A capture of a trusted tool stays
   closed rather than being read back under a second label.
6. **A section is admitted whole.** Every other tool's model-visible ceiling
   is the 4,096-byte inline limit. This tool's ceiling is its own 16 KiB,
   because a section is already a bounded read of a capture, and excerpting it
   again would capture a capture. When a section over the inline limit is
   carried into a later run, it gets the head-and-tail excerpt that every such
   carried result gets (ADR-0137). Its header line survives in the head and
   names the section to read again.
7. **Two reads a step, sixteen a run.** A third read in one step is refused
   with `tool.capture.step_limit`, and a seventeenth in one run with
   `tool.capture.run_limit`. Both are refused before anything is read, and
   refused calls do not count. A search counts as a read. Both limits are
   operator configuration in `tools/limits.yaml`. The count comes from the
   run's completed reads, so a resumed run counts what it had already read.
8. **The reader costs no prompt tokens.** The default agent enables and defers
   it. The planner pins it as the one *unindexed* deferral: it is pinned and
   reachable through `tool.call` like any deferred tool, but it has no index
   entry and uses none of the index's forty entries or 2,000 tokens. The
   capture marker advertises it by printing the exact call. A plan without
   `tool.call` records it in `skipped_tool_names`. The description of
   `tool.call` still says it calls index tools, because rewording it would
   change every plan's prefix.
9. **The marker points to the reader.** When the run pins the reader and the
   reader can open the capture, the marker reads:

   ```text
   [... 520,402 bytes elided; full output: artifact:<id>, 33 sections; read one with tool.call {"name":"artifact.read_capture","arguments":{"artifact":"<id>","section":1}}, or pass "find" to search it ...]
   ```

   Otherwise the marker is ADR-0260's, unchanged. The excerpt fits the same
   inline budget, and the longer marker costs about 200 bytes of it.
   Truncation pointers keep their bytes (`full content at artifact:<id>`). The
   reader accepts that form, so the model can follow a pointer to a readable
   capture, and no in-flight request changes at deploy.

## Consequences

- The filing becomes readable. At 16 KiB a section, the 524 KB S-1 is about
  33 sections. A search for "Results of Operations" or "Total revenue" names
  the sections to read, and four to six reads cover the financial statements.
  A run's sixteen reads cover about half the filing.
- Cost (estimates). The production model has a 272k-token window and an
  8,192-token output reserve, so the tool-result budget is about 57,000 tokens
  and an ADR-0242 block about 14,000. Two sections come to about 8,000 to
  11,000 tokens, so a step of reads moves the truncated prefix at most once. A
  run that spends all sixteen reads costs roughly USD 3 to 4 at gpt-6-astra
  prices, mostly in uncached input after truncation moves. A targeted search
  and four reads costs well under USD 1.
- `memory.learn` is unchanged. The reader is not an admitted source read
  (ADR-0233). A quote found in a section cites the original `web.fetch` call,
  whose canonical content holds the whole result whenever the result fit the
  tool's own limit; for `web.fetch` it always does. A browser result over its
  512 KiB limit keeps only its excerpt canonically, so a quote from beyond the
  excerpt still cannot be learned, as today.
- The default agent's version changes, so chats started after the deploy get
  the reader. Older chats keep ADR-0260's marker until their plan rotates, and
  schedules stay pinned to their agent version until someone re-pins them.
  Captures written before the deploy stay closed.
- The builtin census gains one tool. The four new reason codes need fixed
  messages in the checked-in table.
- PR #204 (truncation recovery, numbered ADR-0258 on its branch) adds a note
  saying that no tool reads a truncated pointer back. Whichever of the two
  changes lands second makes that note except readable captures.

## Alternatives

- **A `section` or `offset` argument on `web.fetch`.** It would fetch the page
  again and pay the provider again, the page could change between reads, and
  it would cover only `web.fetch`. It would also add tokens to a defined tool
  where the website-bound roster has none to spare, and bump a pinned tool
  version.
- **An ordinary deferral with an index entry.** It would cost about 25
  tokens. In a website-bound chat with every flag on, it would leave one more
  Gmail write unoffered.
- **A definition.** It would take one of the thirty slots and about 150 of
  the 9,000 tokens.
- **A higher inline limit.** Every large result in every chat would pay for
  it, not only the ones the model needs, and the admission budget from
  ADR-0137 would stop bounding history.
- **Copying the capture into the workspace or sandbox.** By design, sandbox
  commands ask after web reads, and the owner declined to loosen that.
- **A summary by a model or a child run.** Tool-system decision 10 forbids
  summarizing untrusted output into trusted-looking prose.
- **Ingesting the page as a knowledge document.** That would store an
  untrusted page under knowledge trust permanently. Owner-directed ingestion
  remains a separate decision.
- **Byte offsets.** Overlapping windows would make every read look new to the
  identical-call guard. They would also break the fixed numbering that the
  marker, the search and a re-read share.
- **Opening captures of every label.** Reading back a trusted capture would
  need a second label rule. The alternative, relabeling it down, would make
  later commands ask for an "outside content" reason that is false.

## Validation

Red tests precede each change:

- **Sections.** A property test over multibyte text, lines longer than a
  section and escape-heavy text checks that boundaries cover the text exactly
  once, fall at line ends and render within 16 KiB. The marker's count equals
  the reader's, and a partial capture reads.
- **The reader.** Both result shapes and the header; case-folded search, its
  20-match cap and total; a section past the end. `tool.capture.not_found` for
  another chat, principal and tenant, an expired capture, an unmarked capture,
  a chart, an export, an upload, a trusted capture and a revoked scope. The
  step and run limits, refused calls not counting, and a resumed run counting
  earlier reads.
- **The executor.** The marker names the reader exactly when the run pins it
  and the capture opens, and otherwise is ADR-0260's; both fit the inline
  budget. The capture mark is written. A 16 KiB section is admitted whole and
  is not captured again.
- **Policy.** A read after a web fetch is allowed without approval. A sandbox
  command after a read still records `untrusted_escalation`. The compiled
  `policy_version` is unchanged.
- **Context.** The planner pins the reader with no index entry. With every
  production flag on and a website bound, the rendered prefix, the
  definitions, the index and `skipped_tool_names` are the same with and
  without it. Without `tool.call` it is skipped. Truncation pointer bytes are
  unchanged, and a carried section is excerpted.
- **A gate, `tests/gates/test_capture_reader_adr0261.py`.** A composed run
  fetches a 520 KB fixture through the fake web provider, searches it and
  reads two sections in one step; a third read in that step is refused. The
  next request carries both sections inside untrusted envelopes, and a
  following sandbox command asks.

`make check` on the sidecar gates delivery. Production acceptance: a new Chat
asked to analyze a long filing searches and reads the fetched filing, cites
figures from it and raises no approval to read it, and its plan's
`prefix_tokens` equals that of a plan built without the reader on the same
roster.
