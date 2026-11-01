# ADR-0277: Chat deletes files it generated in a chat

- Status: Accepted by the repository owner on 2026-10-31
- Date: 2026-10-31
- Extends: ADR-0122 and ADR-0261
- Detailed design: `docs/plan/sandbox-isolation.md#owner-requested-deletion-adr-0277`

## Context

In a pumpkin-stencil chat on 2026-10-31, Chat produced a dozen PNG, PDF and SVG
revisions. The owner asked it to "remove all the artifacts except the initial
photo and the final png". Chat answered that no tool could delete them. The
artifact store deletes only by expiry: thirty days for unattached files, and
never for a file attached to a reply (ADR-0122), which lives as long as the
conversation. The owner wants Chat to be allowed to delete what it generated.

## Decision

1. Add `artifact.delete@1.0.0`, an always-registered, in-process builtin that
   takes one to fifty artifact ids (`artifact:` prefix optional).
2. It deletes a file only when the file belongs to the calling owner and the
   current chat and its origin is `sandbox_export` or `model_output`, the two
   origins a reply attaches (ADR-0122). Owner uploads, tool-output captures,
   knowledge sources, trajectory exports and other chats' files are never
   deleted. Ineligible ids are reported as `not_deletable`, and a call with
   nothing deletable fails with `artifact.not_deletable`.
3. Deletion sets the file's `expires_at` to now. The artifact read routes
   already answer 404 for an expired file, and the existing sweeper removes its
   bytes and then its row. No second deletion path exists. A final reply skips
   already-expired files, because attaching one clears its expiry (ADR-0122).
4. Classification: `side_effect: NONE`, `risk: MEDIUM`, `IDEMPOTENT`,
   `artifact.write`, serial, `INTERNAL_TOOL`. `NONE` needs no approval card and
   no policy-profile edit. A repeat call reports files that are already
   expired as `already_deleted`.
5. A turn that has read external content cannot delete. The tool refuses it
   with `artifact.untrusted_turn`, the guard `records.append` uses, because the
   trust overlay does not escalate `NONE`.
6. The session transcript (`GET /v1/sessions/{id}/messages`) omits an
   assistant item's file or image block whose artifact is expired or gone. The
   event log keeps its references, because it is append-only. A reply left
   with no block still counts toward its page and advances the cursor.
7. The tool is the second *unindexed deferral* after `artifact.read_capture`.
   The deferred index is held to ten free entries (ADR-0267), and the fullest
   production chat would otherwise take 41 of 50. One sentence in the default
   agent instructions names the tool instead. The owner chose this on
   2026-10-31 over raising the index cap.

## Consequences

- Chats created after deployment can delete their generated files. Existing
  chats keep their pinned roster (ADR-0123).
- Deletion cannot be undone. Bytes remain on disk until the next sweep, but no
  route serves them.
- A client that already shows a deleted file keeps its chip until it reloads
  the transcript. Opening the file then returns 404.
- The prompt grows by about 35 instruction tokens and has no new index entry.

## Alternatives considered

- **`EXTERNAL_DELETE` with an approval card** (the `records.delete`
  precedent). The owner asked for deletion to be allowed. The files are the
  chat's own regenerable output, and the untrusted-turn guard covers
  injection.
- **Hard delete of bytes and row in the tool.** That duplicates the sweeper's
  bytes-before-row ordering and its failure handling.
- **A normal index entry.** It needs the index cap raised or the headroom
  lowered, and ADR-0267 leaves both to the owner.

## Verification

- `tests/unit/test_artifact_delete.py` covers classification, eligibility by
  origin, chat and owner, the untrusted-turn refusal, repeat calls, the
  sweeper and the instruction advertisement.
- `tests/unit/test_artifact_delete_journey.py` runs two Chat turns. One
  exports two files, the next deletes one through `tool.call` with no
  approval. It then checks that the transcript and artifact read omit only the
  deleted file.
- `tests/gates/test_deferred_tools_adr0123.py` still finds ten free index
  entries.
