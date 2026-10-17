import Foundation
import Testing

@testable import VeetbotCore

/// A decision is visibly on its way the moment the owner taps, is sent once,
/// and gives the controls back if it fails; the conversation's header names
/// the conversation and only what it needs from the owner.
@Suite(.serialized) @MainActor struct ChatViewModelApprovalProgressTests {
    nonisolated private static let sessionID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-000000000001")!
    nonisolated private static let approvalID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-0000000000a1")!
    nonisolated private static let resolveRoute =
        "POST veetbot.test /v1/approvals/\(approvalID.uuidString)/resolve"

    @Test
    func aLateApprovalRefreshDoesNotLoadCardsIntoANewConversation() async throws {
        let approvalRoute = "GET veetbot.test /v1/approvals"
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                guard Self.route(request) == approvalRoute else { return nil }
                let approval = try WireModelsTests.contractExample(
                    "approval_views", "offered", key: "approval"
                )
                let body = try JSONSerialization.data(
                    withJSONObject: ["items": [approval], "next_cursor": NSNull()]
                )
                return (200, String(decoding: body, as: UTF8.self))
            }()
        }
        let model = try await openedModel(server)
        let requestsBeforeReset = server.routes.filter { $0 == approvalRoute }.count
        model.newSession()

        await model.refreshPendingApprovals()

        #expect(model.selectedSessionID == nil)
        #expect(model.runState.approvals.isEmpty)
        #expect(server.routes.filter { $0 == approvalRoute }.count == requestsBeforeReset)
    }

    @Test
    func anApprovalResolvedOnAnotherDeviceIsRefreshedWhenItLeavesThePendingList() async throws {
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/approvals":
                    return (200, #"{"items":[],"next_cursor":null}"#)
                case "GET veetbot.test /v1/approvals/\(Self.approvalID.uuidString)":
                    return (200, try Self.approval(resolvedWith: "approve_once"))
                default: return nil
                }
            }()
        }
        let model = try await openedModel(server)
        let approval = try WireModelsTests.contractApproval("offered")
        model.runState.mergeApproval(approval)
        await model.refreshPendingApprovals()
        #expect(model.runState.approvals.first?.status == .approved)
        #expect(!model.awaitingOwnerApproval)
    }

    @Test
    func aDecisionOnItsWayIsShownAndSentOnlyOnce() async throws {
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                Self.route(request) == Self.resolveRoute ? (200, try Self.approval(resolvedWith: "approve_once")) : nil
            }()
        }
        let response = server.hold(Self.resolveRoute)
        let model = try await openedModel(server)
        let approval = try WireModelsTests.contractApproval("offered")

        let first = Task { await model.resolveApproval(approval, decision: .approveOnce) }
        #expect(await server.arrival(of: Self.resolveRoute))
        #expect(model.approvalsInFlight[approval.id] == .approveOnce)
        // A second tap while the first decision is on its way.
        let second = Task { await model.resolveApproval(approval, decision: .deny) }
        await Task.yield()
        response.open()
        await first.value
        await second.value

        #expect(server.routes.filter { $0 == Self.resolveRoute }.count == 1)
        #expect(server.json(of: Self.resolveRoute)?["decision"] as? String == "approve_once")
        #expect(model.approvalsInFlight.isEmpty)
        #expect(model.runState.approvals.first { $0.id == approval.id }?.status == .approved)
        #expect(model.errorMessage == nil)
    }

    @Test
    func aFailedDecisionGivesTheControlsBack() async throws {
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                Self.route(request) == Self.resolveRoute
                    ? (503, #"{"error":{"code":"unavailable","message":"Try again.","details":{},"request_id":"r"}}"#)
                    : nil
            }()
        }
        let response = server.hold(Self.resolveRoute)
        let model = try await openedModel(server)
        let approval = try WireModelsTests.contractApproval("offered")
        model.runState.mergeApproval(approval)

        let decision = Task { await model.resolveApproval(approval, decision: .deny) }
        #expect(await server.arrival(of: Self.resolveRoute))
        #expect(model.approvalsInFlight[approval.id] == .deny)
        response.open()
        await decision.value

        #expect(model.approvalsInFlight.isEmpty)
        #expect(model.errorMessage != nil)
        #expect(model.runState.approvals.first { $0.id == approval.id }?.status == .pending)
    }

    @Test
    func anApprovalWhoseDecisionIsOnItsWayNoLongerWaitsOnTheOwner() async throws {
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                Self.route(request) == Self.resolveRoute ? (200, try Self.approval(resolvedWith: "approve_once")) : nil
            }()
        }
        let response = server.hold(Self.resolveRoute)
        let model = try await openedModel(server)
        let approval = try WireModelsTests.contractApproval("offered")
        model.runState.begin(runID: approval.runID, status: .waitingForApproval)
        model.runState.mergeApproval(approval)
        #expect(model.awaitingOwnerApproval)

        let decision = Task { await model.resolveApproval(approval, decision: .approveOnce) }
        #expect(await server.arrival(of: Self.resolveRoute))
        #expect(!model.awaitingOwnerApproval)
        response.open()
        await decision.value
        #expect(!model.awaitingOwnerApproval)
    }

    @Test
    func theTitleNamesTheOpenConversation() async throws {
        let server = DeviceFlowServer { request in try Self.conversationRoutes(request) }
        let model = try makeModel(server)
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        #expect(model.selectedConversationTitle == "New conversation")

        await model.openSharedSession(Self.sessionID)

        #expect(model.selectedConversationTitle == "Lesson")
    }

    @Test
    func eachDecisionSaysWhatItIsDoingWhileOnItsWay() {
        #expect(ApprovalDecision.approveOnce.progressLabel(websiteCard: false) == "Approving…")
        #expect(ApprovalDecision.deny.progressLabel(websiteCard: false) == "Denying…")
        #expect(ApprovalDecision.approveOnce.progressLabel(websiteCard: true) == "Allowing…")
        #expect(ApprovalDecision.approveForTask.progressLabel(websiteCard: true) == "Allowing…")
        #expect(ApprovalDecision.deny.progressLabel(websiteCard: true) == "Denying…")
    }

    // MARK: - Helpers

    private func makeModel(_ server: DeviceFlowServer) throws -> ChatViewModel {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        return ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: URLSession(configuration: server.configuration(base: .ephemeral))
        )
    }

    private func openedModel(_ server: DeviceFlowServer) async throws -> ChatViewModel {
        let model = try makeModel(server)
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        await model.openSharedSession(Self.sessionID)
        #expect(model.selectedSessionID == Self.sessionID)
        return model
    }

    nonisolated private static func route(_ request: URLRequest) -> String {
        "\(request.httpMethod ?? "") \(request.url?.host ?? "") \(request.url?.path ?? "")"
    }

    /// The conversation the tests open: no run, no messages.
    nonisolated private static func conversationRoutes(_ request: URLRequest) throws -> (Int, String)? {
        switch route(request) {
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)":
            return (200, #"{"id":"\#(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Lesson","metadata":{},"created_at":"2026-09-25T18:00:00Z","updated_at":"2026-09-25T18:00:00Z","active_run_id":null,"last_run_id":null}"#)
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)/messages":
            return (200, #"{"items":[],"next_cursor":null}"#)
        case "GET veetbot.test /v1/runs/6A1C3F0E-2B4D-4C8E-9F10-000000000002":
            return (200, #"{"id":"6a1c3f0e-2b4d-4c8e-9f10-000000000002","session_id":"\#(sessionID.uuidString)","parent_run_id":null,"status":"RUNNING","step_count":1,"model_call_count":1,"tool_call_count":0,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-09-25T18:00:00Z","updated_at":"2026-09-25T18:01:00Z"}"#)
        default:
            return nil
        }
    }

    /// The offered contract approval as the server returns it once resolved.
    nonisolated private static func approval(resolvedWith decision: String) throws -> String {
        var object = try WireModelsTests.contractExample("approval_views", "offered", key: "approval")
        object["status"] = "APPROVED"
        object["decision"] = decision
        object["resolved_at"] = "2026-09-25T18:00:30Z"
        object["resolved_by"] = "owner"
        return String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self)
    }
}
