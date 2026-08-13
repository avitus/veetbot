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
    func testRoutePathCharactersArePercentEncoded() throws {
        let configuration = try ConnectionConfiguration(
            baseURLString: "https://host.example/base%20path"
        )
        let url = try configuration.url(path: "/v1/artifacts/a value")

        #expect(url.absoluteString == "https://host.example/base%20path/v1/artifacts/a%20value")
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
        #expect(
            captured.map { $0.value(forHTTPHeaderField: "Idempotency-Key") } == [
                "stable-key", "stable-key",
            ])
        #expect(
            captured.allSatisfy {
                $0.value(forHTTPHeaderField: "Authorization") == "Bearer secret"
            })
        #expect(
            captured.allSatisfy {
                $0.value(forHTTPHeaderField: "Content-Type") == "application/json"
            })
        #expect(
            captured.allSatisfy {
                $0.value(forHTTPHeaderField: "X-Request-Id") != nil
            })
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
                Data(
                    #"{"error":{"code":"authentication_error","message":"expired","details":{},"request_id":"req-401"}}"#
                        .utf8)
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
        #expect(await client.transport.authorizationState() == .requiresReauthentication)
    }

    @Test
    func testRetryableHTTPStatusUsesStableIdempotencyKey() async throws {
        defer { StubURLProtocol.handler = nil }
        let lock = NSLock()
        var requests: [URLRequest] = []
        StubURLProtocol.handler = { request in
            let count = lock.withLock {
                requests.append(request)
                return requests.count
            }
            let status = count == 1 ? 429 : 202
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: status,
                    httpVersion: "HTTP/1.1",
                    headerFields: count == 1
                        ? ["Retry-After": "0", "Content-Type": "application/json"]
                        : ["Content-Type": "application/json"]
                )
            )
            let data =
                count == 1
                ? Data(#"{"error":{"code":"rate_limited","message":"slow down"}}"#.utf8)
                : Data(#"{"run_id":"00000000-0000-0000-0000-000000000002","status":"QUEUED"}"#.utf8)
            return (response, data)
        }
        let client = try makeClient(token: "valid")
        _ = try await client.submitMessage(
            sessionID: UUID(),
            content: [.text("hello")],
            idempotencyKey: "retry-key"
        )

        let captured = lock.withLock { requests }
        #expect(captured.count == 2)
        #expect(
            captured.allSatisfy {
                $0.value(forHTTPHeaderField: "Idempotency-Key") == "retry-key"
            })
        #expect(await client.transport.authorizationState() == .authenticated)
    }

    @Test(arguments: [
        "Sun, 06 Nov 1994 08:49:37 GMT",
        "Sunday, 06-Nov-94 08:49:37 GMT",
        "Sun Nov  6 08:49:37 1994",
    ])
    func testRetryAfterAcceptsEveryHTTPDateFormat(value: String) throws {
        let response = try #require(
            HTTPURLResponse(
                url: URL(string: "https://veetbot.test")!,
                statusCode: 503,
                httpVersion: "HTTP/1.1",
                headerFields: ["Retry-After": value]
            )
        )
        let now = try #require(
            ISO8601DateFormatter().date(from: "1994-11-06T08:49:27Z")
        )

        #expect(
            HTTPTransport.retryDelayNanoseconds(
                response: response,
                attempt: 1,
                now: now
            ) == 10_000_000_000
        )
    }

    @Test
    func test403RemainsAuthorizationDenied() async throws {
        defer { StubURLProtocol.handler = nil }
        StubURLProtocol.handler = { request in
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!, statusCode: 403, httpVersion: nil, headerFields: nil)
            )
            return (
                response,
                Data(
                    #"{"error":{"code":"authorization_error","message":"missing scope","details":{},"request_id":"req-403"}}"#
                        .utf8)
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
        #expect(await client.transport.authorizationState() == .authenticated)
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

    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

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
