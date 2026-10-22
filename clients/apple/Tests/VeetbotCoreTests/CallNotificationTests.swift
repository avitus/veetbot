import Foundation
import Testing

@testable import VeetbotCore

struct CallNotificationTests {
    @Test func metadataReflectsOnlyAvailableCallFields() throws {
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let result = try decoder.decode(CallResultViewData.self, from: Data("""
        {"call_id":"00000000-0000-0000-0000-000000000123","created_at":"2026-10-20T12:00:00Z","duration_minutes":0.6833333333,"status":"completed"}
        """.utf8))
        #expect(result.createdAt != nil)
        #expect(result.durationText == "41 s")
        #expect(result.canFollowUp)
        for suffix in [",\"erased\":true", ",\"result_pending\":true", ",\"status\":\"active\""] {
            let hidden = try decoder.decode(CallResultViewData.self, from: Data(
                "{\"call_id\":\"00000000-0000-0000-0000-000000000123\"\(suffix)}".utf8))
            #expect(!hidden.canFollowUp)
            #expect(hidden.durationText == nil)
        }
    }

    @Test
    func acceptsOnlyContentFreeCallIdentity() throws {
        var payload: [String: Any] = [
            "version": 1, "kind": "call_finished", "title": "New call result",
            "call_id": UUID().uuidString, "notification_id": UUID().uuidString,
        ]
        let decoded = NotificationPushPayload(userInfo: ["veetbot": payload])
        #expect(decoded != nil)
        payload["transcript"] = "Untrusted caller content"
        #expect(NotificationPushPayload(userInfo: ["veetbot": payload]) == nil)
        payload.removeValue(forKey: "transcript")
        payload["session_id"] = UUID().uuidString
        #expect(NotificationPushPayload(userInfo: ["veetbot": payload]) == nil)
    }
}

@Suite(.serialized) @MainActor
struct CallResultPresentationTests {
    @Test(arguments: [200, 500])
    func newestNotificationWinsOverLateResultOrError(status: Int) async throws {
        let firstID = UUID()
        let newestID = UUID()
        let gate = CallResultResponseGate()
        defer { gate.release() }
        let model = try await model(delayedCallID: firstID, gate: gate, status: status)
        let firstPayload = try payload(firstID)
        let first = Task { await model.openNotification(firstPayload) }
        try await waitForResponse(gate)
        await model.openNotification(try payload(newestID))
        #expect(model.callResult?.callID == newestID)
        gate.release()
        await first.value
        #expect(model.callResult?.callID == newestID)
        #expect(model.errorMessage == nil)
    }

    @Test(arguments: ["logout", "owner", "server"])
    func connectionTransitionClearsCallContent(transition: String) async throws {
        let model = try await model()
        await model.openNotification(try payload(UUID()))
        #expect(model.callResult?.summary == "Private call")
        if transition == "logout" {
            await model.forgetCredentials()
        } else {
            #expect(await model.configure(
                baseURLString: transition == "server" ? "https://other.test" : "https://veetbot.test",
                token: transition == "owner" ? "replacement-token" : "test-token"
            ))
        }
        #expect(model.callResult == nil)
    }

    @Test(arguments: ["dismiss", "logout", "replace"])
    func abandonedFetchCannotRestoreCallContent(transition: String) async throws {
        let callID = UUID()
        let gate = CallResultResponseGate()
        defer { gate.release() }
        let model = try await model(delayedCallID: callID, gate: gate)
        let notification = try payload(callID)
        let pending = Task { await model.openNotification(notification) }
        try await waitForResponse(gate)
        if transition == "dismiss" {
            model.dismissCallResult()
        } else if transition == "logout" {
            await model.forgetCredentials()
        } else {
            #expect(await model.configure(baseURLString: "https://other.test", token: "replacement-token"))
        }
        gate.release()
        await pending.value
        #expect(model.callResult == nil)
    }

    @Test(arguments: [CallFollowUpAction.callAgain, .somethingWrong])
    func followUpStagesAnEditableReferenceWithoutSendingOrCopyingCallContent(action: CallFollowUpAction) async throws {
        let model = try await model()
        let id = UUID()
        await model.openNotification(try payload(id))
        model.composerText = "Keep my unfinished question"
        #expect(model.prepareCallFollowUp(action))
        #expect(model.composerText.hasPrefix("Keep my unfinished question\n\n"))
        #expect(model.composerText.contains(id.uuidString))
        #expect(!model.composerText.contains("Private call"))
        #expect(model.callResult == nil)
        let saved = model.composerText
        #expect(!model.prepareCallFollowUp(action))
        #expect(model.composerText == saved)
    }

    @Test(arguments: [",\"erased\":true", ",\"result_pending\":true", ",\"status\":\"active\""])
    func unavailableCallsNeverStageFollowUp(fields: String) async throws {
        let model = try await model(callFields: fields)
        await model.openNotification(try payload(UUID()))
        model.composerText = "Keep this"
        #expect(!model.prepareCallFollowUp(.callAgain))
        #expect(model.composerText == "Keep this")
        #expect(model.callResult != nil)
    }

    private func payload(_ callID: UUID) throws -> NotificationPushPayload {
        try #require(NotificationPushPayload(userInfo: ["veetbot": [
            "version": 1, "kind": "call_finished", "title": "New call result",
            "call_id": callID.uuidString, "notification_id": UUID().uuidString,
        ]]))
    }

    private func waitForResponse(_ gate: CallResultResponseGate) async throws {
        let deadline = Date().addingTimeInterval(5)
        while !gate.isWaiting && Date() < deadline {
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        try #require(gate.isWaiting)
    }

    private func model(delayedCallID: UUID? = nil, gate: CallResultResponseGate? = nil,
                       status: Int = 200, callFields: String = "") async throws -> ChatViewModel {
        let configuration = URLSessionConfiguration.ephemeral
        let handlerID = CallResultURLProtocol.register { request in
            guard let url = request.url else { throw URLError(.badURL) }
            if url.path == "/v1/sessions" {
                return (200, #"{"items":[],"next_cursor":null}"#, nil)
            }
            if let callID = UUID(uuidString: url.lastPathComponent), url.path.hasPrefix("/v1/calls/") {
                let delayed = callID == delayedCallID
                let code = delayed ? status : 200
                let body = code == 200
                    ? "{\"call_id\":\"\(callID)\",\"summary\":\"Private call\"\(callFields)}"
                    : #"{"error":{"code":"internal_error","message":"Late failure"}}"#
                return (code, body, delayed ? gate : nil)
            }
            Issue.record("Unexpected call notification request")
            return (500, "{}", nil)
        }
        configuration.protocolClasses = [CallResultURLProtocol.self]
        configuration.httpAdditionalHeaders = ["X-Call-Result-Test": handlerID]
        let defaults = try #require(UserDefaults(suiteName: "com.veetbot.calls.\(UUID())"))
        let model = ChatViewModel(tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: URLSession(configuration: configuration))
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        return model
    }
}

private final class CallResultResponseGate: @unchecked Sendable {
    private let lock = NSLock()
    private var delivery: (() -> Void)?
    private var released = false
    var isWaiting: Bool { lock.withLock { delivery != nil } }

    func install(_ callback: @escaping () -> Void) {
        let ready = lock.withLock {
            if released { return true }
            delivery = callback
            return false
        }
        if ready { callback() }
    }

    func release() {
        let callback = lock.withLock {
            released = true
            let callback = delivery
            delivery = nil
            return callback
        }
        callback?()
    }
}

private final class CallResultURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var handlers: [String: (URLRequest) throws -> (Int, String, CallResultResponseGate?)] = [:]

    static func register(_ handler: @escaping (URLRequest) throws -> (Int, String, CallResultResponseGate?)) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let id = request.value(forHTTPHeaderField: "X-Call-Result-Test"),
              let handler = Self.lock.withLock({ Self.handlers[id] }) else { return }
        do {
            let (status, body, gate) = try handler(request)
            let deliver = {
                let response = HTTPURLResponse(url: self.request.url!, statusCode: status,
                    httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
                self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                self.client?.urlProtocol(self, didLoad: Data(body.utf8))
                self.client?.urlProtocolDidFinishLoading(self)
            }
            if let gate { gate.install(deliver) } else { deliver() }
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }
    override func stopLoading() {}
}
