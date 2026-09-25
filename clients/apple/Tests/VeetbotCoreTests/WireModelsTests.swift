import Foundation
import Testing

@testable import VeetbotCore

@Suite struct WireModelsTests {
    @Test
    func testContentBlockUsesRequestTypeAndAcceptsPersistedKind() throws {
        let encoded = try JSONEncoder.server.encode(ContentBlock.text("hello"))
        let object = try #require(
            JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        )
        #expect(object["type"] as? String == "text")
        #expect(object["kind"] == nil)

        let persisted = Data(#"{"kind":"text","text":"replayed"}"#.utf8)
        #expect(
            try JSONDecoder.server.decode(ContentBlock.self, from: persisted) == .text("replayed"))
    }

    @Test
    func testSessionAndRunViewsDecodeServerShape() throws {
        let sessionData = Data(
            #"{"id":"00000000-0000-0000-0000-000000000001","status":"ACTIVE","agent_id":"general","agent_version":"1","title":null,"metadata":{"source":"test"},"created_at":"2026-08-12T12:00:00Z","updated_at":"2026-08-12T12:00:01.123Z","active_run_id":null,"last_run_id":"00000000-0000-0000-0000-000000000009"}"#
                .utf8
        )
        let session = try JSONDecoder.server.decode(SessionView.self, from: sessionData)
        #expect(session.agentID == "general")
        #expect(session.metadata["source"] == .string("test"))
        #expect(session.lastRunID?.uuidString == "00000000-0000-0000-0000-000000000009")

        let runData = Data(
            #"{"id":"00000000-0000-0000-0000-000000000002","session_id":"00000000-0000-0000-0000-000000000001","parent_run_id":null,"status":"RUNNING","step_count":1,"model_call_count":2,"tool_call_count":3,"usage":{"input_tokens":10,"output_tokens":4,"cost_usd":"0.01"},"limits":{"max_steps":12,"deadline_at":null,"max_cost_usd":"1.00"},"failure":null,"cancel_requested_at":null,"created_at":"2026-08-12T12:00:00Z","updated_at":"2026-08-12T12:00:01Z"}"#
                .utf8
        )
        let run = try JSONDecoder.server.decode(RunView.self, from: runData)
        #expect(run.status == .running)
        #expect(run.usage.costUSD == "0.01")
        #expect(run.limits.maxSteps == 12)
    }

    @Test
    func testPendingApprovalDecodesUppercaseServerStatus() throws {
        let data = Data(
            #"{"id":"00000000-0000-0000-0000-000000000003","run_id":"00000000-0000-0000-0000-000000000002","session_id":"00000000-0000-0000-0000-000000000001","status":"PENDING","tool_name":"sandbox.run_command","action_summary":"Run sandbox.run_command with validated arguments.","arguments":{"argv":["pwd"]},"risk":"HIGH","policy_reason":"policy.approval_required","expires_at":"2026-08-13T12:00:00Z","created_at":"2026-08-12T12:00:00Z","resolved_at":null,"resolved_by":null,"decision":null}"#
                .utf8
        )

        let approval = try JSONDecoder.server.decode(ApprovalView.self, from: data)

        #expect(approval.status == .pending)
        #expect(approval.status.isPending)
        #expect(approval.toolName == "sandbox.run_command")
    }

    @Test
    func testAPIErrorPreservesConflictReasonAndUnknownDetails() throws {
        let data = Data(
            #"{"error":{"code":"conflict","message":"busy","details":{"reason":"active_run_exists","run_id":"00000000-0000-0000-0000-000000000002","extra":{"kept":true}},"request_id":"req-1"}}"#
                .utf8
        )
        let error = try JSONDecoder.server.decode(APIError.self, from: data)
        #expect(error.code == .conflict)
        #expect(error.details.reason == "active_run_exists")
        #expect(error.details.values["extra"] == .object(["kept": .bool(true)]))
        #expect(error.requestID == "req-1")
    }

    @Test
    func testAPIErrorAcceptsMissingOptionalEnvelopeFields() throws {
        let data = Data(
            #"{"error":{"code":"authorization_error","message":"missing scope"}}"#.utf8
        )
        let error = try JSONDecoder.server.decode(APIError.self, from: data)

        #expect(error.code == .authorizationError)
        #expect(error.message == "missing scope")
        #expect(error.details.values.isEmpty)
        #expect(error.requestID == "unknown")
    }

    @Test
    func testOutOfRangeJSONNumberIsNotAnInt() {
        #expect(JSONValue.number(1e30).intValue == nil)
        #expect(JSONValue.number(4.5).intValue == nil)
        #expect(JSONValue.number(4).intValue == 4)
    }

    @Test
    func testUnknownErrorCodeRoundTripsWithoutLosingValue() throws {
        let code = APIErrorCode(rawValue: "future_error")
        let encoded = try JSONEncoder.server.encode(code)
        let decoded = try JSONDecoder.server.decode(APIErrorCode.self, from: encoded)

        #expect(code == .unknown("future_error"))
        #expect(decoded == .unknown("future_error"))
        #expect(decoded.rawValue == "future_error")
    }

    @Test
    func testUnknownRunStatusAndFailureReasonRemainDecodable() throws {
        let status = try JSONDecoder.server.decode(
            RunStatus.self,
            from: Data(#""PAUSED_BY_SERVER""#.utf8)
        )
        let reason = try JSONDecoder.server.decode(
            FailureReason.self,
            from: Data(#""future_failure""#.utf8)
        )

        #expect(status == .unknown("PAUSED_BY_SERVER"))
        #expect(status.isActive)
        #expect(!status.isTerminal)
        #expect(reason == .unknown("future_failure"))
    }

    /// A fact about someone not in People keeps the name it was stated with
    /// (ADR-0121); the unresolved identity key is never shown.
    @Test(arguments: [
        ("person:unresolved:00000000-0000-0000-0000-000000000201,00000000-0000-0000-0000-000000000202:Alex preference", "Alex preference", false),
        ("person:00000000-0000-0000-0000-000000000203:Maya", "Maya", true),
        ("the user", "the user", false),
    ])
    func testPersonSubjectsDisplayTheirName(subject: String, shown: String, linked: Bool) throws {
        let data = Data(
            #"{"id":"00000000-0000-0000-0000-000000000101","subject":"\#(subject)","statement":"A fact.","belief_type":"preference","claim_kind":"preference","derivation":"direct","longevity":"durable","status":"active","polarity":"assert","scope":"session","portability":"portable","authority":"user","sensitivity":"restricted","confidence":0.5,"corroboration_count":1,"flagged_for_review":false,"conflicts_with":[],"superseded_by":null,"source_session_id":"00000000-0000-0000-0000-000000000103","source_event_ids":[1],"formation_run_id":"00000000-0000-0000-0000-000000000104","consolidation_policy_version":"formation@1","origin_scopes":["session"],"valid_from":"2026-08-01T00:00:00Z","valid_to":null,"expires_at":null,"last_evidence_at":"2026-08-15T00:00:00Z","last_used_at":null,"last_reinforced_at":"2026-08-15T00:00:00Z","created_at":"2026-07-01T00:00:00Z","updated_at":"2026-08-20T00:00:00Z"}"#
                .utf8
        )
        let memory = try JSONDecoder.server.decode(MemoryView.self, from: data)
        #expect(memory.displaySubject == shown)
        #expect((memory.personLink != nil) == linked)
    }

    /// Profiles from servers without duplicate handling still decode (ADR-0125).
    @Test
    func testPersonProfileDecodesWithAndWithoutDuplicateSections() throws {
        let person = #"{"id":"00000000-0000-0000-0000-000000000301","revision":1,"display_name":"Erin","state":"active","pinned":false,"sensitivity":"sensitive","support_ids":[]}"#
        let base = #"{"person":"# + person + #","aliases":[],"relationships":[],"history":[],"commitments":[],"facts":[],"fact_revisions":{},"truncated":false,"coverage":"Recorded evidence only""#
        let older = try JSONDecoder.server.decode(PersonProfileView.self, from: Data((base + "}").utf8))
        #expect(older.mergeSuggestions.isEmpty && older.automaticMerges.isEmpty)
        let merged = #"{"operation_id":"00000000-0000-0000-0000-000000000302","revision":2,"merged":"# + person + #","merged_at":"2026-09-24T12:00:00Z"}"#
        let newer = try JSONDecoder.server.decode(
            PersonProfileView.self,
            from: Data((base + #","merge_suggestions":[],"automatic_merges":["# + merged + "]}").utf8)
        )
        #expect(newer.automaticMerges.map(\.operationID.uuidString) == ["00000000-0000-0000-0000-000000000302"])
    }

    @Test
    func testMemoryViewDecodesTheFullExposureListAndToleratesAnUnknownStatus() throws {
        let data = Data(
            #"{"id":"00000000-0000-0000-0000-000000000101","subject":"the user","statement":"The user prefers dark mode.","belief_type":"preference","claim_kind":"preference","derivation":"direct","longevity":"durable","status":"archived","polarity":"assert","scope":"session","portability":"portable","authority":"user","sensitivity":"restricted","confidence":0.87,"corroboration_count":3,"flagged_for_review":true,"conflicts_with":["00000000-0000-0000-0000-000000000102"],"superseded_by":null,"source_session_id":"00000000-0000-0000-0000-000000000103","source_event_ids":[10,11,12],"formation_run_id":"00000000-0000-0000-0000-000000000104","consolidation_policy_version":"formation@1","origin_scopes":["project-a"],"valid_from":"2026-08-01T00:00:00Z","valid_to":null,"expires_at":null,"last_evidence_at":"2026-08-15T00:00:00Z","last_used_at":null,"last_reinforced_at":"2026-08-15T00:00:00Z","created_at":"2026-07-01T00:00:00Z","updated_at":"2026-08-20T00:00:00Z"}"#
                .utf8
        )

        let memory = try JSONDecoder.server.decode(MemoryView.self, from: data)
        let isoDate = ISO8601DateFormatter()

        #expect(memory.id.uuidString == "00000000-0000-0000-0000-000000000101")
        #expect(memory.subject == "the user")
        #expect(memory.statement == "The user prefers dark mode.")
        #expect(memory.beliefType == "preference")
        #expect(memory.beliefTypeKind == .preference)
        #expect(memory.claimKind == "preference")
        #expect(memory.derivation == "direct")
        #expect(memory.longevity == "durable")
        #expect(memory.status == "archived")
        #expect(memory.statusKind == nil)
        #expect(memory.polarity == "assert")
        #expect(memory.polarityKind == .assert)
        #expect(memory.scope == "session")
        #expect(memory.portability == "portable")
        #expect(memory.portabilityKind == .portable)
        #expect(memory.authority == "user")
        #expect(memory.authorityKind == .user)
        #expect(memory.sensitivity == "restricted")
        #expect(memory.sensitivityKind == .restricted)
        #expect(memory.confidence == 0.87)
        #expect(memory.corroborationCount == 3)
        #expect(memory.flaggedForReview)
        #expect(memory.conflictsWith.map(\.uuidString) == ["00000000-0000-0000-0000-000000000102"])
        #expect(memory.supersededBy == nil)
        #expect(memory.sourceSessionID.uuidString == "00000000-0000-0000-0000-000000000103")
        #expect(memory.sourceEventIDs == [10, 11, 12])
        #expect(memory.formationRunID.uuidString == "00000000-0000-0000-0000-000000000104")
        #expect(memory.consolidationPolicyVersion == "formation@1")
        #expect(memory.originScopes == ["project-a"])
        #expect(memory.validFrom == isoDate.date(from: "2026-08-01T00:00:00Z"))
        #expect(memory.validTo == nil)
        #expect(memory.expiresAt == nil)
        #expect(memory.lastEvidenceAt == isoDate.date(from: "2026-08-15T00:00:00Z"))
        #expect(memory.lastUsedAt == nil)
        #expect(memory.lastReinforcedAt == isoDate.date(from: "2026-08-15T00:00:00Z"))
        #expect(memory.createdAt == isoDate.date(from: "2026-07-01T00:00:00Z"))
        #expect(memory.updatedAt == isoDate.date(from: "2026-08-20T00:00:00Z"))
    }

    @Test
    func testScheduleSummaryAndDetailDecodeCalendarValuesAndUnknownState() throws {
        let summaryData = Data(
            #"{"id":"00000000-0000-0000-0000-000000000701","state":"ARCHIVED","pause_reason":null,"current_revision":3,"next_fire_at":"2026-09-30T01:00:00Z","title":"Month-end review","instruction_preview":"Review unfinished commitments.","cadence":{"kind":"MONTHLY","local_time":"18:00:00","days_of_month":[15],"last_day":true,"timezone":"America/Los_Angeles"},"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T01:00:00Z"}"#
                .utf8
        )

        let summary = try JSONDecoder.server.decode(ScheduleListItemView.self, from: summaryData)

        #expect(summary.id.uuidString == "00000000-0000-0000-0000-000000000701")
        #expect(summary.state == "ARCHIVED")
        #expect(summary.stateKind == nil)
        #expect(summary.cadence.kindKind == .monthly)
        #expect(summary.cadence.daysOfMonth == [15])
        #expect(summary.cadence.lastDay == true)
        #expect(summary.cadence.timezone == "America/Los_Angeles")

        let detailData = Data(
            #"{"schedule":{"id":"00000000-0000-0000-0000-000000000701","tenant_id":"local","principal_id":"principal","state":"ACTIVE","pause_reason":null,"current_revision":3,"next_fire_at":"2026-09-30T01:00:00Z","consecutive_failures":0,"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T01:00:00Z"},"revision":{"schedule_id":"00000000-0000-0000-0000-000000000701","revision":3,"title":"Month-end review","instruction":"Review the month and summarize unfinished commitments.","agent_id":"00000000-0000-0000-0000-000000000702","agent_version":"3","policy_profile":"default","requested_scopes":[],"limits":{"max_steps":12,"max_model_calls":10,"max_tool_calls":20,"max_input_tokens":null,"max_output_tokens":4096,"max_cost":"1.25","deadline_at":null},"run_timeout_seconds":300,"cadence":{"kind":"YEARLY","local_time":"09:30:00","dates":[{"month":2,"day":29},{"month":12,"day":31}],"timezone":"America/Los_Angeles"},"timezone":"America/Los_Angeles","misfire_grace_seconds":3600,"max_consecutive_failures":2,"created_by_principal_id":"principal","created_at":"2026-08-29T01:00:00Z"},"replayed":false}"#
                .utf8
        )

        let detail = try JSONDecoder.server.decode(ScheduleRecordView.self, from: detailData)

        #expect(detail.schedule.stateKind == .active)
        #expect(detail.revision.instruction == "Review the month and summarize unfinished commitments.")
        #expect(detail.revision.cadence.kindKind == .yearly)
        #expect(detail.revision.cadence.dates == [
            ScheduleMonthDayView(month: 2, day: 29),
            ScheduleMonthDayView(month: 12, day: 31),
        ])
        #expect(detail.revision.limits.maxCost == "1.25")
        #expect(detail.revision.limits.synthesisReserveSteps == nil)
        #expect(detail.revision.requestedScopes.isEmpty)
    }

    @Test
    func testUnknownSessionMessageRoleRoundTripsWithoutRejectingThePage() throws {
        let page = Data(
            #"{"items":[{"sequence":1,"role":"system","content":[{"type":"text","text":"Notice"}]}],"next_cursor":null}"#.utf8
        )

        let decoded = try JSONDecoder.server.decode(Page<SessionMessageView>.self, from: page)
        let role = try #require(decoded.items.first?.role)
        let encoded = try JSONEncoder.server.encode(role)

        #expect(role == .unknown("system"))
        #expect(try JSONDecoder.server.decode(SessionMessageRole.self, from: encoded) == role)
    }

    @Test
    func testPersonaViewDecodesTheServerShapeWithProvenance() throws {
        let data = Data(
            #"{"version":2,"entries":[{"text":"User values direct answers.","source":"user_edit","source_belief_id":null,"sensitivity":"internal"},{"text":"User prefers concise answers.","source":"affirmation","source_belief_id":"00000000-0000-0000-0000-000000000501","sensitivity":"internal"}],"source":"affirmation","created_at":"2026-09-01T12:00:00Z"}"#
                .utf8
        )

        let persona = try JSONDecoder.server.decode(PersonaView.self, from: data)

        #expect(persona.version == 2)
        #expect(persona.entries.count == 2)
        #expect(persona.entries[0].sourceBeliefID == nil)
        #expect(
            persona.entries[1].sourceBeliefID
                == UUID(uuidString: "00000000-0000-0000-0000-000000000501"))
    }

    @Test
    func testPersonaNominationDecodesOpenAndResolvedShapes() throws {
        let open = Data(
            #"{"id":"00000000-0000-0000-0000-000000000601","belief_id":"00000000-0000-0000-0000-000000000501","statement":"User prefers concise answers.","belief_type":"preference","authority":"affirmed","confidence":0.9,"corroboration_count":3,"sensitivity":"internal","state":"nominated","nominated_at":"2026-09-01T11:00:00Z","resolved_at":null,"affirmed_version":null}"#
                .utf8
        )

        let nomination = try JSONDecoder.server.decode(PersonaNominationView.self, from: open)

        #expect(nomination.state == "nominated")
        #expect(nomination.resolvedAt == nil)
        #expect(nomination.affirmedVersion == nil)
        #expect(nomination.corroborationCount == 3)
    }

    @Test
    func testSessionViewDecodesFolderIDAbsentNullAndPresent() throws {
        let base = #"{"id":"00000000-0000-0000-0000-000000000001","status":"ACTIVE","agent_id":"general","agent_version":"1","title":null,"metadata":{},"created_at":"2026-08-12T12:00:00Z","updated_at":"2026-08-12T12:00:01Z","active_run_id":null,"last_run_id":null"#
        let absent = try JSONDecoder.server.decode(SessionView.self, from: Data((base + "}").utf8))
        #expect(absent.folderID == nil)
        #expect(absent.folderSupported == false)
        let null = try JSONDecoder.server.decode(
            SessionView.self, from: Data((base + #","folder_id":null}"#).utf8)
        )
        #expect(null.folderID == nil)
        #expect(null.folderSupported == true)
        let present = try JSONDecoder.server.decode(
            SessionView.self,
            from: Data((base + #","folder_id":"00000000-0000-0000-0000-0000000000f1"}"#).utf8)
        )
        #expect(present.folderID?.uuidString == "00000000-0000-0000-0000-0000000000F1")
    }

    @Test
    func testFolderViewsDecodeAndTolerateAnUnknownProposalKind() throws {
        let folder = try JSONDecoder.server.decode(
            FolderView.self,
            from: Data(
                #"{"id":"00000000-0000-0000-0000-0000000000f1","name":"Travel","thread_count":3,"created_at":"2026-09-16T12:00:00Z","updated_at":"2026-09-16T12:00:00Z"}"#
                    .utf8
            )
        )
        #expect(folder.name == "Travel")
        #expect(folder.threadCount == 3)
        let template = #"{"id":"00000000-0000-0000-0000-0000000000e1","kind":"KIND","proposed_name":"Lisbon","target_folder_id":null,"member_session_ids":["00000000-0000-0000-0000-000000000001"],"rationale":null,"derivation":"lexical","state":"proposed","withdrawal_reason":null,"resulting_folder_id":null,"created_at":"2026-09-16T12:00:00Z","resolved_at":null}"#
        for (raw, expected) in [("new_folder", FolderProposalKind.newFolder), ("add_to_folder", .addToFolder)] {
            let proposal = try JSONDecoder.server.decode(
                FolderProposalView.self,
                from: Data(template.replacingOccurrences(of: "KIND", with: raw).utf8)
            )
            #expect(proposal.kindValue == expected)
        }
        let unknown = try JSONDecoder.server.decode(
            FolderProposalView.self,
            from: Data(template.replacingOccurrences(of: "KIND", with: "merge_folders").utf8)
        )
        #expect(unknown.kindValue == nil)
        #expect(unknown.memberSessionIDs.count == 1)
    }

    @Test
    func testSetSessionFolderBodyEncodesAnExplicitNull() throws {
        let unfiled = try JSONEncoder.server.encode(SetSessionFolderBody(folderID: nil))
        #expect(String(decoding: unfiled, as: UTF8.self) == #"{"folder_id":null}"#)
        let id = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000000f1"))
        let filed = try JSONEncoder.server.encode(SetSessionFolderBody(folderID: id))
        #expect(String(decoding: filed, as: UTF8.self) == #"{"folder_id":"\#(id.uuidString)"}"#)
    }

    @Test
    func testModelSettingsDecodeTheServerShapeAndTolerateUnknownKeys() throws {
        let data = Data(
            #"""
            {"version":3,"future_top_level":{"x":1},
             "chat":{"model_policy":"astra","reasoning_effort":"high","future":true},
             "memory":{"model_policy":"balanced","reasoning_effort":null},
             "chat_options":[
               {"model_policy":"astra","display_name":"GPT-6 Astra","provider":"openai","model":"gpt-6-astra","reasoning_efforts":["low","medium","high","xhigh","max"],"default_reasoning_effort":"high","pricing":{"input":"1"}},
               {"model_policy":"local","display_name":"Local model","provider":"local","model":"llama","reasoning_efforts":[],"default_reasoning_effort":null}
             ],
             "memory_options":[
               {"model_policy":"balanced","display_name":"GPT-5.6 Sol","provider":"openai","model":"gpt-5.6-sol","reasoning_effort":null,"evaluated_at":"2026-09-20"},
               {"model_policy":"astra","display_name":"GPT-6 Astra","provider":"openai","model":"gpt-6-astra","reasoning_effort":"medium"}
             ]}
            """#.utf8
        )

        let settings = try JSONDecoder.server.decode(ModelSettingsView.self, from: data)

        #expect(settings.version == 3)
        #expect(settings.chat == ModelChoice(modelPolicy: "astra", reasoningEffort: .high))
        #expect(settings.memory == ModelChoice(modelPolicy: "balanced", reasoningEffort: nil))
        #expect(settings.chatOptions.map(\.modelPolicy) == ["astra", "local"])
        #expect(settings.chatOptions[0].displayName == "GPT-6 Astra")
        #expect(settings.chatOptions[0].provider == "openai")
        #expect(settings.chatOptions[0].model == "gpt-6-astra")
        #expect(settings.chatOptions[0].reasoningEfforts == [.low, .medium, .high, .xhigh, .max])
        #expect(settings.chatOptions[0].defaultReasoningEffort == .high)
        #expect(settings.chatOptions[1].reasoningEfforts.isEmpty)
        #expect(settings.chatOptions[1].defaultReasoningEffort == nil)
        #expect(settings.memoryOptions.map(\.modelPolicy) == ["balanced", "astra"])
        #expect(settings.memoryOptions[0].reasoningEffort == nil)
        #expect(settings.memoryOptions[1].reasoningEffort == .medium)
    }

    @Test
    func testModelSettingsUpdateBodyEncodesEveryEffortExplicitly() throws {
        let body = UpdateModelSettingsBody(
            expectedVersion: 0,
            chat: ModelChoice(modelPolicy: "local", reasoningEffort: nil),
            memory: ModelChoice(modelPolicy: "astra", reasoningEffort: .medium)
        )

        let encoded = try JSONEncoder.server.encode(body)

        #expect(
            String(decoding: encoded, as: UTF8.self)
                == #"{"chat":{"model_policy":"local","reasoning_effort":null},"expected_version":0,"memory":{"model_policy":"astra","reasoning_effort":"medium"}}"#
        )
    }

    @Test
    func testReasoningEffortsCarryOwnerFacingLabels() throws {
        #expect(ReasoningEffort.low.displayName == "Low")
        #expect(ReasoningEffort.medium.displayName == "Medium")
        #expect(ReasoningEffort.high.displayName == "High")
        #expect(ReasoningEffort.xhigh.displayName == "Extra high")
        #expect(ReasoningEffort.max.displayName == "Max")
        #expect(ReasoningEffort.displayName(for: nil) == "Default")
        #expect(ReasoningEffort.displayName(for: .xhigh) == "Extra high")
        let unknown = try JSONDecoder.server.decode(
            ModelChoice.self, from: Data(#"{"model_policy":"astra","reasoning_effort":"turbo"}"#.utf8)
        )
        #expect(unknown.reasoningEffort?.rawValue == "turbo")
        #expect(unknown.reasoningEffort?.displayName == "Turbo")
    }
}
