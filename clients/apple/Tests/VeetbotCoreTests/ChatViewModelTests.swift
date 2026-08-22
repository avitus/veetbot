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

        await model.openNotification(payload)
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
            #expect(try ChatViewModel.nextPageCursor(cursor, seen: &seen) == cursor)
        }
        #expect(seen.count == 101)
        #expect(throws: HTTPTransportError.self) {
            try ChatViewModel.nextPageCursor("cursor-101", seen: &seen)
        }
        #expect(try ChatViewModel.nextPageCursor(nil, seen: &seen) == nil)
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
