import Foundation
import Testing

@testable import VeetbotCore

/// Settings → Notifications lists the devices the server will notify
/// (`GET /v1/devices`), marks this one, and offers a content-free test
/// notification and removal (revocation) under `device.write`.
@MainActor
@Suite(.serialized) struct NotificationSettingsViewModelTests {
    private let thisInstallation = "install-this"

    @Test
    func testListsActiveDevicesWithThisDeviceFirstAcrossPages() async throws {
        let mac = UUID(), phone = UUID(), gone = UUID()
        let model = try makeModel { request in
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "cursor" }?.value
            if cursor == nil {
                return (200, #"{"items":[\#(Self.device(phone, name: "Andy's iPhone", install: "install-phone"))],"next_cursor":"page-2"}"#)
            }
            return (200, #"{"items":[\#(Self.device(mac, name: "Studio Mac", install: "install-this")),\#(Self.device(gone, name: "Old iPad", install: "install-old", status: "revoked"))],"next_cursor":null}"#)
        }

        await model.load()

        #expect(model.devices.map(\.id) == [mac, phone])
        #expect(model.isThisDevice(try #require(model.devices.first)))
        #expect(!model.unavailable)
    }

    @Test
    func testATestNotificationCarriesAnIdempotencyKeyAndConfirmsWithAToast() async throws {
        let phone = UUID()
        let recorder = RequestRecorder()
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
            recorder.append(request)
            if request.httpMethod == "POST" { return (202, #"{"notification_id":"\#(UUID())"}"#) }
            return (200, #"{"items":[\#(Self.device(phone, name: "Andy's iPhone", install: "install-phone"))],"next_cursor":null}"#)
        }
        await model.load()

        await model.sendTest(to: try #require(model.devices.first))

        let post = try #require(recorder.snapshot.last)
        #expect(post.httpMethod == "POST")
        #expect(post.url?.path == "/v1/devices/\(phone.uuidString)/test-notification")
        #expect(!(post.value(forHTTPHeaderField: "Idempotency-Key") ?? "").isEmpty)
        #expect(toasts.current?.text == "Test notification sent to Andy's iPhone.")
    }

    @Test
    func testRemovingADeviceRevokesItAndDropsTheRow() async throws {
        let phone = UUID()
        let recorder = RequestRecorder()
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
            recorder.append(request)
            if request.httpMethod == "POST" {
                return (200, Self.device(phone, name: "Andy's iPhone", install: "install-phone", status: "revoked"))
            }
            return (200, #"{"items":[\#(Self.device(phone, name: "Andy's iPhone", install: "install-phone"))],"next_cursor":null}"#)
        }
        await model.load()

        await model.remove(try #require(model.devices.first))

        #expect(recorder.snapshot.last?.url?.path == "/v1/devices/\(phone.uuidString)/revoke")
        #expect(model.devices.isEmpty)
        #expect(toasts.current?.text == "Removed Andy's iPhone. It gets no more notifications.")
    }

    @Test
    func testAServerWithoutTheDeviceRoutesReadsAsUnavailable() async throws {
        let model = try makeModel { _ in
            (404, #"{"error":{"code":"not_found","message":"no route","details":{},"request_id":"r"}}"#)
        }

        await model.load()

        #expect(model.unavailable)
        #expect(model.errorMessage == nil)
        #expect(model.devices.isEmpty)
    }

    @Test
    func testAFailedTestKeepsItsErrorInlineAndRaisesNoToast() async throws {
        let phone = UUID()
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
            if request.httpMethod == "POST" {
                return (409, #"{"error":{"code":"conflict","message":"This device has no push token.","details":{},"request_id":"r"}}"#)
            }
            return (200, #"{"items":[\#(Self.device(phone, name: "Andy's iPhone", install: "install-phone"))],"next_cursor":null}"#)
        }
        await model.load()

        await model.sendTest(to: try #require(model.devices.first))

        #expect(model.errorMessage != nil)
        #expect(toasts.current == nil)
    }

    private func makeModel(
        toasts: ToastCenter? = nil,
        handler: @escaping (URLRequest) throws -> (Int, String)
    ) throws -> NotificationSettingsViewModel {
        let configuration = URLSessionConfiguration.ephemeral
        let id = NotificationSettingsTestURLProtocol.register(handler)
        configuration.protocolClasses = [NotificationSettingsTestURLProtocol.self]
        configuration.httpAdditionalHeaders = [NotificationSettingsTestURLProtocol.header: id]
        let api = VeetbotAPIClient(transport: HTTPTransport(
            configuration: try ConnectionConfiguration(baseURLString: "https://devices.test"),
            tokenStore: InMemoryTokenStore(token: "test-token"),
            session: URLSession(configuration: configuration)
        ))
        let installation = thisInstallation
        return NotificationSettingsViewModel(
            makeAPIClient: { api },
            installationID: { installation },
            toasts: toasts ?? ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        )
    }

    private static func device(_ id: UUID, name: String, install: String, status: String = "active") -> String {
        #"{"id":"\#(id)","client_device_id":"\#(install)","name":"\#(name)","kind":"mobile","platform":"ios","app_bundle_id":"com.veetbot.apple","push_provider":"apns","push_environment":"production","push_token_fingerprint":"abc","push_token_updated_at":"2026-10-19T10:00:00Z","push_token_invalidated_at":null,"muted_kinds":[],"capabilities":[],"status":"\#(status)","revoked_at":null,"last_seen_at":"2026-10-19T12:00:00Z","created_at":"2026-10-01T12:00:00Z","updated_at":"2026-10-19T12:00:00Z"}"#
    }
}

private final class RequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [URLRequest] = []
    func append(_ value: URLRequest) { lock.withLock { values.append(value) } }
    var snapshot: [URLRequest] { lock.withLock { values } }
}

private final class NotificationSettingsTestURLProtocol: URLProtocol {
    static let header = "X-Notification-Settings-Test"
    private static let lock = NSLock()
    nonisolated(unsafe) private static var handlers: [String: (URLRequest) throws -> (Int, String)] = [:]

    static func register(_ handler: @escaping (URLRequest) throws -> (Int, String)) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }

    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let id = request.value(forHTTPHeaderField: Self.header),
              let handler = Self.lock.withLock({ Self.handlers[id] }) else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }
        do {
            let (status, body) = try handler(request)
            let response = HTTPURLResponse(
                url: request.url!, statusCode: status, httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"]
            )!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
