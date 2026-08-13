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
        #expect(try JSONDecoder.server.decode(ContentBlock.self, from: persisted) == .text("replayed"))
    }

    @Test
    func testSessionAndRunViewsDecodeServerShape() throws {
        let sessionData = Data(
            #"{"id":"00000000-0000-0000-0000-000000000001","status":"ACTIVE","agent_id":"general","agent_version":"1","title":null,"metadata":{"source":"test"},"created_at":"2026-08-12T12:00:00Z","updated_at":"2026-08-12T12:00:01.123Z","active_run_id":null}"#.utf8
        )
        let session = try JSONDecoder.server.decode(SessionView.self, from: sessionData)
        #expect(session.agentID == "general")
        #expect(session.metadata["source"] == .string("test"))

        let runData = Data(
            #"{"id":"00000000-0000-0000-0000-000000000002","session_id":"00000000-0000-0000-0000-000000000001","parent_run_id":null,"status":"RUNNING","step_count":1,"model_call_count":2,"tool_call_count":3,"usage":{"input_tokens":10,"output_tokens":4,"cost_usd":"0.01"},"limits":{"max_steps":12,"deadline_at":null,"max_cost_usd":"1.00"},"failure":null,"cancel_requested_at":null,"created_at":"2026-08-12T12:00:00Z","updated_at":"2026-08-12T12:00:01Z"}"#.utf8
        )
        let run = try JSONDecoder.server.decode(RunView.self, from: runData)
        #expect(run.status == .running)
        #expect(run.usage.costUSD == "0.01")
        #expect(run.limits.maxSteps == 12)
    }

    @Test
    func testPendingApprovalDecodesUppercaseServerStatus() throws {
        let data = Data(
            #"{"id":"00000000-0000-0000-0000-000000000003","run_id":"00000000-0000-0000-0000-000000000002","session_id":"00000000-0000-0000-0000-000000000001","status":"PENDING","tool_name":"sandbox.run_command","action_summary":"Run sandbox.run_command with validated arguments.","arguments":{"argv":["pwd"]},"risk":"HIGH","policy_reason":"policy.approval_required","expires_at":"2026-08-13T12:00:00Z","created_at":"2026-08-12T12:00:00Z","resolved_at":null,"resolved_by":null,"decision":null}"#.utf8
        )

        let approval = try JSONDecoder.server.decode(ApprovalView.self, from: data)

        #expect(approval.status == .pending)
        #expect(approval.status.isPending)
        #expect(approval.toolName == "sandbox.run_command")
    }

    @Test
    func testAPIErrorPreservesConflictReasonAndUnknownDetails() throws {
        let data = Data(
            #"{"error":{"code":"conflict","message":"busy","details":{"reason":"active_run_exists","run_id":"00000000-0000-0000-0000-000000000002","extra":{"kept":true}},"request_id":"req-1"}}"#.utf8
        )
        let error = try JSONDecoder.server.decode(APIError.self, from: data)
        #expect(error.code == .conflict)
        #expect(error.details.reason == "active_run_exists")
        #expect(error.details.values["extra"] == .object(["kept": .bool(true)]))
        #expect(error.requestID == "req-1")
    }

    @Test
    func testUnknownErrorCodeRoundTripsWithoutLosingValue() throws {
        let code = APIErrorCode(rawValue: "future_error")
        #expect(code == .unknown("future_error"))
        #expect(code.rawValue == "future_error")
    }
}
