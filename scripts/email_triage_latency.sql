-- Receipt cohort over the seven days ending at end_utc. Supply tenant, owner and end_utc.
-- Only first server-visible Important membership stops the clock. Provisional Inbox
-- rows, model predictions and completed assessments without membership do not.
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '30s';
SET LOCAL agent_core.tenant_id = :'tenant';

WITH observed AS (
    SELECT e.id, e.event_type, e.payload, e.created_at
    FROM events e JOIN sessions s ON s.id = e.session_id
    WHERE s.tenant_id = :'tenant' AND s.principal_id = :'owner'
      AND e.event_type IN ('email.triage.discovered', 'email.triage.assessment_started',
                           'email.triage.assessed', 'email.triage.important')
      AND e.created_at >= :'end_utc'::timestamptz - interval '7 days'
      AND e.created_at < :'end_utc'::timestamptz
), cohort AS (
    SELECT DISTINCT ON (payload->>'arrival_id')
           payload->>'arrival_id' AS arrival, payload->>'account_id' AS account,
           payload->>'mode' AS mode, (payload->>'budget_held')::boolean AS held_at_import,
           (payload->>'expedited')::boolean AS expedited,
           (payload->>'received_at')::timestamptz AS received,
           (payload->>'discovered_at')::timestamptz AS discovered
    FROM observed WHERE event_type = 'email.triage.discovered'
      AND (payload->>'received_at')::timestamptz >= :'end_utc'::timestamptz - interval '7 days'
      AND (payload->>'received_at')::timestamptz < :'end_utc'::timestamptz
    ORDER BY payload->>'arrival_id', created_at, id
), outcomes AS (
    SELECT payload->>'arrival_id' AS arrival,
           min((payload->>'started_at')::timestamptz) AS first_full_start,
           min(created_at) FILTER (WHERE event_type = 'email.triage.discovered'
                    AND (payload->>'expedited')::boolean) AS first_provisional,
           min(created_at) FILTER (WHERE event_type = 'email.triage.assessed'
                    AND (payload->>'grounded')::boolean) AS first_grounded,
           min((payload->>'important_at')::timestamptz) AS first_important,
           (array_agg(payload->>'cause' ORDER BY created_at, id)
              FILTER (WHERE event_type = 'email.triage.important'))[1] AS cause
    FROM observed GROUP BY 1
)
SELECT account, mode, held_at_import, expedited,
       CASE WHEN grouping(cause) = 1 THEN 'all outcomes'
            ELSE coalesce(cause, 'no observed placement') END AS outcome,
       count(*) AS observed_arrivals, count(first_important) AS placed,
       count(*) FILTER (WHERE first_important - received > interval '15 minutes') AS placed_after_15m,
       percentile_disc(0.5) WITHIN GROUP (ORDER BY first_important - received) AS median_to_important,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY first_important - received) AS p95_to_important,
       max(first_important - received) AS max_to_important,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY discovered - received) AS p95_to_discovery,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY first_full_start - discovered) AS p95_to_full_start,
       count(first_full_start) AS started, count(first_grounded) AS grounded,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY first_provisional - received) AS p95_to_provisional,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY first_grounded - received) AS p95_to_grounded,
       count(*) FILTER (WHERE first_important < received OR discovered < received) AS clock_skew
FROM cohort LEFT JOIN outcomes USING (arrival)
GROUP BY GROUPING SETS ((account, mode), (account, mode, held_at_import, expedited, cause))
ORDER BY 1,2,3 NULLS FIRST,4,5;

-- Current right-censored queue. Include older arrivals than the receipt cohort.
SELECT payload->>'account_id' AS account,
       (payload#>>'{arrival,budget_held}')::boolean AS held_at_last_observation,
       count(*) AS pending, max(now() - (payload#>>'{arrival,received_at}')::timestamptz) AS oldest,
       count(*) FILTER (WHERE now() - (payload#>>'{arrival,received_at}')::timestamptz >
                              interval '15 minutes') AS waiting_over_15m
FROM email_records
WHERE tenant_id = :'tenant' AND principal_id = :'owner' AND kind = 'thread'
  AND NOT erasure_pending AND (payload->>'in_inbox')::boolean
  AND (payload#>>'{arrival,expedited}')::boolean
  AND payload#>>'{arrival,assessed_at}' IS NULL AND payload#>>'{arrival,important_at}' IS NULL
GROUP BY 1,2 ORDER BY 1,2;

-- A failed or slow provider read can leave durable history evidence before any
-- Inbox row exists. A partial thread can occur in both queue snapshots.
WITH pending_reads AS (
    SELECT r.key AS account, change->>'thread_id' AS thread,
           min((change->>'first_seen')::timestamptz) AS first_seen,
           bool_or(change->>'inbound' = '1' AND change->>'removed_inbox' <> '1'
                   AND string_to_array(change->>'labels', ' ') @> ARRAY['IMPORTANT', 'INBOX']
                   AND NOT (string_to_array(change->>'labels', ' ') && ARRAY['SPAM','TRASH','SENT'])) AS hinted
    FROM email_records r CROSS JOIN LATERAL jsonb_each(r.payload->'change_events') AS e(key, change)
    WHERE r.tenant_id = :'tenant' AND r.principal_id = :'owner' AND r.kind = 'sync'
      AND change->>'kind' <> 'message_deleted'
    GROUP BY r.key, change->>'thread_id'
)
SELECT account, a.payload->>'status' AS gmail_status, count(*) AS pending_thread_reads,
       count(*) FILTER (WHERE hinted) AS hinted_inbox_threads,
       max(now() - first_seen) FILTER (WHERE hinted) AS oldest_observed_hint,
       count(*) FILTER (WHERE first_seen IS NULL) AS legacy_without_observation_time
FROM pending_reads p LEFT JOIN email_records a
  ON a.tenant_id = :'tenant' AND a.principal_id = :'owner' AND a.kind = 'account' AND a.key = p.account
GROUP BY account, a.payload->>'status' ORDER BY account;

-- Owner-wide window cost, once per refresh. Includes Astra, Jev and conservative settlement.
WITH refresh AS (
    SELECT sum(coalesce((payload->>'settled_cost')::numeric, (payload->>'budget_charge')::numeric)) AS charged,
           count(*) FILTER (WHERE coalesce(payload->>'settled_cost', payload->>'budget_charge') IS NULL) AS unsettled
    FROM email_records WHERE tenant_id = :'tenant' AND principal_id = :'owner'
      AND kind = 'task' AND payload->>'kind' = 'refresh'
      AND created_at >= :'end_utc'::timestamptz - interval '7 days' AND created_at < :'end_utc'::timestamptz
), arrivals AS (
    SELECT count(DISTINCT e.payload->>'arrival_id') AS n FROM events e JOIN sessions s ON s.id = e.session_id
    WHERE s.tenant_id = :'tenant' AND s.principal_id = :'owner' AND e.event_type = 'email.triage.discovered'
      AND e.created_at >= :'end_utc'::timestamptz - interval '7 days' AND e.created_at < :'end_utc'::timestamptz
)
SELECT n AS arrivals_imported, charged AS charged_refresh_usd, unsettled,
       round(100 * charged / nullif(n, 0), 2) AS refresh_usd_per_100_observed
FROM arrivals CROSS JOIN refresh;
COMMIT;
