# Live tests

Credentialed provider tests live here and run only through the explicit live
CI partition.

`test_recall_audit_replay.py` is an opt-in diagnostic for the remaining November
10 recall incidents. Set `RECALL_AUDIT_ROOT` to the private audit directory
containing `requests/garden-explicit/*.json` and
`requests/training-implicit/*.json`, and supply `VEETBOT_OPENAI_KEY`. Run in the
local Linux sandbox with `RUN_LIVE_MODEL_TESTS=1`. No production database or
external action tools are executed: it replays the first location-planning
request and final routine-answer request through the Responses adapter, replacing
only the agent instructions with the current defaults. The routine-use case
disables further tool calls to isolate reading from planning; the location case
keeps tool choice available. Capture modification times
must preserve request order. Missing private inputs skip this optional diagnostic.

The checks observe a residence-directed search and explicit use of swimming.
They do not prove retrieval ranking, correct advice, or end-to-end task success.
Model sampling can vary; retain repeated paired observations outside the repo
and investigate failures rather than retrying until green. Never commit captured
owner requests, provider responses or credentials.
