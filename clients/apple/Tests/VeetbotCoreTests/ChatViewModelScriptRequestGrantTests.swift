import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0342 decision 11: one approval for the scripts of a request. A script
/// card offers "Allow for this request"; resolving it sends the offered tool.
@Suite(.serialized) @MainActor struct ChatViewModelScriptRequestGrantTests {
    nonisolated private static let sessionID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-000000000301")!
    nonisolated private static let approvalID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-0000000003a1")!
    nonisolated private static let tool = "sandbox.run_code"

    nonisolated private static func approval(
        status: String = "PENDING", decision: String = "null", offer: Bool = true
    ) -> String {
        let offerJSON = offer
            ? #""script_request_grant_offer":{"tool_name":"\#(tool)","duration_seconds":7200,"summary":"Allow for this request: run the scripts Veetbot writes for this request without asking again."},"#
            : ""
        return #"{"id":"\#(approvalID.uuidString)","run_id":"6a1c3f0e-2b4d-4c8e-9f10-000000000302","session_id":"\#(sessionID.uuidString)","status":"\#(status)","tool_name":"\#(tool)","action_summary":"Run code with sandbox.run_code","arguments":{"code":"print(6 * 7)"},"argument_digests":{},"risk":"MEDIUM","policy_reason":"This chat has read web pages or other outside content, so Veetbot asks before taking this action.","expires_at":"2099-11-06T07:30:10Z","created_at":"2099-11-06T03:30:10Z","resolved_at":null,"resolved_by":null,\#(offerJSON)"decision":\#(decision)}"#
    }

    @Test
    func theScriptOfferDecodesAndShowsOnlyWhilePending() throws {
        let offered = try JSONDecoder.server.decode(ApprovalView.self, from: Data(Self.approval().utf8))
        let offer = try #require(offered.scriptRequestGrantOffer)
        #expect(offer.toolName == Self.tool)
        #expect(offer.summary.hasPrefix("Allow for this request"))
        #expect(offered.requestOfferSummary == offer.summary)
        #expect(offered.requestGrantOffer == nil)
        #expect(offered.jobRequestGrantOffer == nil)

        let resolved = try JSONDecoder.server.decode(
            ApprovalView.self,
            from: Data(Self.approval(status: "APPROVED", decision: #""approve_for_request""#).utf8)
        )
        #expect(resolved.decision == .approveForRequest)
        #expect(resolved.requestOfferSummary == nil)
        let plain = try JSONDecoder.server.decode(ApprovalView.self, from: Data(Self.approval(offer: false).utf8))
        #expect(plain.requestOfferSummary == nil)
    }

    @Test
    func aMalformedScriptOfferHidesTheButtonNotTheCard() throws {
        let broken = Self.approval().replacingOccurrences(of: #""tool_name":"\#(Self.tool)","duration_seconds""#, with: #""duration_seconds""#)
        let decoded = try JSONDecoder.server.decode(ApprovalView.self, from: Data(broken.utf8))
        #expect(decoded.scriptRequestGrantOffer == nil)
        #expect(decoded.requestOfferSummary == nil)
        #expect(decoded.status == .pending)
    }

    @Test
    func allowForThisRequestSendsTheOfferedScriptTool() async throws {
        let resolved = Self.approval(status: "APPROVED", decision: #""approve_for_request""#)
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    return (200, #"{"items":[],"next_cursor":null}"#)
                case "POST veetbot.test /v1/approvals/\(Self.approvalID.uuidString)/resolve":
                    return (200, resolved)
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)
        let offered = try JSONDecoder.server.decode(ApprovalView.self, from: Data(Self.approval().utf8))

        await model.resolveApproval(offered, decision: .approveForRequest)

        let body = try #require(
            server.json(of: "POST veetbot.test /v1/approvals/\(Self.approvalID.uuidString)/resolve")
        )
        #expect(body["decision"] as? String == "approve_for_request")
        let echo = try #require(body["request_grant"] as? [String: String])
        #expect(echo == ["tool_name": Self.tool])
        #expect(model.errorMessage == nil)
        let merged = try #require(model.runState.approvals.first { $0.id == Self.approvalID })
        #expect(merged.decision == .approveForRequest)
    }

    private func openedModel(_ server: DeviceFlowServer) async throws -> ChatViewModel {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: URLSession(configuration: server.configuration(base: .ephemeral))
        )
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        await model.openSharedSession(Self.sessionID)
        #expect(model.selectedSessionID == Self.sessionID)
        return model
    }

    nonisolated private static func route(_ request: URLRequest) -> String {
        "\(request.httpMethod ?? "") \(request.url?.host ?? "") \(request.url?.path ?? "")"
    }

    nonisolated private static func conversationRoutes(_ request: URLRequest) throws -> (Int, String)? {
        switch route(request) {
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)":
            return (200, #"{"id":"\#(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Compare two lists","metadata":{},"created_at":"2099-11-06T03:00:00Z","updated_at":"2099-11-06T03:00:00Z","active_run_id":null,"last_run_id":null}"#)
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)/messages":
            return (200, #"{"items":[],"next_cursor":null}"#)
        default:
            return nil
        }
    }
}
