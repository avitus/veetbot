# ADR-0189: Governed memory source recovery

- Status: Implementation in progress under ADR-0178; activation remains gated
- Date: 2026-10-12
- Related: ADR-0178, ADR-0183, ADR-0185
- Detailed design: `docs/plan/general-memory-recall.md`

## Decision

Supporting-source recovery starts from a currently permitted original belief. It
may read only that belief's cited owner messages, under the same owner erasure
lock and current-source transaction. It does not search unrelated history, fetch
neighboring messages or invoke formation. Historical and People-scoped recovery
remain withheld until their visibility contracts are implemented and measured.
The source session must have the current query's project scope: portability of
an atom does not make its entire source portable. Frozen snapshots, delegated
recall and callers supplying their own transaction receive ordinary facts only.

Source admission is explicit metadata established when a new authenticated native
owner message is appended. The conservative label is restricted, and the source
is eligible only for the owner's private view at that clearance. Unsupported
origins, external communications, malformed or oversized content, and detected
credential/instruction hazards receive no admission. Existing events receive no
backfill: an absent admission is unknown visibility and withholds content.
The admission binds the exact source text by digest; event edits or redaction
cannot retain permission for different bytes. Metadata never changes the event's
original payload or upgrades its trust.

A whole message can contain several facts. Any content, lifecycle, sensitivity or
attribution change to one of its formed beliefs permanently fences that source
message before the change becomes visible. The fence survives belief deletion;
new extraction, restored text and another live belief citing the same message
cannot reopen it. Usage-only updates do not change source authority. This is
intentionally conservative: detail recovery may be withheld even where a human
could safely select a smaller unaffected excerpt.
An initially inactive sibling also withholds the message. Validity closure and
expiry are checked at source-read time, including when time passes without a
maintenance write; another live atom cannot replay the expired content.

Recovery considers at most three selected originals and each original's two
latest cited events, returning at most three distinct whole excerpts and 8,192
UTF-8 bytes within 250 milliseconds. It consumes the same final context budget
as belief recall, without evicting selected facts. A failed or timed-out source
read rolls back its transaction before one ordinary-recall retry; cancellation
propagates without retry. Each returned excerpt identifies its
current supporting belief and exact source session/event/time. It is quoted at
memory trust, never treated as a user instruction. Traces and erasure dependencies
must retain the source lineage of any rendered excerpt; source recovery cannot
activate until those generated-copy paths are covered. No source text is copied
into the semantic index or a persistent query cache.
The recalled item inherits restricted sensitivity when it carries a quote. Its
opaque copy key follows source-level belief erasure even when the erased fact
was formed after that quote; its People source key follows source erasure.

The complete source policy, bounds, rendering and configuration belong to the
release identity. The optional channel stays disabled unless its ablation shows
benefit within the approved privacy, quality and operational gates. A permit or
source index alone cannot activate recovery.
`retrieval.source_recovery_enabled` defaults to false. Production composition
requires the same release-bound local query service and reviewed actual reader;
an unavailable runtime binding withholds quotes. Admission is checked both before
and after source reads, and withdrawal discards the collected excerpts.

## Consequences

Old conversations continue to supply their permitted atomic memories, but do not
gain raw-message access merely because a recall query failed. This avoids treating
missing provenance as permission and avoids replaying a deleted fact from a
neighboring live fact's source. Source metadata and fences must be implemented in
both persistence adapters and verified against concurrent changes and erasure.

This decision does not expand the approved formation or People semantics. No M33
gate is complete until the implementation and its required evidence are present.
