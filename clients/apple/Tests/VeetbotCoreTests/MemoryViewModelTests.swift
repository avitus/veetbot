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

    @Test
    func testReviewQueueFilterAsksTheServerForFlaggedBeliefsOnly() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000501"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000502"))
        let lock = NSLock()
        var requests: [URLRequest] = []
        let model = try makeModel { request in
            lock.withLock { requests.append(request) }
            return try self.response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID, flagged: true))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        let plain = try #require(lock.withLock { requests.first })
        let plainQuery = URLComponents(url: plain.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        #expect(!plainQuery.contains(where: { $0.name == "flagged" }), "the default list asks for both")

        model.setFlaggedOnly(true)
        // Wait for the reload request, not a fixed delay that can expire
        // before a loaded UI actor schedules the filter's task.
        for _ in 0 ..< 600 where lock.withLock({ requests.count }) < 2 {
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        try #require(lock.withLock { requests.count } == 2)
        #expect(model.flaggedOnly)
        let queue = try #require(lock.withLock { requests.last })
        let queueQuery = URLComponents(url: queue.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        #expect(queueQuery.contains(URLQueryItem(name: "flagged", value: "true")))
        #expect(queueQuery.contains(URLQueryItem(name: "ceiling", value: "restricted")))
    }

    @Test
    func testChoosingASidebarGroupSetsTheQueryWithOneRequest() async throws {
        let lock = NSLock()
        var requests: [URLRequest] = []
        let model = try makeModel { request in
            lock.withLock { requests.append(request) }
            return try self.response(for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#)
        }
        await model.reload()

        func query(after count: Int) async throws -> [URLQueryItem] {
            for _ in 0 ..< 600 where lock.withLock({ requests.count }) < count {
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            try #require(lock.withLock { requests.count } == count)
            let request = try #require(lock.withLock { requests.last })
            return URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        }

        model.selectGroup(.review)
        var items = try await query(after: 2)
        #expect(model.group == .review)
        #expect(items.contains(URLQueryItem(name: "flagged", value: "true")))
        #expect(!items.contains { $0.name == "belief_type" })

        model.selectGroup(.type(.preference))
        items = try await query(after: 3)
        #expect(model.group == .type(.preference))
        #expect(items.contains(URLQueryItem(name: "belief_type", value: "preference")))
        #expect(!items.contains { $0.name == "flagged" })

        model.selectGroup(.all)
        items = try await query(after: 4)
        #expect(model.group == .all)
        #expect(!items.contains { $0.name == "flagged" || $0.name == "belief_type" })
        #expect(MemoryBrowseGroup.browseTypes.map(\.title) == [
            "Preferences", "Facts", "Relationships", "About you", "Procedures",
        ])
    }

    @Test
    func testEachReviewOutcomeAndDeletionConfirmsWithAToast() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000531"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000532"))
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
            switch request.httpMethod {
            case "DELETE": return try self.response(for: request, statusCode: 204, body: "")
            case "POST":
                return try self.response(
                    for: request, statusCode: 200,
                    body: self.memoryJSON(id: memoryID, sessionID: sessionID, flagged: false)
                )
            default:
                return try self.response(
                    for: request, statusCode: 200,
                    body: """
                        {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID, flagged: true))],"next_cursor":null}
                        """
                )
            }
        }

        await model.reload()
        let memory = try #require(model.items.first)
        let expected: [(MemoryReviewOutcome, String)] = [
            (.dismiss, "Marked as reviewed."),
            (.untrue, "Marked not true. Veetbot will not form it again from the same evidence."),
            (.notHere, "Marked not relevant here."),
        ]
        for (outcome, text) in expected {
            _ = await model.review(memory, outcome: outcome)
            #expect(toasts.current?.text == text)
        }
        _ = await model.delete(memory)
        #expect(toasts.current?.text == "Deleted. The same statement will not form again.")
    }

    @Test
    func testAFailedReviewKeepsItsErrorInlineAndRaisesNoToast() async throws {
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000541"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000542"))
        let model = try makeModel(toasts: toasts) { request in
            if request.httpMethod == "POST" {
                return try self.response(for: request, statusCode: 500, body: #"{"error":{"code":"internal","message":"x"}}"#)
            }
            return try self.response(
                for: request, statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID, flagged: true))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        let memory = try #require(model.items.first)
        _ = await model.review(memory, outcome: .dismiss)

        #expect(model.errorMessage != nil)
        #expect(toasts.current == nil)
    }

    @Test
    func testDeleteSendsTheGovernedWriteAndRemovesTheRow() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000511"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000512"))
        let lock = NSLock()
        var requests: [URLRequest] = []
        let model = try makeModel { request in
            lock.withLock { requests.append(request) }
            if request.httpMethod == "DELETE" {
                return try self.response(for: request, statusCode: 204, body: "")
            }
            return try self.response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID, flagged: true))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        let memory = try #require(model.items.first)
        let deleted = await model.delete(memory)

        #expect(deleted)
        #expect(model.items.isEmpty)
        #expect(model.errorMessage == nil)
        #expect(model.changesUnavailable == false)
        let request = try #require(lock.withLock { requests.last })
        #expect(request.httpMethod == "DELETE")
        #expect(request.url?.path == "/v1/memories/\(memoryID.uuidString)")
        let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        #expect(query.contains(URLQueryItem(name: "ceiling", value: "restricted")))
        let key = request.value(forHTTPHeaderField: "Idempotency-Key")
        #expect(key != nil && !(key ?? "").isEmpty, "every write carries a fresh idempotency key")
    }

    @Test
    func testDismissReplacesTheRowAndLeavesTheReviewQueue() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000521"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000522"))
        let lock = NSLock()
        var requests: [URLRequest] = []
        let model = try makeModel { request in
            lock.withLock { requests.append(request) }
            if request.httpMethod == "POST" {
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: self.memoryJSON(id: memoryID, sessionID: sessionID, flagged: false)
                )
            }
            return try self.response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID, flagged: true))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        let memory = try #require(model.items.first)
        #expect(memory.flaggedForReview)
        let reviewed = await model.review(memory, outcome: .dismiss)
        #expect(reviewed?.flaggedForReview == false)
        // Without the queue filter the row is replaced in place.
        #expect(model.items.map(\.flaggedForReview) == [false])
        let request = try #require(lock.withLock { requests.last })
        #expect(request.httpMethod == "POST")
        #expect(request.url?.path == "/v1/memories/\(memoryID.uuidString)/review")
        let body = try #require(request.httpBody ?? request.httpBodyStream.map { stream in
            stream.open()
            defer { stream.close() }
            var data = Data()
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let read = stream.read(&buffer, maxLength: buffer.count)
                if read <= 0 { break }
                data.append(buffer, count: read)
            }
            return data
        })
        let decoded = try JSONSerialization.jsonObject(with: body) as? [String: String]
        #expect(decoded == ["outcome": "dismiss"])

        // Under the queue filter, a dismissed belief leaves the list.
        model.setFlaggedOnly(true)
        try await Task.sleep(nanoseconds: 200_000_000)
        let queued = try #require(model.items.first)
        _ = await model.review(queued, outcome: .dismiss)
        #expect(model.items.isEmpty)
    }

    @Test
    func testAWriteTheServerDoesNotSupportDegradesWithoutDroppingTheRow() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000531"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000532"))
        let model = try makeModel { request in
            if request.httpMethod == "DELETE" {
                // A server that predates ADR-0117 rewrites the method miss to a
                // 400 with this exact message, never a bare 405.
                return try self.response(
                    for: request,
                    statusCode: 400,
                    body: #"{"error":{"code":"malformed_request","message":"The HTTP request is not supported.","details":{},"request_id":"old"}}"#
                )
            }
            return try self.response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID, flagged: true))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        let memory = try #require(model.items.first)
        let deleted = await model.delete(memory)

        #expect(!deleted)
        #expect(model.items.count == 1)
        #expect(model.changesUnavailable)
        #expect(model.errorMessage == "This server does not support memory changes yet.")
    }

    // MARK: ADR-0224

    @Test
    func testCorrectingTheWordingSupersedesTheRowWithTheReplacement() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000561"))
        let replacementID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000562"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000563"))
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let lock = NSLock()
        var bodies: [[String: Any]] = []
        let model = try makeModel(toasts: toasts) { request in
            if request.httpMethod == "POST" {
                let body = try JSONSerialization.jsonObject(with: self.bodyData(request)) as? [String: Any]
                lock.withLock { bodies.append(body ?? [:]) }
                return try self.response(
                    for: request, statusCode: 200,
                    body: self.memoryJSON(id: replacementID, sessionID: sessionID)
                        .replacingOccurrences(of: "The user prefers dark mode.", with: "The user prefers light mode.")
                )
            }
            if request.url?.path == "/v1/memories/counts" {
                return try self.response(for: request, statusCode: 404, body: "")
            }
            return try self.response(
                for: request, statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID, flagged: true))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        let memory = try #require(model.items.first)
        let replacement = await model.correct(memory, wording: "  The user prefers light mode. ")

        #expect(replacement?.id == replacementID)
        #expect(model.items.map(\.id) == [replacementID])
        #expect(toasts.current?.text == MemoryViewModel.correctedConfirmation)
        let body = try #require(lock.withLock { bodies.last })
        #expect(body["outcome"] as? String == "changed")
        #expect(body["replacement_statement"] as? String == "The user prefers light mode.")
        #expect(body["expected_updated_at"] as? String == "2026-08-20T00:00:00Z")
    }

    @Test
    func testAWordingTheServerRefusesStaysInlineAndAStaleOneRefreshes() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000571"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000572"))
        let lock = NSLock()
        var status = 400
        var listings = 0
        let model = try makeModel { request in
            if request.httpMethod == "POST" {
                let code = lock.withLock { status }
                let errorCode = code == 400 ? "malformed_request" : "conflict"
                return try self.response(
                    for: request, statusCode: code,
                    body: #"{"error":{"code":"\#(errorCode)","message":"x","details":{},"request_id":"r"}}"#
                )
            }
            if request.url?.path == "/v1/memories" { lock.withLock { listings += 1 } }
            return try self.response(
                for: request, statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        let memory = try #require(model.items.first)
        #expect(await model.correct(memory, wording: "password: hunter2") == nil)
        #expect(model.errorMessage == MemoryViewModel.refusedWordingMessage)
        #expect(model.items.map(\.id) == [memoryID])

        lock.withLock { status = 409 }
        let before = lock.withLock { listings }
        #expect(await model.correct(memory, wording: "Another wording.") == nil)
        #expect(lock.withLock { listings } == before + 1)
    }

    @Test
    func testCountsArriveWithTheListAndAnOlderServerLeavesThemBlank() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000581"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000582"))
        let lock = NSLock()
        var countsAvailable = true
        var countRequests: [URLRequest] = []
        let model = try makeModel { request in
            if request.url?.path == "/v1/memories/counts" {
                lock.withLock { countRequests.append(request) }
                guard lock.withLock({ countsAvailable }) else {
                    // An older server parses "counts" as an identifier.
                    return try self.response(
                        for: request, statusCode: 400,
                        body: #"{"error":{"code":"malformed_request","message":"The request body or parameters are malformed: path.memory_id.","details":{},"request_id":"r"}}"#
                    )
                }
                return try self.response(
                    for: request, statusCode: 200,
                    body: #"{"live":12,"flagged":3,"by_status":{"active":10,"provisional":2,"superseded":4,"retired":1,"expired":0,"candidate":0},"by_type":{"fact":5,"preference":4,"relationship":2,"user_model_attr":1,"procedure_pointer":0},"synthesis":null,"counted_at":"2026-10-20T15:00:00Z"}"#
                )
            }
            return try self.response(
                for: request, statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        #expect(model.count(for: .all) == nil, "the list alone never asks for counts")
        await model.loadCounts()
        #expect(model.count(for: .review) == 3)
        #expect(model.count(for: .all) == 12)
        #expect(model.count(for: .type(.fact)) == 5)
        #expect(model.count(for: .type(.procedurePointer)) == 0)
        let request = try #require(lock.withLock { countRequests.first })
        let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
        #expect(query.contains(URLQueryItem(name: "ceiling", value: "restricted")))

        lock.withLock { countsAvailable = false }
        await model.reload()
        await model.loadCounts()
        #expect(model.count(for: .all) == nil)
        #expect(model.errorMessage == nil)
        #expect(model.items.count == 1)
    }

    @Test
    func testEvidenceDecodesTheOwnersQuoteAndWithheldSources() async throws {
        let memoryID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000591"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000592"))
        let lock = NSLock()
        var paths: [String] = []
        let model = try makeModel { request in
            lock.withLock { paths.append(request.url?.path ?? "") }
            if request.url?.path.hasSuffix("/evidence") == true {
                return try self.response(
                    for: request, statusCode: 200,
                    body: """
                        {"items":[{"source_kind":"owner","session_id":"\(sessionID.uuidString)","event_sequence":11,"evidence_at":"2026-08-15T00:00:00Z","excerpt":"I like dark mode.","withheld":null},{"source_kind":"email","session_id":"\(sessionID.uuidString)","event_sequence":10,"evidence_at":"2026-08-14T00:00:00Z","excerpt":null,"withheld":"not_owner_text"},{"source_kind":"unknown","session_id":"\(sessionID.uuidString)","event_sequence":9,"evidence_at":null,"excerpt":null,"withheld":"erased"}]}
                        """
                )
            }
            return try self.response(
                for: request, statusCode: 200,
                body: """
                    {"items":[\(self.memoryJSON(id: memoryID, sessionID: sessionID))],"next_cursor":null}
                    """
            )
        }

        await model.reload()
        let memory = try #require(model.items.first)
        let evidence = try #require(await model.evidence(for: memory))

        #expect(evidence.map(\.eventSequence) == [11, 10, 9])
        #expect(evidence[0].excerpt == "I like dark mode.")
        #expect(evidence[0].sourceLine == "You said")
        #expect(evidence[1].sourceLine == "From an email")
        #expect(evidence[2].sourceLine == "Source removed")
        #expect(lock.withLock { paths.contains("/v1/memories/\(memoryID.uuidString)/evidence") })
    }

    private func bodyData(_ request: URLRequest) -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data
    }

    private func makeModel(
        toasts: ToastCenter? = nil,
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
        return MemoryViewModel(
            makeAPIClient: { client },
            toasts: toasts ?? ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        )
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

    private func memoryJSON(id: UUID, sessionID: UUID, flagged: Bool = false) -> String {
        """
        {"id":"\(id.uuidString)","subject":"the user","statement":"The user prefers dark mode.","belief_type":"preference","claim_kind":"preference","derivation":"direct","longevity":"durable","status":"active","polarity":"assert","scope":"session","portability":"portable","authority":"user","sensitivity":"restricted","confidence":0.87,"corroboration_count":3,"flagged_for_review":\(flagged),"conflicts_with":[],"superseded_by":null,"source_session_id":"\(sessionID.uuidString)","source_event_ids":[10,11],"formation_run_id":"00000000-0000-0000-0000-000000000900","consolidation_policy_version":"formation@1","origin_scopes":["session"],"valid_from":"2026-08-01T00:00:00Z","valid_to":null,"expires_at":null,"last_evidence_at":"2026-08-15T00:00:00Z","last_used_at":null,"last_reinforced_at":"2026-08-15T00:00:00Z","created_at":"2026-07-01T00:00:00Z","updated_at":"2026-08-20T00:00:00Z"}
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
