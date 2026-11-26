-- Read one owner's retained refresh records for the seven days ending at end_utc.
-- Run with psql -X -v tenant=... -v owner=... -v end_utc=... -f this_file.
-- The operator's database role needs read access to the three named tables.
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '20s';
SET LOCAL agent_core.tenant_id = :'tenant';

-- Group by refresh run before summing task charges. A single task may make
-- several gateway calls. Jev has its own events and no gateway ledger row.
WITH refresh AS (
    SELECT payload->>'scope' AS scope, (payload->>'run_id')::uuid AS run_id,
           payload->>'stage' = 'completed' AND (payload->>'empty_check')::boolean AS completed_empty,
           COALESCE((payload->>'settled_cost')::numeric,
                    (payload->>'budget_charge')::numeric) AS charge
    FROM email_records
    WHERE tenant_id = :'tenant' AND principal_id = :'owner'
      AND kind = 'task' AND payload->>'kind' = 'refresh'
      AND created_at >= :'end_utc'::timestamptz - interval '168 hours'
      AND created_at < :'end_utc'::timestamptz
), per_run AS (
    SELECT r.*, m.attempts, m.gateway_cost, j.decisions, j.known_cost
    FROM refresh r
    CROSS JOIN LATERAL (
        SELECT count(*) AS attempts, sum(cost) AS gateway_cost
        FROM model_calls WHERE run_id = r.run_id
    ) m
    CROSS JOIN LATERAL (
        SELECT count(*) AS decisions, sum((payload->>'cost')::numeric) AS known_cost
        FROM events WHERE run_id = r.run_id AND event_type = 'email.junk.completed'
    ) j
)
SELECT scope, count(*) AS refreshes, sum(attempts) AS gateway_attempts,
       round(coalesce(sum(gateway_cost), 0), 2) AS known_gateway_usd,
       sum(decisions) AS jev_receipts, round(coalesce(sum(known_cost), 0), 4) AS known_jev_usd,
       round(sum(charge), 2) AS aggregate_charged_usd,
       count(*) FILTER (WHERE completed_empty AND charge = 0) AS completed_empty_free,
       count(*) FILTER (WHERE charge IS NULL) AS unsettled
FROM per_run GROUP BY scope ORDER BY scope;

-- Assessments are overwritten. This is one latest surviving record per thread
-- last updated during the window. Never use these counts as independent labels,
-- a count of historical calls, or itemized cost.
WITH recent AS (
    SELECT t.payload->>'account_id' AS account, a.payload AS assessment
    FROM email_records a
    JOIN email_records t USING (tenant_id, principal_id, key)
    WHERE a.tenant_id = :'tenant' AND a.principal_id = :'owner'
      AND a.kind = 'assessment' AND t.kind = 'thread'
      AND a.updated_at >= :'end_utc'::timestamptz - interval '168 hours'
      AND a.updated_at < :'end_utc'::timestamptz
)
SELECT account, count(*) AS threads,
       count(*) FILTER (WHERE (assessment->>'junk_bypassed')::boolean) AS bypassed,
       count(*) FILTER (WHERE (assessment->>'grounded')::boolean
           AND ((assessment->>'bulk')::boolean OR (assessment->>'spam')::boolean)
           AND NOT coalesce((assessment->>'junk_bypassed')::boolean, false)) AS full_bulk_or_spam,
       count(*) FILTER (WHERE assessment#>>'{junk_screen,raw_category}' = 'keep'
           AND (assessment->>'grounded')::boolean
           AND ((assessment->>'bulk')::boolean OR (assessment->>'spam')::boolean)) AS kept_then_bulk_or_spam
FROM recent GROUP BY account ORDER BY account;
-- Compare the provider timestamp to local import, using a narrow cohort where
-- a later inbound message cannot have replaced the timestamp. The cohort omits
-- unseen mail, revised conversations and historical imports over seven days.
WITH imports AS (
    SELECT payload->>'account_id' AS account,
           created_at - (payload->>'updated_at')::timestamptz AS lag
    FROM email_records
    WHERE tenant_id = :'tenant' AND principal_id = :'owner' AND kind = 'thread'
      AND created_at >= :'end_utc'::timestamptz - interval '168 hours'
      AND created_at < :'end_utc'::timestamptz
      AND payload->>'revision' = '1' AND (payload->>'in_inbox')::boolean
      AND payload#>>'{messages,-1,direction}' = 'received'
)
SELECT account, count(*) AS sampled_imports,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY lag) AS median_to_import,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY lag) AS p95_to_import
FROM imports WHERE lag >= interval '0 seconds' AND lag < interval '168 hours'
GROUP BY account ORDER BY account;
COMMIT;
