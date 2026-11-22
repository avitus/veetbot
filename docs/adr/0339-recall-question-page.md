# ADR-0339: The owner keeps the recall question set on a signed-in page, stored in the database

- Status: Proposed — owner requested on 2026-11-20
- Date: 2026-11-20
- Amends: ADR-0338 (decision 7)
- Related: ADR-0174, ADR-0186, ADR-0239
- Detailed design: `docs/plan/memory-evaluation-and-lifecycle.md`, `docs/plan/http-api-and-streaming.md`

## Context

ADR-0338 made an owner question set the standing measure for a recall change
and kept it as two files on the operator host, outside Git, changed only by
owner review under a new version number. The same day the owner asked for a
page on the server where the questions live, to keep the set current as
Veetbot evolves.

Files do not serve that. Changing one needs root on the host, nothing records
what a question said before, and nothing shows which questions the last run
missed. The first version already needed two corrections within an hour of
being placed.

## Decision

1. **The database is the home.** Two owner-scoped tables under forced row
   security hold the set: `recall_questions`, one row per question, and
   `recall_question_revisions`, every state a question has held. The probe
   file format remains as the import and export format. This replaces
   ADR-0338's version-number rule: history keeps every state, and a run's
   report carries a digest of the exact set it ran.
2. **The half is chosen once.** A question joins the development or the
   held-out half when it is created. No route, import or store write moves it,
   and an import refuses a key that exists in the other half.
3. **A write names the revision it read.** A stale write is a conflict.
   Repeating a write that already took effect returns the stored question, so
   a retried request is safe.
4. **Retire, never delete.** A retired question keeps its history and is left
   out of runs until restored. The store offers a principal-wide erase, as the
   owner-records store does; no flow composes it yet.
5. **The API serves the page**, at `/recall-questions`, the way it serves the
   dreaming review page: a content-free shell with a strict content security
   policy that renders every owner text as text. The page holds the owner's
   access token in tab memory only, so a reload asks for it again. The
   purpose-bound review cookie of ADR-0186 is not extended to it.
6. **Existing scopes, one flag.** Reads need `memory.read` and writes
   `memory.write`. The routes and the page exist only when
   `AGENT_RECALL_QUESTIONS_ENABLED=1`; the default is off.
7. **A fact is named by text or by memory.** An expected fact is text a stored
   memory contains, or one pinned memory. The page finds memories through the
   existing memory read API when that is enabled.
8. **The probe tool runs the stored set.** `agent memory probe --questions
   development|holdout|all` loads active questions from the database it runs
   against. That is still a clone and never production, so it runs the set as
   it stood when the clone was taken. `agent memory questions record --report`
   then writes each question's outcome to production as its latest result:
   stages and counts, no text. A result is not an edit and moves no revision.
   **A result is bound to the content it tested.** Each run carries a
   fingerprint of what the question measured when it ran (everything but its
   notes), computed from the probe that ran. Recording refuses a result whose
   fingerprint is not the question's current one and counts it as stale, and an
   edit that changes what a question measures clears its recorded result; a
   note or a status change leaves it, including a result recorded while that
   edit was open: recording moves no revision, so a save names the result only
   to clear it. The owner may edit a question between the clone and the
   recording, and the page must never show a pass beside content that has not
   run.
9. **A question is owner-written test content.** It may quote a memory's text.
   Forgetting or erasing that memory does not rewrite the question; the owner
   edits or retires it, and a pinned memory that no longer exists reports as
   not stored.

## Consequences

- The owner changes the set from any browser, sees its history, and sees which
  questions the latest run passed or missed.
- The held-out half is protected by construction rather than by convention.
- Each page load needs the access token pasted again. That is the cost of not
  adding a second cookie authority.
- Question rows hold owner facts outside the memory store. Memory erasure does
  not reach them; this is stated here rather than hidden, and the page is the
  place to correct it.
- An export holds the same facts. It is written to a new private file that
  then takes the destination's place, so a file that already existed with
  wider permissions does not keep them.
- The schema gains a migration, so rollback across it is a schema boundary.

## Not decided here

Questions proposed by Veetbot when the owner corrects a missed recall in chat,
a persistent sign-in for the page, and deleting a question outright.
