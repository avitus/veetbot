import Foundation
import Testing

@testable import VeetbotCore

/// A decision is visibly on its way the moment the owner taps, is sent once,
/// and gives the controls back if it fails; the conversation's header names
/// the conversation and only what it needs from the owner.
@Suite(.serialized) @MainActor struct ChatViewModelApprovalProgressTests {
    nonisolated private static let sessionID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-000000000001")!
    nonisolated private static let approvalID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-0000000000a1")!
    nonisolated private static let runID = "6A1C3F0E-2B4D-4C8E-9F10-000000000002"
    nonisolated private static let otherRunID = "6A1C3F0E-2B4D-4C8E-9F10-000000000009"
    nonisolated private static let resolveRoute =
        "POST veetbot.test /v1/approvals/\(approvalID.uuidString)/resolve"

    /// The 2026-11-03 outage: with no stream the card never loaded, and a run waiting on
    /// the owner read as "Working…" until its approval expired.
    @Test
    func aWaitingRunShowsItsApprovalWhenTheStreamIsDown() async throws {
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request, runStatus: "WAITING_FOR_APPROVAL") ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/runs/\(Self.runID)/events":
                    return (503, "")
                case "GET veetbot.test /v1/approvals":
                    return (200, try Self.pendingApprovals())
                default:
                    return nil
                }
            }()
        }

        let model = try await openedModel(server)

        #expect(model.awaitingOwnerApproval)
        #expect(
            ConversationStatus.label(
                runStatus: model.runState.runStatus, awaitingApproval: model.awaitingOwnerApproval
            ) == "Needs your approval"
        )
        #expect(
            RunActivity.label(
                isSending: false, runStatus: model.runState.runStatus,
                awaitingApproval: model.awaitingOwnerApproval,
                reasoningActive: false, reasoningTitle: nil
            ) == nil
        )
    }

    /// An approval raised after the stream died reaches the open conversation through
    /// the background read, with the run's own status, instead of clearing its marker.
    @Test
    func anOpenConversationPicksUpAnApprovalItsStreamMissed() async throws {
        let waiting = LockedFlag()
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(
                request, runStatus: waiting.value ? "WAITING_FOR_APPROVAL" : "RUNNING"
            ) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/runs/\(Self.runID)/events":
                    return (503, "")
                case "GET veetbot.test /v1/approvals":
                    return (
                        200,
                        waiting.value
                            ? try Self.pendingApprovals() : #"{"items":[],"next_cursor":null}"#
                    )
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)
        #expect(!model.awaitingOwnerApproval)
        #expect(model.runState.runStatus == .running)

        waiting.value = true
        await model.refreshSessionsAwaitingApproval()

        #expect(model.awaitingOwnerApproval)
        #expect(model.runState.runStatus == .waitingForApproval)
        #expect(model.sessionsNeedingApproval.contains(Self.sessionID))
    }

    /// The stream named the approval but neither read of its details worked, so the ID
    /// alone must not stop the background read from loading the card.
    @Test
    func anApprovalWhoseDetailsFailedToLoadIsRecovered() async throws {
        let listing = LockedFlag()
        let exampleID = try #require(
            try WireModelsTests.contractExample("approval_views", "offered", key: "approval")["id"]
                as? String
        )
        let frame = "event: approval.requested\ndata: {\"approval_id\":\"\(exampleID)\"}\n\n"
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request, runStatus: "RUNNING") ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/runs/\(Self.runID)/events":
                    return (200, frame)
                case "GET veetbot.test /v1/approvals":
                    return listing.value ? (200, try Self.pendingApprovals()) : (503, "")
                default:
                    return (503, "")
                }
            }()
        }
        let model = try await openedModel(server)
        for _ in 0..<200 where model.runState.needsApprovalIDs.isEmpty {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        #expect(!model.runState.needsApprovalIDs.isEmpty)
        #expect(!model.runState.docksAttention)

        listing.value = true
        await model.refreshSessionsAwaitingApproval()

        #expect(model.runState.docksAttention)
    }

    /// A conversation with no run of its own yet, or an older one, takes the run from the
    /// approval: another device may have started it while this stream was down.
    @Test(arguments: [false, true])
    func anApprovalOnARunThisDeviceNeverSawIsRecovered(hasOlderRun: Bool) async throws {
        let waiting = LockedFlag()
        let server = DeviceFlowServer { request in
            if Self.route(request) == "GET veetbot.test /v1/runs/\(Self.otherRunID)" {
                return (200, Self.runBody(id: Self.otherRunID, status: "WAITING_FOR_APPROVAL"))
            }
            return try Self.conversationRoutes(request, runStatus: hasOlderRun ? "RUNNING" : nil) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/runs/\(Self.runID)/events",
                    "GET veetbot.test /v1/runs/\(Self.otherRunID)/events":
                    return (503, "")
                case "GET veetbot.test /v1/approvals":
                    return (
                        200,
                        waiting.value
                            ? try Self.pendingApprovals(runID: Self.otherRunID)
                            : #"{"items":[],"next_cursor":null}"#
                    )
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)

        waiting.value = true
        await model.refreshSessionsAwaitingApproval()

        #expect(model.runState.activeRunID == UUID(uuidString: Self.otherRunID))
        #expect(model.runState.runStatus == .waitingForApproval)
        #expect(model.awaitingOwnerApproval)
    }

    /// A newer approval on a finished run must not hide an older one still waiting.
    @Test
    func anUnrecoverableApprovalDoesNotHideAnOlderOne() async throws {
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "GET veetbot.test /v1/runs/\(Self.otherRunID)":
                return (200, Self.runBody(id: Self.otherRunID, status: "COMPLETED"))
            case "GET veetbot.test /v1/runs/\(Self.runID)":
                return (200, Self.runBody(id: Self.runID, status: "WAITING_FOR_APPROVAL"))
            default:
                break
            }
            return try Self.conversationRoutes(request, runStatus: nil) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/runs/\(Self.runID)/events":
                    return (503, "")
                case "GET veetbot.test /v1/approvals":
                    return (200, try Self.pendingApprovals(olderOn: Self.runID, newerOn: Self.otherRunID))
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)

        await model.refreshSessionsAwaitingApproval()

        #expect(model.runState.activeRunID == UUID(uuidString: Self.runID))
        #expect(model.awaitingOwnerApproval)
        #expect(model.runState.runStatus == .waitingForApproval)
    }

    /// With no stream, a card decided on another device still leaves when the background
    /// read stops listing it.
    @Test
    func aCardDecidedElsewhereLeavesWithoutAStream() async throws {
        let decided = LockedFlag()
        let server = DeviceFlowServer { request in
            let path = request.url?.path ?? ""
            if path.hasPrefix("/v1/approvals/") {
                return (200, try Self.approval(resolvedWith: "approve_once"))
            }
            return try Self.conversationRoutes(request, runStatus: "WAITING_FOR_APPROVAL") ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/runs/\(Self.runID)/events":
                    return (503, "")
                case "GET veetbot.test /v1/approvals":
                    return (
                        200,
                        decided.value
                            ? #"{"items":[],"next_cursor":null}"# : try Self.pendingApprovals()
                    )
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)
        #expect(model.awaitingOwnerApproval)

        decided.value = true
        await model.refreshSessionsAwaitingApproval()

        #expect(!model.awaitingOwnerApproval)
    }

    /// One card's failed read must not leave the other decided cards on screen.
    @Test
    func aFailedReadDoesNotStopTheOtherDecidedCardsLeaving() async throws {
        let decided = LockedFlag()
        let firstReadFailed = LockedFlag()
        let server = DeviceFlowServer { request in
            let path = request.url?.path ?? ""
            if path.hasPrefix("/v1/approvals/") {
                if !firstReadFailed.value {
                    firstReadFailed.value = true
                    return (503, "")
                }
                return (200, try Self.approval(resolvedWith: "approve_once", id: request.url?.lastPathComponent))
            }
            return try Self.conversationRoutes(request, runStatus: "WAITING_FOR_APPROVAL") ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/runs/\(Self.runID)/events":
                    return (503, "")
                case "GET veetbot.test /v1/approvals":
                    return (
                        200,
                        decided.value
                            ? #"{"items":[],"next_cursor":null}"#
                            : try Self.pendingApprovals(olderOn: Self.runID, newerOn: Self.runID)
                    )
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)
        #expect(model.runState.approvals.filter { $0.status.isPending }.count == 2)

        decided.value = true
        await model.refreshSessionsAwaitingApproval()

        #expect(firstReadFailed.value)
        #expect(model.runState.approvals.filter { $0.status.isPending }.count == 1)
    }

    /// A listing that lags behind its run, or names another conversation's run, changes nothing.
    @Test(arguments: [("COMPLETED", false), ("WAITING_FOR_APPROVAL", true)])
    func aStaleOrForeignApprovalIsNotRecovered(status: String, foreignRun: Bool) async throws {
        let server = DeviceFlowServer { request in
            if Self.route(request) == "GET veetbot.test /v1/runs/\(Self.otherRunID)" {
                return (
                    200,
                    Self.runBody(
                        id: Self.otherRunID, status: status,
                        sessionID: foreignRun ? "6a1c3f0e-2b4d-4c8e-9f10-0000000000ff" : nil
                    )
                )
            }
            return try Self.conversationRoutes(request) ?? {
                guard Self.route(request) == "GET veetbot.test /v1/approvals" else { return nil }
                return (200, try Self.pendingApprovals(runID: Self.otherRunID))
            }()
        }
        let model = try await openedModel(server)

        await model.refreshSessionsAwaitingApproval()

        #expect(model.runState.activeRunID == nil)
        #expect(!model.awaitingOwnerApproval)
    }

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

    /// The conversation the tests open: no run, no messages, unless a run status is named.
    nonisolated private static func conversationRoutes(
        _ request: URLRequest, runStatus: String? = nil
    ) throws -> (Int, String)? {
        let activeRun = runStatus.map { _ in #""\#(runID)""# } ?? "null"
        switch route(request) {
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)":
            return (200, #"{"id":"\#(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Lesson","metadata":{},"created_at":"2026-09-25T18:00:00Z","updated_at":"2026-09-25T18:00:00Z","active_run_id":\#(activeRun),"last_run_id":\#(activeRun)}"#)
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)/messages":
            return (200, #"{"items":[],"next_cursor":null}"#)
        case "GET veetbot.test /v1/runs/\(runID)":
            return (200, runBody(id: runID, status: runStatus ?? "RUNNING"))
        default:
            return nil
        }
    }

    /// Two pending approvals of one conversation, on the runs named, oldest first.
    nonisolated private static func pendingApprovals(olderOn older: String, newerOn newer: String) throws -> String {
        func approval(run: String, id: String, createdAt: String) throws -> [String: Any] {
            var object = try WireModelsTests.contractExample(
                "approval_views", "offered", key: "approval"
            )
            object["id"] = id
            object["session_id"] = sessionID.uuidString.lowercased()
            object["run_id"] = run
            object["created_at"] = createdAt
            return object
        }
        let body = try JSONSerialization.data(
            withJSONObject: [
                "items": [
                    try approval(
                        run: newer, id: "6a1c3f0e-2b4d-4c8e-9f10-0000000000b2",
                        createdAt: "2026-09-25T18:05:00Z"
                    ),
                    try approval(
                        run: older, id: "6a1c3f0e-2b4d-4c8e-9f10-0000000000b1",
                        createdAt: "2026-09-25T18:00:00Z"
                    ),
                ],
                "next_cursor": NSNull(),
            ]
        )
        return String(decoding: body, as: UTF8.self)
    }

    /// One run as the server returns it, by default in the open conversation.
    nonisolated private static func runBody(id: String, status: String, sessionID owner: String? = nil) -> String {
        #"{"id":"\#(id)","session_id":"\#(owner ?? sessionID.uuidString)","parent_run_id":null,"status":"\#(status)","step_count":1,"model_call_count":1,"tool_call_count":0,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-09-25T18:00:00Z","updated_at":"2026-09-25T18:01:00Z"}"#
    }

    /// The offered contract approval, pending, belonging to the open conversation.
    nonisolated private static func pendingApprovals(runID approvalRun: String = runID) throws -> String {
        var approval = try WireModelsTests.contractExample(
            "approval_views", "offered", key: "approval"
        )
        approval["session_id"] = sessionID.uuidString.lowercased()
        approval["run_id"] = approvalRun
        let body = try JSONSerialization.data(
            withJSONObject: ["items": [approval], "next_cursor": NSNull()]
        )
        return String(decoding: body, as: UTF8.self)
    }

    /// The offered contract approval as the server returns it once resolved.
    nonisolated private static func approval(resolvedWith decision: String, id: String? = nil) throws -> String {
        var object = try WireModelsTests.contractExample("approval_views", "offered", key: "approval")
        if let id { object["id"] = id }
        object["status"] = "APPROVED"
        object["decision"] = decision
        object["resolved_at"] = "2026-09-25T18:00:30Z"
        object["resolved_by"] = "owner"
        return String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self)
    }
}

private final class LockedFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var stored = false

    var value: Bool {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}
