# ADR-0210: OpenAI opaque continuation credential boundary

- Status: Accepted by the owner on 2026-10-16
- Date: 2026-10-16
- Related: ADR-0007, ADR-0199, ADR-0205
- Detailed design: `docs/plan/model-gateway.md`

## Context

The v5 confirmation on `f837ed3c` stopped after 124 of 4,800 observations.
Content-free diagnostics identify the provider-key detector on a completed
event's OpenAI `reasoning.encrypted_content`. The original matched bytes were
not retained: this establishes the refused field, not actual credential leakage
or proof that the original match was harmless. All failed evidence and costs
remain retained. Seven original ledgers total USD 88.371792, including uncertain
reservations, under the unchanged USD 300 aggregate cap.

The owner explicitly approved the narrow OpenAI repair. This amends the
whole-event credential-shape check only as described below. It does not approve
an exemption for Anthropic or arbitrary provider payloads.

## Decision

For a completed or failed partial turn, an embedded provider-key-shaped match
may pass only in the direct string value `encrypted_content` of an OpenAI
`reasoning` item. The turn's usage provider must also be OpenAI, its metadata
must name the Responses API, and a failed event's error provider must agree.
The value must be one uninterrupted URL-safe opaque token, optionally padded;
prose, whitespace and nested values do not acquire the exception. A recognizable
key starting the value is still rejected. Every other detector family remains
active, including authorization headers, Bearer credentials and private keys.

The check uses a temporary inspection copy to omit only eligible values. It
never changes the original event, usage, checkpoint or replay bytes. All other
fields, mapping keys, unknown payload types, unknown or mismatched providers,
Anthropic payloads, text, tool arguments, summaries, metadata and errors retain
the existing check. A permitted value cannot hide a rejection elsewhere in the
same event; safe diagnostic labels describe the actual remaining refusal.

This is a deliberately bounded security tradeoff: an embedded key-like sequence
in the admitted opaque value is no longer sufficient for heuristic rejection.
The platform cannot decrypt it to establish its meaning. Opaque continuation
remains confined to the active loop's checkpoint and the originating provider's
continuation slot, with no display, content diagnostics or long-term memory.
There is no retry, byte rewriting, provider switch or change to failure policy.

## Verification and release

Deterministic regressions cover completed and partial turns, standalone secrets,
every detector family, provider/API/type mismatches, malformed and nested values,
unknown fields and keys, and a permitted collision alongside a refused field.
An actual OpenAI adapter and runtime tool loop must replay the original value
unchanged, keep it out of visible/persisted event content, and clear continuation
when the run ends. These tests make no external provider calls.

The failed v5 attempt is never resumed or reused. Final source verification,
independent inspection, current-head CodeRabbit review and ordinary hosted CI
and signing precede fresh matching native and confirmatory evidence. Every
quality gate, human review, per-answer limit and the USD 300 aggregate cap stays
unchanged; all previous costs and reservations count. This decision does not
establish answer quality, authorize a new pull request or activate any channel.
