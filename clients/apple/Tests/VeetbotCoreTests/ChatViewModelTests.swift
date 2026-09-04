import Foundation
import Testing

@testable import VeetbotCore

@Suite(.serialized) @MainActor struct ChatViewModelTests {
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
                origin: "https://example.org",
                loginURL: "https://example.org/login"
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
            origin: "https://example.org",
            loginURL: "https://example.org/login"
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
                origin: "https://example.org",
                loginURL: "https://example.org/login"
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
            origin: "https://example.org",
            loginURL: "https://example.org/login"
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

    private func configuredModel(
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
            urlSession: session
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
    }

    private let lock = NSLock()
    private var entries: [Entry] = []

    func record(_ request: URLRequest) {
        let entry = Entry(
            method: request.httpMethod ?? "",
            url: request.url?.absoluteString ?? "",
            path: request.url?.path ?? "",
            authorization: request.value(forHTTPHeaderField: "Authorization")
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
