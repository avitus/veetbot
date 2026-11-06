import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0302: one approval for a request on a signed-in website. The card
/// offers "Allow for this request"; resolving it sends the offered profile,
/// and a withdrawn offer reloads the card and says so.
@Suite(.serialized) @MainActor struct ChatViewModelRequestGrantTests {
    nonisolated private static let sessionID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-000000000101")!
    nonisolated private static let approvalID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-0000000001a1")!
    nonisolated private static let profileID = "6a1c3f0e-2b4d-4c8e-9f10-000000000103"
    nonisolated private static let withdrawn =
        "Allow for this request is no longer available. Allow once or Deny."

    nonisolated private static func approval(
        status: String = "PENDING", decision: String = "null", offer: Bool = true
    ) -> String {
        let offerJSON = offer
            ? #""request_grant_offer":{"profile_id":"\#(profileID)","profile_generation":5,"origins":["https://secure.chase.com"],"duration_seconds":7200,"summary":"Allow for this request: open pages, choose menus and filters, type dates and searches, and save downloaded files into this chat on secure.chase.com."},"#
            : ""
        return #"{"id":"\#(approvalID.uuidString)","run_id":"6a1c3f0e-2b4d-4c8e-9f10-000000000102","session_id":"\#(sessionID.uuidString)","status":"\#(status)","tool_name":"browser.act","action_summary":"Click a button on secure.chase.com/web/auth/dashboard","arguments":{"view":"browser.act.v1","described":true,"kind":"click","element_role":"button","element_name":"See all transactions","page_origin":"https://secure.chase.com","page_path":"/web/auth/dashboard","consequence":"unknown","refused":false},"argument_digests":{},"risk":"HIGH","policy_reason":"Veetbot asks before changing anything outside Veetbot.","expires_at":"2099-11-06T07:30:10Z","created_at":"2099-11-06T03:30:10Z","resolved_at":null,"resolved_by":null,\#(offerJSON)"decision":\#(decision)}"#
    }

    @Test
    func theOfferAndTheDecisionDecode() throws {
        let offered = try JSONDecoder.server.decode(ApprovalView.self, from: Data(Self.approval().utf8))
        let offer = try #require(offered.requestGrantOffer)

        #expect(offer.profileID.uuidString.lowercased() == Self.profileID)
        #expect(offer.origins == ["https://secure.chase.com"])
        #expect(offer.summary.hasPrefix("Allow for this request"))
        let resolved = try JSONDecoder.server.decode(
            ApprovalView.self,
            from: Data(Self.approval(status: "APPROVED", decision: #""approve_for_request""#).utf8)
        )
        #expect(resolved.decision == .approveForRequest)
    }

    @Test
    func theCardOffersTheRequestOnlyWhilePending() throws {
        let offered = try JSONDecoder.server.decode(ApprovalView.self, from: Data(Self.approval().utf8))
        let resolved = try JSONDecoder.server.decode(
            ApprovalView.self,
            from: Data(Self.approval(status: "APPROVED", decision: #""approve_for_request""#).utf8)
        )
        let plain = try JSONDecoder.server.decode(ApprovalView.self, from: Data(Self.approval(offer: false).utf8))

        #expect(try #require(BrowserActionApprovalPresentation(approval: offered)).offersRequestGrant)
        #expect(try #require(BrowserActionApprovalPresentation(approval: resolved)).offersRequestGrant == false)
        #expect(try #require(BrowserActionApprovalPresentation(approval: plain)).offersRequestGrant == false)
        #expect(ApprovalDecision.approveForRequest.progressLabel(websiteCard: true) == "Allowing…")
    }

    @Test
    func allowForThisRequestSendsTheOfferedProfile() async throws {
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
        // UUIDs go out uppercase; the server parses either case.
        let echo = try #require(body["request_grant"] as? [String: String])
        #expect(Array(echo.keys) == ["profile_id"])
        #expect(echo["profile_id"]?.lowercased() == Self.profileID)
        #expect(body["task_grant"] == nil)
        #expect(model.errorMessage == nil)
        let merged = try #require(model.runState.approvals.first { $0.id == Self.approvalID })
        #expect(merged.decision == .approveForRequest)
    }

    @Test(arguments: ["request_grant_unavailable", "request_grant_offer_mismatch"])
    func aWithdrawnOfferReloadsTheApprovalAndSaysSo(reason: String) async throws {
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

        #expect(model.errorMessage == Self.withdrawn)
        let merged = try #require(model.runState.approvals.first { $0.id == Self.approvalID })
        #expect(merged.status == .pending)
        #expect(merged.requestGrantOffer == nil)
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
            return (200, #"{"id":"\#(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Chase","metadata":{"browser_profile_id":"\#(profileID)"},"created_at":"2099-11-06T03:00:00Z","updated_at":"2099-11-06T03:00:00Z","active_run_id":null,"last_run_id":null}"#)
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)/messages":
            return (200, #"{"items":[],"next_cursor":null}"#)
        default:
            return nil
        }
    }
}
