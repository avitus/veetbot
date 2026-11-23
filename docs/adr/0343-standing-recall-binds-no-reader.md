# ADR-0343: The standing recall admission binds no reader, and a question run is labelled as the chat model

- Status: Accepted (the owner approved relaxing the reader binding on
  2026-11-22, answering "Fix the code so a model change can't switch semantic
  search off again?" with "Yes, fix it")
- Date: 2026-11-22
- Amends: ADR-0188 (the reader binding of the local query authority), ADR-0290
  (decision 1, which this implements as written), `docs/plan/bootstrap-and-composition.md`
- Related: ADR-0178, ADR-0199, ADR-0223, ADR-0269, ADR-0338, ADR-0339
- Detailed design: `docs/plan/general-memory-recall.md`,
  `docs/plan/memory-evaluation-and-lifecycle.md`

## Context

Semantic recall stopped in the owner's chats when the release of 2026-11-21
went live at 6:03 PM Pacific, and nothing said so. A read of production the
same evening found one owner chat since the release with seven recalls, none
of them semantic.

The cause is a binding ADR-0290 said it had removed. A run's query authority
(`LocalQueryAuthority`, ADR-0188) admitted the run only when the reader it
selected equalled the one `reader_sha256` written in the server's
`memory/semantic-index.yaml`. That fingerprint covers the resolved chat model,
the version of the model registry, the reasoning effort and the summary mode.
The release added GPT-6.2 Astra to the model list, which changed the registry
version and so the fingerprint of every model. The owner then chose the new
model, which alone would have had the same effect. There was no error and no
log line: the run fell back to sparse recall.

ADR-0290 decided that the owner's standing admission "survives releases,
policy edits, and reader or formation changes by design", because it carries
a decision and not quality evidence measured under one configuration. The
binder honours that: it enforces only the encoder tuple. Composition did not.
It still required a reviewed reader in the profile and handed it to the query
authority, so the admission stood while every run was withheld.

The loss is measurable. On the owner's stored question set, run on a clone of
production that evening with no model calls, 81 of 114 questions passed with
semantic recall and 72 without.

The measurement exposed a second fault. `agent memory probe` runs a scripted
model, and a scripted composition is on the fake model. Its reader never
matches a reviewed one, so the documented question run measured sparse recall
only, and the fake model's limits are not a chat model's, so the budget it
packed memories into was not a chat's either. The 81 came from a scratch
harness that labelled the scripted run as the chat model.

## Decision

1. **Under the standing admission the query authority binds no reader.** The
   standing binder returns its own marker, `StandingAdmission`, a
   `BoundedAdmission` that keeps every rule of that marker. Composition gives
   the query authority no reader when it holds this marker. A chat model, a
   reasoning effort or a model registry change then withholds nothing.
2. **A reader is neither required nor read under the standing admission.** A
   profile with no `reader_sha256` composes, and `agent memory
   standing-admission` no longer refuses one. A profile that still carries the
   line composes too and the line is ignored, so the file installed in
   production needs no edit.
3. **Evidence-bound admissions keep the reader binding unchanged.** The release
   evidence route and the bounded routes (ADR-0220 through ADR-0246) certify a
   measurement made with one reader. They still require the reviewed reader in
   the profile, and a run on any other reader is still withheld.
4. **A run withheld for its reader says so.** When a run is eligible in every
   other respect and is withheld only because its reader is not the reviewed
   one, the authority logs `semantic_recall_withheld_reader_changed` with the
   two fingerprints, once for each reader it sees, for at most sixteen readers
   in a process. A run withheld for any other reason logs nothing, as before.
5. **The authority revision names the difference.** A bound authority keeps
   `local-query-authority@1` over its reviewed reader. An authority bound to no
   reader uses `local-query-authority@2` over the fixed word `any-reader`, so
   a model change does not change what a prepared query is bound to.
6. **A scripted question run is labelled as the chat model.** `agent memory
   probe` routes its scripted runs to a chat model policy the way a chat is
   routed: `--model-policy NAME`, and the default chat model when it is
   omitted. The composition is built in metadata-only mode, so the model's own
   adapter is never constructed, and the scripted provider is installed in its
   place. The run takes that model's limits, provider pin and reader; no model
   is called and no provider key is needed. `--live-model` is unchanged apart
   from accepting the same option.

## What is not weakened

The reader binding protected a quality claim, not an access boundary. Evidence
measured with one reader says nothing about another, so an admission that rests
on evidence must not carry over. The standing admission rests on the owner's
decision and makes no such claim.

Everything that decides who may read what still holds on both routes: the
actual owner and tenant, the run, its session and checkpoint, the run's scopes,
the owner's seed message, the live paired surface for a direct message, and
interactive owner chats only. The query is embedded by the local encoder on
the host and nothing leaves it. Sensitivity, visibility, erasure, correction
and the indexing ceilings are untouched. The memories semantic recall returns
are ones sparse recall already delivers to whichever model the owner chose;
this changes which are found, not who may see them.

## Deviation from the plan

`docs/plan/bootstrap-and-composition.md` required an explicit reviewed
`reader_sha256` whenever `retrieval.semantic_enabled` is on, and a match with
the run's selected reader. That now holds for evidence-bound admissions only.
The owner approved the change explicitly. No Milestone 33 gate moves, and the
failed comparison that ADR-0290 records is not relabelled.

## Consequences

- After this is deployed, semantic recall returns in the owner's chats on any
  chat model, with no change on the server. Chats that keep an earlier model
  regain it as well.
- A later model-list change, model choice or effort change no longer turns
  semantic recall off.
- A future trial still binds one reader. If the reader changes during it, the
  run workers' journal now names the cause.
- The `reader_sha256` line in the server's semantic index file has no effect
  under the standing admission. It may stay or be removed.
- A question run made with the earlier default is not comparable with one made
  now: it was on the fake model's limits. The baseline of 2026-11-21 was made
  with the chat model's identity and stands.
- `--model-policy` must name the policy the owner's chats use when the answer
  depends on it. The two Astra policies share their limits, so under the
  standing admission the default and the owner's choice measure the same.

## Verification

- `tests/contract/test_local_query_authority.py`: an authority bound to no
  reader admits whichever reader the run selected and keeps one revision
  across readers; it still withholds a foreign owner, another tenant and
  another run's checkpoint; a bound authority warns once for a changed reader
  and never for a run withheld for another reason.
- `tests/unit/test_standing_recall_admission.py`: under the standing admission
  an owner chat is admitted on the reviewed reader and on another one, and a
  profile with no reader composes; under release evidence the changed reader
  is still withheld.
- `tests/unit/test_standing_admission_cli.py`: the preparation command writes
  both files for a profile with no reader.
- `tests/contract/owner_recall_probe_cases.py` (`chat_model_case`), run in
  memory by `tests/unit/test_owner_recall_probes.py` and through a worker on
  PostgreSQL by `tests/integration/test_owner_recall_probes_postgres.py`: each
  scripted run is pinned to the chat model, records that model's reader, starts
  its script over, and is answered by the scripted provider.
- `tests/unit/test_recall_questions_cli.py`: the command passes the policy on.

All of these ran in the Linux container and a disposable PostgreSQL. Nothing
was run against production.
