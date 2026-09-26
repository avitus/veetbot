import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0129, 0129-design §14 item 4: resolving with a task grant, a withdrawn
/// offer, the open conversation's active permission, Stop, and the stream's
/// task-permission events.
@Suite(.serialized) @MainActor struct ChatViewModelTaskGrantTests {
    nonisolated private static let sessionID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-000000000001")!
    nonisolated private static let grantID = UUID(uuidString: "ecb511f1-2976-5326-9f73-9748dc26211f")!
    nonisolated private static let approvalID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-0000000000a1")!
    nonisolated private static let withdrawn =
        "Allow for this task is no longer available. Allow once or Deny."

    @Test
    func theActivePermissionLoadsWhenAConversationOpens() async throws {
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    return (200, Self.page([Self.grant(used: 23)]))
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)

        #expect(model.activeTaskGrant?.id == Self.grantID)
        #expect(model.activeTaskGrant?.actionsUsed == 23)
        let list = try #require(server.entryURL(of: "GET veetbot.test /v1/browser-task-grants"))
        let query = try #require(URLComponents(string: list)?.queryItems)
        #expect(query.contains(URLQueryItem(name: "session_id", value: Self.sessionID.uuidString)))
        #expect(query.contains(URLQueryItem(name: "status", value: "active")))
    }

    @Test
    func aConversationWithoutAWebsiteProfileNeverAsksForTaskPermissions() async throws {
        let other = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "GET veetbot.test /v1/sessions/\(other.uuidString)":
                return (200, #"{"id":"\#(other.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Plain","metadata":{},"created_at":"2026-09-25T18:00:00Z","updated_at":"2026-09-25T18:00:00Z","active_run_id":null,"last_run_id":null}"#)
            case "GET veetbot.test /v1/sessions/\(other.uuidString)/messages":
                return (200, #"{"items":[],"next_cursor":null}"#)
            default:
                return nil
            }
        }
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

        await model.openSharedSession(other)
        await model.reconcileTaskGrant()

        #expect(model.selectedSessionID == other)
        #expect(model.activeTaskGrant == nil)
        #expect(!server.routes.contains { $0.contains("browser-task-grants") })
    }

    @Test
    func allowForThisTaskSendsTheOfferScopeAndShowsTheNewPermission() async throws {
        let grants = Counter()
        let resolved = try Self.contractApproval("resolved_for_task")
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    return (200, grants.next() == 1 ? Self.page([]) : Self.page([Self.grant(used: 0)]))
                case "POST veetbot.test /v1/approvals/\(Self.approvalID.uuidString)/resolve":
                    return (200, resolved)
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)
        #expect(model.activeTaskGrant == nil)
        let offered = try JSONDecoder.server.decode(
            ApprovalView.self, from: Data(try Self.contractApproval("offered").utf8)
        )
        let offer = try #require(offered.taskGrantOffer)

        await model.resolveApproval(offered, decision: .approveForTask, taskGrant: TaskGrantEcho(offer: offer))

        let body = try #require(server.json(of: "POST veetbot.test /v1/approvals/\(Self.approvalID.uuidString)/resolve"))
        #expect(body["decision"] as? String == "approve_for_task")
        #expect(
            body["task_grant"] as? [String: String]
                == ["origin": "https://www.duolingo.com", "path_prefix": "/lesson"]
        )
        #expect(model.activeTaskGrant?.id == Self.grantID)
        #expect(model.errorMessage == nil)
    }

    @Test(arguments: ["task_grant_unavailable", "task_grant_offer_mismatch"])
    func aWithdrawnOfferReloadsTheApprovalAndSaysSo(reason: String) async throws {
        let offered = try Self.contractApproval("offered")
        let reloaded = offered.replacingOccurrences(of: #""task_grant_offer":{"#, with: #""ignored_offer":{"#)
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    return (200, Self.page([]))
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
        let approval = try JSONDecoder.server.decode(ApprovalView.self, from: Data(offered.utf8))

        await model.resolveApproval(
            approval, decision: .approveForTask,
            taskGrant: TaskGrantEcho(origin: "https://www.duolingo.com", pathPrefix: "/lesson")
        )

        #expect(model.errorMessage == Self.withdrawn)
        #expect(server.routes.contains("GET veetbot.test /v1/approvals/\(Self.approvalID.uuidString)"))
        let merged = try #require(model.runState.approvals.first { $0.id == Self.approvalID })
        #expect(merged.status == .pending)
        #expect(merged.taskGrantOffer == nil)
    }

    @Test
    func stopEndsThePermissionAtOnce() async throws {
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    return (200, Self.page([Self.grant(used: 23)]))
                case "POST veetbot.test /v1/browser-task-grants/\(Self.grantID.uuidString)/revoke":
                    return (200, Self.grant(used: 23, status: "revoked"))
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)
        #expect(model.activeTaskGrant != nil)

        await model.stopTaskGrant()

        #expect(model.activeTaskGrant == nil)
        #expect(server.routes.contains("POST veetbot.test /v1/browser-task-grants/\(Self.grantID.uuidString)/revoke"))
    }

    @Test
    func aUseCountsLocallyAndACreatedOrEndedPermissionIsReRead() async throws {
        let lists = Counter()
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    return (200, lists.next() < 3 ? Self.page([Self.grant(used: 23)]) : Self.page([]))
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)

        await model.applyTaskGrantFrame(Self.authorized(ref: Self.grantID, use: 24))
        #expect(model.activeTaskGrant?.actionsUsed == 24)
        await model.applyTaskGrantFrame(Self.authorized(ref: Self.grantID, use: 24))
        #expect(model.activeTaskGrant?.actionsUsed == 24)
        await model.applyTaskGrantFrame(Self.authorized(ref: UUID(), use: 90))
        #expect(model.activeTaskGrant?.actionsUsed == 24)

        await model.applyTaskGrantFrame(
            SSEFrame(id: 40, event: "browser.task_grant.created", data: ["grant_id": .string(Self.grantID.uuidString)])
        )
        #expect(lists.value == 2)
        #expect(model.activeTaskGrant?.actionsUsed == 23)
        await model.applyTaskGrantFrame(
            SSEFrame(
                id: 41, event: "browser.task_grant.ended",
                data: ["grant_id": .string(Self.grantID.uuidString), "reason": .string("revoked")]
            )
        )
        #expect(lists.value == 3)
        #expect(model.activeTaskGrant == nil)
    }

    @Test
    func reconcileReadsTheServerAndATurnedOffFeatureIsQuiet() async throws {
        let lists = Counter()
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    switch lists.next() {
                    case 1: return (200, Self.page([Self.grant(used: 23)]))
                    case 2: return (200, Self.page([Self.grant(used: 31)]))
                    default:
                        return (404, #"{"error":{"code":"not_found","message":"The requested resource was not found.","details":{},"request_id":"r"}}"#)
                    }
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)

        await model.reconcileTaskGrant()
        #expect(model.activeTaskGrant?.actionsUsed == 31)
        await model.reconcileTaskGrant()
        #expect(model.activeTaskGrant == nil)
        #expect(model.errorMessage == nil)
    }

    // MARK: - The banner and Settings (A-11)

    @Test
    func theBannerCountsActionsAndMinutesLeft() async throws {
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    return (200, Self.page([Self.grant(used: 23)]))
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)
        let grant = try #require(model.activeTaskGrant)

        let eighteenMinutesLeft = grant.expiresAt.addingTimeInterval(-18 * 60)
        #expect(
            model.taskGrantBannerText(now: eighteenMinutesLeft)
                == "Allowed on www.duolingo.com/lesson · 23 of 200 · 18 min left"
        )
        #expect(
            grant.bannerText(now: grant.expiresAt.addingTimeInterval(-17 * 60 - 20))
                == "Allowed on www.duolingo.com/lesson · 23 of 200 · 18 min left"
        )
        #expect(
            grant.bannerText(now: grant.expiresAt.addingTimeInterval(-30))
                == "Allowed on www.duolingo.com/lesson · 23 of 200 · less than a minute left"
        )

        await model.applyTaskGrantFrame(Self.authorized(ref: Self.grantID, use: 24))
        #expect(model.taskGrantBannerText(now: eighteenMinutesLeft)?.contains("24 of 200") == true)
        model.newSession()
        #expect(model.taskGrantBannerText(now: eighteenMinutesLeft) == nil)
    }

    @Test
    func settingsListsEveryActivePermissionAndStopsOne() async throws {
        let other = UUID()
        let stopped = Counter()
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
                    if query.contains(where: { $0.name == "session_id" }) {
                        return (200, Self.page([Self.grant(used: 23)]))
                    }
                    let second = Self.grant(used: 5).replacingOccurrences(
                        of: Self.grantID.uuidString, with: other.uuidString
                    )
                    return (200, stopped.value == 0 ? Self.page([Self.grant(used: 23), second]) : Self.page([second]))
                case "POST veetbot.test /v1/browser-task-grants/\(Self.grantID.uuidString)/revoke":
                    _ = stopped.next()
                    return (200, Self.grant(used: 23, status: "revoked"))
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)

        await model.refreshTaskPermissions()
        #expect(model.activeTaskGrants.map(\.id) == [Self.grantID, other])
        let list = try #require(
            server.routes.lastIndex(of: "GET veetbot.test /v1/browser-task-grants").map { _ in true }
        )
        #expect(list)

        await model.stopTaskGrant(Self.grantID)

        #expect(model.activeTaskGrants.map(\.id) == [other])
        #expect(model.activeTaskGrant == nil)
        #expect(server.routes.contains("POST veetbot.test /v1/browser-task-grants/\(Self.grantID.uuidString)/revoke"))
    }

    /// The Mac's Settings window stays open and never re-runs onAppear, so
    /// its list follows the open conversation's permission as it is created,
    /// used and ended (0129-design §14 item 5).
    @Test
    func settingsFollowsTheOpenConversationsPermissionWhileItIsOpen() async throws {
        let otherGrant = UUID()
        let otherSession = UUID()
        let sessionReads = Counter()
        let resolved = try Self.contractApproval("resolved_for_task")
        let server = DeviceFlowServer { request in
            try Self.conversationRoutes(request) ?? {
                switch Self.route(request) {
                case "GET veetbot.test /v1/browser-task-grants":
                    let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
                    if query.contains(where: { $0.name == "session_id" }) {
                        return (200, sessionReads.next() == 2 ? Self.page([Self.grant(used: 0)]) : Self.page([]))
                    }
                    let other = Self.grant(used: 5)
                        .replacingOccurrences(of: Self.grantID.uuidString, with: otherGrant.uuidString)
                        .replacingOccurrences(of: Self.sessionID.uuidString, with: otherSession.uuidString)
                    return (200, Self.page([other]))
                case "POST veetbot.test /v1/approvals/\(Self.approvalID.uuidString)/resolve":
                    return (200, resolved)
                default:
                    return nil
                }
            }()
        }
        let model = try await openedModel(server)
        await model.refreshTaskPermissions()
        #expect(model.activeTaskGrants.map(\.id) == [otherGrant])
        let offered = try JSONDecoder.server.decode(
            ApprovalView.self, from: Data(try Self.contractApproval("offered").utf8)
        )
        let offer = try #require(offered.taskGrantOffer)

        await model.resolveApproval(offered, decision: .approveForTask, taskGrant: TaskGrantEcho(offer: offer))

        #expect(model.activeTaskGrant?.id == Self.grantID)
        #expect(Set(model.activeTaskGrants.map(\.id)) == [Self.grantID, otherGrant])

        await model.applyTaskGrantFrame(Self.authorized(ref: Self.grantID, use: 4))
        #expect(model.activeTaskGrants.first { $0.id == Self.grantID }?.actionsUsed == 4)

        await model.applyTaskGrantFrame(
            SSEFrame(
                id: 41, event: "browser.task_grant.ended",
                data: ["grant_id": .string(Self.grantID.uuidString), "reason": .string("expired")]
            )
        )
        #expect(model.activeTaskGrant == nil)
        #expect(model.activeTaskGrants.map(\.id) == [otherGrant])
    }

    @Test
    func aToolRowAllowedByATaskPermissionSaysSoAndWhatItDid() throws {
        let reducer = RunStateReducer()
        reducer.reduce(
            SSEFrame(
                id: 3, event: "tool.call.authorized",
                data: [
                    "call_id": .string("call-7"), "name": .string("browser.act"),
                    "authorization_kind": .string("browser_task_grant"),
                    "authorization_ref": .string(Self.grantID.uuidString),
                    "authorization_use": .number(7),
                    "authorization_view": .object([
                        "view": .string("browser.act.v1"), "described": .bool(true),
                        "kind": .string("type"), "page_origin": .string("https://www.duolingo.com"),
                        "page_path": .string("/lesson/unit-3"), "element_role": .string("textbox"),
                        "element_name": .string("Type in Spanish"), "field": .string("text"),
                        "text": .string("la manzana"), "consequence": .string("routine"),
                        "refused": .bool(false),
                    ]),
                ]
            )
        )
        let tool = try #require(reducer.tools.first)
        #expect(tool.allowedByTaskGrant)
        #expect(
            tool.taskGrantSummary
                == "Type into textbox “Type in Spanish” on www.duolingo.com/lesson/unit-3: “la manzana”"
        )
        reducer.reduce(
            SSEFrame(id: 4, event: "tool.call.authorized", data: ["call_id": .string("call-8"), "name": .string("browser.act")])
        )
        let plain = try #require(reducer.tools.first { $0.callID == "call-8" })
        #expect(!plain.allowedByTaskGrant)
        #expect(plain.taskGrantSummary == nil)
    }

    @Test
    func theChatShowsTheBannerAboveTheComposerAndReconcilesWhileVisible() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let chat = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/ChatView.swift"), encoding: .utf8
        )
        let banner = try #require(chat.range(of: "TaskGrantBanner("))
        let composer = try #require(chat.range(of: "            composer\n"))
        #expect(banner.lowerBound < composer.lowerBound)
        #expect(chat.contains("await model.stopTaskGrant()"))
        #expect(chat.contains("await model.reconcileTaskGrant()"))
        #expect(chat.contains("30_000_000_000"))
        #expect(chat.contains("taskGrant: taskGrant"))
        let activity = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/ActivityViews.swift"), encoding: .utf8
        )
        #expect(activity.contains("Allowed by task permission"))
        #expect(activity.contains("activity.taskGrantSummary"))
    }

    // MARK: - Helpers

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

    /// The conversation the tests open: no run, no messages.
    nonisolated private static func conversationRoutes(_ request: URLRequest) throws -> (Int, String)? {
        switch route(request) {
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)":
            return (200, #"{"id":"\#(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Lesson","metadata":{"browser_profile_id":"6a1c3f0e-2b4d-4c8e-9f10-000000000003"},"created_at":"2026-09-25T18:00:00Z","updated_at":"2026-09-25T18:00:00Z","active_run_id":null,"last_run_id":null}"#)
        case "GET veetbot.test /v1/sessions/\(sessionID.uuidString)/messages":
            return (200, #"{"items":[],"next_cursor":null}"#)
        default:
            return nil
        }
    }

    nonisolated private static func contractApproval(_ name: String) throws -> String {
        String(
            decoding: try JSONSerialization.data(
                withJSONObject: try WireModelsTests.contractExample("approval_views", name, key: "approval")
            ),
            as: UTF8.self
        )
    }

    nonisolated private static func page(_ items: [String]) -> String {
        "{\"items\":[\(items.joined(separator: ","))],\"next_cursor\":null}"
    }

    nonisolated private static func grant(used: Int, status: String = "active") -> String {
        let ended = status == "active" ? "null" : "\"2099-09-25T18:10:00Z\""
        let reason = status == "active" ? "null" : "\"\(status)\""
        return #"{"id":"\#(grantID.uuidString)","session_id":"\#(sessionID.uuidString)","profile_id":"6a1c3f0e-2b4d-4c8e-9f10-000000000003","origin":"https://www.duolingo.com","path_prefix":"/lesson","action_kinds":["click","type","select","check","press","scroll"],"status":"\#(status)","end_reason":\#(reason),"max_actions":200,"actions_used":\#(used),"max_typed_characters":4096,"typed_characters":57,"created_at":"2099-09-25T18:00:30Z","expires_at":"2099-09-25T18:30:30Z","last_used_at":null,"ended_at":\#(ended),"approval_id":"\#(approvalID.uuidString)","approved_by":"owner"}"#
    }

    nonisolated private static func authorized(ref: UUID, use: Int) -> SSEFrame {
        SSEFrame(
            id: nil,
            event: "tool.call.authorized",
            data: [
                "call_id": .string("call-\(use)"),
                "name": .string("browser.act"),
                "authorization_kind": .string("browser_task_grant"),
                "authorization_ref": .string(ref.uuidString),
                "authorization_use": .number(Double(use)),
            ]
        )
    }
}
