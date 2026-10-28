# Owner recall probes (`owner-recall-probes@1`)

Fabricated analogues of the owner's real memory-retrieval incidents, one JSON
object per line. The probe driver (`agent memory probe`) runs each probe
through the composed production run loop and reports, per expected belief,
how far it travelled:

```text
Saved -> Stored -> Eligible -> Candidate -> Ranked -> Packed -> Delivered -> Used
```

A belief the store never held reports `not_stored`; one the lifecycle retired
reports `stored_but_retired`; one a hard predicate excluded names that
predicate; a refused owner save reports `save_refused:<reason_code>`.

The real probe file lives outside Git on the operator host
(`/etc/veetbot/recall/probes/owner-probes.v1.jsonl`, root-owned) and runs only
against a clone of the production database named `veetbot_recall_probe_<name>`;
the driver refuses production. Reports carry identifiers, ranks, stages and
latencies, never belief text. This file is the synthetic shape the unit and
integration tests exercise; it is not release evidence and not one of the
frozen Milestone 16/33 controls, whose paths the loader refuses.

Fields: `id`, `incident`, `family` (one of the general-recall families),
`question` (the owner turn), `search_text` (the deliberate `memory.search`
text; defaults to the question), `scope`, `paths` (`automatic`, `deliberate`),
`expected` (`belief_id` or `statement_contains`, `required`),
`min_required_rendered`, `forbidden` (`statement_contains`),
`delivered_must_contain` (whole words or names the delivered recall must
render, such as the family an overview delivers as People lines; reported as
one flag each, never the text),
`answer_must_contain` / `answer_must_not_contain` (live-model mode only),
`latency_ceiling_ms`, `write` (`statement`, `subject`, `web_results`) for the
Saved stage, and `notes`.
