-- Compare two fetch orders within the Gmail history pages returned in one
-- recorded refresh. Supply psql variables tenant, owner, run_id, read_server
-- (the configured read MCP name), and provider_thread. Only ranks leave SQL.
-- Old pending work, unobserved Gmail pages and subsequent arrivals are outside
-- this replay. A working-set rank does not predict end-to-end latency.
\set ON_ERROR_STOP on
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL statement_timeout = '20s';
SET LOCAL agent_core.tenant_id = :'tenant';

WITH pages AS (
    SELECT e.sequence, (e.payload#>>'{result_item,content,0,text}')::jsonb AS page
    FROM events e
    WHERE e.run_id = :'run_id'::uuid AND e.event_type = 'tool.call.completed'
      AND e.payload->>'name' = 'mcp.' || :'read_server' || '.sync_changes'
      AND e.payload#>>'{result_item,is_error}' = 'false'
      AND EXISTS (
          SELECT FROM email_records
          WHERE tenant_id = :'tenant' AND principal_id = :'owner' AND kind = 'task'
            AND payload->>'kind' = 'refresh' AND payload->>'run_id' = :'run_id'
      )
), changes AS (
    SELECT row_number() OVER (ORDER BY sequence, ord) AS position,
           c->>'thread_id' AS thread, c->>'message_id' AS message,
           (c->>'history_id')::numeric AS history, c->>'kind' AS kind, c->'label_ids' AS labels
    FROM pages CROSS JOIN LATERAL jsonb_array_elements(page->'changes') WITH ORDINALITY r(c,ord)
), live AS (
    -- Deletion wins a tied provider revision, even if a page is replayed.
    SELECT c.* FROM changes c
    WHERE NOT EXISTS (
        SELECT FROM changes d WHERE d.thread = c.thread AND d.message = c.message
          AND d.kind = 'message_deleted' AND d.history >= c.history
    )
), messages AS (
    SELECT thread, message, (array_agg(position ORDER BY history DESC, position DESC))[1] AS last_position,
           bool_or(kind = 'message_added') AS arrived
    FROM live GROUP BY thread, message
), last_labels AS (
    -- Added messages give the observed starting labels. A label change only
    -- affects the labels named in that change; absent metadata is unknown.
    SELECT DISTINCT ON (thread, message, label) thread, message, label,
           CASE WHEN kind = 'message_added' THEN labels ? label
                ELSE kind = 'labels_added' END AS present
    FROM live CROSS JOIN unnest(ARRAY['INBOX','IMPORTANT','SENT','SPAM','TRASH']) AS label
    WHERE kind = 'message_added' OR (kind IN ('labels_added','labels_removed') AND labels ? label)
    ORDER BY thread, message, label, history DESC, position DESC
), flags AS (
    SELECT thread, message, array_agg(label) FILTER (WHERE present) AS labels
    FROM last_labels GROUP BY thread, message
), threads AS (
    SELECT m.thread, max(m.last_position) AS newest,
           bool_or(m.arrived AND coalesce(f.labels @> ARRAY['IMPORTANT','INBOX']
               AND NOT f.labels && ARRAY['SENT','SPAM','TRASH'], false)) AS expedite
    FROM messages m LEFT JOIN flags f USING (thread, message)
    GROUP BY m.thread
), ranked AS (
    SELECT thread, count(*) OVER () AS live_candidates,
           count(*) FILTER (WHERE expedite) OVER () AS expedited,
           row_number() OVER (ORDER BY newest DESC) AS current_position,
           row_number() OVER (ORDER BY expedite DESC, newest DESC) AS candidate_position
    FROM threads
)
SELECT live_candidates, expedited, current_position, candidate_position
FROM ranked WHERE thread = :'provider_thread';
COMMIT;
