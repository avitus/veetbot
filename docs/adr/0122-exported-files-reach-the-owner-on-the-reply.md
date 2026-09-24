# ADR-0122: Files the agent exports reach the owner on its reply

- Status: Accepted (authorized by the repository owner, 2026-09-23)
- Date: 2026-09-23
- Related: ADR-0026, ADR-0029, ADR-0047, ADR-0053, ADR-0086, ADR-0105, ADR-0120;
  Sections 8, 18.4, and 29 of the engineering plan
- Detailed design: `docs/plan/builtin-tools.md`, `docs/plan/sandbox-isolation.md`,
  `docs/plan/runtime-loop.md`, `docs/plan/http-api-and-streaming.md`,
  `docs/apple-client.md`, `docs/client.md`

## Context

On 2026-09-23 the owner asked Chat for a market update. The reply could not be
copied whole, so they asked for it as a text file. The agent answered that it had
attached `Jev-Market-Update-2026-09-23.txt`, and the owner could not find it.

The run did everything the platform offered. `workspace.write_text` wrote the text
into the run's sandbox workspace; because a tool argument counts as untrusted unless
the owner typed it verbatim, the trust overlay asked the owner to approve that write.
`artifact.export` then stored the file as a durable artifact, which still exists
and downloads correctly. Three gaps hid it:

1. `artifact.export` returned its artifact id only in `structured`, and the model
   sees only `content`, so the model saw an empty result.
2. The final assistant message is text only. Nothing tied the artifact to the
   conversation, and the transcript returns messages, not tool activity (ADR-0053).
3. The tool descriptions said the workspace "does not survive an interruption". In
   fact it is discarded when every run finishes or pauses, and nothing told the
   model that exporting is the only way a file reaches the owner.

The shipped Apple client already renders a `file` content block on any message as a
button that opens the artifact viewer with a preview and a download action, and the
thin client prints artifact references in a final message. Section 29 of the plan
already says artifacts are "files the agent produces, addressed by opaque ID so any
device can fetch them". The missing piece was putting the file on the reply.

The owner also reported that chat text cannot be selected and copied. The first
native client drew each message as one selectable `Text`; the Markdown renderer that
replaced it draws every paragraph, list item, and table cell as its own `Text`, and
SwiftUI selection never crosses from one to the next.

## Decisions

1. **The final reply carries the run's files.** When a run records its final
   assistant message, one unit of work under the run's lease lists the run's
   artifacts, keeps those with origin `sandbox_export` or `model_output` in creation
   order and once per identical name and content, appends a `file` part for each to
   the reply, and records `assistant.message.completed`. The same message object is
   the run's final message, so `run.completed` repeats identical content; a client
   that compares the two therefore shows one reply, not two. Tool-output captures
   and the owner's own uploads are never re-attached. The transcript route, the
   event stream, and the model adapters already carry `file` parts; a later model
   call sees each as a one-line marker (ADR-0120 decision 4).
2. **An attached file lives as long as its conversation.** The same unit of work
   clears the attached artifacts' expiry through a new repository operation that
   accepts only the owner's `sandbox_export` and `model_output` artifacts of that
   run and is fenced by People erasure, as knowledge retention is. This is the rule
   ADR-0120 gives a claimed upload. Deleting the conversation deletes the files, and
   email and People erasure still expire every non-upload artifact of an affected
   run. A file from a run that fails or is cancelled keeps its 30-day expiry.
3. **`artifact.export@2.0.0` also takes text.** The tool accepts either a
   non-empty `path`, exactly as before, or `content`: at most 1 MiB of UTF-8 text
   saved as `text/plain`, `text/markdown`, `text/csv`, or `application/json` under a
   bare file name. Content mode records origin `model_output`, never touches the
   workspace, and so starts no sandbox. `path` defaults to the empty string, and the
   tool itself requires exactly one of the two. A changed default is a major bump, so
   version `1.0.0` stays registered for sessions whose plan pinned it.
4. **No approval, and no policy change.** The owner decided that an attached text
   file needs no approval. The classification stays `WORKSPACE_READ` / `LOW` /
   `IDEMPOTENT`, which the trust overlay already exempts, and the defaulted path is
   admitted by the shipped `path_inside_workspace` condition while a supplied path
   is still confined. Neither policy file changes, because `policy_version` hashes
   them and released memory-formation evidence is bound to that version (ADR-0086).
5. **The model is told what happened.** Both versions return a text result naming
   the artifact id and its stored size and stating that the file is attached to the
   reply; the name and type the model chose are not echoed back as tool output. The
   workspace tools now say the workspace is discarded when the run finishes or
   pauses and that `artifact.export` gives the user a file, and the default agent
   instructions add that a file must never be called attached unless
   `artifact.export` succeeded.
6. **Every client shows the file and can copy a reply.** The Apple client shows the
   reply's file as it already would and sizes the viewer sheet like the other sheets
   on macOS. Under every finished message it adds Copy, which writes formatted text
   (RTF, semantic HTML, and plain text without Markdown symbols), and Select Text,
   which opens the message in a native selectable text view so any range can be
   copied; the owner chose formatted text over Markdown source. The thin client gains
   `/download ARTIFACT_ID [PATH]`, which amends the ADR-0047 limitation that it does
   not download artifact bytes.

## Consequences

- Chat's tool roster is unchanged, so no tool is evicted and every roster gate holds.
- The default agent's content-addressed version changes, so new conversations get the
  new instruction. An existing conversation keeps `artifact.export@1.0.0`, whose
  exports are now attached as well, until its plan rotates.
- Telegram, WhatsApp, and SMS replies stay text only (`inbound-surfaces.md`); a file
  made in such a run is visible in the app's copy of the conversation.
- A delegated child run's files are attached to the child's reply, which its parent
  receives as a tool result, not to the parent's reply.
- The run-bound writer now keeps `builtin-tools.md`'s identity rule: a run
  that writes the same origin, name, type, and content twice gets the first
  artifact back instead of a second one. The reply's own de-duplication by name
  and content still covers artifacts created before this rule.

## Alternatives considered

- **A new builtin for text files:** rejected. Production Chat already fills the
  30-definition roster (ADR-0105), so a new enabled tool would evict a calling tool,
  and a conversation's enabled tools are fixed when its agent version is.
- **A `tool_rules` entry for the text mode:** rejected; any policy edit changes
  `policy_version` and strands the released memory evidence (ADR-0086).
- **Raising the tool-definition item cap:** rejected here; `context-engine.md` and
  ADR-0105 argue against it, and this change does not need it.
- **A file reference in the tool result:** rejected; tool cards are not restored with
  the transcript, and a reference in a truncated result is labelled as its full
  content.
- **Attaching files at finalization:** rejected; `assistant.message.completed` would
  lack the file while `run.completed` carried it, and clients would draw two replies.
- **Files written inline in the reply text:** rejected; the stored reply would lose
  the file's content, and a later turn could not revise it.
