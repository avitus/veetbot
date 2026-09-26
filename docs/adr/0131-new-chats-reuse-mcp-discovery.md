# ADR-0131: New chats pin MCP catalogs from the last discovery and start servers on first use

- Status: Accepted (authorized by the repository owner, 2026-09-25)
- Date: 2026-09-25
- Related: Sections 8 and 19 of the engineering plan; ADR-0021, ADR-0103,
  ADR-0104
- Amends: ADR-0103 (adopts, for full catalog preparation, the cross-session
  discovery reuse it rejected for operational Email sessions) and the
  `tool-system.md` rule that every session open connects to every configured
  server
- Detailed design: `docs/plan/tool-system.md`, `docs/plan/model-gateway.md`,
  `docs/plan/bootstrap-and-composition.md`

## Context

On 2026-09-25 the owner reported that Chat feels sluggish. Read-only queries
over the production `events` and `model_calls` tables measured the 80 Chat turns
completed between 2026-09-04 and 2026-09-25, excluding turns that waited for an
approval:

| Phase | Median | 90th percentile |
| --- | --- | --- |
| Whole turn, `run.queued` to the answer | 29.2 s | 73.9 s |
| Worker queue | 0.24 s | 0.35 s |
| `run.started` to the first `model.request.started` | 7.9 s | 14.6 s |
| Model calls per turn | 18.8 s | 50.4 s |
| Tools, in the 60 turns that used any | 3.9 s | 13.1 s |

Splitting the pre-model gap at the events inside it attributed 523.4 of its
558.9 seconds (94%) to the gaps that ended at `mcp.server.connected`. The cost
lands where a session's context plan is first built. The 45 first turns of new
chats waited 11.1 s at the median (17.2 s at p90, 25.5 s at most) before their
first model request. The 35 follow-up turns waited 0.4 s. The gap correlated
negatively with prior history size (−0.55 by bytes), so thread length, memory
recall, checkpoints and context assembly (about 0.4 s per turn together) are not
the cause. The 311 connection events over those 45 plans are the six Gmail
servers and the calling servers, started two at a time under the stdio startup
limit, each Gmail process exchanging its OAuth refresh token before it answers.

The API pays the same cost earlier. `POST /v1/sessions` opens the skill catalog,
which prepares every enabled server to discover MCP prompts. ADR-0103 measured
10–16 s for the same six Gmail servers during Email admission. The Gmail servers
advertise no prompts. That time sits before `run.queued`, outside the run
timeline, and was not measured here.

The production servers are operator-configured stdio processes. Their tools are
fixed per mode in code that ships with the release, and every release restarts
the API and the workers. Production delivered to `main` 29 times in the same
21 days in which the owner opened 45 chats.

ADR-0103 rejected caching MCP discovery across sessions because it "would change
the tool-system rule that each session discovers its own servers, to optimize a
catalog that is never rendered." ADR-0104 repeated that rejection for typed
Email runs. Chat's catalog is rendered, and its discovery is the largest
avoidable cost the measurement found.

## Decision

1. **Each MCP runtime remembers its last successful discovery of each
   configured server.** The API and every worker keep their own memory. It is
   keyed by the server's whole configuration: tenant, server id, transport,
   endpoint, authentication, credential reference, classification and limits.
   Any configuration change is therefore a different entry. Every live discovery
   replaces the entry, whether it happens at session open, when a call
   reconnects, or during the startup warm-up. A live attempt that fails forgets
   the entry.
2. **Full catalog preparation pins a remembered discovery without starting the
   server.**
   - An operator-configured stdio server's discovery is reused for the life of
     the process. Its catalog can change only with a release, and a release
     restarts the process.
   - A tenant HTTP server's discovery is reused for at most
     `mcp.discovery_reuse_seconds` (`tools/limits.yaml`, default 3600), because a
     third party can change its catalog at any time.
   - Otherwise discovery is live, as before. Typed Email preparation that names
     its servers (ADR-0104) always stays live, because the task is about to call
     them and concurrent startup is the point of preparing them early.

   A reused pin writes the same catalog rows, registrations, and conflict and
   rejection events as a live pin. It emits `mcp.server.pinned` rather than
   `mcp.server.connected`, because no handshake happened.
3. **A reused pin's server starts on the first call of one of its tools.** This
   is the existing reconnection path. It compares the live discovery with the
   pin, keeps added tools unadvertised, answers removed or changed declarations
   with `tool.withdrawn`, and emits `mcp.catalog.changed` when the hash
   differs. Availability stays a call-time decision, as `tool-system.md`
   already states. A server that has become unreachable since it was remembered
   is therefore advertised, and its calls return `tool.server_unreachable`.
4. **The API and the interactive worker warm the memory when they start.** In
   the background and within the existing preparation slots, each discovers
   every enabled server of the configured tenant, then closes it. The warm-up
   records no session state and emits no event. A failure is logged and leaves
   the entry empty, so the next session open discovers that server live.
5. **Every handshake is timed.** `mcp.server.connected` also carries the
   transport, which `tool-system.md` already lists, and `duration_ms` for
   connection plus discovery. It is now emitted when a call reconnects, too.
6. **Model attempts are timed as `model-gateway.md` specifies.**
   `model.response.completed` gains the specified `usage`,
   `internal_retry_count`, `duration_ms` and `time_to_first_event_ms`. It also
   gains `time_to_first_text_ms`, measured to the first text delta, which is
   what a reader waits for; it is `None` when the attempt produced no text.
   `ModelCompletedEvent` carries the adapter's retries before output, so the
   loop can report them.
7. **`agent run latency` prints the report the measurement above used.** It is a
   read-only, aggregates-only report over `events` and `model_calls` for recent
   Chat turns: phase percentiles, first and follow-up setup, per-tool and
   per-server durations, model timing and cache ratio, and queue waits by
   session kind. It prints no message, argument or title content.

The new payload fields are additive at payload version 1, the way
`reasoning_effort` joined `model.request.started`. Readers treat a missing field
as unknown. Pins, registrations, tool classification, policy, idempotency and
the reauthentication ladder are unchanged.

## Consequences

- Once warmed, a new chat's first turn starts no MCP server before its first
  model request, and creating the chat starts none in the API. The measured
  saving is the 11.1 s median first-turn setup, less the 0.4 s every turn keeps.
- The first call to a server in a new chat pays that one server's startup.
  Run teardown already releases transports, so every later run in a thread
  already paid this on its first call.
- A server that stops working between discovery and call is advertised and
  fails at call time with a structured reason. Before, a new session omitted it.
  This is the direction `tool-system.md` already chose for servers that
  disconnect mid-session.
- Each API and interactive-worker start briefly runs every enabled server, two
  at a time: seven in production. Nothing waits for it.
- One more `mcp.server.connected` event is appended per server per run that
  calls it.
- A release that changes a stdio server's tools is picked up when the new
  release starts, because that restart empties the memory.

## Alternatives considered

- **Remember discoveries durably in Postgres.** Both processes could share one
  entry, and it would survive restarts. But a stdio endpoint embeds the
  release's interpreter path, so no entry survives a release anyway. The
  warm-up covers restarts without a new table.
- **Skip MCP discovery at session creation.** The API work is spent only to pin
  MCP prompts, and there are none. But `skills.md` pins a session's catalog,
  prompts included, when the session is created. Changing that is a larger
  decision than this repair needs.
- **Keep transports open between runs.** That removes the first-call startup
  per run and would speed up Email actions. It attacks a different cost with a
  different memory trade on a small host, so it is proposed separately.
- **Reuse a stdio discovery for a fixed period.** There is no staleness to
  bound within a release, and the first chat after the period would be slow
  again.
- **Start typed Email servers lazily as well.** ADR-0104 rejected this because
  it serializes the read and write startups an archive needs.
