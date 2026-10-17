# ADR-0210: OpenAI opaque continuation credential boundary

- Status: Accepted by the owner on 2026-10-16
- Date: 2026-10-16
- Related: ADR-0007, ADR-0205
- Detailed design: `docs/plan/model-gateway.md`

## Context

A normalized OpenAI completion can contain a provider-key-shaped sequence in
its opaque `reasoning.encrypted_content`. The shared whole-event detector then
rejects the completion and fails the run. Content-free diagnostics identified
this field in an observed failure; the original bytes were not retained, so
this does not prove actual credential leakage or a harmless false positive.

The owner explicitly approved the narrow OpenAI repair on 2026-10-16 and
separated existing-app reliability into its own branch on 2026-10-17. This
amends the credential-shape check only as described below. It does not approve
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

The independent app repair requires current-source verification, privacy and
failure regressions, CodeRabbit review and hosted CI. It adds no retry, paid
evaluation, memory feature or activation. The memory retrieval workstream's
measurements, failed originals and release gates remain independent and unchanged.
