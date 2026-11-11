# Live tests

Credentialed provider tests live here and run only through the explicit live
CI partition.

`test_recall_audit_replay.py` is an opt-in diagnostic for the remaining November
10 recall incidents. Set `RECALL_AUDIT_ROOT` to the private audit directory
containing `requests/garden-explicit/*.json`, `requests/training-implicit/*.json`,
`requests/portfolio-implicit/*.json` and `requests/mother-implicit/*.json`, and
supply `VEETBOT_OPENAI_KEY`. Run in the local Linux sandbox with
`RUN_LIVE_MODEL_TESTS=1`. No production database or
external action tools are executed: it replays the first location-planning
request, final routine-answer and comparison requests, and initial current-list
request through the Responses adapter, replacing only the agent instructions with
the current defaults. The routine and comparison cases disable further tool
calls to isolate reading from planning; the location and
current-list cases keep tool choice available. A separate `mother-planning` case
replays the first comparison request with tools available. Capture modification
times must preserve request order. Missing opt-in settings skip this diagnostic;
an incomplete supplied capture fixture fails it.

The checks observe a residence-directed search, explicit use of swimming, and
a deliberate search before treating an older list as the latest complete set.
Residence and collection queries must preserve first-person ownership rather
than substitute a name that can resolve to an unrelated People record.
The list search must avoid enumerating already-known members or searching output
format instructions instead of underlying collection changes. The comparison
answer must identify its known baseline. Keep `replay-criteria.json` beside the
captures: `portfolio-implicit.known_list_members` contains the old list members,
and `mother-implicit.baseline_terms` contains acceptable names for the current
comparison baseline. These values must be nonempty string lists.
`mother-implicit.person_name` is the other person's name already present in the
first captured request; its planning check requires that grounded name in the
baseline search. None of these private fixture values may be committed.

These checks do not prove retrieval ranking, correct advice, or end-to-end task success.
Model sampling can vary; retain repeated paired observations outside the repo
and investigate failures rather than retrying until green. Never commit captured
owner requests, provider responses or credentials.
