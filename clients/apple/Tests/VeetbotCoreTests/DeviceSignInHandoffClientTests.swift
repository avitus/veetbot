import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0128, 0128-design §2.3, §2.5 items 5 and 6: the handoff request and
/// how each answer maps to an outcome.
@Suite(.serialized) struct DeviceSignInHandoffClientTests {
    private static let ceremonyID = "6f1c0000-0000-4000-8000-00000000c001"
    private static let capability = "Qk9HVVMtY2FwYWJpbGl0eS1mb3ItdGVzdHMtb25seQ"
    private static let launch =
        "https://browser.example/authentication/\(ceremonyID)/handoff#capability=\(capability)"

    @Test
    func theTargetIsTheLaunchURLWithoutItsFragment() throws {
        let target = try #require(DeviceSignInHandoffTarget(launchURL: URL(string: Self.launch)))
        #expect(
            target.endpoint.absoluteString
                == "https://browser.example/authentication/\(Self.ceremonyID)/handoff"
        )
        #expect(target.capability == Self.capability)
        #expect(!String(describing: target).contains(Self.capability))
        #expect(!String(reflecting: target).contains(Self.capability))
    }

    @Test(arguments: [
        "http://browser.example/authentication/6f1c0000-0000-4000-8000-00000000c001/handoff#capability=Qk9HVVMtY2FwYWJpbGl0eS1mb3ItdGVzdHMtb25seQ",
        "https://browser.example/authentication/6f1c0000-0000-4000-8000-00000000c001/frame#capability=Qk9HVVMtY2FwYWJpbGl0eS1mb3ItdGVzdHMtb25seQ",
        "https://browser.example/authentication/not-a-ceremony/handoff#capability=Qk9HVVMtY2FwYWJpbGl0eS1mb3ItdGVzdHMtb25seQ",
        "https://browser.example/authentication/6f1c0000-0000-4000-8000-00000000c001/handoff",
        "https://browser.example/authentication/6f1c0000-0000-4000-8000-00000000c001/handoff#capability=short",
        "https://browser.example/authentication/6f1c0000-0000-4000-8000-00000000c001/handoff#capability=Qk9HVVMtY2FwYWJpbGl0eS1mb3ItdGVzdHMtb25seQ&x=1",
        "https://browser.example/authentication/6f1c0000-0000-4000-8000-00000000c001/handoff#token=Qk9HVVMtY2FwYWJpbGl0eS1mb3ItdGVzdHMtb25seQ",
        "https://browser.example/authentication/6f1c0000-0000-4000-8000-00000000c001/handoff#capability=Qk9HVVMt+2FwYWJpbGl0eS1mb3ItdGVzdHMtb25seQ",
        "https://owner@browser.example/authentication/6f1c0000-0000-4000-8000-00000000c001/handoff#capability=Qk9HVVMtY2FwYWJpbGl0eS1mb3ItdGVzdHMtb25seQ",
        "https://browser.example/authentication/6f1c0000-0000-4000-8000-00000000c001/handoff?next=x#capability=Qk9HVVMtY2FwYWJpbGl0eS1mb3ItdGVzdHMtb25seQ",
    ])
    func aMalformedLaunchURLIsAFailedSetup(launch: String) {
        #expect(DeviceSignInHandoffTarget(launchURL: URL(string: launch)) == nil)
        #expect(DeviceSignInHandoffTarget(launchURL: nil) == nil)
    }

    @Test
    func theRequestCarriesOnlyTheCapabilityAndTheBody() async throws {
        let recorder = HandoffRequestRecorder()
        let client = makeClient { request in
            recorder.record(request)
            return (200, #"{"status":"ready"}"#)
        }
        let target = try #require(DeviceSignInHandoffTarget(launchURL: URL(string: Self.launch)))
        let body = Data(#"{"confirmed_url":"https://www.example.org/learn","cookies":[],"origins":[]}"#.utf8)

        let outcome = await client.send(body, to: target)

        #expect(outcome == .sealed)
        let request = try #require(recorder.requests.first)
        #expect(recorder.requests.count == 1)
        #expect(request.httpMethod == "POST")
        #expect(request.url == target.endpoint)
        #expect(request.url?.fragment == nil)
        #expect(request.value(forHTTPHeaderField: DeviceSignInHandoffClient.capabilityHeader) == Self.capability)
        #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
        #expect(request.value(forHTTPHeaderField: "Authorization") == nil)
        #expect(request.value(forHTTPHeaderField: "Cookie") == nil)
        #expect(recorder.bodies.first == body)
    }

    @Test(arguments: [
        (200, #"{"status":"ready"}"#, DeviceHandoffOutcome.sealed),
        (401, #"{"error":{"code":"unauthorized","message":"Unauthorized."}}"#, .capabilityRejected),
        (413, #"{"error":{"code":"payload_too_large","message":"Too large."}}"#, .tooLarge),
        (422, #"{"error":{"code":"session_empty","message":"Empty."}}"#, .rejected(code: "session_empty")),
        (422, #"{"error":{"code":"session_signed_out","message":"Signed out."}}"#,
         .rejected(code: "session_signed_out")),
        (422, #"{"error":{"code":"session_unconfirmed","message":"Unconfirmed."}}"#,
         .rejected(code: "session_unconfirmed")),
        (409, #"{"error":{"code":"tool.browser.provider_unavailable","message":"Unavailable."}}"#,
         .rejected(code: "tool.browser.provider_unavailable")),
        (400, #"{"error":{"code":"invalid_request","message":"Invalid."}}"#,
         .rejected(code: "invalid_request")),
        (500, "not json", .rejected(code: "http_500")),
        // An undocumented success leaves the outcome to the ceremony status.
        (200, "{}", .transportFailed),
    ])
    func eachAnswerMapsToAnOutcome(status: Int, body: String, expected: DeviceHandoffOutcome) async throws {
        let client = makeClient { _ in (status, body) }
        let target = try #require(DeviceSignInHandoffTarget(launchURL: URL(string: Self.launch)))
        #expect(await client.send(Data("{}".utf8), to: target) == expected)
    }

    @Test
    func noAnswerIsATransportFailure() async throws {
        let client = makeClient { _ in throw URLError(.networkConnectionLost) }
        let target = try #require(DeviceSignInHandoffTarget(launchURL: URL(string: Self.launch)))
        #expect(await client.send(Data("{}".utf8), to: target) == .transportFailed)
    }

    @Test
    func aRedirectIsNotFollowed() async throws {
        let recorder = HandoffRequestRecorder()
        HandoffStubURLProtocol.redirectTo = URL(string: "https://elsewhere.example/collect")
        defer { HandoffStubURLProtocol.redirectTo = nil }
        let client = makeClient { request in
            recorder.record(request)
            return (200, #"{"status":"ready"}"#)
        }
        let target = try #require(DeviceSignInHandoffTarget(launchURL: URL(string: Self.launch)))

        let outcome = await client.send(Data("{}".utf8), to: target)

        #expect(outcome != .sealed)
        #expect(recorder.requests.allSatisfy { $0.url?.host == "browser.example" })
    }

    @Test
    func theDefaultSessionKeepsNoCookiesCacheOrCredentials() {
        let configuration = DeviceSignInHandoffClient.makeSessionConfiguration()
        #expect(configuration.httpShouldSetCookies == false)
        #expect(configuration.httpCookieAcceptPolicy == .never)
        #expect(configuration.urlCache == nil)
        #expect(configuration.urlCredentialStorage == nil)
        #expect(configuration.timeoutIntervalForRequest == 75)
        #expect(DeviceSignInHandoffClient.makeDefaultSession().delegate is RejectRedirectsDelegate)
    }

    private func makeClient(
        handler: @escaping @Sendable (URLRequest) throws -> (Int, String)
    ) -> DeviceSignInHandoffClient {
        HandoffStubURLProtocol.handler = handler
        let configuration = DeviceSignInHandoffClient.makeSessionConfiguration()
        configuration.protocolClasses = [HandoffStubURLProtocol.self]
        return DeviceSignInHandoffClient(
            session: URLSession(
                configuration: configuration, delegate: RejectRedirectsDelegate(), delegateQueue: nil
            )
        )
    }
}

private final class HandoffRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [URLRequest] = []
    private var recordedBodies: [Data] = []

    var requests: [URLRequest] { lock.withLock { recorded } }
    var bodies: [Data] { lock.withLock { recordedBodies } }

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
        lock.withLock {
            recorded.append(request)
            recordedBodies.append(body ?? Data())
        }
    }
}

private final class HandoffStubURLProtocol: URLProtocol {
    nonisolated(unsafe) static var handler: (@Sendable (URLRequest) throws -> (Int, String))?
    nonisolated(unsafe) static var redirectTo: URL?

    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler, let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }
        if let redirect = Self.redirectTo, url.host != redirect.host,
            let response = HTTPURLResponse(
                url: url, statusCode: 307, httpVersion: nil, headerFields: ["Location": redirect.absoluteString]
            )
        {
            var next = request
            next.url = redirect
            client?.urlProtocol(self, wasRedirectedTo: next, redirectResponse: response)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data())
            client?.urlProtocolDidFinishLoading(self)
            return
        }
        do {
            let (status, body) = try handler(request)
            let response = HTTPURLResponse(
                url: url, statusCode: status, httpVersion: nil,
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
