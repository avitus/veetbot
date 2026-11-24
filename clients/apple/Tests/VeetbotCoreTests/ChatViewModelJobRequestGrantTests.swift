import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0344: one approval for a request that changes a saved job. A job card
/// offers "Allow for this request"; resolving it sends the offered job slug.
@Suite(.serialized) @MainActor struct ChatViewModelJobRequestGrantTests {
    nonisolated private static let sessionID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-000000000201")!
    nonisolated private static let approvalID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-0000000002a1")!
    nonisolated private static let slug = "emerging-market-radar"

    nonisolated private static func approval(
        status: String = "PENDING", decision: String = "null", offer: Bool = true
    ) -> String {
        let offerJSON = offer
            ? #""job_request_grant_offer":{"slug":"\#(slug)","ceilings":[{"hosts":{},"mcp_servers":["svp_read"],"feeds":["agent.radar"],"timeout_seconds":60,"maximum_requests":16,"maximum_response_bytes":49152,"model_cost":"0.15"}],"duration_seconds":7200,"summary":"Allow for this request: change, test and run the \#(slug) job and repin its schedule using access it already has."},"#
            : ""
        return #"{"id":"\#(approvalID.uuidString)","run_id":"6a1c3f0e-2b4d-4c8e-9f10-000000000202","session_id":"\#(sessionID.uuidString)","status":"\#(status)","tool_name":"job.manage","action_summary":"Publish something with job.manage","arguments":{"operation":"update","slug":"\#(slug)"},"argument_digests":{},"risk":"MEDIUM","policy_reason":"This chat has read web pages or other outside content, so Veetbot asks before taking this action.","expires_at":"2099-11-06T07:30:10Z","created_at":"2099-11-06T03:30:10Z","resolved_at":null,"resolved_by":null,\#(offerJSON)"decision":\#(decision)}"#
    }

    @Test
    func theJobOfferDecodesAndShowsOnlyWhilePending() throws {
        let offered = try JSONDecoder.server.decode(ApprovalView.self, from: Data(Self.approval().utf8))
        let offer = try #require(offered.jobRequestGrantOffer)
        #expect(offer.slug == Self.slug)
        #expect(offer.summary.hasPrefix("Allow for this request"))
        #expect(offered.offersJobRequestGrant)
        #expect(offered.requestGrantOffer == nil)

        let resolved = try JSONDecoder.server.decode(
            ApprovalView.self,
            from: Data(Self.approval(status: "APPROVED", decision: #""approve_for_request""#).utf8)
        )
        #expect(resolved.decision == .approveForRequest)
        #expect(resolved.offersJobRequestGrant == false)
        let plain = try JSONDecoder.server.decode(ApprovalView.self, from: Data(Self.approval(offer: false).utf8))
        #expect(plain.offersJobRequestGrant == false)
    }

    @Test
    func aMalformedJobOfferHidesTheButtonNotTheCard() throws {
        let broken = Self.approval().replacingOccurrences(of: #""slug":"\#(Self.slug)","#, with: "")
        let decoded = try JSONDecoder.server.decode(ApprovalView.self, from: Data(broken.utf8))
        #expect(decoded.jobRequestGrantOffer == nil)
        #expect(decoded.status == .pending)
    }

    @Test
    func allowForThisRequestSendsTheOfferedJob() async throws {
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
        #expect(echo == ["job_slug": Self.slug])
        #expect(model.errorMessage == nil)
        let merged = try #require(model.runState.approvals.first { $0.id == Self.approvalID })
        #expect(merged.decision == .approveForRequest)
    }

    /// The server can withdraw a job's offer before the tap arrives. The card is read again as
    /// it now stands: still pending, with Approve once and Deny and no offer to accept.
    @Test(arguments: ["request_grant_unavailable", "request_grant_offer_mismatch"])
    func aWithdrawnJobOfferReloadsTheCardWithoutIt(reason: String) async throws {
        let reloaded = Self.approval(offer: false)
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    return (200, #"{"items":[],"next_cursor":null}"#)
                case "POST veetbot.test /v1/approvals/\(Self.approvalID.uuidString)/resolve":
                    return (409, #"{"error":{"code":"conflict","message":"conflict","details":{"reason":"\#(reason)"},"request_id":"r"}}"#)
                case "GET veetbot.test /v1/approvals/\(Self.approvalID.uuidString)":
                    return (200, reloaded)
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)
        let offered = try JSONDecoder.server.decode(ApprovalView.self, from: Data(Self.approval().utf8))

        await model.resolveApproval(offered, decision: .approveForRequest)

        #expect(model.errorMessage?.hasPrefix("Allow for this request is no longer available.") == true)
        let merged = try #require(model.runState.approvals.first { $0.id == Self.approvalID })
        #expect(merged.status == .pending)
        #expect(merged.decision == nil)
        #expect(merged.offersJobRequestGrant == false)
        #expect(merged.requestOfferSummary == nil)
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
            return (200, #"{"id":"\#(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Fix Emerging Market Radar","metadata":{},"created_at":"2099-11-06T03:00:00Z","updated_at":"2099-11-06T03:00:00Z","active_run_id":null,"last_run_id":null}"#)
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)/messages":
            return (200, #"{"items":[],"next_cursor":null}"#)
        default:
            return nil
        }
    }
}
