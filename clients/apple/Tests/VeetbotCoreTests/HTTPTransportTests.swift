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
    func testConnectionRejectsEveryDocumentedUnsafeBaseURLShape() {
        let cases: [(String, ConnectionConfigurationError)] = [
            ("https://user:password@host.example", .credentialsNotAllowed),
            ("https://host.example?token=secret", .baseURLMustNotContainQueryOrFragment),
            ("https://host.example#credentials", .baseURLMustNotContainQueryOrFragment),
        ]

        for (value, expected) in cases {
            do {
                _ = try ConnectionConfiguration(baseURLString: value)
                Issue.record("expected \(value) to be rejected")
            } catch let error as ConnectionConfigurationError {
                #expect(error == expected)
            } catch {
                Issue.record("unexpected error for \(value): \(error)")
            }
        }
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
    func testWebsiteAccessUsesProfileIdentifiersAndDirectAuthenticationCeremonies() async throws {
        defer { StubURLProtocol.handler = nil }
        let profileID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000a1")
        )
        let authenticationID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-0000000000a2")
        )
        let lock = NSLock()
        var requests: [URLRequest] = []
        StubURLProtocol.handler = { request in
            lock.withLock { requests.append(request) }
            let path = request.url?.path ?? ""
            let body: String
            let statusCode: Int
            switch (request.httpMethod, path) {
            case ("POST", "/v1/browser-profiles"):
                statusCode = 201
                body = #"{"id":"\#(profileID.uuidString)","allowed_origins":["https://example.org"],"status":"authentication_required","generation":1,"created_at":"2026-08-22T12:00:00Z","updated_at":"2026-08-22T12:00:00Z","last_used_at":null}"#
            case ("POST", "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies"):
                statusCode = 201
                body = #"{"id":"\#(authenticationID.uuidString)","profile_id":"\#(profileID.uuidString)","status":"authentication_required","expires_at":"2026-08-22T12:05:00Z","launch_url":"https://browser.example/authentication/\#(authenticationID.uuidString)#capability=opaque"}"#
            case ("POST", "/v1/sessions"):
                statusCode = 201
                body = #"{"id":"00000000-0000-0000-0000-0000000000a3","status":"ACTIVE","agent_id":"general","agent_version":"1","title":null,"metadata":{"browser_profile_id":"\#(profileID.uuidString)"},"created_at":"2026-08-22T12:00:00Z","updated_at":"2026-08-22T12:00:00Z","active_run_id":null,"last_run_id":null}"#
            default:
                Issue.record("unexpected request: \(request.httpMethod ?? "") \(path)")
                statusCode = 500
                body = "{}"
            }
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: statusCode,
                    httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"]
                )
            )
            return (response, Data(body.utf8))
        }

        let client = try makeClient(token: "valid")
        let profile = try await client.createBrowserProfile(
            allowedOrigins: ["https://example.org"],
            idempotencyKey: "profile-create"
        )
        let ceremony = try await client.beginBrowserAuthentication(
            profileID: profile.id,
            loginURL: "https://example.org/login"
        )
        _ = try await client.createSession(browserProfileID: profile.id)

        #expect(ceremony.launchURL?.fragment == "capability=opaque")
        let captured = lock.withLock { requests }
        #expect(captured.count == 3)
        #expect(
            captured[0].value(forHTTPHeaderField: "Idempotency-Key") == "profile-create"
        )
        let profileJSON = try requestJSONObject(captured[0])
        #expect(profileJSON["allowed_origins"] as? [String] == ["https://example.org"])
        let ceremonyJSON = try requestJSONObject(captured[1])
        #expect(ceremonyJSON["login_url"] as? String == "https://example.org/login")
        let sessionJSON = try requestJSONObject(captured[2])
        #expect(sessionJSON["browser_profile_id"] as? String == profileID.uuidString)
        #expect(sessionJSON["username"] == nil)
        #expect(sessionJSON["password"] == nil)
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
        let requestIDs = captured.compactMap {
            $0.value(forHTTPHeaderField: "X-Request-Id")
        }
        #expect(requestIDs.count == captured.count)
        #expect(
            requestIDs.allSatisfy {
                !$0.isEmpty && UUID(uuidString: $0) != nil
            })
        #expect(Set(requestIDs).count == 1)
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
    func testMissingBearerTokenFailsBeforeAnyNetworkRequest() async throws {
        defer { StubURLProtocol.handler = nil }
        let lock = NSLock()
        var requestCount = 0
        StubURLProtocol.handler = { request in
            lock.withLock { requestCount += 1 }
            throw URLError(.badServerResponse)
        }
        let client = try makeClient(token: " \n ")

        do {
            _ = try await client.getSession(UUID())
            Issue.record("expected a missing-token failure")
        } catch HTTPTransportError.missingToken {
            // Expected: authentication fails closed before URLSession receives a request.
        } catch {
            Issue.record("unexpected error: \(error)")
        }

        #expect(lock.withLock { requestCount } == 0)
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

    @Test
    func testDeleteSessionUsesTheAuthoritativeDeleteRoute() async throws {
        defer { StubURLProtocol.handler = nil }
        let lock = NSLock()
        var captured: URLRequest?
        StubURLProtocol.handler = { request in
            lock.withLock { captured = request }
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!, statusCode: 204, httpVersion: nil, headerFields: nil
                )
            )
            return (response, Data())
        }
        let client = try makeClient(token: "valid")
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000001")
        )

        try await client.deleteSession(sessionID)

        let request = try #require(lock.withLock { captured })
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path == "/v1/sessions/\(sessionID.uuidString)")
        #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer valid")
    }

    @Test
    func testHistoryRoutesExplainAnOutdatedServer() async throws {
        defer { StubURLProtocol.handler = nil }
        StubURLProtocol.handler = { request in
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!, statusCode: 400, httpVersion: nil, headerFields: nil
                )
            )
            return (
                response,
                Data(
                    #"{"error":{"code":"malformed_request","message":"The HTTP request is not supported.","details":{},"request_id":"old-server"}}"#
                        .utf8)
            )
        }
        let client = try makeClient(token: "valid")

        do {
            _ = try await client.listSessions()
            Issue.record("expected the session index to require a server upgrade")
        } catch let error as VeetbotAPIClientError {
            guard case .serverUpgradeRequired = error else {
                Issue.record("unexpected compatibility error: \(error)")
                return
            }
            #expect(error.errorDescription?.contains("Update the server") == true)
        } catch {
            Issue.record("unexpected error: \(error)")
        }

        do {
            try await client.deleteSession(UUID())
            Issue.record("expected deletion to require a server upgrade")
        } catch let error as VeetbotAPIClientError {
            guard case .serverUpgradeRequired = error else {
                Issue.record("unexpected compatibility error: \(error)")
                return
            }
        } catch {
            Issue.record("unexpected error: \(error)")
        }

        do {
            _ = try await client.listSessionMessages(sessionID: UUID())
            Issue.record("expected transcript history to require a server upgrade")
        } catch let error as VeetbotAPIClientError {
            guard case .serverUpgradeRequired = error else {
                Issue.record("unexpected compatibility error: \(error)")
                return
            }
        } catch {
            Issue.record("unexpected error: \(error)")
        }
    }

    @Test
    func testTypedClientImplementsEveryDocumentedRequestContract() async throws {
        defer { StubURLProtocol.handler = nil }
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000001")
        )
        let runID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000002")
        )
        let approvalID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000003")
        )
        let artifactID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000004")
        )
        let questionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000005")
        )
        let lock = NSLock()
        var requests: [URLRequest] = []
        let sessionBody = """
            {"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"research","agent_version":"7","title":null,"metadata":{"source":"mobile"},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:01:00Z","active_run_id":"\(runID.uuidString)","last_run_id":"\(runID.uuidString)"}
            """
        let runBody = """
            {"id":"\(runID.uuidString)","session_id":"\(sessionID.uuidString)","parent_run_id":null,"status":"RUNNING","step_count":1,"model_call_count":1,"tool_call_count":0,"usage":{"input_tokens":10,"output_tokens":2,"cost_usd":"0.01"},"limits":{"max_steps":40,"deadline_at":null,"max_cost_usd":"1.00"},"failure":null,"cancel_requested_at":null,"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:01:00Z"}
            """
        let approvalBody = """
            {"id":"\(approvalID.uuidString)","run_id":"\(runID.uuidString)","session_id":"\(sessionID.uuidString)","status":"PENDING","tool_name":"shell.exec","action_summary":"Run command","arguments":{"command":"pwd"},"risk":"HIGH","policy_reason":"Side effect","expires_at":null,"created_at":"2026-08-14T00:00:00Z","resolved_at":null,"resolved_by":null,"decision":null}
            """
        let artifactBody = """
            {"id":"\(artifactID.uuidString)","session_id":"\(sessionID.uuidString)","run_id":"\(runID.uuidString)","name":"report.txt","media_type":"text/plain","sha256":"abc123","size_bytes":7,"metadata":{},"created_at":"2026-08-14T00:00:00Z"}
            """
        StubURLProtocol.handler = { request in
            lock.withLock { requests.append(request) }
            let path = try #require(request.url?.path)
            let method = try #require(request.httpMethod)
            let statusCode: Int
            let body: String
            let headers: [String: String]
            switch (method, path) {
            case ("POST", "/v1/sessions"):
                statusCode = 201
                body = sessionBody
                headers = ["Content-Type": "application/json"]
            case ("GET", "/v1/sessions/\(sessionID.uuidString)"):
                statusCode = 200
                body = sessionBody
                headers = ["Content-Type": "application/json"]
            case ("GET", "/v1/sessions"):
                statusCode = 200
                body = "{\"items\":[\(sessionBody)],\"next_cursor\":\"next-session\"}"
                headers = ["Content-Type": "application/json"]
            case ("GET", "/v1/sessions/\(sessionID.uuidString)/messages"):
                statusCode = 200
                body = "{\"items\":[{\"sequence\":2,\"role\":\"user\",\"content\":[{\"type\":\"text\",\"text\":\"hello\"}]}],\"next_cursor\":\"next-message\"}"
                headers = ["Content-Type": "application/json"]
            case ("POST", "/v1/sessions/\(sessionID.uuidString)/messages"):
                statusCode = 202
                body = "{\"run_id\":\"\(runID.uuidString)\",\"status\":\"QUEUED\"}"
                headers = ["Content-Type": "application/json"]
            case ("GET", "/v1/runs/\(runID.uuidString)"),
                ("POST", "/v1/runs/\(runID.uuidString)/cancel"):
                statusCode = 200
                body = runBody
                headers = ["Content-Type": "application/json"]
            case ("POST", "/v1/runs/\(runID.uuidString)/input"):
                statusCode = 202
                body = "{\"run_id\":\"\(runID.uuidString)\",\"status\":\"QUEUED\"}"
                headers = ["Content-Type": "application/json"]
            case ("GET", "/v1/approvals"):
                statusCode = 200
                body = "{\"items\":[\(approvalBody)],\"next_cursor\":\"next-approval\"}"
                headers = ["Content-Type": "application/json"]
            case ("GET", "/v1/approvals/\(approvalID.uuidString)"),
                ("POST", "/v1/approvals/\(approvalID.uuidString)/resolve"):
                statusCode = 200
                body = approvalBody
                headers = ["Content-Type": "application/json"]
            case ("GET", "/v1/artifacts/\(artifactID.uuidString)"):
                statusCode = 200
                body = artifactBody
                headers = ["Content-Type": "application/json"]
            case ("GET", "/v1/artifacts/\(artifactID.uuidString)/content"):
                statusCode = 200
                body = "payload"
                headers = ["Content-Type": "text/plain", "ETag": "abc123"]
            default:
                Issue.record("unexpected request: \(method) \(path)")
                statusCode = 500
                body = ""
                headers = [:]
            }
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!,
                    statusCode: statusCode,
                    httpVersion: nil,
                    headerFields: headers
                )
            )
            return (response, Data(body.utf8))
        }

        let client = try makeClient(token: "valid")
        #expect(
            try await client.createSession(
                agentID: "research", metadata: ["source": .string("mobile")]
            ).id == sessionID
        )
        #expect(try await client.getSession(sessionID).activeRunID == runID)
        #expect(try await client.listSessions(limit: 999, cursor: "session cursor").items.count == 1)
        #expect(
            try await client.listSessionMessages(
                sessionID: sessionID,
                limit: 999,
                cursor: "message cursor"
            ).items.first?.content == [.text("hello")]
        )
        #expect(
            try await client.submitMessage(
                sessionID: sessionID,
                content: [.text("hello")],
                idempotencyKey: "stable-key"
            ).runID == runID
        )
        #expect(try await client.getRun(runID).status == .running)
        #expect(
            try await client.deliverInput(
                runID: runID,
                content: [.text("EU")],
                questionID: questionID
            ).status == .queued
        )
        #expect(try await client.cancelRun(runID).id == runID)
        #expect(
            try await client.listPendingApprovals(
                runID: runID,
                sessionID: sessionID,
                limit: 0,
                cursor: "approval cursor"
            ).items.first?.id == approvalID
        )
        #expect(try await client.getApproval(approvalID).toolName == "shell.exec")
        #expect(
            try await client.resolveApproval(
                approvalID,
                decision: .deny,
                reason: "Not now"
            ).id == approvalID
        )
        #expect(try await client.getArtifact(artifactID).name == "report.txt")
        let content = try await client.getArtifactContent(artifactID, etag: "cached-tag")
        guard case .content(let data, let etag) = content else {
            Issue.record("expected artifact bytes")
            return
        }
        #expect(String(decoding: data, as: UTF8.self) == "payload")
        #expect(etag == "abc123")

        let captured = lock.withLock { requests }
        #expect(captured.count == 13)
        let create = try #require(
            captured.first {
                $0.httpMethod == "POST" && $0.url?.path == "/v1/sessions"
            }
        )
        let createJSON = try requestJSONObject(create)
        #expect(createJSON["agent_id"] as? String == "research")
        #expect((createJSON["metadata"] as? [String: String])?["source"] == "mobile")

        let sessionList = try #require(
            captured.first {
                $0.httpMethod == "GET" && $0.url?.path == "/v1/sessions"
            }
        )
        let sessionQuery = try #require(
            URLComponents(url: sessionList.url!, resolvingAgainstBaseURL: false)?.queryItems
        )
        #expect(sessionQuery.contains(URLQueryItem(name: "limit", value: "200")))
        #expect(sessionQuery.contains(URLQueryItem(name: "cursor", value: "session cursor")))
        #expect(!sessionQuery.contains { $0.name == "tenant_id" || $0.name == "principal_id" })

        let transcript = try #require(
            captured.first {
                $0.httpMethod == "GET" && $0.url?.path.hasSuffix("/messages") == true
            }
        )
        let transcriptQuery = try #require(
            URLComponents(url: transcript.url!, resolvingAgainstBaseURL: false)?.queryItems
        )
        #expect(transcriptQuery.contains(URLQueryItem(name: "limit", value: "200")))
        #expect(transcriptQuery.contains(URLQueryItem(name: "cursor", value: "message cursor")))

        let message = try #require(
            captured.first {
                $0.httpMethod == "POST" && $0.url?.path.hasSuffix("/messages") == true
            }
        )
        #expect(message.value(forHTTPHeaderField: "Idempotency-Key") == "stable-key")
        #expect(
            ((try requestJSONObject(message)["content"] as? [[String: String]])?.first)?["text"]
                == "hello"
        )

        let input = try #require(
            captured.first { $0.url?.path.hasSuffix("/input") == true }
        )
        let inputJSON = try requestJSONObject(input)
        #expect(inputJSON["question_id"] as? String == questionID.uuidString)
        #expect(input.value(forHTTPHeaderField: "Idempotency-Key") == nil)

        let approvals = try #require(
            captured.first { $0.url?.path == "/v1/approvals" }
        )
        let approvalQuery = try #require(
            URLComponents(url: approvals.url!, resolvingAgainstBaseURL: false)?.queryItems
        )
        #expect(approvalQuery.contains(URLQueryItem(name: "status", value: "pending")))
        #expect(approvalQuery.contains(URLQueryItem(name: "limit", value: "1")))
        #expect(approvalQuery.contains(URLQueryItem(name: "run_id", value: runID.uuidString)))
        #expect(
            approvalQuery.contains(URLQueryItem(name: "session_id", value: sessionID.uuidString))
        )
        #expect(approvalQuery.contains(URLQueryItem(name: "cursor", value: "approval cursor")))

        let resolve = try #require(
            captured.first { $0.url?.path.hasSuffix("/resolve") == true }
        )
        let resolveJSON = try requestJSONObject(resolve)
        #expect(resolveJSON["decision"] as? String == "deny")
        #expect(resolveJSON["reason"] as? String == "Not now")

        let artifactContent = try #require(
            captured.first { $0.url?.path.hasSuffix("/content") == true }
        )
        #expect(artifactContent.value(forHTTPHeaderField: "If-None-Match") == "cached-tag")
    }

    @Test
    func testArtifactConditionalRequestAcceptsNotModifiedWithoutDecoding() async throws {
        defer { StubURLProtocol.handler = nil }
        StubURLProtocol.handler = { request in
            let response = try #require(
                HTTPURLResponse(
                    url: request.url!, statusCode: 304, httpVersion: nil, headerFields: nil
                )
            )
            return (response, Data())
        }
        let client = try makeClient(token: "valid")

        let result = try await client.getArtifactContent(UUID(), etag: "known")

        guard case .notModified = result else {
            Issue.record("expected a typed not-modified result")
            return
        }
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
