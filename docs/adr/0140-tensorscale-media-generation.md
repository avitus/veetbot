# ADR-0140: Image and video tools with TensorScale as the first provider

- Status: Accepted (requested by the repository owner, 2026-09-28)
- Date: 2026-09-28
- Related: ADR-0021, ADR-0026, ADR-0042, ADR-0122, ADR-0123
- Detailed design: `docs/plan/builtin-tools.md`

## Decision

Add `image.generate` and `video.generate` as a non-milestone extension to
the existing tool and artifact system. Their names and request types are
provider-neutral; a `MediaGenerationProvider` port initially uses TensorScale.
This does not authorize model routing or change any milestone gate.

Start with text prompts: SenseNova U1.5 produces PNG images and LTX-2.5 Fast
produces MP4 video with audio. The fixed endpoints and supported dimensions
come from the [TensorScale reference](https://tensorscale.io/docs.html), checked
2026-09-28. The owner's follow-up on the same date authorizes image prompting:
version 1.1.0 accepts ordered chat artifact IDs for SenseNova image edits and
LTX-2.5 first/last-frame guidance; version 1.0.0 remains for pinned chats.
Only live, caller-owned images from the same conversation (claimed uploads or
generated/exported files) are released, under `artifact.read`. A dedicated
resolver verifies checksums, type and bounded sizes before a paid request;
the adapter alone builds inline data URIs. Input bytes never enter tool
arguments, events or results. URL, audio and video references remain deferred.

`TENSORSCALE_API_KEY` enables both tools through the existing credential
resolver; no key is passed to a model, sandbox, or artifact. Both capabilities
require `media.generate` and `artifact.write`. They are external writes,
non-idempotent, serialized, and subject to the existing approval policy.
TensorScale does not document an idempotency guarantee for these endpoints:
neither the adapter nor the tool retries a request, including a timeout or
quota failure. Existing invocation replay and uncertain crash recovery apply.

Responses stream into the run-bound artifact writer under `MODEL_OUTPUT`,
with external-untrusted provenance and a finite byte limit. The existing
reply attachment and conversation retention path delivers them. The provider
never chooses a local path, endpoint, or credential reference. Errors are
mapped to fixed messages; raw provider bodies and exception text are discarded.

## Consequences

New chats with the configured credential advertise both tools; old chats
retain their pinned roster. The existing deferred index handles overflow.
Token-authenticated owners also need the new `media.generate` scope.
Generation uses the effective tool/run deadline, with no increase to other
run limits. Each request produces one file. Charges are external provider
charges, not model token usage; the approval is the spend boundary, and the
run's language-model cost ledger does not meter these charges. No dependency,
policy exception, deployment action, or new registered gate is introduced.

Deterministic provider-contract and composed tool tests cover credential
selection, request translation, limits, redaction, approval, scope denial,
artifact delivery, and replay. A real-provider smoke remains separate from
offline verification.
