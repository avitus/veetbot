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

    @Test
    func testMemoryViewDecodesTheFullExposureListAndToleratesAnUnknownStatus() throws {
        let data = Data(
            #"{"id":"00000000-0000-0000-0000-000000000101","subject":"the user","statement":"The user prefers dark mode.","belief_type":"preference","status":"archived","polarity":"assert","scope":"session","portability":"portable","authority":"user","sensitivity":"restricted","confidence":0.87,"corroboration_count":3,"flagged_for_review":true,"conflicts_with":["00000000-0000-0000-0000-000000000102"],"superseded_by":null,"source_session_id":"00000000-0000-0000-0000-000000000103","source_event_ids":[10,11,12],"valid_from":"2026-08-01T00:00:00Z","valid_to":null,"expires_at":null,"last_reinforced_at":"2026-08-15T00:00:00Z","created_at":"2026-07-01T00:00:00Z","updated_at":"2026-08-20T00:00:00Z"}"#
                .utf8
        )

        let memory = try JSONDecoder.server.decode(MemoryView.self, from: data)
        let isoDate = ISO8601DateFormatter()

        #expect(memory.id.uuidString == "00000000-0000-0000-0000-000000000101")
        #expect(memory.subject == "the user")
        #expect(memory.statement == "The user prefers dark mode.")
        #expect(memory.beliefType == "preference")
        #expect(memory.beliefTypeKind == .preference)
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
        #expect(memory.validFrom == isoDate.date(from: "2026-08-01T00:00:00Z"))
        #expect(memory.validTo == nil)
        #expect(memory.expiresAt == nil)
        #expect(memory.lastReinforcedAt == isoDate.date(from: "2026-08-15T00:00:00Z"))
        #expect(memory.createdAt == isoDate.date(from: "2026-07-01T00:00:00Z"))
        #expect(memory.updatedAt == isoDate.date(from: "2026-08-20T00:00:00Z"))
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
}
