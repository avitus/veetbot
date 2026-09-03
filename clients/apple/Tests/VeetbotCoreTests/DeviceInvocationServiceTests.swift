import Foundation
import Testing

@testable import VeetbotCore

@Suite struct DeviceInvocationServiceTests {
    private static let now = Date(timeIntervalSince1970: 1_000_000)
    private static let deviceID = UUID(uuidString: "00000000-0000-0000-0000-0000000000d1")!

    @Test
    func testOnlyLiveSmsInvocationsSurviveAndLapsedRowsAreExpired() async throws {
        let live = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000001"))
        let lapsed = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000002"))
        let foreign = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000003"))
        let api = FakeDeviceInvocationAPI(
            pending: [
                Self.pendingRow(
                    id:live,
                    arguments: ["recipient": .string("+15550001111"), "body": .string("running late")],
                    expiresAt: Self.now.addingTimeInterval(300)
                ),
                Self.pendingRow(
                    id:lapsed,
                    arguments: ["recipient": .string("+15550002222"), "body": .string("stale")],
                    expiresAt: Self.now.addingTimeInterval(-1)
                ),
                Self.pendingRow(
                    id:foreign,
                    toolName: "sandbox.run_command",
                    arguments: ["command": .string("ls")],
                    expiresAt: Self.now.addingTimeInterval(300)
                ),
            ]
        )
        let service = DeviceInvocationService(
            api: api,
            deviceID: Self.deviceID,
            now: { Self.now }
        )

        let ready = try await service.nextSmsInvocations()

        #expect(
            ready == [
                SmsInvocation(
                    id: live,
                    recipient: "+15550001111",
                    body: "running late",
                    expiresAt: Self.now.addingTimeInterval(300)
                )
            ]
        )
        #expect(
            await api.postedResults() == [
                FakeDeviceInvocationAPI.PostedResult(invocationID: lapsed, result: .expired)
            ]
        )
        #expect(await api.postedDeviceIDs() == [Self.deviceID])
    }

    @Test
    func testCompletingASendPostsExactlyOneResult() async throws {
        let invocationID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000011"))
        let api = FakeDeviceInvocationAPI(pending: [])
        let service = DeviceInvocationService(
            api: api,
            deviceID: Self.deviceID,
            now: { Self.now }
        )

        await service.complete(
            SmsInvocation(
                id: invocationID,
                recipient: "+15550001111",
                body: "on my way",
                expiresAt: Self.now.addingTimeInterval(300)
            ),
            with: .sent
        )

        #expect(
            await api.postedResults() == [
                FakeDeviceInvocationAPI.PostedResult(invocationID: invocationID, result: .sent)
            ]
        )
    }

    @Test
    func testAConflictMeansTheRowIsAlreadySettledSoTheResultPostIsNeverRetried() async throws {
        let invocationID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000021"))
        let api = FakeDeviceInvocationAPI(
            pending: [],
            postFailure: .conflict
        )
        let service = DeviceInvocationService(
            api: api,
            deviceID: Self.deviceID,
            now: { Self.now },
            resultPostAttempts: 3,
            retryBackoff: { _ in }
        )

        await service.complete(Self.invocation(id: invocationID), with: .cancelled)

        #expect(
            await api.postedResults() == [
                FakeDeviceInvocationAPI.PostedResult(invocationID: invocationID, result: .cancelled)
            ]
        )
    }

    @Test
    func testATransientResultPostFailureIsRetriedRememberedAndReplayedRatherThanRepresented()
        async throws
    {
        let invocationID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000022"))
        let sent = Self.invocation(id: invocationID)
        let api = FakeDeviceInvocationAPI(
            pending: [
                Self.pendingRow(
                    id: invocationID,
                    arguments: [
                        "recipient": .string("+15550001111"), "body": .string("on my way"),
                    ],
                    expiresAt: Self.now.addingTimeInterval(300)
                )
            ],
            postFailure: .transient
        )
        let service = DeviceInvocationService(
            api: api,
            deviceID: Self.deviceID,
            now: { Self.now },
            resultPostAttempts: 3,
            retryBackoff: { _ in }
        )

        await service.complete(sent, with: .sent)

        // Every attempt failed, so the result is owed rather than lost.
        #expect(await api.postedResults().count == 3)
        #expect(await api.postedResults().allSatisfy { $0.result == .sent })

        await api.stopFailing()
        let ready = try await service.nextSmsInvocations()

        // The row is still pending server-side, but the owner already sent the
        // message: the recovery replays the remembered result instead of
        // asking them to compose it a second time.
        #expect(ready.isEmpty)
        #expect(
            await api.postedResults().suffix(1) == [
                FakeDeviceInvocationAPI.PostedResult(invocationID: invocationID, result: .sent)
            ]
        )
        #expect(await api.postedResults().count == 4)
    }

    @Test
    func testMalformedArgumentsAreFailedRatherThanPresented() async throws {
        let malformed = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000031"))
        let api = FakeDeviceInvocationAPI(
            pending: [
                Self.pendingRow(
                    id:malformed,
                    arguments: ["body": .string("no recipient")],
                    expiresAt: Self.now.addingTimeInterval(300)
                )
            ]
        )
        let service = DeviceInvocationService(
            api: api,
            deviceID: Self.deviceID,
            now: { Self.now }
        )

        let ready = try await service.nextSmsInvocations()

        #expect(ready.isEmpty)
        #expect(
            await api.postedResults() == [
                FakeDeviceInvocationAPI.PostedResult(invocationID: malformed, result: .failed)
            ]
        )
    }

    @Test
    func testTheQueuePresentsOneInvocationAtATimeWithoutRepeatingItself() throws {
        let first = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000041"))
        let second = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000042"))
        let firstInvocation = SmsInvocation(
            id: first,
            recipient: "+15550001111",
            body: "first",
            expiresAt: Self.now.addingTimeInterval(300)
        )
        let secondInvocation = SmsInvocation(
            id: second,
            recipient: "+15550002222",
            body: "second",
            expiresAt: Self.now.addingTimeInterval(300)
        )
        var queue = SmsInvocationQueue()

        queue.merge([firstInvocation, secondInvocation])
        #expect(queue.presented == firstInvocation)

        // A re-fetch (foreground recovery over the same pending queue) must not
        // re-present what is already on screen or duplicate what is waiting.
        queue.merge([firstInvocation, secondInvocation])
        #expect(queue.presented == firstInvocation)

        let settledFirst = queue.settle(first)
        #expect(settledFirst)
        #expect(queue.presented == secondInvocation)

        let settledSecond = queue.settle(second)
        #expect(settledSecond)
        #expect(queue.presented == nil)
        #expect(queue.isEmpty)
    }

    @Test
    func testASettleForAnInvocationNoLongerOnScreenNeverSettlesTheOneThatIs() throws {
        let first = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000043"))
        let second = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000044"))
        let firstInvocation = SmsInvocation(
            id: first,
            recipient: "+15550001111",
            body: "first",
            expiresAt: Self.now.addingTimeInterval(300)
        )
        let secondInvocation = SmsInvocation(
            id: second,
            recipient: "+15550002222",
            body: "second",
            expiresAt: Self.now.addingTimeInterval(300)
        )
        var queue = SmsInvocationQueue()
        queue.merge([firstInvocation, secondInvocation])

        let settled = queue.settle(first)
        #expect(settled)

        // The sheet's dismissal write-back arrives after the head advanced.
        // It names the invocation it was showing, and settling it again must
        // not consume the one the owner has not answered yet.
        let settledAgain = queue.settle(first)
        #expect(settledAgain == false)
        #expect(queue.presented == secondInvocation)
    }

    @Test
    func testADeviceThatCannotSendTextsFailsTheInvocationInsteadOfComposing() throws {
        let invocationID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000051"))
        let invocation = SmsInvocation(
            id: invocationID,
            recipient: "+15550001111",
            body: "on my way",
            expiresAt: Self.now.addingTimeInterval(300)
        )

        #expect(
            SmsInvocationDisposition.resolve(invocation, canSendText: true, now: Self.now)
                == .compose(invocation)
        )
        #expect(
            SmsInvocationDisposition.resolve(invocation, canSendText: false, now: Self.now)
                == .unsupported(invocation)
        )
        #expect(SmsInvocationDisposition.resolve(nil, canSendText: true, now: Self.now) == .idle)
    }

    @Test
    func testALapsedQueueHeadIsExpiredRatherThanComposedWhileALiveHeadStillPresents() throws {
        let first = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000052"))
        let second = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000053"))
        let firstInvocation = SmsInvocation(
            id: first,
            recipient: "+15550001111",
            body: "first",
            expiresAt: Self.now.addingTimeInterval(300)
        )
        // Queued behind the first invocation, and already expired by the time
        // the owner finally gets to it.
        let secondInvocation = SmsInvocation(
            id: second,
            recipient: "+15550002222",
            body: "second",
            expiresAt: Self.now.addingTimeInterval(60)
        )
        var queue = SmsInvocationQueue()
        queue.merge([firstInvocation, secondInvocation])
        #expect(queue.presented == firstInvocation)

        // The owner sits on the first sheet past the second invocation's
        // deadline before finally answering it.
        let pastSecondDeadline = Self.now.addingTimeInterval(120)
        let settledFirst = queue.settle(first)
        #expect(settledFirst)
        #expect(queue.presented == secondInvocation)

        // The lapsed head is settled expired instead of surfacing as the
        // composing invocation.
        #expect(
            SmsInvocationDisposition.resolve(
                queue.presented,
                canSendText: true,
                now: pastSecondDeadline
            ) == .expired(secondInvocation)
        )

        // A head that is still live at the moment of presentation composes
        // normally.
        #expect(
            SmsInvocationDisposition.resolve(firstInvocation, canSendText: true, now: Self.now)
                == .compose(firstInvocation)
        )
    }

    fileprivate static func pendingRow(
        id: UUID,
        toolName: String = "device.sms.send",
        arguments: [String: JSONValue],
        expiresAt: Date
    ) -> DeviceInvocationView {
        DeviceInvocationView(
            id: id,
            toolName: toolName,
            arguments: arguments,
            createdAt: now.addingTimeInterval(-10),
            expiresAt: expiresAt
        )
    }

    fileprivate static func invocation(id: UUID) -> SmsInvocation {
        SmsInvocation(
            id: id,
            recipient: "+15550001111",
            body: "on my way",
            expiresAt: now.addingTimeInterval(300)
        )
    }
}

private actor FakeDeviceInvocationAPI: DeviceInvocationAPI {
    struct PostedResult: Equatable, Sendable {
        let invocationID: UUID
        let result: DeviceInvocationResult
    }

    /// The two failures a result post has to tell apart: a conflict, which the
    /// server raises only for a row it has already expired, and a transient
    /// failure, which says nothing about whether the row is settled.
    enum PostFailure {
        case conflict
        case transient

        var error: Error {
            switch self {
            case .conflict:
                return HTTPTransportError.api(
                    APIError(
                        code: .conflict,
                        message: "already resolved",
                        requestID: "conflict-1",
                        statusCode: 409
                    )
                )
            case .transient:
                return HTTPTransportError.api(
                    APIError(
                        code: .internalError,
                        message: "upstream unavailable",
                        requestID: "transient-1",
                        statusCode: 503
                    )
                )
            }
        }
    }

    private let pending: [DeviceInvocationView]
    private var postFailure: PostFailure?
    private var posted: [PostedResult] = []
    private var deviceIDs: [UUID] = []

    init(pending: [DeviceInvocationView], postFailure: PostFailure? = nil) {
        self.pending = pending
        self.postFailure = postFailure
    }

    func stopFailing() {
        postFailure = nil
    }

    func pendingInvocations(deviceID: UUID) async throws -> DeviceInvocationList {
        DeviceInvocationList(invocations: pending)
    }

    func postInvocationResult(
        deviceID: UUID,
        invocationID: UUID,
        result: DeviceInvocationResult
    ) async throws -> DeviceInvocationResultView {
        posted.append(PostedResult(invocationID: invocationID, result: result))
        deviceIDs.append(deviceID)
        if let postFailure { throw postFailure.error }
        return DeviceInvocationResultView(
            id: invocationID,
            status: result.rawValue,
            resolvedAt: Date(timeIntervalSince1970: 1_000_001)
        )
    }

    func postedResults() -> [PostedResult] { posted }
    func postedDeviceIDs() -> [UUID] { deviceIDs }
}

/// The push tap, the fetch, and the settle path as the shipping app drives
/// them, over a stubbed transport.
@Suite(.serialized) @MainActor struct SmsInvocationFlowTests {
    private static let deviceID = UUID(uuidString: "00000000-0000-0000-0000-0000000000d2")!

    @Test
    func testAStaleSheetDismissalNeverCancelsTheInvocationThatIsNowOnScreen() async throws {
        let first = try #require(UUID(uuidString: "00000000-0000-0000-0000-00000000a001"))
        let second = try #require(UUID(uuidString: "00000000-0000-0000-0000-00000000a002"))
        let recorder = InvocationRequestRecorder()
        let invocations = """
            {"invocations":[\
            {"id":"\(first.uuidString)","tool_name":"device.sms.send",\
            "arguments":{"recipient":"+15550001111","body":"first"},\
            "created_at":"2026-09-01T00:00:00Z","expires_at":"2099-01-01T00:00:00Z"},\
            {"id":"\(second.uuidString)","tool_name":"device.sms.send",\
            "arguments":{"recipient":"+15550002222","body":"second"},\
            "created_at":"2026-09-01T00:00:00Z","expires_at":"2099-01-01T00:00:00Z"}]}
            """
        let model = try Self.configuredModel { request in
            let method = request.httpMethod ?? ""
            let path = request.url?.path ?? ""
            recorder.record(method: method, path: path)
            switch (method, path) {
            case ("GET", "/v1/sessions"), ("GET", "/v1/devices"):
                return try Self.response(
                    for: request,
                    body: #"{"items":[],"next_cursor":null}"#
                )
            case ("GET", "/v1/devices/\(Self.deviceID.uuidString)/invocations"):
                return try Self.response(for: request, body: invocations)
            case (
                "POST",
                "/v1/devices/\(Self.deviceID.uuidString)/invocations/\(first.uuidString)/result"
            ):
                return try Self.response(
                    for: request,
                    body:
                        #"{"id":"\#(first.uuidString)","status":"sent","resolved_at":"2026-09-01T00:01:00Z"}"#
                )
            default:
                Issue.record("unexpected request: \(method) \(path)")
                return try Self.response(for: request, statusCode: 500, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "token"))

        await model.openNotification(try Self.invocationPush(invocationID: first))
        let presented = try #require(model.pendingSmsInvocation)
        #expect(presented.id == first)

        await model.completeSmsInvocation(presented, with: .sent)
        #expect(model.pendingSmsInvocation?.id == second)

        // SwiftUI writes the sheet's binding back to nil as the first sheet
        // goes away. It names the invocation that was on screen, and that
        // invocation is already settled: nothing more may be posted, and the
        // invocation now waiting must survive untouched.
        await model.completeSmsInvocation(presented, with: .cancelled)

        #expect(model.pendingSmsInvocation?.id == second)
        #expect(
            recorder.posts() == [
                "/v1/devices/\(Self.deviceID.uuidString)/invocations/\(first.uuidString)/result"
            ]
        )
    }

    private static func invocationPush(invocationID: UUID) throws -> NotificationPushPayload {
        try #require(
            NotificationPushPayload(
                userInfo: [
                    "veetbot": [
                        "version": 1,
                        "kind": "device_invocation",
                        "title": "Your device has a pending action",
                        "status": "pending",
                        "invocation_id": invocationID.uuidString,
                        "device_id": deviceID.uuidString,
                        "notification_id": UUID().uuidString,
                    ]
                ]
            )
        )
    }

    private static func configuredModel(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> ChatViewModel {
        let configuration = URLSessionConfiguration.ephemeral
        let handlerID = InvocationURLProtocol.register(handler)
        configuration.httpAdditionalHeaders = [InvocationURLProtocol.handlerHeader: handlerID]
        configuration.protocolClasses = [InvocationURLProtocol.self]
        let suiteName = "com.veetbot.tests.invocations.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        return ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: URLSession(configuration: configuration)
        )
    }

    private static func response(
        for request: URLRequest,
        statusCode: Int = 200,
        body: String
    ) throws -> (HTTPURLResponse, Data) {
        let url = try #require(request.url)
        let response = try #require(
            HTTPURLResponse(
                url: url,
                statusCode: statusCode,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )
        )
        return (response, Data(body.utf8))
    }
}

private final class InvocationRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var entries: [(method: String, path: String)] = []

    func record(method: String, path: String) {
        lock.withLock { entries.append((method, path)) }
    }

    func posts() -> [String] {
        lock.withLock { entries.filter { $0.method == "POST" }.map(\.path) }
    }
}

private final class InvocationURLProtocol: URLProtocol {
    static let handlerHeader = "X-Veetbot-Test-Handler-ID"
    private static let handlers = InvocationHandlerStore()

    static func register(
        _ handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) -> String {
        handlers.register(handler)
    }

    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard
            let handlerID = request.value(forHTTPHeaderField: Self.handlerHeader),
            let handler = Self.handlers.handler(for: handlerID)
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

private final class InvocationHandlerStore: @unchecked Sendable {
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
