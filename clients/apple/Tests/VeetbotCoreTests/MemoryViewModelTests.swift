import Foundation
import Testing

@testable import VeetbotCore

@Suite(.serialized) @MainActor struct MemoryViewModelTests {
    @Test
    func testReloadHappyPathRendersItemsAndDeclaresTheRestrictedCeiling() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000301"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000302"))
        let lock = NSLock()
        var requests: [URLRequest] = []
        let model = try makeModel { request in
            lock.withLock { requests.append(request) }
            return try self.response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID))],"next_cursor":null}
                    """
            )
        }

        await model.reload()

        #expect(model.items.map(\.id) == [memoryID])
        #expect(model.isLoading == false)
        #expect(model.unavailable == false)
        #expect(model.errorMessage == nil)

        let request = try #require(lock.withLock { requests.first })
        #expect(request.url?.path == "/v1/memories")
        let query = try #require(
            URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems
        )
        #expect(query.contains(URLQueryItem(name: "ceiling", value: "restricted")))
    }

    @Test
    func testLoadMoreAppendsWithoutDuplicatesAndStopsOnRepeatedCursor() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000401"))
        let secondID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000402"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000403"))
        let lock = NSLock()
        var requests: [URLRequest] = []
        let model = try makeModel { request in
            lock.withLock { requests.append(request) }
            let query =
                URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems
                ?? []
            let cursor = query.first(where: { $0.name == "cursor" })?.value
            switch cursor {
            case nil:
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: firstID, sessionID: sessionID))],"next_cursor":"page-2"}
                        """
                )
            case "page-2":
                // The second page repeats the first item (a keyset boundary
                // overlap) and echoes back the same cursor it was given.
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: firstID, sessionID: sessionID)),\(self.memoryJSON(id: secondID, sessionID: sessionID))],"next_cursor":"page-2"}
                        """
                )
            default:
                Issue.record("unexpected cursor \(cursor ?? "nil")")
                return try self.response(for: request, statusCode: 500, body: "")
            }
        }

        await model.reload()
        #expect(model.items.map(\.id) == [firstID])

        await model.loadMore()
        #expect(model.items.map(\.id) == [firstID, secondID], "the repeated first item must not be duplicated")
        #expect(lock.withLock { requests.count } == 2)
        let secondRequest = try #require(lock.withLock { requests.last })
        let secondQuery = try #require(
            URLComponents(url: secondRequest.url!, resolvingAgainstBaseURL: false)?.queryItems
        )
        #expect(secondQuery.contains(URLQueryItem(name: "ceiling", value: "restricted")))
        #expect(secondQuery.contains(URLQueryItem(name: "cursor", value: "page-2")))

        // The server echoed the cursor it was given, so a further loadMore
        // must not issue another request at all.
        await model.loadMore()
        #expect(model.items.map(\.id) == [firstID, secondID])
        #expect(lock.withLock { requests.count } == 2)
    }

    @Test
    func testLoadMoreFailureKeepsItemsThenClearsOnASubsequentSuccess() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000411"))
        let secondID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000412"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000413"))
        let lock = NSLock()
        var pageTwoAttempts = 0
        let model = try makeModel { request in
            let query =
                URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems
                ?? []
            let cursor = query.first(where: { $0.name == "cursor" })?.value
            switch cursor {
            case nil:
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: firstID, sessionID: sessionID))],"next_cursor":"page-2"}
                        """
                )
            case "page-2":
                let attempt = lock.withLock {
                    pageTwoAttempts += 1
                    return pageTwoAttempts
                }
                if attempt == 1 {
                    return try self.response(
                        for: request,
                        statusCode: 503,
                        body: #"{"error":{"code":"service_unavailable","message":"unavailable","details":{},"request_id":"loadmore-failed"}}"#
                    )
                }
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: secondID, sessionID: sessionID))],"next_cursor":null}
                        """
                )
            default:
                Issue.record("unexpected cursor \(cursor ?? "nil")")
                return try self.response(for: request, statusCode: 500, body: "")
            }
        }

        await model.reload()
        #expect(model.items.map(\.id) == [firstID])

        await model.loadMore()
        #expect(
            model.items.map(\.id) == [firstID],
            "a page-2 failure must not empty the already-loaded page"
        )
        #expect(model.errorMessage != nil)

        await model.loadMore()
        #expect(
            model.items.map(\.id) == [firstID, secondID],
            "a retry after the failure must still land on the same page"
        )
        #expect(
            model.errorMessage == nil,
            "a subsequent successful loadMore must clear the earlier failure"
        )
    }

    @Test
    func testUnavailableDuringLoadMoreKeepsAlreadyLoadedItems() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000421"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000422"))
        let model = try makeModel { request in
            let query =
                URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems
                ?? []
            let cursor = query.first(where: { $0.name == "cursor" })?.value
            if cursor == nil {
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: firstID, sessionID: sessionID))],"next_cursor":"page-2"}
                        """
                )
            }
            return try self.response(
                for: request,
                statusCode: 404,
                body: #"{"error":{"code":"not_found","message":"Not found.","details":{},"request_id":"old-server"}}"#
            )
        }

        await model.reload()
        #expect(model.items.map(\.id) == [firstID])
        #expect(model.unavailable == false)

        await model.loadMore()

        #expect(
            model.items.map(\.id) == [firstID],
            "a mid-scroll degradation must not clobber the populated list"
        )
        #expect(model.unavailable == true)
        #expect(model.errorMessage != nil)
    }

    @Test
    func testRetryAfterAReloadFailureReRunsReloadAndRecovers() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000431"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000432"))
        let lock = NSLock()
        var attempts = 0
        let model = try makeModel { request in
            let attempt = lock.withLock {
                attempts += 1
                return attempts
            }
            if attempt == 2 {
                return try self.response(
                    for: request,
                    statusCode: 503,
                    body: #"{"error":{"code":"service_unavailable","message":"unavailable","details":{},"request_id":"reload-failed"}}"#
                )
            }
            return try self.response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: firstID, sessionID: sessionID))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        #expect(model.items.map(\.id) == [firstID])

        // A second reload (as a filter or search-text change would trigger)
        // fails; the footer's Retry would previously call loadMore(), which
        // no-ops once reload() has already nulled the cursor, leaving it
        // permanently dead.
        await model.reload()
        #expect(model.errorMessage != nil)

        await model.retry()

        #expect(
            model.items.map(\.id) == [firstID],
            "retry() must re-run reload() (not the dead loadMore()) and recover"
        )
        #expect(model.errorMessage == nil)
    }

    @Test
    func testFilterChangeFailureDoesNotLeaveThePreviousFiltersRowsDisplayed() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000441"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000442"))
        let lock = NSLock()
        var attempts = 0
        let model = try makeModel { request in
            let attempt = lock.withLock {
                attempts += 1
                return attempts
            }
            if attempt == 1 {
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: firstID, sessionID: sessionID))],"next_cursor":null}
                        """
                )
            }
            return try self.response(
                for: request,
                statusCode: 503,
                body: #"{"error":{"code":"service_unavailable","message":"unavailable","details":{},"request_id":"filter-failed"}}"#
            )
        }

        await model.reload()
        #expect(model.items.map(\.id) == [firstID])

        model.setStatusFilter(.active)

        for _ in 0 ..< 200 where model.errorMessage == nil {
            try await Task.sleep(nanoseconds: 5_000_000)
        }

        #expect(
            model.items.isEmpty,
            "a filter-change reload failure must not leave the previous filter's rows displayed"
        )
        #expect(model.errorMessage != nil)
    }

    @Test
    func testRetryAfterALoadMoreFailureStillReRunsLoadMoreNotReload() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000451"))
        let secondID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000452"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000453"))
        let lock = NSLock()
        var pageTwoAttempts = 0
        var totalRequests = 0
        let model = try makeModel { request in
            lock.withLock { totalRequests += 1 }
            let query =
                URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems
                ?? []
            let cursor = query.first(where: { $0.name == "cursor" })?.value
            switch cursor {
            case nil:
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: firstID, sessionID: sessionID))],"next_cursor":"page-2"}
                        """
                )
            case "page-2":
                let attempt = lock.withLock {
                    pageTwoAttempts += 1
                    return pageTwoAttempts
                }
                if attempt == 1 {
                    return try self.response(
                        for: request,
                        statusCode: 503,
                        body: #"{"error":{"code":"service_unavailable","message":"unavailable","details":{},"request_id":"loadmore-failed"}}"#
                    )
                }
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: secondID, sessionID: sessionID))],"next_cursor":null}
                        """
                )
            default:
                Issue.record("unexpected cursor \(cursor ?? "nil")")
                return try self.response(for: request, statusCode: 500, body: "")
            }
        }

        await model.reload()
        #expect(model.items.map(\.id) == [firstID])

        await model.loadMore()
        #expect(model.items.map(\.id) == [firstID], "a page-2 failure must not empty the already-loaded page")
        #expect(model.errorMessage != nil)

        await model.retry()

        #expect(
            model.items.map(\.id) == [firstID, secondID],
            "retry() after a loadMore failure must still re-run loadMore(), not reload()"
        )
        #expect(model.errorMessage == nil)
        #expect(
            lock.withLock { totalRequests } == 3,
            "a correct retry re-fetches page two only, not page one again"
        )
    }

    @Test
    func testStaleGuardDropsAnOutOfOrderResponse() async throws {
        let staleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000501"))
        let freshID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000502"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000503"))
        let gate = OrderedRequestGate()
        let model = try makeModel { request in
            if gate.isFirstCall() {
                // Simulate a slow response for the reload that is about to be
                // abandoned; it is released only after the fresh reload below
                // has already completed.
                gate.waitForRelease()
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: staleID, sessionID: sessionID))],"next_cursor":null}
                        """
                )
            }
            return try self.response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: freshID, sessionID: sessionID))],"next_cursor":null}
                    """
            )
        }

        let staleTask = Task { await model.reload() }

        for _ in 0 ..< 2_000 where !gate.isBlocked {
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        #expect(gate.isBlocked, "the abandoned reload's request never reached the network layer")

        // This reload starts and completes before the stale one is released.
        await model.reload()
        #expect(model.items.map(\.id) == [freshID])

        gate.release()
        _ = await staleTask.value

        #expect(
            model.items.map(\.id) == [freshID],
            "a late response for an abandoned reload must not overwrite the fresh one"
        )
    }

    @Test
    func testListNotFoundSetsUnavailableWithoutAnErrorMessage() async throws {
        let model = try makeModel { request in
            try self.response(
                for: request,
                statusCode: 404,
                body: #"{"error":{"code":"not_found","message":"Not found.","details":{},"request_id":"old-server"}}"#
            )
        }

        await model.reload()

        #expect(model.unavailable == true)
        #expect(model.errorMessage == nil)
        #expect(model.items.isEmpty)
    }

    @Test
    func testSearchDebounceIssuesOneRequestForRapidKeystrokes() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000601"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000602"))
        let lock = NSLock()
        var requests: [URLRequest] = []
        let model = try makeModel { request in
            lock.withLock { requests.append(request) }
            return try self.response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        #expect(lock.withLock { requests.count } == 1)

        model.setSearchText("d")
        model.setSearchText("da")
        model.setSearchText("dar")
        model.setSearchText("dark")

        // Immediately after the rapid keystrokes nothing has fired yet; the
        // debounce window (~300ms) has not elapsed. This assertion depends on
        // real wall-clock timing rather than an injected clock, since no
        // clock-injection seam exists on this view model's siblings.
        #expect(lock.withLock { requests.count } == 1)

        // Poll rather than sleep a fixed duration: a loaded test machine can
        // stretch the debounce well past 300ms, and a fixed sleep just under
        // that margin is exactly the flake this guards against.
        for _ in 0 ..< 600 where lock.withLock({ requests.count }) < 2 {
            try await Task.sleep(nanoseconds: 10_000_000)
        }

        #expect(
            lock.withLock { requests.count } == 2,
            "four rapid keystrokes must coalesce into exactly one debounced request"
        )
        let lastRequest = try #require(lock.withLock { requests.last })
        let query = try #require(
            URLComponents(url: lastRequest.url!, resolvingAgainstBaseURL: false)?.queryItems
        )
        #expect(query.contains(URLQueryItem(name: "text", value: "dark")))
    }

    private func makeModel(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> MemoryViewModel {
        let configuration = try ConnectionConfiguration(baseURLString: "https://veetbot.test")
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        let handlerID = MemoryViewModelURLProtocol.register(handler)
        sessionConfiguration.httpAdditionalHeaders = [
            MemoryViewModelURLProtocol.handlerHeader: handlerID
        ]
        sessionConfiguration.protocolClasses = [MemoryViewModelURLProtocol.self]
        let session = URLSession(configuration: sessionConfiguration)
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: "valid"),
            session: session
        )
        let client = VeetbotAPIClient(transport: transport)
        return MemoryViewModel(makeAPIClient: { client })
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

    private func memoryJSON(id: UUID, sessionID: UUID) -> String {
        """
        {"id":"\(id.uuidString)","subject":"the user","statement":"The user prefers dark mode.","belief_type":"preference","status":"active","polarity":"assert","scope":"session","portability":"portable","authority":"user","sensitivity":"restricted","confidence":0.87,"corroboration_count":3,"flagged_for_review":false,"conflicts_with":[],"superseded_by":null,"source_session_id":"\(sessionID.uuidString)","source_event_ids":[10,11],"formation_run_id":"00000000-0000-0000-0000-000000000900","consolidation_policy_version":"formation@1","origin_scopes":["session"],"valid_from":"2026-08-01T00:00:00Z","valid_to":null,"expires_at":null,"last_reinforced_at":"2026-08-15T00:00:00Z","created_at":"2026-07-01T00:00:00Z","updated_at":"2026-08-20T00:00:00Z"}
        """
    }
}

private final class OrderedRequestGate: @unchecked Sendable {
    private let lock = NSLock()
    private var callCount = 0
    private var blocked = false
    private let semaphore = DispatchSemaphore(value: 0)

    func isFirstCall() -> Bool {
        lock.lock()
        callCount += 1
        let isFirst = callCount == 1
        lock.unlock()
        return isFirst
    }

    func waitForRelease() {
        lock.lock()
        blocked = true
        lock.unlock()
        semaphore.wait()
    }

    var isBlocked: Bool {
        lock.withLock { blocked }
    }

    func release() {
        semaphore.signal()
    }
}

private final class MemoryViewModelURLProtocol: URLProtocol {
    static let handlerHeader = "X-Veetbot-Memory-Test-Handler-ID"
    private static let handlerStore = MemoryViewModelURLProtocolHandlerStore()

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
        // Dispatched off the loading system's own queue: a handler that
        // blocks (the stale-guard test's slow response) must not prevent a
        // concurrently in-flight request on the same session from starting.
        let capturedRequest = request
        DispatchQueue.global().async { [weak self] in
            guard let self else { return }
            do {
                let (response, data) = try handler(capturedRequest)
                self.client?.urlProtocol(
                    self,
                    didReceive: response,
                    cacheStoragePolicy: .notAllowed
                )
                self.client?.urlProtocol(self, didLoad: data)
                self.client?.urlProtocolDidFinishLoading(self)
            } catch {
                self.client?.urlProtocol(self, didFailWithError: error)
            }
        }
    }

    override func stopLoading() {}
}

private final class MemoryViewModelURLProtocolHandlerStore: @unchecked Sendable {
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
