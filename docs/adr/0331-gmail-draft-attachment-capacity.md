# ADR-0331: Match draft attachment capacity to Gmail

- Status: Accepted — owner approved the bounded limit change on 2026-11-18.
- Date: 2026-11-18
- Amends: ADR-0317's attachment and encoded-message byte ceilings.
- Detailed design: `docs/plan/email-experience.md`, `docs/plan/email-integration.md`.

## Context

Research correctly selected the requested source documents, but their aggregate
size exceeded the platform's 15 MiB decoded-file cap. No file was staged and no
draft was saved. Raising that cap alone would still fail the separate 24 MiB
outer-base64 admission check. The original limits are platform choices, not
Gmail's documented attachment capacity. Private correspondence and file contents
remain outside this decision and its fabricated regression fixtures.

Google documents [25 MB of attachments](https://support.google.com/mail/answer/6584).
The [Gmail discovery document](https://gmail.googleapis.com/$discovery/rest?version=v1)
sets the `users.messages.send` media-upload maximum to 36,700,160 bytes (35 MiB).
That upload limit applies to MIME bytes; JSON's outer base64url is a separate
encoding layer.

## Decision

Allow at most ten files totaling 25,000,000 decoded bytes. Keep a separate
35 MiB MIME ceiling and bound the outer base64url representation to the padded
base64 size of that ceiling. Preparation conservatively reserves MIME envelope,
header and text overhead before downloading files. The Gmail adapter checks the
actual serialized MIME bytes before dispatch. Both checks remain required.

Align the immutable manifest, private staging decoder, application-only send
schema and Gmail decoder with the decoded-file ceiling. The first-party Gmail
package remains import-isolated from the application; regression coverage binds
their limits together. Existing 32 MiB source-fetch bounds do not change.

Source selection, source freshness, file inspection, ownership, retention,
immutable digests, exact-file owner consent, read budgets and non-idempotent send
handling remain unchanged. Files are neither recompressed nor dropped to fit.
An over-limit set still reports a preparation issue before downloading any file.
No automatic send, Drive upload, shared link or new permission is introduced.

## Verification and delivery

Require fabricated large-file preparation and MIME round-trip regressions,
25 MB boundary admission and over-limit refusal, matched platform/provider
schema limits, unchanged digest validation, and existing staging/send/erasure
coverage in the isolated Linux sandbox. Run the affected partition, lint,
typing, citation repair and documentation checks before the authorized `dev`
push. Production deployment and regeneration of a live draft remain separate.
No milestone or acceptance gate moves.

The initial regression run used the prescribed Linux container with
`UV_NO_SYNC=1` and `PYTHONPATH` pointing at this checkout's `src` and root because
the guest could not resolve PyPI. The installed CircleCI testsuite extension ran
`run static --run-tests=all -t
'^tests/unit/test_email_(attachment_limits|research_budget).py$'`.
Both selected atoms ran: eight cases passed and four failed as expected in
43.29 seconds. The failures demonstrate the 15 MiB manifest/schema ceiling,
rejection of 20 MB and 25 MB transport admission, and refusal to prepare the
fabricated large document set. The owner subsequently approved the limit change;
green verification follows below.

The three focused files (`test_email_attachment_limits.py`,
`test_email_research_budget.py`, and `test_email_researched_replies.py`) passed
all 63 cases in 65.83 seconds. These cover a 20 MB two-document proposal, a
24 MB single-file staging operation, a 25 MB MIME round trip, single-file and
aggregate overflow, actual MIME/outer-base64 boundaries, and the existing
consent, integrity, erasure and read-budget behavior. The former 14 MiB refusal
case now exercises the approved 25 MB decoded limit; separate tests retain
encoded-message refusal coverage.

The full static partition selected and ran all 612 atoms: 10,570 cases passed,
three existing permission checks skipped, and five real-stdio cases failed
because the offline sandbox had not installed the project. Those subprocesses
correctly discard `PYTHONPATH`, so they could not import the local MCP packages.
Installing the checkout as an editable package inside that same isolated
container, using offline build dependencies, repaired the environment without
changing application code or test assertions. All five affected files then
passed: 111 cases, with two of the same permission skips. The full sweep took
1,131.50 seconds; the repair rerun took 38.69 seconds.

The full contract partition selected and ran all 201 atoms and passed 2,309
cases in 172.08 seconds. Lint, full typing (1,853 source files), citation repair
and strict documentation checks passed. Citation changes only repair shifted
references. The static and contract sweeps overlapped; the static sweep was the
slowest check. No native surface, persistence schema or dependency lock changed.
Reading lane A is also the diff-derived minimum. This is the owner-approved
ADR-0317 ceiling amendment, with no other engineering-plan deviation. Production
deployment, regeneration of a live draft and live sending were not performed.
