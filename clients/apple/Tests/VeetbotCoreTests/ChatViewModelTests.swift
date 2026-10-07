import Combine
import Foundation
import Testing
import UserNotifications

@testable import VeetbotCore

@Suite(.serialized) @MainActor struct ChatViewModelTests {
    @Test
    func testSendWaitsForStartupHistoryReconciliation() async throws {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let configurationStore = ConnectionConfigurationStore(defaults: defaults)
        await configurationStore.save(try ConnectionConfiguration(baseURLString: "https://veetbot.test"))
        let historyStore = SuspendedReconciliationHistoryStore()
        defer { Task { await historyStore.release() } }
        let lock = NSLock()
        var writeCount = 0
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "saved-token"),
            configurationStore: configurationStore,
            historyStore: historyStore,
            urlSession: urlSession { request in
                if request.httpMethod == "GET", request.url?.path == "/v1/sessions" {
                    return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
                }
                lock.withLock { writeCount += 1 }
                return try response(for: request, statusCode: 503, body: "{}")
            }
        )
        for _ in 0..<5_000 {
            if await historyStore.isWaiting { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        try #require(await historyStore.isWaiting)
        #expect(model.currentAPIClient != nil)
        #expect(!model.isConfigured)
        model.composerText = "Keep this draft until startup finishes"
        #expect(!ChatView(model: model).canSendDraft)
        #expect(await model.send(model.composerText) == false)
        #expect(lock.withLock { writeCount } == 0)
        #expect(model.errorMessage == nil)
        #expect(model.composerText == "Keep this draft until startup finishes")
        await historyStore.release()
        for await restoring in model.$isBootstrapping.values {
            if !restoring { break }
        }
        #expect(model.isConfigured)
        #expect(ChatView(model: model).canSendDraft)
    }

    @Test(arguments: [true, false], [true, false])
    func testStartupFinishesWithSavedConnectionOrSetup(
        savedConfiguration: Bool, savedToken: Bool
    ) async throws {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let configurationStore = ConnectionConfigurationStore(defaults: defaults)
        if savedConfiguration {
            await configurationStore.save(try ConnectionConfiguration(baseURLString: "https://veetbot.test"))
        }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: savedToken ? "saved-token" : nil),
            configurationStore: configurationStore,
            historyStore: VolatileSessionHistoryStore(),
            urlSession: urlSession { request in
                #expect(savedConfiguration && savedToken)
                #expect(request.url?.path == "/v1/sessions")
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            }
        )
        #expect(model.isBootstrapping)
        #expect(!model.isConfigured)
        #expect(AppCoordinator(chat: model).mode == .chat)
        for await restoring in model.$isBootstrapping.values {
            if !restoring { break }
        }
        #expect(model.isConfigured == (savedConfiguration && savedToken))
        #expect(model.errorMessage == nil)
    }

    @Test
    func testFailedStartupLeavesSetupAvailable() async throws {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let configurationStore = ConnectionConfigurationStore(defaults: defaults)
        await configurationStore.save(try ConnectionConfiguration(baseURLString: "https://veetbot.test"))
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "saved-token"),
            configurationStore: configurationStore,
            historyStore: VolatileSessionHistoryStore(),
            urlSession: urlSession { _ in throw URLError(.notConnectedToInternet) }
        )
        #expect(model.isBootstrapping)
        for await restoring in model.$isBootstrapping.values {
            if !restoring { break }
        }
        #expect(!model.isConfigured)
        #expect(model.errorMessage != nil)
    }

    @Test
    func testDeletingCallClearsDerivedChatPresentation() async throws {
        let callID = UUID()
        let model = try configuredModel { request in
            if request.url?.path == "/v1/sessions" {
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            }
            if request.url?.path.lowercased() == "/v1/calls/\(callID.uuidString.lowercased())" {
                let body = request.httpMethod == "DELETE"
                    ? "{\"call_id\":\"\(callID)\",\"erased\":true,\"provider_deleted\":false}"
                    : "{\"call_id\":\"\(callID)\",\"summary\":\"Private call summary\"}"
                return try response(for: request, statusCode: 200, body: body)
            }
            Issue.record("Unexpected call result request")
            return try response(for: request, statusCode: 500, body: "{}")
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let message = try JSONDecoder().decode(SessionMessageView.self, from: Data(
            #"{"sequence":1,"role":"assistant","content":[{"type":"text","text":"Private call summary"}]}"#.utf8
        ))
        model.runState.restore(messages: [message])
        let payload = try #require(NotificationPushPayload(userInfo: ["veetbot": [
            "version": 1, "kind": "call_finished", "title": "New call result",
            "call_id": callID.uuidString, "notification_id": UUID().uuidString,
        ]]))
        await model.openNotification(payload)
        #expect(model.callResult?.summary == "Private call summary")
        await model.deleteCallResult()
        #expect(model.callResult?.erased == true)
        #expect(model.runState.timeline.isEmpty)
    }

    @Test
    func testDeniedNotificationPermissionDoesNotPresentARepeatedAppError() throws {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "existing-token"),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore()
        )
        let denied = NSError(domain: UNErrorDomain, code: UNError.notificationsNotAllowed.rawValue)
        model.reportNotificationRegistrationFailure(denied)
        model.reportNotificationRegistrationFailure(denied)
        #expect(model.errorMessage == nil)

        let unavailable = NSError(
            domain: NSURLErrorDomain, code: NSURLErrorNotConnectedToInternet,
            userInfo: [NSLocalizedDescriptionKey: "Push registration is offline"]
        )
        model.reportNotificationRegistrationFailure(unavailable)
        #expect(model.errorMessage == "Push registration is offline")
        model.reportNotificationRegistrationFailure(denied)
        #expect(model.errorMessage == "Push registration is offline")
    }

    @Test
    func testConfigureReportsTheCurrentAttemptFailure() async {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "existing-token"),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore()
        )

        let configured = await model.configure(
            baseURLString: "not a server URL",
            token: "replacement-token"
        )

        #expect(configured == false)
        #expect(model.errorMessage != nil)
    }

    @Test
    func testForgetCredentialsDeletesTheLocalTokenWhenDeviceRevocationFails() async throws {
        let installationID = "00000000-0000-0000-0000-000000000123"
        let deviceID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000456")
        )
        let tokenStore = InMemoryTokenStore(token: "local-bearer")
        let coordinator = DeviceRegistrationCoordinator(
            identityStore: InMemoryInstallationIdentityStore(installationID: installationID)
        )
        let session = urlSession { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("GET", "/v1/devices"):
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[{"id":"\(deviceID.uuidString)","client_device_id":"\(installationID)","name":"Owner's iPhone","kind":"mobile","platform":"ios","app_bundle_id":"com.veetbot.apple","push_provider":"apns","push_environment":"sandbox","push_token_fingerprint":"abcdef","push_token_updated_at":"2026-08-22T00:00:00Z","push_token_invalidated_at":null,"muted_kinds":[],"status":"active","revoked_at":null,"last_seen_at":"2026-08-22T00:00:00Z","created_at":"2026-08-22T00:00:00Z","updated_at":"2026-08-22T00:00:00Z"}],"next_cursor":null}
                        """
                )
            case ("POST", "/v1/devices/\(deviceID.uuidString)/revoke"):
                return try self.response(
                    for: request,
                    statusCode: 503,
                    body: #"{"error":{"code":"service_unavailable","message":"unavailable","details":{},"request_id":"revoke-failed"}}"#
                )
            default:
                Issue.record(
                    "unexpected request: \(request.httpMethod ?? "nil") \(request.url?.path ?? "nil")"
                )
                return try self.response(for: request, statusCode: 500, body: "")
            }
        }
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let model = ChatViewModel(
            tokenStore: tokenStore,
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            deviceRegistrationCoordinator: coordinator,
            urlSession: session
        )

        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "local-bearer"
            )
        )
        await model.forgetCredentials()

        #expect(await tokenStore.readToken() == nil)
        #expect(model.isConfigured == false)
        #expect(model.requiresReauthentication)
        #expect(model.errorMessage != nil)
    }

    @Test
    func testNotificationTapRestoresTranscriptAttachesExactRunAndFocusesApproval() async throws {
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000101")
        )
        let runID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000102")
        )
        let approvalID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000103")
        )
        let notificationID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000104")
        )
        let sessionJSON = """
            {"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Push target","metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:04:00Z","active_run_id":"\(runID.uuidString)","last_run_id":"\(runID.uuidString)"}
            """
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: "{\"items\":[\(sessionJSON)],\"next_cursor\":null}"
                )
            case ("GET", "/v1/sessions/\(sessionID.uuidString)"):
                return try response(for: request, statusCode: 200, body: sessionJSON)
            case ("GET", "/v1/sessions/\(sessionID.uuidString)/messages"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[{"sequence":1,"role":"user","content":[{"type":"text","text":"Restored before focus"}]}],"next_cursor":null}"#
                )
            case ("GET", "/v1/runs/\(runID.uuidString)"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"id":"\(runID.uuidString)","session_id":"\(sessionID.uuidString)","parent_run_id":null,"status":"WAITING_FOR_APPROVAL","step_count":1,"model_call_count":1,"tool_call_count":1,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-08-14T00:03:00Z","updated_at":"2026-08-14T00:04:00Z"}
                        """
                )
            case ("GET", "/v1/approvals/\(approvalID.uuidString)"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"id":"\(approvalID.uuidString)","run_id":"\(runID.uuidString)","session_id":"\(sessionID.uuidString)","status":"PENDING","tool_name":"sandbox.run_command","action_summary":"Run command","arguments":{},"risk":"HIGH","policy_reason":"approval required","expires_at":null,"created_at":"2026-08-14T00:04:00Z","resolved_at":null,"resolved_by":null,"decision":null}
                        """
                )
            case ("GET", "/v1/runs/\(runID.uuidString)/events"):
                return try response(for: request, statusCode: 200, body: "")
            default:
                Issue.record(
                    "unexpected request: \(request.httpMethod ?? "nil") \(request.url?.path ?? "nil")"
                )
                return try response(for: request, statusCode: 500, body: "")
            }
        }
        let payload = try #require(
            NotificationPushPayload(
                userInfo: [
                    "veetbot": [
                        "version": 1,
                        "kind": "approval_requested",
                        "title": "Approval needed",
                        "status": "WAITING_FOR_APPROVAL",
                        "tool_name": "sandbox.run_command",
                        "session_id": sessionID.uuidString,
                        "run_id": runID.uuidString,
                        "approval_id": approvalID.uuidString,
                        "notification_id": notificationID.uuidString,
                    ]
                ]
            )
        )

        let delegate = NotificationApplicationDelegateBase(remoteRegistrationEnabled: false)
        delegate.received(payload: payload)
        #expect(delegate.pendingResponseCount == 1)
        delegate.attach(to: model)
        #expect(delegate.pendingResponseCount == 0)
        #expect(model.selectedSessionID == nil)
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )

        #expect(model.selectedSessionID == sessionID)
        #expect(model.runState.activeRunID == runID)
        #expect(model.runState.timeline.map(\.text) == ["Restored before focus"])
        #expect(model.runState.approvals.map(\.id) == [approvalID])
        #expect(model.notificationFocus == .approval(approvalID))
        #expect(model.notificationNavigationID != nil)
    }

    @Test
    func testColdAndWarmEmailNotificationsRouteWithoutReplacingChatState() async throws {
        let sessionID = UUID()
        let threadID = UUID()
        let runID = UUID()
        let approvalID = UUID()
        let model = try configuredModel { request in
            if request.url?.path == "/v1/sessions" {
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            }
            if request.url?.path == "/v1/sessions/\(sessionID)" {
                return try response(for: request, statusCode: 200, body: """
                    {"id":"\(sessionID)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Email discussion","metadata":{"email_thread_id":"\(threadID)","email_account_id":"work"},"created_at":"2026-09-11T00:00:00Z","updated_at":"2026-09-11T00:00:00Z","active_run_id":null,"last_run_id":null}
                    """)
            }
            if request.url?.path == "/v1/email/threads/\(threadID)" {
                return try response(for: request, statusCode: 200, body: Self.emailThreadJSON(
                    threadID, sessionID: sessionID, runID: runID, draftApprovalID: approvalID
                ))
            }
            Issue.record("Email notification unexpectedly replaced Chat: \(request.url?.path ?? "")")
            return try response(for: request, statusCode: 500, body: "{}")
        }
        var routed: [(UUID, UUID?)] = []
        model.emailNotificationHandler = { routed.append(($0, $1)) }
        let payload = try #require(NotificationPushPayload(userInfo: ["veetbot": [
            "version": 1, "kind": "approval_requested", "title": "Approval needed",
            "status": "WAITING_FOR_APPROVAL", "session_id": sessionID.uuidString,
            "run_id": runID.uuidString, "approval_id": approvalID.uuidString,
            "notification_id": UUID().uuidString,
        ]]))
        let delegate = NotificationApplicationDelegateBase(remoteRegistrationEnabled: false)
        delegate.received(payload: payload)
        delegate.attach(to: model)
        #expect(routed.isEmpty)
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        #expect(routed.count == 1)
        #expect(routed.first?.0 == threadID)
        #expect(routed.first?.1 == approvalID)
        model.composerText = "Keep the unfinished Chat message"
        await model.openNotification(payload)
        #expect(routed.count == 2)
        #expect(model.composerText == "Keep the unfinished Chat message")
        #expect(model.selectedSessionID == nil)
        #expect(model.runState.activeRunID == nil)
    }

    /// Discuss in Chat runs in the thread's own session, so a tool approval the
    /// conversation raises arrives with that session. Email acts only on the
    /// approval its draft awaits; this one is answered in Chat, even when the
    /// owner tapped it from Email.
    @Test
    func testThreadSessionApprovalTheDraftDoesNotAwaitOpensTheDiscussionInChat() async throws {
        let ids = ThreadSessionIDs()
        let model = try threadSessionModel(ids, runStatus: "WAITING_FOR_APPROVAL")
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let coordinator = AppCoordinator(chat: model)
        coordinator.mode = .email
        let payload = try #require(NotificationPushPayload(userInfo: ["veetbot": [
            "version": 1, "kind": "approval_requested", "title": "Approval needed",
            "status": "WAITING_FOR_APPROVAL", "tool_name": "sandbox.run",
            "session_id": ids.session.uuidString, "run_id": ids.run.uuidString,
            "approval_id": ids.approval.uuidString, "notification_id": UUID().uuidString,
        ]]))

        await model.openNotification(payload)

        #expect(coordinator.mode == .chat)
        #expect(coordinator.email.selectedThreadID == nil)
        #expect(model.selectedSessionID == ids.session)
        #expect(model.runState.approvals.map(\.id) == [ids.approval])
        #expect(model.notificationFocus == .approval(ids.approval))
    }

    /// A thread that cannot be read cannot show that Email owns the approval,
    /// so the tap falls back to Chat, which can resolve any approval.
    @Test
    func testThreadSessionApprovalOpensChatWhenTheThreadCannotBeRead() async throws {
        let ids = ThreadSessionIDs()
        let model = try threadSessionModel(ids, runStatus: "WAITING_FOR_APPROVAL", threadReadFails: true)
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let coordinator = AppCoordinator(chat: model)
        coordinator.mode = .email
        let payload = try #require(NotificationPushPayload(userInfo: ["veetbot": [
            "version": 1, "kind": "approval_requested", "title": "Approval needed",
            "status": "WAITING_FOR_APPROVAL", "tool_name": "sandbox.run",
            "session_id": ids.session.uuidString, "run_id": ids.run.uuidString,
            "approval_id": ids.approval.uuidString, "notification_id": UUID().uuidString,
        ]]))

        await model.openNotification(payload)

        #expect(coordinator.mode == .chat)
        #expect(model.selectedSessionID == ids.session)
        #expect(model.notificationFocus == .approval(ids.approval))
        #expect(model.errorMessage == nil)
    }

    /// Email has no question card, so a question from the thread's discussion
    /// opens in Chat, where the owner can answer it.
    @Test
    func testThreadSessionQuestionOpensTheDiscussionInChat() async throws {
        let ids = ThreadSessionIDs()
        let model = try threadSessionModel(ids, runStatus: "WAITING_FOR_USER")
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let coordinator = AppCoordinator(chat: model)
        coordinator.mode = .email
        let payload = try #require(NotificationPushPayload(userInfo: ["veetbot": [
            "version": 1, "kind": "question_asked", "title": "The agent has a question",
            "status": "WAITING_FOR_USER", "session_id": ids.session.uuidString,
            "run_id": ids.run.uuidString, "question_id": ids.question.uuidString,
            "notification_id": UUID().uuidString,
        ]]))

        await model.openNotification(payload)

        #expect(coordinator.mode == .chat)
        #expect(coordinator.email.selectedThreadID == nil)
        #expect(model.selectedSessionID == ids.session)
        #expect(model.notificationFocus == .question(ids.question))
    }

    /// The Mac window observes the coordinator while account capabilities arrive asynchronously.
    @Test
    func testCoordinatorUpdatesWindowControlsWhenEmailAccountsLoadOrReset() async throws {
        let model = try configuredModel { request in
            let body = request.url?.path == "/v1/email/accounts"
                ? #"{"items":[{"id":"work","label":"Work","status":"ready","history_complete":true,"history_processed":1,"unsubscribe_supported":true}]}"#
                : #"{"items":[],"next_cursor":null}"#
            return try response(for: request, statusCode: 200, body: body)
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let coordinator = AppCoordinator(chat: model)
        var changes = 0
        let subscription = coordinator.objectWillChange.sink { changes += 1 }
        defer { subscription.cancel() }
        #expect(!coordinator.email.unsubscribeAvailable)

        await coordinator.email.reload()
        #expect(coordinator.email.unsubscribeAvailable)
        #expect(changes > 0, "The window must discover newly advertised controls")

        changes = 0
        coordinator.email.resetConnection()
        #expect(!coordinator.email.unsubscribeAvailable)
        #expect(changes > 0, "The window must withdraw controls and any open capability sheet")
    }

    @Test
    func testModeSwitchPreservesLiveChatAndUsesSameConnectionForEmailHandoff() async throws {
        let chatSessionID = UUID()
        let discussionID = UUID()
        let threadID = UUID()
        let runID = UUID()
        let lock = NSLock()
        var requests: [URLRequest] = []
        let model = try configuredModel { request in
            lock.withLock { requests.append(request) }
            let path = request.url!.path
            if path == "/v1/sessions" || path.hasSuffix("/messages") {
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            }
            if path == "/v1/email/threads/\(threadID)" {
                return try response(for: request, statusCode: 200, body: """
                    {"id":"\(threadID)","account_id":"work","subject":"Shared discussion","senders":["alex@example.test"],"updated_at":"2026-09-11T00:00:00Z","revision":1,"summary":"Review the agenda","reason":"A colleague","needs_reply":false,"draft_id":null,"session_id":"\(discussionID)","priority":0.9,"complete":true,"messages":[],"draft":null}
                    """)
            }
            if path == "/v1/sessions/\(chatSessionID)" || path == "/v1/sessions/\(discussionID)" {
                let id = path.hasSuffix(discussionID.uuidString) ? discussionID : chatSessionID
                return try response(for: request, statusCode: 200, body: """
                    {"id":"\(id)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Shared agent","metadata":{},"created_at":"2026-09-11T00:00:00Z","updated_at":"2026-09-11T00:00:00Z","active_run_id":null,"last_run_id":null}
                    """)
            }
            Issue.record("Unexpected mode request: \(path)")
            return try response(for: request, statusCode: 500, body: "{}")
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "same-owner-token"))
        await model.openSharedSession(chatSessionID)
        model.composerText = "Keep my unfinished Chat message"
        model.runState.begin(runID: runID, status: .running)
        let coordinator = AppCoordinator(chat: model)
        coordinator.mode = .email
        await coordinator.email.openThread(threadID)
        coordinator.mode = .chat
        #expect(coordinator.chat === model)
        #expect(model.selectedSessionID == chatSessionID)
        #expect(model.runState.activeRunID == runID)
        #expect(model.runState.runStatus == .running)
        #expect(model.composerText == "Keep my unfinished Chat message")
        #expect(coordinator.email.selectedThreadID == threadID)
        coordinator.mode = .email
        await coordinator.discussSelectedThread()
        #expect(coordinator.mode == .chat)
        #expect(model.selectedSessionID == discussionID)
        #expect(coordinator.email.selectedThreadID == threadID)
        #expect(model.composerText == "Keep my unfinished Chat message")
        let observed = lock.withLock { requests }
        #expect(observed.allSatisfy { $0.value(forHTTPHeaderField: "Authorization") == "Bearer same-owner-token" })
        #expect(observed.allSatisfy { $0.url?.host == "veetbot.test" })
        #expect(!observed.contains { $0.httpMethod == "POST" }, "mode handoff must not forge an owner message or create another agent")
    }

    @Test
    func testHistoryPaginationHasNoArbitraryPageCapAndRejectsLoops() throws {
        var seen: Set<String> = []

        for page in 1 ... 101 {
            let cursor = "cursor-\(page)"
            #expect(try nextPageCursor(cursor, seen: &seen) == cursor)
        }
        #expect(seen.count == 101)
        #expect(throws: HTTPTransportError.self) {
            try nextPageCursor("cursor-101", seen: &seen)
        }
        #expect(try nextPageCursor(nil, seen: &seen) == nil)
    }

    @Test
    func testConfigureFailsWhenInitialHistoryNeedsAServerUpgrade() async throws {
        let model = try configuredModel { request in
            try response(
                for: request,
                statusCode: 400,
                body: #"{"error":{"code":"malformed_request","message":"The HTTP request is not supported.","details":{},"request_id":"old-server"}}"#
            )
        }
        let configured = await model.configure(
            baseURLString: "https://veetbot.test",
            token: "replacement-token"
        )

        #expect(configured == false)
        #expect(model.isConfigured == false)
        #expect(model.baseURL == nil)
        #expect(model.errorMessage?.contains("Update the server") == true)
    }

    @Test
    func testConfigureFailsWhenInitialHistoryRequiresReauthentication() async throws {
        let model = try configuredModel { request in
            try response(
                for: request,
                statusCode: 401,
                body: #"{"error":{"code":"authentication_error","message":"expired","details":{},"request_id":"expired-token"}}"#
            )
        }
        let configured = await model.configure(
            baseURLString: "https://veetbot.test",
            token: "replacement-token"
        )

        #expect(configured == false)
        #expect(model.isConfigured == false)
        #expect(model.requiresReauthentication == true)
        #expect(model.errorMessage == "expired")
    }

    @Test
    func testConfigureFailsOnAnyOtherInitialHistoryError() async throws {
        let model = try configuredModel { request in
            try response(
                for: request,
                statusCode: 500,
                body: #"{"error":{"code":"internal_error","message":"temporarily unavailable","details":{},"request_id":"server-error"}}"#
            )
        }
        let configured = await model.configure(
            baseURLString: "https://veetbot.test",
            token: "replacement-token"
        )

        #expect(configured == false)
        #expect(model.isConfigured == false)
        #expect(model.baseURL == nil)
        #expect(model.errorMessage == "temporarily unavailable")
    }

    /// ADR-0155: the server titles a conversation a few seconds after a reply
    /// ends, so the sidebar reads the list once more after a watched run ends.
    @Test
    func testTheSidebarRereadsTitlesShortlyAfterAWatchedReplyEnds() async throws {
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000321"))
        let runID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000654"))
        let lock = NSLock()
        var listReads = 0
        func sessionBody(_ title: String) -> String {
            """
            {"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"\(title)","metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:04:00Z","active_run_id":"\(runID.uuidString)","last_run_id":"\(runID.uuidString)"}
            """
        }
        let model = try configuredModel(titleRefreshDelay: 0.05) { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                // The first read predates the reply; later ones carry the server's title.
                let read = lock.withLock { () -> Int in
                    listReads += 1
                    return listReads
                }
                let title = read == 1 ? "can you help with the garden" : "Shade garden planting"
                return try response(
                    for: request, statusCode: 200,
                    body: "{\"items\":[\(sessionBody(title))],\"next_cursor\":null}"
                )
            case ("GET", "/v1/sessions/\(sessionID.uuidString)"):
                return try response(
                    for: request, statusCode: 200, body: sessionBody("can you help with the garden")
                )
            case ("GET", "/v1/sessions/\(sessionID.uuidString)/messages"):
                return try response(
                    for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#
                )
            case ("GET", "/v1/runs/\(runID.uuidString)"):
                return try response(
                    for: request, statusCode: 200,
                    body: """
                        {"id":"\(runID.uuidString)","session_id":"\(sessionID.uuidString)","parent_run_id":null,"status":"RUNNING","step_count":1,"model_call_count":1,"tool_call_count":0,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-08-14T00:03:00Z","updated_at":"2026-08-14T00:04:00Z"}
                        """
                )
            case ("GET", "/v1/runs/\(runID.uuidString)/events"):
                return try response(
                    for: request, statusCode: 200,
                    body: "id: 3\nevent: run.completed\ndata: {\"run_id\":\"\(runID.uuidString)\"}\n\n"
                )
            default:
                return try response(
                    for: request, statusCode: 404,
                    body: #"{"error":{"code":"not_found","message":"not found","details":{},"request_id":"t"}}"#
                )
            }
        }
        defer { model.newSession() }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "token"))
        let entry = try #require(model.history.first)
        #expect(entry.title == "can you help with the garden")

        await model.selectSession(entry)
        for _ in 0 ..< 200 where model.history.first?.title != "Shade garden planting" {
            try await Task.sleep(nanoseconds: 10_000_000)
        }

        #expect(model.history.first?.title == "Shade garden planting")
    }

    @Test
    func testSelectingHistoricalSessionAfterRelaunchRestoresEveryCompletedTurn() async throws {
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000123")
        )
        let runID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000456")
        )
        let sessionBody = """
            {"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"First question","metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:04:00Z","active_run_id":null,"last_run_id":"\(runID.uuidString)"}
            """
        let lock = NSLock()
        var messageRequests = 0
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: "{\"items\":[\(sessionBody)],\"next_cursor\":null}"
                )
            case ("GET", "/v1/sessions/\(sessionID.uuidString)"):
                return try response(for: request, statusCode: 200, body: sessionBody)
            case ("GET", "/v1/sessions/\(sessionID.uuidString)/messages"):
                let attempt = lock.withLock { () -> Int in
                    messageRequests += 1
                    return messageRequests
                }
                if attempt == 1 {
                    return try response(
                        for: request,
                        statusCode: 503,
                        body: #"{"error":{"code":"internal_error","message":"retry","details":{},"request_id":"retry-history"}}"#,
                        headers: ["Retry-After": "0"]
                    )
                }
                let cursor = URLComponents(
                    url: try #require(request.url),
                    resolvingAgainstBaseURL: false
                )?.queryItems?.first { $0.name == "cursor" }?.value
                if cursor == nil {
                    return try response(
                        for: request,
                        statusCode: 200,
                        body: """
                            {"items":[
                              {"sequence":2,"role":"user","content":[{"type":"text","text":"First question"}]},
                              {"sequence":6,"role":"assistant","content":[{"type":"text","text":"First answer"}]}
                            ],"next_cursor":"messages-2"}
                            """
                    )
                }
                #expect(cursor == "messages-2")
                return try response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[
                          {"sequence":7,"role":"user","content":[{"type":"text","text":"Second question"}]},
                          {"sequence":11,"role":"assistant","content":[{"type":"text","text":"Second answer"}]}
                        ],"next_cursor":null}
                        """
                )
            case ("GET", "/v1/runs/\(runID.uuidString)"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"id":"\(runID.uuidString)","session_id":"\(sessionID.uuidString)","parent_run_id":null,"status":"COMPLETED","step_count":1,"model_call_count":1,"tool_call_count":0,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-08-14T00:03:00Z","updated_at":"2026-08-14T00:04:00Z"}
                        """
                )
            case ("GET", "/v1/runs/\(runID.uuidString)/events"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: """
                        id: 2
                        event: user.message.created
                        data: {"content":[{"type":"text","text":"First question"}]}

                        id: 6
                        event: assistant.message.completed
                        data: {"message":{"kind":"assistant","content":[{"kind":"text","text":"First answer"}]}}

                        id: 7
                        event: user.message.created
                        data: {"content":[{"type":"text","text":"Second question"}]}

                        id: 11
                        event: assistant.message.completed
                        data: {"message":{"kind":"assistant","content":[{"kind":"text","text":"Second answer"}]}}

                        id: 12
                        event: context.working_state.updated
                        data: {"working_state":{"objective":"Replay observed","constraints":[],"tasks":[],"established_facts":[],"open_questions":[],"next_action":null}}

                        id: 13
                        event: run.completed
                        data: {"run_id":"\(runID.uuidString)"}

                        """
                )
            default:
                Issue.record(
                    "unexpected request: \(request.httpMethod ?? "nil") \(request.url?.path ?? "nil")"
                )
                return try response(for: request, statusCode: 500, body: "")
            }
        }
        defer { model.newSession() }
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )

        let entry = try #require(model.history.first)
        await model.selectSession(entry)
        for _ in 0 ..< 100 where model.runState.workingState == nil {
            await Task.yield()
        }

        #expect(model.runState.workingState?.objective == "Replay observed")
        #expect(model.runState.timeline.map(\.text) == [
            "First question",
            "First answer",
            "Second question",
            "Second answer",
        ])
        #expect(lock.withLock { messageRequests } == 3)
    }

    /// Reopening a chat whose answer followed three approved commands lands on
    /// the answer, with the commands in one collapsed summary above it.
    @Test
    func testReopenedChatShowsTheAnswerBelowTheApprovedCommandsItRan() async throws {
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000123")
        )
        let runID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000456")
        )
        let approvalIDs = (1...3).map { index in
            UUID(uuidString: "00000000-0000-0000-0000-00000000070\(index)")!
        }
        let sessionBody = """
            {"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Fwd: Intro","metadata":{},"created_at":"2026-10-07T15:33:00Z","updated_at":"2026-10-07T15:46:00Z","active_run_id":null,"last_run_id":"\(runID.uuidString)"}
            """
        var events = """
            id: 2
            event: user.message.created
            data: {"content":[{"type":"text","text":"Is Polargrid being tracked?"}]}


            """
        for (index, approvalID) in approvalIDs.enumerated() {
            let base = 3 + index * 7
            let call = "{\"name\":\"sandbox.run_command\",\"call_id\":\"command-\(index + 1)\""
            events += """
                id: \(base)
                event: tool.call.proposed
                data: \(call)}

                id: \(base + 1)
                event: approval.requested
                data: {"approval_id":"\(approvalID.uuidString)"}

                id: \(base + 2)
                event: run.waiting_for_approval
                data: {"approval_id":"\(approvalID.uuidString)"}

                id: \(base + 3)
                event: approval.resolved
                data: {"approval_id":"\(approvalID.uuidString)","resolution":"approve_once"}

                id: \(base + 4)
                event: run.resumed
                data: {}

                id: \(base + 5)
                event: tool.call.started
                data: \(call)}

                id: \(base + 6)
                event: tool.call.completed
                data: \(call),"result_item":{"content":[{"type":"text","text":"exit 0"}],"is_error":false,"trust":"external_untrusted"}}


                """
        }
        events += """
            id: 24
            event: assistant.message.completed
            data: {"message":{"kind":"assistant","content":[{"kind":"text","text":"Two partners track Polargrid."}]}}

            id: 25
            event: run.completed
            data: {"run_id":"\(runID.uuidString)"}


            """
        let model = try configuredModel { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod, path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: "{\"items\":[\(sessionBody)],\"next_cursor\":null}"
                )
            case ("GET", "/v1/sessions/\(sessionID.uuidString)"):
                return try response(for: request, statusCode: 200, body: sessionBody)
            case ("GET", "/v1/sessions/\(sessionID.uuidString)/messages"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[
                          {"sequence":2,"role":"user","content":[{"type":"text","text":"Is Polargrid being tracked?"}]},
                          {"sequence":24,"role":"assistant","content":[{"type":"text","text":"Two partners track Polargrid."}]}
                        ],"next_cursor":null}
                        """
                )
            case ("GET", "/v1/runs/\(runID.uuidString)"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"id":"\(runID.uuidString)","session_id":"\(sessionID.uuidString)","parent_run_id":null,"status":"COMPLETED","step_count":4,"model_call_count":4,"tool_call_count":3,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-10-07T15:33:00Z","updated_at":"2026-10-07T15:46:00Z"}
                        """
                )
            case ("GET", "/v1/runs/\(runID.uuidString)/events"):
                return try response(for: request, statusCode: 200, body: events)
            case ("GET", "/v1/approvals"):
                return try response(
                    for: request, statusCode: 200, body: "{\"items\":[],\"next_cursor\":null}"
                )
            case ("GET", _) where path.hasPrefix("/v1/approvals/"):
                let approvalID = String(path.dropFirst("/v1/approvals/".count))
                return try response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"id":"\(approvalID)","run_id":"\(runID.uuidString)","session_id":"\(sessionID.uuidString)","status":"APPROVED","tool_name":"sandbox.run_command","action_summary":"Run a command in the sandbox","arguments":{"command":["python3","parse.py"]},"risk":"HIGH","policy_reason":"policy.matrix.code_execution","expires_at":null,"created_at":"2026-10-07T15:34:00Z","resolved_at":"2026-10-07T15:34:56Z","resolved_by":"owner","decision":"approve_once"}
                        """
                )
            default:
                Issue.record("unexpected request: \(request.httpMethod ?? "nil") \(path)")
                return try response(for: request, statusCode: 500, body: "")
            }
        }
        defer { model.newSession() }
        #expect(
            await model.configure(baseURLString: "https://veetbot.test", token: "replacement-token")
        )

        let entry = try #require(model.history.first)
        await model.selectSession(entry)
        // The stored run is already complete; wait for its replay to settle.
        for _ in 0 ..< 2_000 {
            if model.runState.approvals.count == 3,
                model.runState.tools.count == 3,
                model.runState.tools.allSatisfy({ $0.status == .completed })
            {
                break
            }
            try await Task.sleep(for: .milliseconds(1))
        }

        #expect(model.runState.tools.map(\.status) == [.completed, .completed, .completed])
        #expect(model.runState.activityTimeline.map(\.id) == [
            "message:event-2",
            "tool:command-1",
            "message:event-24",
        ])
        guard case .toolBundle(let bundle) = model.runState.activityTimeline[1] else {
            Issue.record("expected the approved commands in one collapsed summary")
            return
        }
        #expect(bundle.summary == "3 tool calls · Completed")
        #expect(bundle.activities.map(\.approvalID) == approvalIDs)
    }

    @Test
    func testSuccessfulServerDeleteRemovesVisibleRowWhenCacheDeleteFails() async throws {
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000123")
        )
        let store = DeleteFailingHistoryStore()
        let session = urlSession { request in
            if request.httpMethod == HTTPMethod.delete.rawValue {
                return try response(for: request, statusCode: 204, body: "")
            }
            return try response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[{"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Delete me","metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:01:00Z","active_run_id":null,"last_run_id":null}],"next_cursor":null}
                    """
            )
        }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(
                defaults: try #require(UserDefaults(suiteName: "com.veetbot.tests.\(UUID())"))
            ),
            historyStore: store,
            urlSession: session
        )
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )
        let entry = try #require(model.history.first)

        await model.deleteSessionEverywhere(entry)

        #expect(model.history.isEmpty)
        #expect(model.errorMessage == DeleteFailingHistoryStore.message)
        #expect(await store.list().map(\.sessionID) == [sessionID])
    }

    @Test
    func testSynchronizationPrunesCachedPeopleAuditSessions() async throws {
        let conversationID = UUID()
        let auditID = UUID()
        let conversationJSON = """
            {"id":"\(conversationID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Keep me","metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:01:00Z","active_run_id":null,"last_run_id":null}
            """
        let auditJSON = """
            {"id":"\(auditID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":null,"metadata":{"purpose":"people-management"},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:02:00Z","active_run_id":null,"last_run_id":null}
            """
        let session = urlSession { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: "{\"items\":[\(conversationJSON)],\"next_cursor\":null}"
                )
            case ("GET", "/v1/sessions/\(auditID.uuidString)"):
                return try response(for: request, statusCode: 200, body: auditJSON)
            default:
                Issue.record(
                    "unexpected request: \(request.httpMethod ?? "nil") \(request.url?.path ?? "nil")"
                )
                return try response(for: request, statusCode: 500, body: "")
            }
        }
        // An earlier build cached the Add person audit session as a conversation.
        let store = VolatileSessionHistoryStore()
        try await store.upsert(
            SessionHistoryEntry(
                sessionID: auditID,
                title: "New conversation",
                agentID: "general",
                createdAt: Date(timeIntervalSince1970: 0),
                updatedAt: Date(timeIntervalSince1970: 0),
                lastRunID: nil
            )
        )
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(
                defaults: try #require(UserDefaults(suiteName: "com.veetbot.tests.\(UUID())"))
            ),
            historyStore: store,
            urlSession: session
        )
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )
        await model.synchronizeHistory()

        #expect(model.history.map(\.sessionID) == [conversationID])
        #expect(await store.list().map(\.sessionID) == [conversationID])
    }

    @Test
    func testPendingApprovalPaginationHasNoArbitraryPageCap() async throws {
        let lock = NSLock()
        var approvalRequests = 0
        let model = try configuredModel { request in
            if request.url?.path == "/v1/sessions" {
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            }
            #expect(request.url?.path == "/v1/approvals")
            let cursor = URLComponents(
                url: try #require(request.url),
                resolvingAgainstBaseURL: false
            )?.queryItems?.first { $0.name == "cursor" }?.value
            let page = cursor.flatMap { Int($0.replacingOccurrences(of: "page-", with: "")) } ?? 1
            lock.withLock { approvalRequests += 1 }
            let next = page < 21 ? "\"page-\(page + 1)\"" : "null"
            return try response(
                for: request,
                statusCode: 200,
                body: "{\"items\":[],\"next_cursor\":\(next)}"
            )
        }
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )

        await model.refreshPendingApprovals()

        #expect(lock.withLock { approvalRequests } == 21)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testPendingApprovalPaginationRejectsRepeatedCursor() async throws {
        let lock = NSLock()
        var approvalRequests = 0
        let model = try configuredModel { request in
            if request.url?.path == "/v1/sessions" {
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            }
            #expect(request.url?.path == "/v1/approvals")
            lock.withLock { approvalRequests += 1 }
            return try response(
                for: request,
                statusCode: 200,
                body: #"{"items":[],"next_cursor":"repeated"}"#
            )
        }
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )

        await model.refreshPendingApprovals()

        #expect(lock.withLock { approvalRequests } == 2)
        #expect(model.errorMessage != nil)
    }

    @Test
    func testFirstMessageAppearsBeforeSessionStartupCompletesAndReconcilesOnce() async throws {
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000123")
        )
        let runID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000456")
        )
        let lock = NSLock()
        let releaseSessionStartup = DispatchSemaphore(value: 0)
        var sessionStartupBegan = false
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("POST", "/v1/sessions"):
                lock.withLock { sessionStartupBegan = true }
                releaseSessionStartup.wait()
                return try response(
                    for: request,
                    statusCode: 201,
                    body: """
                        {"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":null,"metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:00:00Z","active_run_id":null,"last_run_id":null}
                        """
                )
            case ("POST", "/v1/sessions/\(sessionID.uuidString)/messages"):
                return try response(
                    for: request,
                    statusCode: 202,
                    body: "{\"run_id\":\"\(runID.uuidString)\",\"status\":\"QUEUED\"}"
                )
            case ("GET", "/v1/runs/\(runID.uuidString)/events"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: """
                        id: 2
                        event: user.message.created
                        data: {"content":[{"type":"text","text":"Hello now"}]}

                        id: 3
                        event: run.completed
                        data: {"run_id":"\(runID.uuidString)"}

                        """,
                    headers: ["Content-Type": "text/event-stream"]
                )
            default:
                Issue.record(
                    "unexpected request: \(request.httpMethod ?? "nil") \(request.url?.path ?? "nil")"
                )
                return try response(for: request, statusCode: 500, body: "")
            }
        }
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )

        let submission = Task { await model.send("Hello now") }
        for _ in 0 ..< 1_000 where !lock.withLock({ sessionStartupBegan }) {
            await Task.yield()
        }

        #expect(lock.withLock { sessionStartupBegan })
        #expect(model.runState.timeline.count == 1)
        #expect(model.runState.activityTimeline.count == 1)
        #expect(model.runState.timeline.first?.text == "Hello now")
        #expect(model.runState.timeline.first?.id.hasPrefix("pending-user-") == true)

        releaseSessionStartup.signal()
        #expect(await submission.value)
        for _ in 0 ..< 1_000 where model.runState.timeline.first?.id != "event-2" {
            await Task.yield()
        }

        #expect(model.runState.timeline.count == 1)
        #expect(model.runState.activityTimeline.count == 1)
        #expect(model.runState.timeline.first?.id == "event-2")
        #expect(model.runState.timeline.first?.text == "Hello now")
    }

    @Test
    func testRetryingTheSameMessageReusesOneKeyAndDoesNotCreateAnotherSession() async throws {
        let lock = NSLock()
        var requests: [URLRequest] = []
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000123")
        )
        let runID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000456")
        )
        let sessionBody = """
            {"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":null,"metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:01:00Z","active_run_id":null,"last_run_id":null}
            """
        let model = try configuredModel { request in
            let captured = lock.withLock { () -> [URLRequest] in
                requests.append(request)
                return requests
            }
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("POST", "/v1/sessions"):
                return try response(for: request, statusCode: 201, body: sessionBody)
            case ("GET", "/v1/sessions/\(sessionID.uuidString)"):
                return try response(for: request, statusCode: 200, body: sessionBody)
            case ("POST", "/v1/sessions/\(sessionID.uuidString)/messages"):
                let attempts = captured.filter { $0.url?.path.hasSuffix("/messages") == true }.count
                if attempts <= 3 {
                    return try response(
                        for: request,
                        statusCode: 503,
                        body: #"{"error":{"code":"internal_error","message":"retry","details":{},"request_id":"retry"}}"#,
                        headers: ["Retry-After": "0"]
                    )
                }
                return try response(
                    for: request,
                    statusCode: 202,
                    body: "{\"run_id\":\"\(runID.uuidString)\",\"status\":\"QUEUED\"}"
                )
            case ("GET", "/v1/runs/\(runID.uuidString)/events"):
                return try response(for: request, statusCode: 200, body: "")
            default:
                Issue.record(
                    "unexpected request: \(request.httpMethod ?? "nil") \(request.url?.path ?? "nil")"
                )
                return try response(for: request, statusCode: 500, body: "")
            }
        }
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )

        #expect(await model.send("  retry me  ") == false)
        #expect(model.runState.timeline.isEmpty)
        #expect(model.runState.activityTimeline.isEmpty)
        model.clearError()
        #expect(await model.send("retry me") == true)
        model.newSession()

        let captured = lock.withLock { requests }
        #expect(
            captured.filter {
                $0.httpMethod == "POST" && $0.url?.path == "/v1/sessions"
            }.count == 1
        )
        let submissions = captured.filter { $0.url?.path.hasSuffix("/messages") == true }
        #expect(submissions.count == 4)
        let keys = submissions.compactMap {
            $0.value(forHTTPHeaderField: "Idempotency-Key")
        }
        #expect(keys.count == submissions.count)
        #expect(Set(keys).count == 1)
        #expect(model.selectedSessionID == nil)
    }

    @Test
    func testSelectedWebsiteProfileIsBoundWhenANewConversationIsCreated() async throws {
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000b1")
        )
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000b2")
        )
        let runID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000b3")
        )
        let lock = NSLock()
        var createSessionRequest: URLRequest?
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("GET", "/v1/browser-profiles"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[{"id":"\#(profileID.uuidString)","allowed_origins":["https://example.org"],"status":"ready","generation":2,"created_at":"2026-08-22T12:00:00Z","updated_at":"2026-08-22T12:01:00Z","last_used_at":null}],"next_cursor":null}"#
                )
            case ("POST", "/v1/sessions"):
                lock.withLock { createSessionRequest = request }
                return try response(
                    for: request,
                    statusCode: 201,
                    body: #"{"id":"\#(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":null,"metadata":{"browser_profile_id":"\#(profileID.uuidString)"},"created_at":"2026-08-22T12:00:00Z","updated_at":"2026-08-22T12:00:00Z","active_run_id":null,"last_run_id":null}"#
                )
            case ("POST", "/v1/sessions/\(sessionID.uuidString)/messages"):
                return try response(
                    for: request,
                    statusCode: 202,
                    body: #"{"run_id":"\#(runID.uuidString)","status":"QUEUED"}"#
                )
            case ("GET", "/v1/runs/\(runID.uuidString)/events"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: "",
                    headers: ["Content-Type": "text/event-stream"]
                )
            default:
                throw URLError(.badURL)
            }
        }

        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )
        await model.refreshBrowserProfiles()
        await model.selectBrowserProfile(profileID)

        #expect(await model.send("Use my account") == true)
        model.newSession()

        let request = try #require(lock.withLock { createSessionRequest })
        let json = try requestJSONObject(request)
        #expect(json["browser_profile_id"] as? String == profileID.uuidString)
        #expect(json["username"] == nil)
        #expect(json["password"] == nil)
    }

    @Test
    func testSameOriginCredentialChangeRevalidatesSelectedWebsiteProfile() async throws {
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000b4")
        )
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let configurationStore = ConnectionConfigurationStore(defaults: defaults)
        let lock = NSLock()
        var profileRequests = 0
        let session = urlSession { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("GET", "/v1/browser-profiles"):
                let attempt = lock.withLock { () -> Int in
                    profileRequests += 1
                    return profileRequests
                }
                let items = attempt == 1
                    ? #"[{"id":"\#(profileID.uuidString)","allowed_origins":["https://example.org"],"status":"ready","generation":1,"created_at":"2026-08-22T12:00:00Z","updated_at":"2026-08-22T12:01:00Z","last_used_at":null}]"#
                    : "[]"
                return try response(
                    for: request,
                    statusCode: 200,
                    body: "{\"items\":\(items),\"next_cursor\":null}"
                )
            default:
                throw URLError(.badURL)
            }
        }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "principal-one"),
            configurationStore: configurationStore,
            historyStore: VolatileSessionHistoryStore(),
            urlSession: session
        )

        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: ""
            )
        )
        await model.refreshBrowserProfiles()
        await model.selectBrowserProfile(profileID)
        #expect(model.selectedBrowserProfileID == profileID)

        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "principal-two"
            )
        )

        #expect(lock.withLock { profileRequests } == 2)
        #expect(model.browserProfiles.isEmpty)
        #expect(model.selectedBrowserProfileID == nil)
        #expect(await configurationStore.loadBrowserProfileID() == nil)
    }

    @Test
    func testReadyAuthenticationDoesNotRestoreAnAbsentWebsiteProfile() async throws {
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000b6")
        )
        let authenticationID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000b7")
        )
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let configurationStore = ConnectionConfigurationStore(defaults: defaults)
        let session = urlSession { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("POST", "/v1/browser-profiles"):
                return try response(
                    for: request,
                    statusCode: 201,
                    body: #"{"id":"\#(profileID.uuidString)","allowed_origins":["https://example.org"],"status":"authentication_required","generation":1,"created_at":"2026-08-22T12:00:00Z","updated_at":"2026-08-22T12:01:00Z","last_used_at":null}"#
                )
            case (
                "POST",
                "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies"
            ):
                return try response(
                    for: request,
                    statusCode: 201,
                    body: #"{"id":"\#(authenticationID.uuidString)","profile_id":"\#(profileID.uuidString)","status":"needs_user","expires_at":"2026-08-22T13:00:00Z","launch_url":"https://browser.example.org/login"}"#
                )
            case ("GET", "/v1/browser-profiles"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case (
                "GET",
                "/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)"
            ):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"id":"\#(authenticationID.uuidString)","profile_id":"\#(profileID.uuidString)","status":"ready","expires_at":"2026-08-22T13:00:00Z","launch_url":null}"#
                )
            default:
                throw URLError(.badURL)
            }
        }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: nil),
            configurationStore: configurationStore,
            historyStore: VolatileSessionHistoryStore(),
            urlSession: session
        )

        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "principal-one"
            )
        )
        #expect(
            await model.createWebsiteAccess(
                websiteURL: "https://example.org/login"
            ) != nil
        )

        await model.refreshBrowserAuthentication()

        #expect(model.browserAuthentication?.status == .ready)
        #expect(model.browserProfiles.isEmpty)
        #expect(model.selectedBrowserProfileID == nil)
        #expect(await configurationStore.loadBrowserProfileID() == nil)
    }

    @Test
    func testBootstrapClearsARevokedPersistedWebsiteProfile() async throws {
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000b5")
        )
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let configurationStore = ConnectionConfigurationStore(defaults: defaults)
        await configurationStore.save(
            try ConnectionConfiguration(baseURLString: "https://veetbot.test")
        )
        await configurationStore.saveBrowserProfileID(profileID)
        let lock = NSLock()
        var profileRequests = 0
        let session = urlSession { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("GET", "/v1/browser-profiles"):
                lock.withLock { profileRequests += 1 }
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[{"id":"\#(profileID.uuidString)","allowed_origins":["https://example.org"],"status":"revoked","generation":2,"created_at":"2026-08-22T12:00:00Z","updated_at":"2026-08-22T12:01:00Z","last_used_at":null}],"next_cursor":null}"#
                )
            default:
                throw URLError(.badURL)
            }
        }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "persisted-principal"),
            configurationStore: configurationStore,
            historyStore: VolatileSessionHistoryStore(),
            urlSession: session
        )

        for _ in 0 ..< 100 {
            if lock.withLock({ profileRequests }) == 1,
                model.isConfigured,
                model.selectedBrowserProfileID == nil
            {
                break
            }
            try await Task.sleep(for: .milliseconds(1))
        }

        #expect(lock.withLock { profileRequests } == 1)
        #expect(model.isConfigured)
        #expect(model.selectedBrowserProfileID == nil)
        #expect(await configurationStore.loadBrowserProfileID() == nil)
    }

    @Test(arguments: [
        ("https://example.org", "https://example.org", "https://example.org"),
        ("example.org", "https://example.org", "https://example.org"),
        ("  www.example.org/login?next=%2Flearn#sign-in  ",
         "https://www.example.org/login?next=%2Flearn#sign-in", "https://www.example.org"),
        ("https://www.example.org/?isLoggingIn=true",
         "https://www.example.org/?isLoggingIn=true", "https://www.example.org"),
        ("example.org/login?next=https://example.org/learn",
         "https://example.org/login?next=https://example.org/learn", "https://example.org"),
        ("HTTPS://WWW.EXAMPLE.ORG:443/login", "https://www.example.org/login",
         "https://www.example.org"),
    ])
    func testWebsiteAccessDerivesOriginFromOneURL(
        input: String, expectedLoginURL: String, expectedOrigin: String
    ) async throws {
        let profileID = UUID()
        let authenticationID = UUID()
        let lock = NSLock()
        var submittedOrigins: [String]?
        var submittedLoginURL: String?
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"), ("GET", "/v1/browser-profiles"):
                return try response(
                    for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#
                )
            case ("POST", "/v1/browser-profiles"):
                let body = try requestJSONObject(request)
                lock.withLock { submittedOrigins = body["allowed_origins"] as? [String] }
                return try response(
                    for: request, statusCode: 201,
                    body: #"{"id":"\#(profileID.uuidString)","allowed_origins":["\#(expectedOrigin)"],"status":"authentication_required","generation":1,"created_at":"2026-08-23T12:00:00Z","updated_at":"2026-08-23T12:00:00Z","last_used_at":null}"#
                )
            case ("POST", "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies"):
                let body = try requestJSONObject(request)
                lock.withLock { submittedLoginURL = body["login_url"] as? String }
                return try response(
                    for: request, statusCode: 201,
                    body: #"{"id":"\#(authenticationID.uuidString)","profile_id":"\#(profileID.uuidString)","status":"authentication_required","expires_at":"2026-08-23T12:05:00Z","launch_url":"https://browser.example/authentication/\#(authenticationID.uuidString)#capability=opaque"}"#
                )
            default:
                Issue.record("unexpected website setup request")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))

        let launchURL = await model.createWebsiteAccess(websiteURL: input)

        #expect(launchURL != nil)
        #expect(lock.withLock { submittedOrigins } == [expectedOrigin])
        #expect(lock.withLock { submittedLoginURL } == expectedLoginURL)
        #expect(model.errorMessage == nil)
    }

    @Test(arguments: [
        "", "   ", "http://example.org/login", "https://", "not a website",
        "https://example.org:8443/login", "https://user@example.org/login",
    ])
    func testInvalidWebsiteURLDoesNotCreateAProfile(input: String) async throws {
        let lock = NSLock()
        var mutationCount = 0
        let model = try configuredModel { request in
            if request.httpMethod != "GET" {
                lock.withLock { mutationCount += 1 }
            }
            return try response(
                for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#
            )
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))

        #expect(await model.createWebsiteAccess(websiteURL: input) == nil)
        #expect(lock.withLock { mutationCount } == 0)
        #expect(model.errorMessage != nil)
    }

    @Test(arguments: [", ", "\n"])
    func testWebsiteAccessSendsEveryExplicitlyAllowedOrigin(separator: String) async throws {
        let profileID = UUID()
        let authenticationID = UUID()
        let origins = ["https://www.example.org", "https://static.example.org"]
        let profile = #"{"id":"\#(profileID.uuidString)","allowed_origins":["https://www.example.org","https://static.example.org"],"status":"authentication_required","generation":1,"created_at":"2026-08-23T12:00:00Z","updated_at":"2026-08-23T12:00:00Z","last_used_at":null}"#
        let lock = NSLock()
        var submittedOrigins: [String]?
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"), ("GET", "/v1/browser-profiles"):
                return try response(
                    for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#
                )
            case ("POST", "/v1/browser-profiles"):
                let body = try requestJSONObject(request)
                lock.withLock { submittedOrigins = body["allowed_origins"] as? [String] }
                return try response(for: request, statusCode: 201, body: profile)
            case ("POST", "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies"):
                let body = try requestJSONObject(request)
                #expect(body["login_url"] as? String == "https://www.example.org/login")
                return try response(
                    for: request,
                    statusCode: 201,
                    body: #"{"id":"\#(authenticationID.uuidString)","profile_id":"\#(profileID.uuidString)","status":"authentication_required","expires_at":"2026-08-23T12:05:00Z","launch_url":"https://browser.example/authentication/\#(authenticationID.uuidString)#capability=opaque"}"#
                )
            default:
                Issue.record("unexpected website setup request")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let launchURL = await model.createWebsiteAccess(
            websiteURL: "  https://www.example.org/login  ",
            additionalOrigins: "  " + origins.joined(separator: separator) + "  "
        )

        #expect(launchURL != nil)
        #expect(lock.withLock { submittedOrigins } == origins)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testFailedWebsiteAuthenticationLaunchCancelsAndDeletesUnusedProfile() async throws {
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000c1")
        )
        let authenticationID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000c2")
        )
        let lock = NSLock()
        var requests: [(String, String)] = []
        var deleted = false
        let profile = #"{"id":"\#(profileID.uuidString)","allowed_origins":["https://example.org"],"status":"authentication_required","generation":1,"created_at":"2026-08-23T12:00:00Z","updated_at":"2026-08-23T12:00:00Z","last_used_at":null}"#
        let model = try configuredModel { request in
            let method = request.httpMethod ?? ""
            let path = request.url?.path ?? ""
            lock.withLock { requests.append((method, path)) }
            switch (method, path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("POST", "/v1/browser-profiles"):
                return try response(for: request, statusCode: 201, body: profile)
            case ("POST", "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies"):
                return try response(
                    for: request,
                    statusCode: 201,
                    body: #"{"id":"\#(authenticationID.uuidString)","profile_id":"\#(profileID.uuidString)","status":"authentication_required","expires_at":"2026-08-23T12:05:00Z","launch_url":"https://browser.example/authentication/\#(authenticationID.uuidString)#capability=opaque"}"#
                )
            case ("GET", "/v1/browser-profiles"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: deleted ? #"{"items":[],"next_cursor":null}"# : #"{"items":[\#(profile)],"next_cursor":null}"#
                )
            case ("POST", "/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)/cancel"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"id":"\#(authenticationID.uuidString)","profile_id":"\#(profileID.uuidString)","status":"cancelled","expires_at":"2026-08-23T12:05:00Z","launch_url":null}"#
                )
            case ("POST", "/v1/browser-profiles/\(profileID.uuidString)/revoke"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: profile.replacingOccurrences(
                        of: #""status":"authentication_required""#,
                        with: #""status":"revoked""#
                    )
                )
            case ("DELETE", "/v1/browser-profiles/\(profileID.uuidString)"):
                lock.withLock { deleted = true }
                return try response(for: request, statusCode: 204, body: "")
            default:
                Issue.record("unexpected request: \(method) \(path)")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )

        let launchURL = await model.createWebsiteAccess(
            websiteURL: "https://example.org/login"
        )
        #expect(launchURL?.fragment == "capability=opaque")

        await model.websiteAuthenticationLaunchFailed()

        let captured = lock.withLock { requests }
        #expect(
            captured.contains {
                $0 == (
                    "POST",
                    "/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)/cancel"
                )
            }
        )
        #expect(
            captured.contains {
                $0 == ("POST", "/v1/browser-profiles/\(profileID.uuidString)/revoke")
            }
        )
        #expect(
            captured.contains {
                $0 == ("DELETE", "/v1/browser-profiles/\(profileID.uuidString)")
            }
        )
        #expect(model.browserAuthentication == nil)
        #expect(model.browserProfiles.isEmpty)
        #expect(model.errorMessage?.contains("couldn’t open the secure login page") == true)
        #expect(model.errorMessage?.contains("try again") == true)
    }

    @Test
    func testAuthenticationCreationFailureDeletesPartiallyCreatedProfile() async throws {
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000d1")
        )
        let lock = NSLock()
        var requests: [(String, String)] = []
        let profile = #"{"id":"\#(profileID.uuidString)","allowed_origins":["https://example.org"],"status":"authentication_required","generation":1,"created_at":"2026-08-23T12:00:00Z","updated_at":"2026-08-23T12:00:00Z","last_used_at":null}"#
        let model = try configuredModel { request in
            let method = request.httpMethod ?? ""
            let path = request.url?.path ?? ""
            lock.withLock { requests.append((method, path)) }
            switch (method, path) {
            case ("GET", "/v1/sessions"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("POST", "/v1/browser-profiles"):
                return try response(for: request, statusCode: 201, body: profile)
            case ("POST", "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies"):
                return try response(
                    for: request,
                    statusCode: 503,
                    body: #"{"error":{"code":"internal_error","message":"browser temporarily unavailable","details":{},"request_id":"browser-down"}}"#
                )
            case ("POST", "/v1/browser-profiles/\(profileID.uuidString)/revoke"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: profile.replacingOccurrences(
                        of: #""status":"authentication_required""#,
                        with: #""status":"revoked""#
                    )
                )
            case ("DELETE", "/v1/browser-profiles/\(profileID.uuidString)"):
                return try response(for: request, statusCode: 204, body: "")
            default:
                Issue.record("unexpected request: \(method) \(path)")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )

        #expect(
            await model.createWebsiteAccess(
                websiteURL: "https://example.org/login"
            ) == nil
        )

        let captured = lock.withLock { requests }
        #expect(
            captured.contains {
                $0 == ("POST", "/v1/browser-profiles/\(profileID.uuidString)/revoke")
            }
        )
        #expect(
            captured.contains {
                $0 == ("DELETE", "/v1/browser-profiles/\(profileID.uuidString)")
            }
        )
        #expect(model.browserProfiles.isEmpty)
        #expect(model.browserAuthentication == nil)
        #expect(model.errorMessage == "browser temporarily unavailable")
    }

    @Test
    func testReplacingTheCredentialCancelsTheInFlightWebsiteLogin() async throws {
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000e1")
        )
        let authenticationID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000e2")
        )
        let recorder = WebsiteLoginRequestRecorder()
        let model = try await modelWithLiveWebsiteLogin(
            profileID: profileID,
            authenticationID: authenticationID,
            recorder: recorder,
            token: "principal-one"
        )

        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "principal-two"
            )
        )

        let cancels = recorder.matching(
            method: "POST",
            url:
                "https://veetbot.test/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)/cancel"
        )
        #expect(cancels.count == 1)
        #expect(cancels.first?.authorization == "Bearer principal-one")
        #expect(model.browserAuthentication == nil)
        #expect(model.websiteAuthenticationLaunchURL == nil)
    }

    @Test
    func testMovingToAnotherServerCancelsTheInFlightWebsiteLogin() async throws {
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000e3")
        )
        let authenticationID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000e4")
        )
        let recorder = WebsiteLoginRequestRecorder()
        let model = try await modelWithLiveWebsiteLogin(
            profileID: profileID,
            authenticationID: authenticationID,
            recorder: recorder,
            token: "principal-one"
        )

        #expect(
            await model.configure(
                baseURLString: "https://other.veetbot.test",
                token: "principal-one"
            )
        )

        let cancels = recorder.matching(
            method: "POST",
            path:
                "/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)/cancel"
        )
        #expect(cancels.count == 1)
        #expect(
            cancels.first?.url
                == "https://veetbot.test/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)/cancel"
        )
        #expect(model.browserAuthentication == nil)
        #expect(model.websiteAuthenticationLaunchURL == nil)
    }

    @Test
    func testForgettingTheCredentialCancelsTheInFlightWebsiteLogin() async throws {
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000e5")
        )
        let authenticationID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000e6")
        )
        let recorder = WebsiteLoginRequestRecorder()
        let model = try await modelWithLiveWebsiteLogin(
            profileID: profileID,
            authenticationID: authenticationID,
            recorder: recorder,
            token: "principal-one",
            deviceRegistrationCoordinator: DeviceRegistrationCoordinator(
                identityStore: InMemoryInstallationIdentityStore(
                    installationID: "00000000-0000-0000-0000-0000000000e7"
                )
            )
        )

        await model.forgetCredentials()

        let cancels = recorder.matching(
            method: "POST",
            url:
                "https://veetbot.test/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)/cancel"
        )
        #expect(cancels.count == 1)
        #expect(cancels.first?.authorization == "Bearer principal-one")
        #expect(model.browserAuthentication == nil)
        #expect(model.websiteAuthenticationLaunchURL == nil)
    }

    /// Configures a model against `https://veetbot.test` and leaves one
    /// non-terminal website-login ceremony in flight, with its one-time launch
    /// capability still held by the client.
    private func modelWithLiveWebsiteLogin(
        profileID: UUID,
        authenticationID: UUID,
        recorder: WebsiteLoginRequestRecorder,
        token: String,
        deviceRegistrationCoordinator: DeviceRegistrationCoordinator? = nil
    ) async throws -> ChatViewModel {
        let profile = #"{"id":"\#(profileID.uuidString)","allowed_origins":["https://example.org"],"status":"authentication_required","generation":1,"created_at":"2026-08-23T12:00:00Z","updated_at":"2026-08-23T12:00:00Z","last_used_at":null}"#
        let ceremony = #"{"id":"\#(authenticationID.uuidString)","profile_id":"\#(profileID.uuidString)","status":"authentication_required","expires_at":"2026-08-23T12:05:00Z","launch_url":"https://browser.example/authentication/\#(authenticationID.uuidString)#capability=opaque"}"#
        let session = urlSession { request in
            let method = request.httpMethod ?? ""
            let path = request.url?.path ?? ""
            recorder.record(request)
            switch (method, path) {
            case ("GET", "/v1/sessions"), ("GET", "/v1/devices"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("POST", "/v1/browser-profiles"):
                return try response(for: request, statusCode: 201, body: profile)
            case (
                "POST",
                "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies"
            ):
                return try response(for: request, statusCode: 201, body: ceremony)
            case ("GET", "/v1/browser-profiles"):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: #"{"items":[\#(profile)],"next_cursor":null}"#
                )
            case (
                "POST",
                "/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)/cancel"
            ):
                return try response(
                    for: request,
                    statusCode: 200,
                    body: ceremony.replacingOccurrences(
                        of: #""status":"authentication_required""#,
                        with: #""status":"cancelled""#
                    )
                    .replacingOccurrences(
                        of:
                            #""launch_url":"https://browser.example/authentication/\#(authenticationID.uuidString)#capability=opaque""#,
                        with: #""launch_url":null"#
                    )
                )
            default:
                Issue.record("unexpected request: \(method) \(path)")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            deviceRegistrationCoordinator: deviceRegistrationCoordinator
                ?? DeviceRegistrationCoordinator(
                    identityStore: InMemoryInstallationIdentityStore(
                        installationID: "00000000-0000-0000-0000-0000000000ff"
                    )
                ),
            urlSession: session
        )
        #expect(
            await model.configure(baseURLString: "https://veetbot.test", token: token)
        )
        let launchURL = await model.createWebsiteAccess(
            websiteURL: "https://example.org/login"
        )
        #expect(launchURL?.fragment == "capability=opaque")
        #expect(model.browserAuthentication?.status == .authenticationRequired)
        #expect(model.websiteAuthenticationLaunchURL == launchURL)
        return model
    }

    private func requestJSONObject(_ request: URLRequest) throws -> [String: Any] {
        let data: Data
        if let body = request.httpBody {
            data = body
        } else {
            let stream = try #require(request.httpBodyStream)
            stream.open()
            defer { stream.close() }
            var bytes = Data()
            var buffer = [UInt8](repeating: 0, count: 1_024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                guard count >= 0 else {
                    throw stream.streamError ?? HTTPTransportError.invalidResponse
                }
                if count == 0 { break }
                bytes.append(buffer, count: count)
            }
            data = bytes
        }
        return try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    // MARK: - Conversation folders (Milestone 29)

    private static let folderFixtureID = "00000000-0000-0000-0000-0000000000F1"
    private static let otherFolderFixtureID = "00000000-0000-0000-0000-0000000000F2"
    private static let filedSessionFixtureID = "00000000-0000-0000-0000-000000000123"
    private static let looseSessionFixtureID = "00000000-0000-0000-0000-000000000456"
    private static let proposalFixtureID = "00000000-0000-0000-0000-0000000000E1"
    private static let newFolderFixtureID = "00000000-0000-0000-0000-0000000000F3"

    private func sessionJSON(_ id: String, title: String, folderID: String?) -> String {
        let folder = folderID.map { "\"\($0)\"" } ?? "null"
        return #"{"id":"\#(id)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"\#(title)","metadata":{},"created_at":"2026-08-12T12:00:00Z","updated_at":"2026-08-12T12:00:01Z","active_run_id":null,"last_run_id":null,"folder_id":\#(folder)}"#
    }

    private func folderJSON(_ id: String, name: String, count: Int) -> String {
        #"{"id":"\#(id)","name":"\#(name)","thread_count":\#(count),"created_at":"2026-09-16T12:00:00Z","updated_at":"2026-09-16T12:00:00Z"}"#
    }

    private func proposalJSON(state: String, resultingFolderID: String? = nil) -> String {
        let resulting = resultingFolderID.map { "\"\($0)\"" } ?? "null"
        return #"{"id":"\#(Self.proposalFixtureID)","kind":"new_folder","proposed_name":"Lisbon Trip","target_folder_id":null,"member_session_ids":["\#(Self.looseSessionFixtureID)"],"rationale":null,"derivation":"lexical","state":"\#(state)","withdrawal_reason":null,"resulting_folder_id":\#(resulting),"created_at":"2026-09-16T12:00:00Z","resolved_at":null}"#
    }

    private func sessionsPageJSON() -> String {
        "{\"items\":[\(sessionJSON(Self.filedSessionFixtureID, title: "Filed", folderID: Self.folderFixtureID)),\(sessionJSON(Self.looseSessionFixtureID, title: "Loose", folderID: nil))],\"next_cursor\":null}"
    }

    private func folderPageJSON() -> String {
        "{\"items\":[\(folderJSON(Self.folderFixtureID, name: "Travel", count: 1)),\(folderJSON(Self.otherFolderFixtureID, name: "Work", count: 0))],\"next_cursor\":null}"
    }

    @Test
    func testConfigureSucceedsAndStaysFlatWhenTheFoldersRouteIsMissing() async throws {
        let model = try configuredModel { request in
            if request.url?.path == "/v1/sessions" {
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            }
            if request.url?.path == "/v1/folders" {
                return try response(
                    for: request, statusCode: 404,
                    body: #"{"error":{"code":"not_found","message":"Not found.","details":{},"request_id":"old"}}"#
                )
            }
            Issue.record("Unexpected request \(request.url?.path ?? "")")
            return try response(for: request, statusCode: 500, body: "{}")
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        #expect(model.foldersAvailable == false)
        #expect(model.errorMessage == nil)
        #expect(model.groupedHistory.uncategorized.count == 2)
        #expect(model.groupedHistory.folders.isEmpty)
        #expect(model.suggestedFolders.isEmpty)
    }

    @Test
    func testAFolderLoadFailureNeverPresentsABanner() async throws {
        let model = try configuredModel { request in
            if request.url?.path == "/v1/sessions" {
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            }
            return try response(
                for: request, statusCode: 500,
                body: #"{"error":{"code":"internal_error","message":"boom","details":{},"request_id":"r"}}"#
            )
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        #expect(model.errorMessage == nil)
        #expect(model.foldersAvailable == false)
        await model.synchronizeHistory()
        #expect(model.errorMessage == nil)
    }

    @Test
    func testReconcileLoadsFoldersAndProposalsAndGroupsTheHistory() async throws {
        let model = try configuredModel { request in
            switch request.url?.path {
            case "/v1/sessions":
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case "/v1/folders":
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case "/v1/folders/proposals":
                return try response(
                    for: request, statusCode: 200,
                    body: "{\"items\":[\(proposalJSON(state: "proposed"))],\"next_cursor\":null}"
                )
            default:
                Issue.record("Unexpected request \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        #expect(model.foldersAvailable)
        #expect(model.folders.map(\.name) == ["Travel", "Work"])
        let grouped = model.groupedHistory
        #expect(grouped.uncategorized.map(\.title) == ["Loose"])
        #expect(grouped.folders.map(\.folder.name) == ["Travel", "Work"])
        #expect(grouped.folders.first?.entries.map(\.title) == ["Filed"])
        #expect(model.suggestedFolders.map(\.headline) == ["New folder “Lisbon Trip”"])
        #expect(model.suggestedFolders.first?.memberTitles == ["Loose"])
    }

    /// The sidebar sync runs every 30 seconds. An unchanged server must not
    /// republish history or folders, which would redraw the open conversation
    /// (and could reset a selection in progress) each time.
    @Test
    func testAnUnchangedSyncPublishesNoHistoryOrFolderChange() async throws {
        let model = try configuredModel { request in
            switch request.url?.path {
            case "/v1/sessions":
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case "/v1/folders":
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case "/v1/folders/proposals":
                return try response(
                    for: request, statusCode: 200,
                    body: "{\"items\":[\(proposalJSON(state: "proposed"))],\"next_cursor\":null}"
                )
            default:
                Issue.record("Unexpected request \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        await model.synchronizeHistory()
        var publications: [String] = []
        let subscriptions = [
            model.$history.dropFirst().sink { _ in publications.append("history") },
            model.$folders.dropFirst().sink { _ in publications.append("folders") },
            model.$folderProposals.dropFirst().sink { _ in publications.append("proposals") },
            model.$foldersAvailable.dropFirst().sink { _ in publications.append("available") },
        ]

        await model.synchronizeHistory()

        #expect(publications == [])
        #expect(model.foldersAvailable)
        #expect(model.folders.map(\.name) == ["Travel", "Work"])
        withExtendedLifetime(subscriptions) {}
    }

    @Test
    func testMovingASessionSendsAnExplicitNullAndKeepsItsRowTimestamp() async throws {
        let recorder = WebsiteLoginRequestRecorder()
        let model = try configuredModel { request in
            recorder.record(request)
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case ("GET", "/v1/folders"):
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("PUT", "/v1/sessions/\(Self.filedSessionFixtureID)/folder"):
                return try response(
                    for: request, statusCode: 200,
                    body: sessionJSON(Self.filedSessionFixtureID, title: "Filed", folderID: nil)
                )
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let filedID = try #require(UUID(uuidString: Self.filedSessionFixtureID))
        let before = try #require(model.history.first { $0.sessionID == filedID })
        #expect(before.folderID?.uuidString == Self.folderFixtureID.uppercased())
        await model.moveSession(filedID, toFolder: nil)
        let sent = recorder.matching(method: "PUT", path: "/v1/sessions/\(Self.filedSessionFixtureID)/folder")
        #expect(sent.count == 1)
        let body = try #require(sent.first?.body)
        let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(object.keys.contains("folder_id"))
        #expect(object["folder_id"] is NSNull)
        let after = try #require(model.history.first { $0.sessionID == filedID })
        #expect(after.folderID == nil)
        #expect(after.updatedAt == before.updatedAt)
        #expect(model.groupedHistory.uncategorized.map(\.title).sorted() == ["Filed", "Loose"])
        #expect(model.errorMessage == nil)
    }

    /// The ten-second refresh can start while a move is in flight and read the
    /// index as it stood before the move. The move's own answer is newer, so
    /// that page must not put the conversation back where it was.
    @Test
    func testARefreshStartedDuringAMoveDoesNotRestoreTheStaleFolder() async throws {
        let lock = NSLock()
        var staleListing = false
        var moveInFlight = false
        let releaseMove = DispatchSemaphore(value: 0)
        let releaseListing = DispatchSemaphore(value: 0)
        // Every suite shares one loading thread, so no wait here is unbounded.
        let patience = DispatchTimeInterval.seconds(10)
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                // Read before the move committed, answered after it.
                if lock.withLock({ staleListing }) { _ = releaseListing.wait(timeout: .now() + patience) }
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case ("GET", "/v1/folders"):
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("PUT", "/v1/sessions/\(Self.looseSessionFixtureID)/folder"):
                lock.withLock { moveInFlight = true }
                _ = releaseMove.wait(timeout: .now() + patience)
                return try response(
                    for: request, statusCode: 200,
                    body: sessionJSON(Self.looseSessionFixtureID, title: "Loose", folderID: Self.folderFixtureID)
                )
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        model.notificationSyncActive = true
        lock.withLock { staleListing = true }
        let looseID = try #require(UUID(uuidString: Self.looseSessionFixtureID))
        let folderID = try #require(UUID(uuidString: Self.folderFixtureID))

        // The move's request is being served before the refresh starts, so
        // the refresh's listing can only be answered after it.
        let move = Task { await model.moveSession(looseID, toFolder: folderID) }
        for _ in 0..<5_000 where !lock.withLock({ moveInFlight }) {
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        try #require(lock.withLock { moveInFlight })
        // The task runs to its request before the test resumes on the main actor.
        var refreshBegan = false
        let refresh = Task {
            refreshBegan = true
            await model.refreshScheduledReportHistory()
        }
        while !refreshBegan { await Task.yield() }
        releaseMove.signal()
        await move.value
        #expect(model.history.first { $0.sessionID == looseID }?.folderID == folderID)
        releaseListing.signal()
        await refresh.value

        #expect(model.history.first { $0.sessionID == looseID }?.folderID == folderID)
        #expect(model.groupedHistory.uncategorized.isEmpty)
        #expect(model.errorMessage == nil)
    }

    /// The same refresh can start while a delete is in flight and read the
    /// index and the folder list as they stood before it. The delete's own
    /// answer is newer, so those pages must not revive the folder or refile
    /// the conversations it held.
    @Test
    func testARefreshStartedDuringADeleteDoesNotReviveTheFolder() async throws {
        let lock = NSLock()
        var staleListing = false
        var deleteInFlight = false
        let releaseDelete = DispatchSemaphore(value: 0)
        let releaseListing = DispatchSemaphore(value: 0)
        // Every suite shares one loading thread, so no wait here is unbounded.
        let patience = DispatchTimeInterval.seconds(10)
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                // Read before the delete committed, answered after it.
                if lock.withLock({ staleListing }) { _ = releaseListing.wait(timeout: .now() + patience) }
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case ("GET", "/v1/folders"):
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("DELETE", "/v1/folders/\(Self.folderFixtureID)"):
                lock.withLock { deleteInFlight = true }
                _ = releaseDelete.wait(timeout: .now() + patience)
                return try response(for: request, statusCode: 204, body: "")
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        model.notificationSyncActive = true
        lock.withLock { staleListing = true }
        let folderID = try #require(UUID(uuidString: Self.folderFixtureID))
        let filedID = try #require(UUID(uuidString: Self.filedSessionFixtureID))

        // The delete's request is being served before the refresh starts, so
        // the refresh's listing can only be answered after it.
        let delete = Task { await model.deleteFolder(folderID) }
        for _ in 0..<5_000 where !lock.withLock({ deleteInFlight }) {
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        try #require(lock.withLock { deleteInFlight })
        var refreshBegan = false
        let refresh = Task {
            refreshBegan = true
            await model.refreshScheduledReportHistory()
        }
        while !refreshBegan { await Task.yield() }
        releaseDelete.signal()
        await delete.value
        #expect(model.folders.map(\.name) == ["Work"])
        #expect(try #require(model.history.first { $0.sessionID == filedID }).folderID == nil)
        releaseListing.signal()
        await refresh.value

        #expect(model.folders.map(\.name) == ["Work"])
        #expect(try #require(model.history.first { $0.sessionID == filedID }).folderID == nil)
        #expect(model.groupedHistory.uncategorized.count == 2)
        #expect(model.errorMessage == nil)
    }

    /// A refresh started while a rename is in flight holds a folder page that
    /// predates it. The rename's own answer is newer, so that page must not
    /// bring the old name back.
    @Test
    func testARefreshStartedDuringARenameDoesNotRestoreTheOldName() async throws {
        let lock = NSLock()
        var staleListing = false
        var renameInFlight = false
        let releaseRename = DispatchSemaphore(value: 0)
        let releaseListing = DispatchSemaphore(value: 0)
        // Every suite shares one loading thread, so no wait here is unbounded.
        let patience = DispatchTimeInterval.seconds(10)
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case ("GET", "/v1/folders"):
                // Read before the rename committed, answered after it.
                if lock.withLock({ staleListing }) { _ = releaseListing.wait(timeout: .now() + patience) }
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("PATCH", "/v1/folders/\(Self.folderFixtureID)"):
                lock.withLock { renameInFlight = true }
                _ = releaseRename.wait(timeout: .now() + patience)
                return try response(
                    for: request, statusCode: 200,
                    body: folderJSON(Self.folderFixtureID, name: "Trips", count: 1)
                )
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        model.notificationSyncActive = true
        lock.withLock { staleListing = true }
        let folderID = try #require(UUID(uuidString: Self.folderFixtureID))

        // The rename's request is being served before the refresh starts, so
        // the refresh's folder page can only be answered after it.
        let rename = Task { await model.renameFolder(folderID, to: "Trips") }
        for _ in 0..<5_000 where !lock.withLock({ renameInFlight }) {
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        try #require(lock.withLock { renameInFlight })
        var refreshBegan = false
        let refresh = Task {
            refreshBegan = true
            await model.refreshScheduledReportHistory()
        }
        while !refreshBegan { await Task.yield() }
        releaseRename.signal()
        #expect(await rename.value)
        #expect(model.folders.map(\.name) == ["Trips", "Work"])
        releaseListing.signal()
        await refresh.value

        #expect(model.folders.map(\.name) == ["Trips", "Work"])
        #expect(model.folderEditorError == nil)
        #expect(model.errorMessage == nil)
    }

    /// A refresh started while a create is in flight holds a folder page from
    /// before it. The create's own answer is newer, so that page must not
    /// drop the new folder.
    @Test
    func testARefreshStartedDuringACreateDoesNotDropTheNewFolder() async throws {
        let lock = NSLock()
        var staleListing = false
        var createInFlight = false
        let releaseCreate = DispatchSemaphore(value: 0)
        let releaseListing = DispatchSemaphore(value: 0)
        // Every suite shares one loading thread, so no wait here is unbounded.
        let patience = DispatchTimeInterval.seconds(10)
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case ("GET", "/v1/folders"):
                // Read before the create committed, answered after it.
                if lock.withLock({ staleListing }) { _ = releaseListing.wait(timeout: .now() + patience) }
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("POST", "/v1/folders"):
                lock.withLock { createInFlight = true }
                _ = releaseCreate.wait(timeout: .now() + patience)
                return try response(
                    for: request, statusCode: 201,
                    body: folderJSON(Self.newFolderFixtureID, name: "Lisbon", count: 0)
                )
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        model.notificationSyncActive = true
        lock.withLock { staleListing = true }

        // The create's request is being served before the refresh starts, so
        // the refresh's folder page can only be answered after it.
        let create = Task { await model.createFolder(named: "Lisbon") }
        for _ in 0..<5_000 where !lock.withLock({ createInFlight }) {
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        try #require(lock.withLock { createInFlight })
        var refreshBegan = false
        let refresh = Task {
            refreshBegan = true
            await model.refreshScheduledReportHistory()
        }
        while !refreshBegan { await Task.yield() }
        releaseCreate.signal()
        #expect(await create.value?.name == "Lisbon")
        #expect(model.folders.map(\.name) == ["Lisbon", "Travel", "Work"])
        releaseListing.signal()
        await refresh.value

        #expect(model.folders.map(\.name) == ["Lisbon", "Travel", "Work"])
        #expect(model.folderEditorError == nil)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testRenameConflictKeepsTheNameAndSetsTheInlineError() async throws {
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case ("GET", "/v1/folders"):
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("PATCH", "/v1/folders/\(Self.folderFixtureID)"):
                return try response(
                    for: request, statusCode: 409,
                    body: #"{"error":{"code":"conflict","message":"folder name 'Work' is taken","details":{"reason":"folder_name_taken"},"request_id":"r"}}"#
                )
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let folderID = try #require(UUID(uuidString: Self.folderFixtureID))
        let renamed = await model.renameFolder(folderID, to: "Work")
        #expect(renamed == false)
        #expect(model.folderEditorError == "folder name 'Work' is taken")
        #expect(model.errorMessage == nil)
        #expect(model.folders.map(\.name) == ["Travel", "Work"])
        model.clearFolderEditorError()
        #expect(model.folderEditorError == nil)
    }

    @Test
    func testDeletingAFolderReturnsItsConversationsToHistory() async throws {
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case ("GET", "/v1/folders"):
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("DELETE", "/v1/folders/\(Self.folderFixtureID)"):
                return try response(for: request, statusCode: 204, body: "")
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let folderID = try #require(UUID(uuidString: Self.folderFixtureID))
        await model.deleteFolder(folderID)
        #expect(model.folders.map(\.name) == ["Work"])
        #expect(model.history.allSatisfy { $0.folderID == nil })
        #expect(model.groupedHistory.uncategorized.count == 2)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testAcceptingAProposalFilesItsConversationsAndDecliningRemovesIt() async throws {
        let state = FolderProposalFixtureState()
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                let loose = sessionJSON(
                    Self.looseSessionFixtureID, title: "Loose",
                    folderID: state.accepted ? Self.otherFolderFixtureID : nil
                )
                let filed = sessionJSON(Self.filedSessionFixtureID, title: "Filed", folderID: Self.folderFixtureID)
                return try response(for: request, statusCode: 200, body: "{\"items\":[\(filed),\(loose)],\"next_cursor\":null}")
            case ("GET", "/v1/folders"):
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                let items = state.resolved ? "" : proposalJSON(state: "proposed")
                return try response(for: request, statusCode: 200, body: "{\"items\":[\(items)],\"next_cursor\":null}")
            case ("POST", "/v1/folders/proposals/\(Self.proposalFixtureID)/accept"):
                state.accepted = true
                state.resolved = true
                return try response(
                    for: request, statusCode: 200,
                    body: proposalJSON(state: "accepted", resultingFolderID: Self.otherFolderFixtureID)
                )
            case ("POST", "/v1/folders/proposals/\(Self.proposalFixtureID)/decline"):
                state.resolved = true
                return try response(for: request, statusCode: 200, body: proposalJSON(state: "declined"))
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let proposalID = try #require(UUID(uuidString: Self.proposalFixtureID))
        #expect(model.suggestedFolders.map(\.id) == [proposalID])
        await model.acceptFolderProposal(proposalID)
        #expect(model.folderProposals.isEmpty)
        #expect(model.pendingFolderProposalIDs.isEmpty)
        let looseID = try #require(UUID(uuidString: Self.looseSessionFixtureID))
        #expect(model.history.first { $0.sessionID == looseID }?.folderID?.uuidString == Self.otherFolderFixtureID.uppercased())
        #expect(model.errorMessage == nil)

        state.resolved = false
        state.accepted = false
        await model.synchronizeHistory()
        #expect(model.suggestedFolders.count == 1)
        await model.declineFolderProposal(proposalID)
        #expect(model.folderProposals.isEmpty)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testAcceptingUnderANewNameSendsItAndFilesTheConversations() async throws {
        let recorder = WebsiteLoginRequestRecorder()
        let state = FolderProposalFixtureState()
        let model = try configuredModel { request in
            recorder.record(request)
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                let loose = sessionJSON(
                    Self.looseSessionFixtureID, title: "Loose",
                    folderID: state.accepted ? Self.otherFolderFixtureID : nil
                )
                let filed = sessionJSON(Self.filedSessionFixtureID, title: "Filed", folderID: Self.folderFixtureID)
                return try response(for: request, statusCode: 200, body: "{\"items\":[\(filed),\(loose)],\"next_cursor\":null}")
            case ("GET", "/v1/folders"):
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                let items = state.resolved ? "" : proposalJSON(state: "proposed")
                return try response(for: request, statusCode: 200, body: "{\"items\":[\(items)],\"next_cursor\":null}")
            case ("POST", "/v1/folders/proposals/\(Self.proposalFixtureID)/accept"):
                state.accepted = true
                state.resolved = true
                return try response(
                    for: request, statusCode: 200,
                    body: proposalJSON(state: "accepted", resultingFolderID: Self.otherFolderFixtureID)
                )
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let proposalID = try #require(UUID(uuidString: Self.proposalFixtureID))
        #expect(model.suggestedFolders.first?.proposedName == "Lisbon Trip")
        let accepted = await model.acceptFolderProposal(proposalID, name: "  Portugal 2027 ")
        #expect(accepted)
        let sent = recorder.matching(method: "POST", path: "/v1/folders/proposals/\(Self.proposalFixtureID)/accept")
        #expect(sent.count == 1)
        let body = try #require(sent.first?.body)
        let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(object["name"] as? String == "Portugal 2027")
        #expect(model.folderProposals.isEmpty)
        let looseID = try #require(UUID(uuidString: Self.looseSessionFixtureID))
        #expect(model.history.first { $0.sessionID == looseID }?.folderID?.uuidString == Self.otherFolderFixtureID.uppercased())
        #expect(model.folderEditorError == nil)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testATakenNameKeepsTheProposalOpenWithTheInlineError() async throws {
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case ("GET", "/v1/folders"):
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                return try response(for: request, statusCode: 200, body: "{\"items\":[\(proposalJSON(state: "proposed"))],\"next_cursor\":null}")
            case ("POST", "/v1/folders/proposals/\(Self.proposalFixtureID)/accept"):
                return try response(
                    for: request, statusCode: 409,
                    body: #"{"error":{"code":"conflict","message":"folder name 'Work' is taken","details":{"reason":"folder_name_taken"},"request_id":"r"}}"#
                )
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let proposalID = try #require(UUID(uuidString: Self.proposalFixtureID))
        let accepted = await model.acceptFolderProposal(proposalID, name: "Work")
        #expect(!accepted)
        #expect(model.folderEditorError == "folder name 'Work' is taken")
        #expect(model.suggestedFolders.map(\.id) == [proposalID])
        #expect(model.pendingFolderProposalIDs.isEmpty)
        #expect(model.errorMessage == nil)

        // Without a typed name there is no sheet to hold the error, so a
        // taken name is reported rather than mistaken for a resolved proposal.
        model.clearFolderEditorError()
        await model.acceptFolderProposal(proposalID)
        #expect(model.errorMessage == "folder name 'Work' is taken")
        #expect(model.folderEditorError == nil)
        #expect(model.suggestedFolders.map(\.id) == [proposalID])
    }

    @Test
    func testAProposalResolvedElsewhereClosesTheNameSheet() async throws {
        let state = FolderProposalFixtureState()
        let model = try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case ("GET", "/v1/folders"):
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case ("GET", "/v1/folders/proposals"):
                let items = state.resolved ? "" : proposalJSON(state: "proposed")
                return try response(for: request, statusCode: 200, body: "{\"items\":[\(items)],\"next_cursor\":null}")
            case ("POST", "/v1/folders/proposals/\(Self.proposalFixtureID)/accept"):
                state.resolved = true
                return try response(
                    for: request, statusCode: 409,
                    body: #"{"error":{"code":"conflict","message":"proposal is resolved","details":{"reason":"proposal_resolved"},"request_id":"r"}}"#
                )
            default:
                Issue.record("Unexpected request \(request.httpMethod ?? "") \(request.url?.path ?? "")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        let proposalID = try #require(UUID(uuidString: Self.proposalFixtureID))
        let finished = await model.acceptFolderProposal(proposalID, name: "Portugal")
        #expect(finished)
        #expect(model.folderProposals.isEmpty)
        #expect(model.folderEditorError == nil)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testForgettingCredentialsClearsFolderState() async throws {
        let model = try configuredModel { request in
            switch request.url?.path {
            case "/v1/sessions":
                return try response(for: request, statusCode: 200, body: sessionsPageJSON())
            case "/v1/folders":
                return try response(for: request, statusCode: 200, body: folderPageJSON())
            case "/v1/folders/proposals":
                return try response(for: request, statusCode: 200, body: "{\"items\":[\(proposalJSON(state: "proposed"))],\"next_cursor\":null}")
            default:
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        #expect(model.foldersAvailable)
        await model.forgetCredentials()
        #expect(model.foldersAvailable == false)
        #expect(model.folders.isEmpty)
        #expect(model.folderProposals.isEmpty)
    }

    @Test(arguments: [true, false])
    func testNotificationAttentionRequiresVisibleLoadedTranscriptAndAcknowledgesOnlyItsResult(
        replaysTerminal: Bool
    ) async throws {
        let sessionID = UUID(), runID = UUID(), scheduleID = UUID()
        let sessionBody = """
        {"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Result","metadata":{"schedule_id":"\(scheduleID.uuidString)"},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:04:00Z","active_run_id":null,"last_run_id":"\(runID.uuidString)"}
        """
        let recorder = WebsiteLoginRequestRecorder()
        let model = try configuredModel { request in
            switch request.url?.path {
            case "/v1/sessions":
                return try response(for: request, statusCode: 200, body: "{\"items\":[\(sessionBody)],\"next_cursor\":null}")
            case "/v1/sessions/\(sessionID.uuidString)":
                recorder.record(request)
                return try response(for: request, statusCode: 200, body: sessionBody)
            case "/v1/sessions/\(sessionID.uuidString)/messages":
                return try response(for: request, statusCode: 200, body: #"{"items":[{"sequence":2,"role":"assistant","content":[{"type":"text","text":"Done"}]}],"next_cursor":null}"#)
            case "/v1/runs/\(runID.uuidString)":
                return try response(for: request, statusCode: 200, body: """
                {"id":"\(runID.uuidString)","session_id":"\(sessionID.uuidString)","parent_run_id":null,"status":"COMPLETED","step_count":1,"model_call_count":1,"tool_call_count":0,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-08-14T00:03:00Z","updated_at":"2026-08-14T00:04:00Z"}
                """)
            case "/v1/runs/\(runID.uuidString)/events":
                let replay = replaysTerminal
                    ? "id: 3\nevent: run.completed\ndata: {\"run_id\":\"\(runID.uuidString)\"}\n\n"
                    : ": heartbeat\n\n"
                return try response(for: request, statusCode: 200, body: replay, headers: ["Content-Type": "text/event-stream"])
            case "/v1/notifications/sync":
                recorder.record(request)
                let seen = recorder.matching(method: "POST", path: "/v1/notifications/sync").contains {
                    guard let body = $0.body,
                        let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return false }
                    return (json["seen_run_ids"] as? [String])?.contains(runID.uuidString) == true
                }
                let unread = seen ? "[]" : "[\"\(runID.uuidString)\"]"
                return try response(for: request, statusCode: 200, body: "{\"obsolete_notification_ids\":[],\"unread_run_ids\":\(unread)}")
            default:
                return try response(for: request, statusCode: 404, body: "{}")
            }
        }
        model.notificationAttention = NotificationAttentionCoordinator(store: AttentionStore(values: []))
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        model.notificationSyncActive = true
        await model.synchronizeNotifications()
        let report = try #require(model.history.first)
        #expect(model.hasUnreadReport(report))
        model.notificationAttentionEnabled = true
        model.notificationTranscriptVisible = true
        #expect(model.visibleNotificationSessionID == nil)
        model.notificationSyncActive = false
        await model.selectSession(try #require(model.history.first))
        #expect(model.visibleNotificationSessionID == sessionID)
        model.notificationAttentionEnabled = false
        #expect(model.visibleNotificationSessionID == nil) // Email or an inactive scene
        model.notificationAttentionEnabled = true
        model.notificationOverlayPresented = true
        #expect(model.visibleNotificationSessionID == nil)
        model.notificationOverlayPresented = false
        model.reportNotificationRegistrationFailure(NSError(domain: "notification-test", code: 1))
        #expect(model.visibleNotificationSessionID == nil)
        model.clearError()
        #expect(model.visibleNotificationSessionID == sessionID)
        model.notificationTranscriptVisible = false
        #expect(model.visibleNotificationSessionID == nil)
        model.notificationTranscriptVisible = true
        model.notificationSyncActive = true
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        #expect(model.visibleNotificationSessionID == sessionID)
        #expect(recorder.matching(method: "GET", path: "/v1/sessions/\(sessionID.uuidString)").count == 2)
        // A loaded completed report must clear even while terminal replay is unavailable.
        for _ in 0..<100 {
            await model.synchronizeNotifications()
            let acknowledged = recorder.matching(method: "POST", path: "/v1/notifications/sync").contains {
                guard let body = $0.body,
                    let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return false }
                return (json["seen_run_ids"] as? [String])?.contains(runID.uuidString) == true
            }
            if acknowledged { break }
            await Task.yield()
        }
        let requests = recorder.matching(method: "POST", path: "/v1/notifications/sync")
        #expect(!model.hasUnreadReport(report))
        #expect(requests.contains {
            guard let body = $0.body,
                let json = try? JSONSerialization.jsonObject(with: body) as? [String: Any] else { return false }
            return (json["seen_run_ids"] as? [String])?.contains(runID.uuidString) == true
        })
        #expect(requests.allSatisfy { $0.authorization == "Bearer test-token" })
        model.newSession()
        #expect(model.visibleNotificationSessionID == nil)
    }

    @Test(arguments: ["running", "completionDuringLoad", "failedTranscript", "hiddenTranscript"])
    func testNotificationSnapshotDoesNotAcknowledgeAnUnseenResult(scenario: String) async throws {
        let sessionID = UUID(), runID = UUID(), scheduleID = UUID()
        let sessionBody = """
        {"id":"\(sessionID)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Report","metadata":{"schedule_id":"\(scheduleID)"},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:04:00Z","active_run_id":null,"last_run_id":"\(runID)"}
        """
        let recorder = WebsiteLoginRequestRecorder()
        let model = try configuredModel { request in
            switch request.url?.path {
            case "/v1/sessions":
                return try response(for: request, statusCode: 200, body: "{\"items\":[\(sessionBody)],\"next_cursor\":null}")
            case "/v1/sessions/\(sessionID)":
                return try response(for: request, statusCode: 200, body: sessionBody)
            case "/v1/sessions/\(sessionID)/messages":
                recorder.record(request)
                if scenario == "failedTranscript" {
                    if request.url?.query != nil {
                        return try response(for: request, statusCode: 503, body: "{}")
                    }
                    return try response(for: request, statusCode: 200, body: #"{"items":[{"sequence":2,"role":"assistant","content":[{"type":"text","text":"Partial report"}]}],"next_cursor":"next"}"#)
                }
                return try response(for: request, statusCode: 200, body: #"{"items":[{"sequence":2,"role":"assistant","content":[{"type":"text","text":"Report"}]}],"next_cursor":null}"#)
            case "/v1/runs/\(runID)":
                // Completing between a transcript read and a later status read
                // must not acknowledge a final message the transcript did not load.
                let loaded = !recorder.matching(method: "GET", path: "/v1/sessions/\(sessionID)/messages").isEmpty
                let active = scenario == "running" || (scenario == "completionDuringLoad" && !loaded)
                let status = active ? "RUNNING" : "COMPLETED"
                return try response(for: request, statusCode: 200, body: """
                {"id":"\(runID)","session_id":"\(sessionID)","parent_run_id":null,"status":"\(status)","step_count":1,"model_call_count":1,"tool_call_count":0,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-08-14T00:03:00Z","updated_at":"2026-08-14T00:04:00Z"}
                """)
            case "/v1/runs/\(runID)/events":
                return try response(for: request, statusCode: 200, body: ": heartbeat\n\n", headers: ["Content-Type": "text/event-stream"])
            case "/v1/notifications/sync":
                recorder.record(request)
                return try response(for: request, statusCode: 200, body: #"{"obsolete_notification_ids":[],"unread_run_ids":[]}"#)
            default:
                return try response(for: request, statusCode: 404, body: "{}")
            }
        }
        model.notificationAttention = NotificationAttentionCoordinator(store: AttentionStore(values: []))
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        model.notificationAttentionEnabled = true
        model.notificationTranscriptVisible = scenario != "hiddenTranscript"
        await model.selectSession(try #require(model.history.first))
        if scenario == "failedTranscript" {
            #expect(model.errorMessage != nil)
            model.clearError() // Dismissing an error cannot turn a partial load into a read.
            #expect(model.visibleNotificationSessionID == nil)
        }
        model.notificationSyncActive = true
        await model.synchronizeNotifications()
        let requests = recorder.matching(method: "POST", path: "/v1/notifications/sync")
        #expect(!requests.isEmpty)
        for request in requests {
            let body = try #require(request.body)
            let json = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
            #expect((json["seen_run_ids"] as? [String]) == [])
        }
        model.newSession()
    }

    private struct ThreadSessionIDs {
        let session = UUID()
        let thread = UUID()
        let run = UUID()
        let approval = UUID()
        let question = UUID()
        /// The send approval the thread's draft awaits, distinct from the
        /// discussion's own approval.
        let draftApproval = UUID()
    }

    private static func emailThreadJSON(
        _ threadID: UUID, sessionID: UUID, runID: UUID, draftApprovalID: UUID
    ) -> String {
        let draftID = UUID()
        return """
            {"id":"\(threadID)","account_id":"work","subject":"Board discussion","senders":["alex@example.test"],"updated_at":"2026-09-11T00:00:00Z","revision":1,"summary":"Review the agenda","reason":"A colleague","needs_reply":true,"draft_id":"\(draftID)","session_id":"\(sessionID)","priority":0.9,"complete":true,"messages":[],"draft":{"id":"\(draftID)","thread_id":"\(threadID)","account_id":"work","revision":1,"source_revision":1,"provider_thread_id":"provider-thread","send_tool_name":"mcp.gmail_send.send_message","to":["alex@example.test"],"cc":[],"bcc":[],"subject":"Re: Board discussion","body":"Thanks","status":"awaiting_approval","stale":false,"run_id":"\(runID)","approval_id":"\(draftApprovalID)","session_id":"\(sessionID)","updated_at":"2026-09-11T00:00:00Z"}}
            """
    }

    /// Serves one thread-bound session whose run waits on the discussion's own
    /// approval or question, while the thread's draft awaits a different approval.
    private func threadSessionModel(
        _ ids: ThreadSessionIDs, runStatus: String, threadReadFails: Bool = false
    ) throws -> ChatViewModel {
        try configuredModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/sessions"):
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("GET", "/v1/sessions/\(ids.session)"):
                return try response(for: request, statusCode: 200, body: """
                    {"id":"\(ids.session)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Board discussion","metadata":{"email_thread_id":"\(ids.thread)","email_account_id":"work"},"created_at":"2026-09-11T00:00:00Z","updated_at":"2026-09-11T00:00:00Z","active_run_id":"\(ids.run)","last_run_id":"\(ids.run)"}
                    """)
            case ("GET", "/v1/sessions/\(ids.session)/messages"):
                return try response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("GET", "/v1/runs/\(ids.run)"):
                return try response(for: request, statusCode: 200, body: """
                    {"id":"\(ids.run)","session_id":"\(ids.session)","parent_run_id":null,"status":"\(runStatus)","step_count":1,"model_call_count":1,"tool_call_count":1,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-09-11T00:00:00Z","updated_at":"2026-09-11T00:00:00Z"}
                    """)
            case ("GET", "/v1/runs/\(ids.run)/events"):
                return try response(for: request, statusCode: 200, body: "")
            case ("GET", "/v1/approvals/\(ids.approval)"):
                return try response(for: request, statusCode: 200, body: """
                    {"id":"\(ids.approval)","run_id":"\(ids.run)","session_id":"\(ids.session)","status":"PENDING","tool_name":"sandbox.run","action_summary":"Run code","arguments":{},"risk":"HIGH","policy_reason":"approval required","expires_at":null,"created_at":"2026-09-11T00:00:00Z","resolved_at":null,"resolved_by":null,"decision":null}
                    """)
            case ("GET", "/v1/email/threads/\(ids.thread)"):
                if threadReadFails {
                    return try response(for: request, statusCode: 404, body: #"{"error":{"code":"not_found","message":"Not found"}}"#)
                }
                return try response(for: request, statusCode: 200, body: Self.emailThreadJSON(
                    ids.thread, sessionID: ids.session, runID: UUID(), draftApprovalID: ids.draftApproval
                ))
            case ("GET", "/v1/email/accounts"):
                return try response(for: request, statusCode: 200, body: #"{"items":[]}"#)
            default:
                Issue.record("unexpected request: \(request.httpMethod ?? "nil") \(request.url?.path ?? "nil")")
                return try response(for: request, statusCode: 500, body: "{}")
            }
        }
    }

    private func configuredModel(
        titleRefreshDelay: TimeInterval = 10,
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> ChatViewModel {
        let session = urlSession(handler: handler)
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        return ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: session,
            titleRefreshDelay: titleRefreshDelay
        )
    }

    private func urlSession(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) -> URLSession {
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        let handlerID = ChatViewModelURLProtocol.register(handler)
        sessionConfiguration.httpAdditionalHeaders = [
            ChatViewModelURLProtocol.handlerHeader: handlerID
        ]
        sessionConfiguration.protocolClasses = [ChatViewModelURLProtocol.self]
        return URLSession(configuration: sessionConfiguration)
    }

    private func response(
        for request: URLRequest,
        statusCode: Int,
        body: String,
        headers: [String: String] = [:]
    ) throws -> (HTTPURLResponse, Data) {
        var responseHeaders = ["Content-Type": "application/json"]
        responseHeaders.merge(headers) { _, new in new }
        let response = try #require(
            HTTPURLResponse(
                url: request.url!,
                statusCode: statusCode,
                httpVersion: nil,
                headerFields: responseHeaders
            )
        )
        return (response, Data(body.utf8))
    }
}

/// Records the transport identity of every request a website-login test makes,
/// so a cancellation can be attributed to the connection that began the
/// ceremony rather than to whichever connection replaced it.
private final class WebsiteLoginRequestRecorder: @unchecked Sendable {
    struct Entry: Sendable {
        let method: String
        let url: String
        let path: String
        let authorization: String?
        let body: Data?
    }

    private let lock = NSLock()
    private var entries: [Entry] = []

    func record(_ request: URLRequest) {
        var body = request.httpBody
        if body == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var collected = Data()
            var buffer = [UInt8](repeating: 0, count: 1_024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                collected.append(buffer, count: count)
            }
            body = collected
        }
        let entry = Entry(
            method: request.httpMethod ?? "",
            url: request.url?.absoluteString ?? "",
            path: request.url?.path ?? "",
            authorization: request.value(forHTTPHeaderField: "Authorization"),
            body: body
        )
        lock.withLock { entries.append(entry) }
    }

    func matching(method: String, url: String) -> [Entry] {
        lock.withLock { entries.filter { $0.method == method && $0.url == url } }
    }

    func matching(method: String, path: String) -> [Entry] {
        lock.withLock { entries.filter { $0.method == method && $0.path == path } }
    }
}

private actor SuspendedReconciliationHistoryStore: SessionHistoryStore {
    private var listCount = 0
    private var continuation: CheckedContinuation<Void, Never>?
    var isWaiting: Bool { continuation != nil }

    func list() async -> [SessionHistoryEntry] {
        listCount += 1
        if listCount == 2 {
            await withCheckedContinuation { continuation = $0 }
        }
        return []
    }

    func release() {
        continuation?.resume()
        continuation = nil
    }

    func upsert(_ entry: SessionHistoryEntry) {}
    func delete(sessionID: UUID) {}
}

private enum DeleteFailingHistoryStoreError: Error, LocalizedError {
    case syntheticFailure

    var errorDescription: String? { DeleteFailingHistoryStore.message }
}

private actor DeleteFailingHistoryStore: SessionHistoryStore {
    static let message = "Synthetic history deletion failure."
    private var entries: [UUID: SessionHistoryEntry] = [:]

    func list() -> [SessionHistoryEntry] {
        entries.values.sortedForHistoryList()
    }

    func upsert(_ entry: SessionHistoryEntry) {
        entries[entry.sessionID] = entry
    }

    func delete(sessionID: UUID) throws {
        throw DeleteFailingHistoryStoreError.syntheticFailure
    }
}

private final class ChatViewModelURLProtocol: URLProtocol {
    static let handlerHeader = "X-Veetbot-Test-Handler-ID"
    private static let handlerStore = ChatViewModelURLProtocolHandlerStore()

    static func register(
        _ handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) -> String {
        handlerStore.register(handler)
    }

    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard
            let handlerID = request.value(forHTTPHeaderField: Self.handlerHeader),
            let handler = Self.handlerStore.handler(for: handlerID)
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private final class ChatViewModelURLProtocolHandlerStore: @unchecked Sendable {
    typealias Handler = (URLRequest) throws -> (HTTPURLResponse, Data)

    private let lock = NSLock()
    private var handlers: [String: Handler] = [:]

    func register(_ handler: @escaping Handler) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }

    func handler(for id: String) -> Handler? {
        lock.withLock { handlers[id] }
    }
}

/// Mutable server-side state for the proposal fixture, shared with the handler.
private final class FolderProposalFixtureState: @unchecked Sendable {
    private let lock = NSLock()
    private var acceptedValue = false
    private var resolvedValue = false

    var accepted: Bool {
        get { lock.withLock { acceptedValue } }
        set { lock.withLock { acceptedValue = newValue } }
    }

    var resolved: Bool {
        get { lock.withLock { resolvedValue } }
        set { lock.withLock { resolvedValue = newValue } }
    }
}
