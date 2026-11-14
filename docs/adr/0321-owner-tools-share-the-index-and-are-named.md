# ADR-0321: Owner-added servers share the index in turn, and the rest are named

- Status: Accepted by owner request on 2026-11-13
- Date: 2026-11-13
- Related: ADR-0123, ADR-0255, ADR-0258, ADR-0291
- Amends: ADR-0255 decision 8, the owner-added tools' index budget
- Detailed design: `docs/plan/context-engine.md`

## Context

On 2026-11-13 the owner asked Chat to design an infographic with MagicPath.
MagicPath was signed in and ready, but the chat listed only
`mcp.magicpath.read_resource`, and its call to
`mcp.magicpath.start_component_code_session` was refused with
`tool.not_found.not_offered`.

ADR-0255 gave owner-added servers one index class, `owner_tool_index`, forty
entries and 2,000 tokens, filled in the shared reads-first order: reads
before writes, then by server, then by name. The owner's Scale VP server,
added on 2026-11-03, is read-only, so every one of its tools ranked ahead of
every MagicPath tool. MagicPath's access is `changes`, which classifies all
its tools as writes, so only the platform's `read_resource` entry ranked with
the reads. Scale VP's reads filled the class and the rest of MagicPath was
recorded as skipped.

The class could not have held MagicPath on its own either. A MagicPath entry
measures roughly sixty to one hundred and ten tokens (an estimate from the
conservative three-bytes-per-token estimator), so about thirty of its
forty-two tools fit in 2,000 tokens. In name order, the cut falls before
`start_component_code_session` and `submit_component_code`, the two calls
that create and save a design.

## Decision

1. **Servers take entries in turn.** Owner-added tools are grouped by server,
   servers in name order, each keeping the reads-first order within it. Each
   round offers every server's next tool to `owner_tool_index`. A tool that
   does not fit gives up that turn and the server's next tool may still fit.
   No server's reads can fill the class before another server's first tool.

2. **What misses an entry is named.** A second Region A class,
   `owner_tool_names` (three hundred names, 1,500 tokens), lists the
   remaining tools by name, one line per server:
   `- mcp.magicpath: start_component_code_session, submit_component_code, …`.
   The lines follow a heading in the discovered tools' untrusted envelope,
   because the names come from the server. The heading says a named tool is
   called through `tool.call` as the server prefix, a dot and the name, and
   that an invalid call returns its definition (ADR-0258). Names are admitted
   in the same turns. Only what fits neither class is recorded in
   `skipped_tool_names`.

3. **A named tool is pinned and deferred like an indexed one.** It is in
   `deferred_tool_names` and `deferred_tool_specs`, so resolution,
   validation, policy, approval and the definition-on-invalid-call answer
   are unchanged. The plan records the named ones in a new field,
   `listed_tool_names`, a subset of `deferred_tool_names`, and the builder
   renders the prefix from it.

4. **Region A's ceiling rises from 25,000 to 26,500 tokens**, exactly the new
   class's cap, as ADR-0079 requires.

5. **Earlier plans are unchanged.** A plan without listed names renders
   byte-for-byte as before and keeps its prefix hash, so no chat is
   re-planned and the builder version is unchanged. The new order reaches
   chats created after deployment, as roster changes always have.

## Consequences

MagicPath's create and save calls are callable in a chat beside Scale VP, and
each server keeps full entries for its first tools. In the regression case
(MagicPath's forty-two tools beside a hundred read-only ones), each server
holds eleven or twelve entries, the other 119 tools are named in 988 tokens,
and no owner tool is skipped. A chat with owner-added servers carries up to
1,500 more prefix tokens, almost all of them cached.

A named tool shows no description or parameters. The model learns its
definition from a call with invalid arguments. A named tool whose schema has
no required parameter and accepts the model's guess runs without that step.
For a server with `changes` access, that call still waits for the owner's
approval. For a read-only server it is a read.

## Alternatives considered

- **Turns only.** No cap changes, but MagicPath's create and save calls sort
  past any share of 2,000 tokens it would receive.
- **Callable by name, unlisted.** No added tokens, but the model would have
  to know each name already. On 2026-11-13 it knew one, probably from
  MagicPath's guide, and no others.
- **Raise `owner_tool_index`.** Full entries for every tool would cost about
  five times as much per tool as a name, and would still need a cap.
