import Foundation
import Security
import Testing
@testable import VeetbotCore

@Suite(.serialized) struct HTTPTransportTests {
    @Test
    func testHTTPSIsRequired() throws {
        do {
            _ = try ConnectionConfiguration(baseURLString: "http://host:8000")
            Issue.record("expected plaintext URL to be rejected")
        } catch let error as ConnectionConfigurationError {
            #expect(error == .httpsRequired)
        } catch {
            Issue.record("unexpected error: \(error)")
        }
        _ = try ConnectionConfiguration(baseURLString: "https://host:8000")
    }

    @Test
    func testInMemoryTokenStoreMatchesKeychainNormalization() async throws {
        let store = InMemoryTokenStore()
        await store.saveToken("  secret\n")
        #expect(await store.readToken() == "secret")
        await store.saveToken(" \n ")
        #expect(await store.readToken() == nil)
    }

    @Test
    func testKeychainStoreUsesLocalDataProtectionKeychain() {
        let query = KeychainTokenStore.makeBaseQuery(
            service: "com.veetbot.test",
            account: "test"
        )

        #expect(query[kSecUseDataProtectionKeychain as String] as? Bool == true)
        #expect(query[kSecAttrSynchronizable as String] as? Bool == false)
    }

    @Test
    func testMissingKeychainEntitlementExplainsSigningFix() {
        let error = KeychainTokenStoreError.operationFailed(errSecMissingEntitlement)

        #expect(error.errorDescription?.contains("Signing & Capabilities") == true)
        #expect(error.errorDescription?.contains("choose your team") == true)
    }

    @Test
    func testSubmitAddsSecurityHeadersAndReusesIdempotencyKeyOnRetry() async throws {
        defer { StubURLProtocol.handler = nil }
        let lock = NSLock()
        var requests: [URLRequest] = []
        StubURLProtocol.handler = { request in
            let count = lock.withLock {
                requests.append(request)
                return requests.count
            }
            if count == 1 { throw URLError(.networkConnectionLost) }
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 202,
                    httpVersion: "HTTP/1.1",
                    headerFields: ["Content-Type": "application/json"]
                )
            )
            return (
                response,
                Data(#"{"run_id":"00000000-0000-0000-0000-000000000002","status":"QUEUED"}"#.utf8)
            )
        }

        let client = try makeClient(token: "secret")
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000001"))
        let result = try await client.submitMessage(
            sessionID: sessionID,
            content: [.text("hello")],
            idempotencyKey: "stable-key"
        )
        #expect(result.status == .queued)

        let captured = lock.withLock { requests }
        #expect(captured.count == 2)
        #expect(captured.map { $0.value(forHTTPHeaderField: "Idempotency-Key") } == ["stable-key", "stable-key"])
        #expect(captured.last?.value(forHTTPHeaderField: "Authorization") == "Bearer secret")
        #expect(captured.last?.value(forHTTPHeaderField: "Content-Type") == "application/json")
        #expect(captured.last?.value(forHTTPHeaderField: "X-Request-Id") != nil)
    }

    @Test
    func test401TransitionsToReauthenticationWithTypedError() async throws {
        defer { StubURLProtocol.handler = nil }
        StubURLProtocol.handler = { request in
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: 401,
                    httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"]
                )
            )
            return (
                response,
                Data(#"{"error":{"code":"authentication_error","message":"expired","details":{},"request_id":"req-401"}}"#.utf8)
            )
        }
        let client = try makeClient(token: "expired")
        do {
            _ = try await client.getSession(UUID())
            Issue.record("expected authentication failure")
        } catch let HTTPTransportError.reauthenticationRequired(error) {
            #expect(error.code == .authenticationError)
            #expect(error.requestID == "req-401")
        } catch {
            Issue.record("unexpected error: \(error)")
        }
        let state = await client.transport.authorizationState()
        #expect(state == .requiresReauthentication)
    }

    @Test
    func test403RemainsAuthorizationDenied() async throws {
        defer { StubURLProtocol.handler = nil }
        StubURLProtocol.handler = { request in
            let response = try #require(
                HTTPURLResponse(url: request.url!, statusCode: 403, httpVersion: nil, headerFields: nil)
            )
            return (
                response,
                Data(#"{"error":{"code":"authorization_error","message":"missing scope","details":{},"request_id":"req-403"}}"#.utf8)
            )
        }
        let client = try makeClient(token: "valid")
        do {
            _ = try await client.getRun(UUID())
            Issue.record("expected authorization failure")
        } catch let HTTPTransportError.authorizationDenied(error) {
            #expect(error.code == .authorizationError)
        } catch {
            Issue.record("unexpected error: \(error)")
        }
    }

    private func makeClient(token: String) throws -> VeetbotAPIClient {
        let configuration = try ConnectionConfiguration(baseURLString: "https://veetbot.test")
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.protocolClasses = [StubURLProtocol.self]
        let session = URLSession(configuration: sessionConfiguration)
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: token),
            session: session
        )
        return VeetbotAPIClient(transport: transport)
    }
}

private final class StubURLProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (HTTPURLResponse, Data))?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let handler = Self.handler else {
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
