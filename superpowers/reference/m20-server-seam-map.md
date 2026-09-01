# M20 server seam map (explored 2026-09-01, HEAD 56efe50)

Read alongside docs/plan/device-channel-and-sms.md. Line numbers are from the
exploration; re-grep if an edit has moved them.

## 1. Tool system

### ports/tools.py — only two Protocols exist; DeviceChannel does NOT exist
- `Tool` (:11) — `spec: ToolSpec`; `async def execute(self, arguments: dict[str, Any], context: ToolExecutionContext) -> ToolResult` (:14-16).
- `ToolRegistry` (:19) — `register_dynamic(tool, *, tenant_id)` (:20), `unregister_dynamic(name, version, *, tenant_id)` (:24), `get(name, version=None, *, tenant_id=None, source=None, server_id=None)` (:28-38), `specs_for_session(agent, principal, profile, environment)` (:40-46).
- `StaticToolRegistry.register()` (static path) is NOT on the Protocol.
- Contract suites: tests/contract/test_tool_registry_contract.py, test_tool_contract.py.

### tools/registry.py
- `TOOL_NAME` regex :23; `BUILTIN_DOMAINS` :24-42 (15 domains, no device); `RESERVED_DOMAINS = frozenset({"mcp", "device"})` :43; `CONTROL_TOOL_NAMES` :45.
- `RegisteredTool` dataclass :55 (execute :60, optional approval_view :63).
- `validate_registration(spec)` :75 — seven ordered refusals; **:94-95 `if spec.source is ToolSource.DEVICE and domain != "device": raise ToolValidationError("device tools must use the device namespace")`**; :96-99 validate_required_scopes; **:146-149 output_trust forcing: MCP/DEVICE/SANDBOX (or target_kind=="sandbox") → model_copy(update={"output_trust": TrustLevel.EXTERNAL_UNTRUSTED})** — the gate-6 mechanism.
- `StaticToolRegistry` :153 — `register` :162 (static); `register_dynamic` :170 **refuses any source that is not ToolSource.MCP (:172-173 "only MCP discovery may dynamically register tools")**; `unregister_dynamic` :184; `get` :200; `specs_for_session` :243 (filters deprecated + required_scopes ⊆ principal.scopes).

### domain/tools.py
- `ToolSource` :33-37 — BUILTIN, MCP, **DEVICE = "device"** :36, SANDBOX.
- `ToolSpec` :40-58 — `target_kind: str = "in_process"` :54, `output_trust` :55, `source` :56, `server_id: str | None` :57. **No device_id field** (server_id is the precedent for adding one).
- `ToolFailureKind` :61-70 (TRANSPORT :69 maps to UNAVAILABLE), `ToolResult` :82 (output_trust override :88), `ToolExecutionContext` :98-127, `ToolOutcomeStatus` :130-135 (incl. UNAVAILABLE), `ToolInvocation` :187-221 (suspended_kind/suspended_ref :208-209, tool_source :195, origin_trust :213).
- Re-exported from domain/__init__.py:54,104.

### bootstrap.py composition wiring
- `registry = StaticToolRegistry()` :1397; unconditional builtins :1398-1408; conditional precedents: web :1409-1412, browser :1413-1416, `if settings.delegation_enabled:` :1417-1419, **paired-flag: `if settings.schedule_api_enabled and settings.schedule_worker_enabled: registry.register(ScheduleCreateTool(...))` :1420-1421**.
- MCP dynamic registration: mcp/runtime.py:135-157 (`register_dynamic` under per-(tenant,name,version) owner set; unregister when last owning session closes).

### tool.device_offline — DOES NOT EXIST in code
- Only in docs (tool-system.md:453 availability row `| Target device offline | unavailable | tool.device_offline |`).
- Must join: tools/messages.py:5-87 message table (`message_for()` :90-95; siblings "tool.server_unreachable" :43, "tool.unavailable" :85) — without an entry the model gets the generic fallback.
- Outcome mapping: tools/executor.py `_finish()` :1779-1830 — :1803 `unavailable = result.failure.kind is ToolFailureKind.TRANSPORT` → `ToolOutcomeStatus.UNAVAILABLE` :1808, remediation "none". So device-offline = `ToolFailure(kind=TRANSPORT, reason_code="tool.device_offline")`.
- `_effective_output_trust()` :249-253 pins result trust to EXTERNAL_UNTRUSTED when spec says so (runtime half of gate 6).

### Pipeline (tools/executor.py, ToolPipeline :350)
- `dispatch()` :431; `_dispatch_one()` :487 — resolve :504, enabled-set :511-522, **scope check :524-534 (policy.scope.missing)**, validate :544-551, `_execute_once()` :554.
- `_execute_once()` :581 — ToolInvocation candidate :607-628; `decision = await self._policy.evaluate(action, principal, run)` :642; DENY :773-776; REQUIRE_APPROVAL :777-796; AUTHORIZED→RUNNING :826-844; ask_user suspension (suspended_kind="user_input") :848-873; delegation (suspended_kind="child_run") :875+.
- Timeout pattern :1029-1099: `deadline = now + timedelta(seconds=tool.spec.timeout_seconds)` (min with run.deadline_at); `async with asyncio.timeout(effective_timeout)` :1099; timeout → ToolFailure(kind=TIMEOUT, reason_code="tool.timeout") :1145-1146. **Only wait pattern; no durable poll-back precedent.**
- ExecutionTarget construction — two hardcoded sites, neither sets device_id: :1047-1051 (context) and :1362-1367 (`_proposed_action`, adds server_id=tool.spec.server_id).
- `_finish()` :1779 terminal invocation + tool.call.completed|failed|uncertain; `_event_in()` :1948 appends with actor_type="runtime".
- `_turn_origin_trust(checkpoint, run_kind)` :272-306 — walks to most recent UserMessage with trust USER; none found (for..else :281-283) → EXTERNAL_UNTRUSTED; untrusted tool results taint :295-301. Feeds ToolInvocation.origin_trust :626 and ProposedAction.origin_trust :1361 → `LoadedRuleset.external_untrusted_requires_approval` (domain/policies.py:172). **Gate 10's chain.**

### domain/policies.py
- `ExecutionTarget` :55-60 — kind: str, isolated, network_enabled, **device_id: str | None = None (:59, reserved, never populated)**, server_id.
- TrustLevel :13-20, SideEffectClass :23-37, RiskLevel :40, IdempotencyClass :47, PolicyDecisionType :63, PolicyCondition :75, HardlineRuleKind :81, ProposedAction :96 (target :114), PolicyDecision :117, LoadedRuleset :163.

### Hardline secret scan (gate 8)
- policy/hardline.yaml:36-41 — rule `credential_to_egress`, kind trust_flow, applies_to [external_message, external_write, publication], source credential_shaped, message_code policy.hardline.secret_exfiltration.
- policy/hardline.py:76-80 — `credential_shape = re.compile(r"(?:api[_-]?key|secret|password|token|bearer)\s*[:=]\s*\S+", re.I)`.
- A tool with SideEffectClass.EXTERNAL_MESSAGE picks this up automatically. Existing test: tests/gates/test_secret_scanner.py.

## 2. Devices & notifications (M12)

### domain/devices.py (264 lines)
- DeviceKind :30-36; PushProvider :39-41 (APNS, TELEGRAM); PushEnvironment :44-46; DeviceStatus :49-51 (ACTIVE, REVOKED).
- `device_routing_issue(...)` :62-102 (closed reason codes).
- `DeviceRegistration` :105-118 frozen, extra="forbid": client_device_id, name, kind, platform, app_bundle_id, push_provider, push_token, push_environment, muted_kinds. **No capabilities.**
- `Device` :139-216 — full field list in report; validators :164 (aware UTC), :178 (token not blank), :185 (revoked consistency :197-203). **No capabilities — adding touches Device, DeviceRegistration, DeviceView (domain/views.py:177), DeviceRegistrationRequest (api/app.py:240-292), DeviceRow, devices table migration, mappers.py, _registration_hash (device_management.py:392).**
- DeviceCursor :219; PushTarget :233-258; push_token_fingerprint :261-264.

### ports/devices.py
- `DeviceRegistry` :20 — upsert :21, get :23, get_by_client_device_id :25, list :29, revoke :37, delete :39, invalidate_push_token :41, push_targets(tenant_id, principal_id, kind) :45.
- `DeviceRegistrationIdempotencyRepository` :53.

### ports/notifications.py
- `RunNotificationProducer` :27 — for_run_transition(uow, *, run, principal_id, status, approval_id=None, question_id=None, approval_expires_at=None) -> bool.
- `NotificationOutbox` :41 — enqueue :42, claim_due(now, limit, claimant, lease_seconds, providers) :44, list_pending_older_than :53, record_delivery :59, list_deliveries :61/:65, settle :70, list :78.
- `PushTransport` :87 — deliver(target, message) -> PushOutcome.

### Trigger catalog — closed in three Python tables + two DB CHECKs
1. `NotificationKind` domain/notifications.py:17-25 — APPROVAL_REQUESTED, QUESTION_ASKED, RUN_FAILED, SCHEDULE_RUN_FINISHED, SCHEDULE_OCCURRENCE_SKIPPED, OPS_ALERT, OPS_RECOVERED, TEST.
2. Tables enforced by `NotificationPayload.vocabulary_matches_kind` :112-150: NOTIFICATION_TITLES :367-376; _REQUIRED_IDENTIFIERS :378-392; _ALLOWED_SUBJECT_STATUSES :394-415. Dedupe-key builders :332-364. `test_notification_target_device_id()` :31-44 — the only device-targeted narrowing today; dispatcher filters at notification_dispatcher.py:170-172.
3. DB CHECKs repeating the kind list: sqlalchemy_models.py:1285-1290 (notification_kind_closed) and :1225-1229 (device_muted_kinds_closed); mirrored in migration c7e9a4f2d105 :110-116 and :179-184. **A new kind ⇒ migration rewriting both constraints.**

### Enqueue mechanics
- application/notification_producer.py — NotificationProducer :36; for_run_transition :43-115; `_enqueue_or_audit` :233-263 (exception → notification.enqueue_failed process event, never raises). `NotificationProductionUnitOfWork` Protocol :29-33 (process_events + notification_outbox only).
- Run-loop call site: runtime/executor.py:894-915 (in finalization transaction).
- PostgresNotificationOutbox.enqueue adapters/persistence/notifications.py:727-742 — `pg_insert(...).on_conflict_do_nothing(index_elements=[dedupe_key]).returning(...)` in begin_nested(); real insert → `SELECT pg_notify('agent_notification_wakeup', 'due')`; dedupe hit returns None. InMemory twin :254.
- Wakeup: adapters/notification_wakeup.py (channel "agent_notification_wakeup").

### APNs & payload
- adapters/apns.py APNsPushTransport :32; deliver :65-120; **payload :80-83: {"aps": {"alert": {"title": payload.title}}, "veetbot": payload.model_dump(mode="json")}** — title forced by kind table; outcome mapping :97-120.
- adapters/push.py = FakePushTransport :16-27 (scripted; used by contract/app tests).

### Dispatcher / worker
- application/notification_dispatcher.py — NotificationDispatchUnitOfWork :49-56 (approvals, checkpoints, devices, notification_outbox, process_events, runs, sessions); NotificationDispatcher(providers: frozenset[PushProvider]) :67-99; run_once :101-144; _dispatch :146-285 (staleness :149-156, expiry :157-164, push_targets :165-169, **TEST-kind single-device narrowing :170-172**, provider split :180-198, settle ladder :226-285).
- application/notification_worker.py — NotificationWorker :16 (dispatch_once, clock, fallback_poll_seconds, wait_for_wakeup); run_forever :40-62.

### Device routes & service
- api/app.py notification_router :1069, mounted :1183-1184 under settings.notification_api_enabled. Seven routes :1071-1181 (POST /v1/devices device.write w/ optional Idempotency-Key; GET list/get device.read; revoke/delete device.write; test-notification device.write w/ required key; GET /v1/notifications notification.read).
- DeviceRegistrationRequest :240-292 (extra="forbid"; registration() coerces enums + device_routing_issue → DeviceValidationError).
- application/device_management.py — DeviceManagementService :44 (register :60-167 with idempotency replay :73-80, Device construction :88-121, lifecycle events :124-141 actor_type="api" + token_fingerprint; get/list/revoke/delete; enqueue_test_notification :229-267; `_registration_hash` :392 — capabilities must enter it; cursor codecs :417-441).
- Service Protocols: application/services.py DeviceService ~:255-278, NotificationService :281-284.

## 3. Persistence patterns

### Migrations
- Dir migrations/versions/, naming `<12-hex>_<snake>.py`. **Current head: a9c5e2f7d413 (M13 delegations).**
- Header shape: c7e9a4f2d105_add_milestone_12_notifications.py:1-16.
- RLS helper verbatim (c7e9a4f2d105:19-26):
  ```python
  def _tenant_policy(table: str) -> None:
      op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
      op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
      op.execute(
          f"CREATE POLICY {table}_tenant_isolation ON {table} "
          "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
          "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
      )
  ```
- Conventions: op.f("ck_<table>_<name>"), pk names, named UniqueConstraint, create_index with postgresql_where, FKs with ondelete + op.f names; downgrade drops in reverse. Graph gate: tests/gates/test_migration_graph.py. Tenant GUC set per-UoW: unit_of_work.py:237-239.

### New-repository checklist (per existing pattern)
1. Port Protocol under ports/ → **requires tests/contract/test_<snake_class>_contract.py (scripts/architecture_checks.py contract_coverage_errors; enforced by tests/gates/test_contract_coverage.py)**.
2. Row in adapters/persistence/sqlalchemy_models.py (DeviceRow :1177-1266; NotificationOutboxRow :1279-1335; __table_args__ duplicates migration constraints).
3. mappers.py row↔domain.
4. InMemory* + Postgres* pair in one module (notifications.py precedent: InMemoryDeviceRegistry :60 / PostgresDeviceRegistry :469).
5. UoW plumbing: ports/persistence.py RepositoryUnitOfWork attrs (~:84-86); adapters/persistence/unit_of_work.py UnitOfWorkRepositories :106-110, MemoryUnitOfWork :161-165, PostgresUnitOfWork :272-276; bootstrap factories.
6. Least-privilege worker UoW if a role needs it (bootstrap _NotificationUnitOfWork :879-931).

M12 schemas in migration c7e9a4f2d105: devices :46-136, device_registration_idempotency_keys :137-155, notification_outbox :156-219, notification_deliveries :220-257. Structural test: tests/unit/test_persistence_schema.py.

## 4. HTTP routes
- create_app(services, settings, principal, new_request_id, readiness_probe) api/app.py:370-376.
- Router precedent: build APIRouter, declare routes with openapi_extra={"required_scope": "<scope>"}, mount under flag (schedule :945/:1066-1067; notification :1069/:1183-1184; memory :1186/:1239-1240). Flag off ⇒ routes never registered (gate-12 mirror; see tests/gates/test_notification_api_m12.py:329-345).
- `secured(scope)` :471-472 → Depends(auth.require(scope)); openapi_extra is a second declaration cross-checked by structural gates (test_api_m5.py:394; test_notification_api_m12.py:316-326).
- **Auth: dev-loopback or ONE static bearer (api/auth.py:31-61). No per-device credential exists.**
- Errors: api/errors.py ERROR_STATUS_MAP :16-51 (DeviceValidationError → device_validation_error/422 :22); details_for :79-93. Domain errors: domain/errors.py:43-49.
- Scopes: policy/scopes.py:9-37 PLATFORM_SCOPES — device.read :33, device.write :34, notification.read :35 already exist. validate_required_scopes :41-50.

## 5. Runs/sessions
- **Programmatic seeding precedent: scheduling/materializer.py ScheduleMaterializer.materialize() :78-330**, one transaction: Session with provenance metadata :236-253; session.created event (actor_type="scheduler", payload_schema_version=2) :256-266; Run(principal_scopes=..., QUEUED, deadline_at) :270-285; queue.enqueue :286-288; **user.message.created seed with payload {"content": instruction} :289-299**; runs.set_seed_event_sequence :300; run.queued :302-317; narrowed run_principal :318-320; _seed_checkpoint :321. HTTP twin: PublicRunService.submit public_services.py:745-812.
- actor_type is a free str (no DB constraint) — "device" needs no schema change. Origin metadata rides event payload + Session.metadata.
- **Deterministic routing: PublicRunService.submit :656-820** — active_for_session :697; WAITING_FOR_USER → _deliver_input_in :699-700; other active → ConflictError(active_run_exists) :717-724; none → new run. `_deliver_input_in()` :913-1064 requires exactly one RUNNING invocation with suspended_kind=="user_input" :933-941; completes ask_user with ToolResultItem(trust=USER) :978-1001; checkpoint rewrite to QUEUED :1018-1052. **No append-to-RUNNING path exists.**
- Trust: domain/messages.py UserMessage.trust default USER :64; projector conversation_items() domain/events.py:67-113 builds UserMessage WITHOUT setting trust (payload trust ignored today); RunCheckpoint.context_origin_trust default USER (domain/runs.py:159). Gate-10 chain runs through _turn_origin_trust (see §1).

## 6. Config & flags
- config.py Settings :71-118; paired default-off flags :91-96; parse `_parse_flag(values, "AGENT_<NAME>_ENABLED")` :935-940; construction :1050-1055.
- **Pairing enforcement precedent: validate_settings :709-711 — notification API/dispatch "must be enabled or disabled together".** Provider cross-check form :712-741. `_validate_private_regular_file` :733.
- Limits: src/agent_core/runtime/limits.yaml blocks (queue, worker, sweeps, model, context, run_defaults, scheduling, notifications, delegation) — a `device:` block belongs here. Read via load_config_document(bootstrap :1088/:1160), indexed as dicts (:1197-1206). SHIPPED_CONFIGS :122-135; **SHIPPED_KNOB_PATHS :140+ (corpus asserts 150 knobs; adding knobs updates the count — config.py:136-139).**
- Dev mode grants auth_scopes = PLATFORM_SCOPES (config.py:1033).

## 7. Gates & tests
- evals/gates/device.yaml: entries 1-6 M12 green (:2-37); **12 M20 entries :38-109, all check: tests/gates/pending.py::pending_gate** (ids/kinds/statements verbatim in the report — statements strip backticks).
- Flip mechanism: scripts/gate_registry.py `_check_resolves` :166 (supports Class::method); active-milestone pending refusal :274-278 (applies only ≤ current_milestone = 12, so flipping early is allowed and REQUIRED to resolve).
- tests/gates/test_gate_registry.py: test_registry_complete :22-23 (current_milestone=11); **test_no_stale_active_gate :37-43 asserts len(active)==227 for ≤11** — M20 flips don't disturb it.
- **M19 worked example: evals/gates/schedule.yaml:142-171 + tests/gates/test_schedule_tool_m19.py** — file docstring "Milestone 19 ... gates."; bare async test functions; `async with build(settings=..., script=FakeModelScript(...)) as composition:` against in-memory storage; `replace(memory_settings(), <flag>=True)`; default-off asserted by building plain and expecting NotFoundError (:50-51). Route-not-mounted form: test_notification_api_m12.py:329-360.
- Contract suites: shared helper + one test per adapter (tests/contract/test_push_transport_contract.py:23-74); shared factories tests/contract/support.py (tool_context() :126-152 builds ExecutionTarget(kind="in_process",...) :143).
- make test-fast = test-static + test-contract (pytest -m static; -m "not static and not integration and not live"), **no DB needed**; make test-integration needs db-up. asyncio_mode auto; strict markers.

## 8. Workers/roles
- Loop shapes: NotificationWorker (dispatch_once callback, fallback poll, wakeup) :16-78; ScheduleWorker; **MaintenanceWorker runtime/worker.py:178+ — ten optional `sweep_*` callbacks, each in its own try/except in run_once, interval-gated timers — an invocation-expiry sweep fits as `sweep_device_invocations`**.
- bootstrap: _NotificationUnitOfWork :879-931 (7 repos); _validate_notification_role :954-1004 (least-privilege refusals); build_schedule_worker :1085-1148; build_notification_worker :1152-1214 (injectable transport — the fake-APNs seam).
- CLI: WorkerRole StrEnum cli/main.py:61-67 (WORKER, INTERACTIVE, ASYNC, MAINTENANCE, SCHEDULE, NOTIFY); _serve_worker :478-496; `agent worker --role` :521-538.
- systemd: deploy/systemd/veetbot-notify.service is the minimal 23-line template; maintenance unit carries extra Requires/ReadWritePaths. make test-deploy covers units.

## Gaps M20 must CREATE (not extend)
- DeviceChannel Protocol (+ contract suite, mandatory).
- Device.capabilities everywhere (domain, registration, view, request, row, migration, hash, mappers).
- tool.device_offline message entry + TRANSPORT-failure producer.
- A capability-derived registration path (register_dynamic refuses non-MCP; ToolRegistry has no static register on the Protocol).
- ExecutionTarget.device_id population (both hardcoded sites) — likely via a new ToolSpec.device_id (server_id precedent).
- Per-device route auth = bearer + device-ownership + presence checks (no device credential exists).
- A sixth NotificationKind + tables + dedupe key + dispatcher narrowing + CHECK-constraint migration.
- Projector trust honoring for device-originated user.message.created (payload trust is ignored today) + untrusted checkpoint seeding.
