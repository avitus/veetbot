import Foundation
import Testing

@testable import VeetbotCore

/// The census surface: what an owner may select, what one confirmed gesture
/// sends, and how a row settles from the durable operation or is restored.
@Suite(.serialized) @MainActor struct EmailSubscriptionsViewModelTests {
    private static let digest = String(repeating: "b2", count: 32)
    private let threadID = UUID(uuidString: "00000000-0000-0000-0000-000000000801")!
    private let operationID = UUID(uuidString: "00000000-0000-0000-0000-000000000a01")!
    private let runID = UUID(uuidString: "00000000-0000-0000-0000-000000000a02")!

    /// The browse request carries the owner's filters and follows the server's cursor.
    @Test func testBrowsingCarriesTheFiltersAndFollowsTheCursor() async throws {
        let requests = EmailSubscriptionRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            let cursor = request.url!.query?.contains("cursor=next-page") == true
            return (200, self.page(ids: [cursor ? self.id(2) : self.id(1)],
                                   cursor: cursor ? nil : "next-page"))
        }
        defer { model.resetConnection() }
        model.setAccountFilter("work")
        model.setStateFilter("failed")
        await model.reload()
        #expect(model.items.map(\.id) == [id(1)])
        #expect(model.hasMore)
        await model.loadMore()

        let queries = requests.snapshot.map { $0.url!.query ?? "" }
        #expect(queries.count == 2)
        #expect(requests.snapshot.allSatisfy { $0.url!.path == "/v1/email/subscriptions" })
        #expect(queries[0].contains("account_id=work"))
        #expect(queries[0].contains("state=failed"))
        #expect(queries[0].contains("limit="))
        #expect(!queries[0].contains("cursor="))
        #expect(queries[1].contains("cursor=next-page"))
        #expect(model.items.map(\.id) == [id(1), id(2)])
        #expect(!model.hasMore)
    }

    /// Select all keeps the owner's valued senders and the batch bound intact.
    @Test func testSelectAllSkipsProtectedAndIneligibleRowsAndStopsAtTwentyFive() async throws {
        var rows = (1...28).map { row(id: id($0), mechanism: "one_click", verified: true, digest: Self.digest) }
        rows.insert(row(id: id(90), mechanism: "one_click", verified: true, digest: Self.digest, protected: true), at: 0)
        rows.insert(row(id: id(91), mechanism: "none", verified: true), at: 1)
        rows.insert(row(id: id(92), mechanism: "one_click", verified: false), at: 2)
        rows.insert(row(id: id(93), mechanism: "one_click", verified: true, digest: Self.digest, state: "kept"), at: 3)
        let model = try makeModel { _ in (200, "{\"items\":[\(rows.joined(separator: ","))],\"next_cursor\":null}") }
        defer { model.resetConnection() }
        await model.reload()
        model.selectAll()

        #expect(model.selection.count == 25)
        #expect(!model.selection.contains(id(90)), "Select all skips protected senders")
        #expect(!model.selection.contains(id(91)), "Select all skips a sender offering nothing")
        #expect(!model.selection.contains(id(92)), "Select all skips an unverified sender")
        #expect(!model.selection.contains(id(93)), "Select all skips a decided sender")
        #expect(model.selection == (1...25).map { id($0) })

        let protectedRow = try #require(model.items.first { $0.id == id(90) })
        model.toggleSelection(protectedRow)
        #expect(!model.isSelected(id(90)), "The batch bound holds even for an explicit tap")
        let extra = try #require(model.items.first { $0.id == id(26) })
        model.toggleSelection(extra)
        #expect(model.selection.count == 25)
    }

    /// A protected sender stays individually actionable; the bound is the only refusal.
    @Test func testAProtectedSenderCanStillBeSelectedIndividually() async throws {
        let rows = [row(id: id(1), mechanism: "one_click", verified: true, digest: Self.digest, protected: true)]
        let model = try makeModel { _ in (200, "{\"items\":[\(rows.joined(separator: ","))],\"next_cursor\":null}") }
        defer { model.resetConnection() }
        await model.reload()
        let protectedRow = try #require(model.items.first)

        #expect(model.canSelect(protectedRow))
        model.toggleSelection(protectedRow)
        #expect(model.isSelected(protectedRow.id))
        model.toggleSelection(protectedRow)
        #expect(model.selection.isEmpty)
    }

    /// An unverified sender is still being checked, and only Unsubscribe waits on that.
    @Test func testAnUnverifiedRowCannotBeSelectedButKeepAndReportSpamRemain() async throws {
        let requests = EmailSubscriptionRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "POST", request.url!.path.hasSuffix("/keep") {
                return (200, self.row(id: self.id(1), state: "kept"))
            }
            if request.httpMethod == "POST" { return (202, self.operationJSON(status: "COMPLETED")) }
            if request.url!.path.contains("/operations/") { return (200, self.operationJSON(status: "COMPLETED")) }
            return (200, self.page(ids: [self.id(1)], verified: false))
        }
        defer { model.resetConnection() }
        await model.reload()
        let pending = try #require(model.items.first)

        #expect(!model.canSelect(pending))
        model.toggleSelection(pending)
        #expect(model.selection.isEmpty)
        #expect(model.status(for: pending) == "Checking…")
        model.beginUnsubscribe(pending)
        #expect(model.confirmation == nil, "An unverified sender has no destination to confirm")
        await model.setKept(pending, kept: true)
        await model.report(pending, spam: true)
        #expect(requests.snapshot.contains { $0.url!.path.hasSuffix("/keep") })
        #expect(requests.snapshot.contains { $0.url!.path.hasSuffix("/spam") })
    }

    /// One confirmation names every sender, warns that the request is final, and archives only on request.
    @Test func testConfirmingSendsOneConsentedBatchWithTheArchiveChoice() async throws {
        let requests = EmailSubscriptionRequestRecorder()
        let bodies = EmailSubscriptionBodyRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "POST" {
                bodies.append(request.veetbotTestBody)
                return (202, self.operationJSON(status: "COMPLETED"))
            }
            if request.url!.path.contains("/operations/") { return (200, self.operationJSON(status: "COMPLETED")) }
            return (200, self.page(ids: [self.id(1), self.id(2)], verified: true))
        }
        defer { model.resetConnection() }
        await model.reload()
        model.selectAll()
        model.beginUnsubscribe()
        let confirmation = try #require(model.confirmation)

        #expect(confirmation.source == .list)
        #expect(confirmation.targets.count == 2)
        #expect(confirmation.targets.map(\.sentence) == Array(repeating: "One-click request to news.example.com", count: 2))
        #expect(EmailUnsubscribeConfirmation.warning == "An unsubscribe cannot be undone.")
        #expect(!model.archiveExisting, "Archiving existing mail is off until the owner asks for it")
        model.archiveExisting = true
        await model.confirmUnsubscribe()

        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(posts.count == 1)
        #expect(posts[0].url!.path == "/v1/email/subscriptions/unsubscribe")
        let body = try #require(try JSONSerialization.jsonObject(with: bodies.snapshot[0]) as? [String: Any])
        let targets = try #require(body["targets"] as? [[String: Any]])
        #expect(targets.map { $0["subscription_id"] as? String } == [id(1), id(2)])
        #expect(targets.allSatisfy { $0["evidence_digest"] as? String == Self.digest })
        #expect(targets.allSatisfy { $0["expected_revision"] as? Int == 3 })
        #expect(targets.allSatisfy { $0.count == 3 }, "A target names a sender, never a destination")
        #expect(body["archive_existing"] as? Bool == true)
        let key = try #require(body["idempotency_key"] as? String)
        #expect(posts[0].value(forHTTPHeaderField: "Idempotency-Key") == key)
        #expect(model.confirmation == nil)
        #expect(model.selection.isEmpty)
        #expect(model.rowErrors.isEmpty, "A confirmed outcome is quiet")
    }

    /// Rows settle from the durable operation; an accepted request says nothing more.
    @Test func testRowsAreOptimisticUntilTheDurableOperationSettlesThem() async throws {
        let admission = EmailSubscriptionGate()
        let settled = EmailSubscriptionSignal()
        let model = try makeGatedModel { request in
            if request.httpMethod == "POST" { return (202, self.operationJSON(status: "QUEUED"), admission) }
            if request.url!.path.contains("/operations/") {
                return (200, self.operationJSON(status: "COMPLETED"), nil)
            }
            let state = settled.isSignalled ? "unsubscribed" : "active"
            return (200, self.page(ids: [self.id(1)], verified: true, state: state), nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        let target = try #require(model.items.first)
        model.beginUnsubscribe(target)
        let confirming = Task { await model.confirmUnsubscribe() }
        try await waitFor { admission.isWaiting }
        #expect(model.status(for: target) == "Unsubscribing…")
        settled.signal()
        admission.release()
        await confirming.value

        #expect(model.items.first?.state == "unsubscribed")
        #expect(model.status(for: try #require(model.items.first)) == nil)
        #expect(model.rowErrors.isEmpty)
    }

    /// An uncertain, failed, or unreadable outcome restores the row with what remains.
    @Test(arguments: ["uncertain", "failed", "run-failed", "unreadable"])
    func testAnUnsettledOutcomeRestoresTheRowWithAnActionableError(outcome: String) async throws {
        let model = try makeModel(statusBackoff: { _ in }) { request in
            if request.httpMethod == "POST" { return (202, self.operationJSON(status: "QUEUED")) }
            if request.url!.path.contains("/operations/") {
                if outcome == "unreadable" { return (503, "{}") }
                return (200, self.operationJSON(status: outcome == "run-failed" ? "FAILED" : "COMPLETED"))
            }
            let status = ["uncertain", "failed"].contains(outcome) ? outcome : nil
            return (200, self.page(ids: [self.id(1)], verified: true, state: "failed", operation: status))
        }
        defer { model.resetConnection() }
        await model.reload()
        let target = try #require(model.items.first)
        model.beginUnsubscribe(target)
        await model.confirmUnsubscribe()

        let restored = try #require(model.items.first)
        #expect(model.rowErrors[restored.id] != nil, "An unsettled outcome must be reported on its row")
        #expect(model.status(for: restored) == model.rowErrors[restored.id])
        #expect(restored.canUnsubscribe, "Try again remains available")
        #expect(model.confirmation == nil)
    }

    /// An operation the server never settles stops polling and restores the row.
    @Test func testAnOperationThatNeverSettlesStopsPollingAndRestoresTheRow() async throws {
        let polls = EmailSubscriptionPollCounter()
        let model = try makeModel(statusBackoff: { _ in }) { request in
            if request.httpMethod == "POST" { return (202, self.operationJSON(status: "QUEUED")) }
            if request.url!.path.contains("/operations/") {
                // Settles only far past any sane bound, so an unbounded poller ends instead of hanging.
                return (200, self.operationJSON(status: polls.increment() > 100 ? "COMPLETED" : "QUEUED"))
            }
            return (200, self.page(ids: [self.id(1)], verified: true))
        }
        defer { model.resetConnection() }
        await model.reload()
        let target = try #require(model.items.first)
        model.beginUnsubscribe(target)
        await model.confirmUnsubscribe()

        #expect(polls.count <= 60, "Polling must be bounded")
        let restored = try #require(model.items.first)
        #expect(model.rowErrors[restored.id] != nil, "A row whose outcome is unknown must say so")
        #expect(model.status(for: restored) == model.rowErrors[restored.id])
        #expect(restored.canUnsubscribe, "Try again remains available")
    }

    /// Keep and its reversal are durable local decisions that change no mailbox.
    @Test(arguments: [true, false])
    func testKeepAndUnkeepReplaceTheRowFromTheServer(kept: Bool) async throws {
        let bodies = EmailSubscriptionBodyRecorder()
        let requests = EmailSubscriptionRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "POST" {
                bodies.append(request.veetbotTestBody)
                return (200, self.row(id: self.id(1), state: kept ? "kept" : "active", revision: 4))
            }
            return (200, self.page(ids: [self.id(1)], verified: true, state: kept ? "active" : "kept"))
        }
        defer { model.resetConnection() }
        await model.reload()
        let target = try #require(model.items.first)
        await model.setKept(target, kept: kept)

        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(posts.count == 1)
        #expect(posts[0].url!.path == "/v1/email/subscriptions/\(id(1))/keep")
        let body = try #require(try JSONSerialization.jsonObject(with: bodies.snapshot[0]) as? [String: Any])
        #expect(body["expected_revision"] as? Int == 3)
        #expect(body["kept"] as? Bool == kept)
        #expect(body.count == 2)
        #expect(model.items.first?.state == (kept ? "kept" : "active"))
        #expect(model.items.first?.revision == 4)
    }

    /// Report spam and Not spam are the same durable path with a strict boolean.
    @Test(arguments: [true, false])
    func testReportingSpamSettlesFromItsOwnOperation(spam: Bool) async throws {
        let bodies = EmailSubscriptionBodyRecorder()
        let requests = EmailSubscriptionRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "POST" {
                bodies.append(request.veetbotTestBody)
                return (202, self.operationJSON(status: "QUEUED"))
            }
            if request.url!.path.contains("/operations/") { return (200, self.operationJSON(status: "COMPLETED")) }
            return (200, self.page(ids: [self.id(1)], verified: true,
                                   state: spam ? "reported_spam" : "active"))
        }
        defer { model.resetConnection() }
        await model.reload()
        let target = try #require(model.items.first)
        await model.report(target, spam: spam)

        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(posts.count == 1)
        #expect(posts[0].url!.path == "/v1/email/subscriptions/\(id(1))/spam")
        let body = try #require(try JSONSerialization.jsonObject(with: bodies.snapshot[0]) as? [String: Any])
        #expect(body["expected_revision"] as? Int == 3)
        #expect(body["spam"] as? Bool == spam)
        let key = try #require(body["idempotency_key"] as? String)
        #expect(posts[0].value(forHTTPHeaderField: "Idempotency-Key") == key)
        #expect(model.items.first?.state == (spam ? "reported_spam" : "active"))
        #expect(model.rowErrors.isEmpty)
    }

    /// A bulk conversation opens the same confirmation, for exactly its own sender.
    @Test func testTheThreadActionOpensTheSameConfirmationWithOneRow() async throws {
        let model = try makeModel { _ in (200, self.page(ids: [self.id(1)], verified: true)) }
        defer { model.resetConnection() }
        let eligible = try thread(state: "active", digest: Self.digest)
        let decided = try thread(state: "unsubscribed", digest: Self.digest)
        let unverified = try thread(state: "active", digest: nil)

        model.beginUnsubscribe(thread: eligible)
        let confirmation = try #require(model.confirmation)
        #expect(confirmation.source == .thread)
        #expect(confirmation.targets.count == 1)
        #expect(confirmation.targets[0].id == id(1))
        #expect(confirmation.targets[0].evidenceDigest == Self.digest)
        #expect(confirmation.targets[0].revision == 4)
        #expect(confirmation.targets[0].sentence == "One-click request to news.example.com")

        model.cancelConfirmation()
        #expect(model.confirmation == nil)
        model.beginUnsubscribe(thread: decided)
        #expect(model.confirmation == nil, "A decided sender offers no unsubscribe")
        model.beginUnsubscribe(thread: unverified)
        #expect(model.confirmation == nil, "Unverified evidence offers no unsubscribe")
    }

    /// Each state offers exactly what remains possible, and nothing in its place.
    @Test func testEachRowOffersOnlyWhatItsStateStillAllows() async throws {
        let rows = [
            row(id: id(1), state: "active"), row(id: id(2), state: "failed"),
            row(id: id(3), state: "still_sending"), row(id: id(4), state: "kept"),
            row(id: id(5), state: "reported_spam"), row(id: id(6), state: "unsubscribed"),
            row(id: id(7), state: "pending"),
            row(id: id(8), mechanism: "none", verified: true, digest: nil, state: "active"),
            row(id: id(9), mechanism: "none", verified: false, digest: nil, state: "active")
        ]
        let model = try makeModel { _ in (200, "{\"items\":[\(rows.joined(separator: ","))],\"next_cursor\":null}") }
        defer { model.resetConnection() }
        await model.reload()
        let offered = model.items.map { model.actions(for: $0) }

        #expect(offered[0] == [.unsubscribe, .reportSpam, .keep])
        #expect(offered[1] == [.tryAgain, .reportSpam, .keep])
        #expect(offered[2] == [.tryAgain, .reportSpam, .keep])
        #expect(offered[3] == [.unkeep])
        #expect(offered[4] == [.notSpam])
        #expect(offered[5] == [])
        #expect(offered[6] == [], "An action in flight offers nothing until it settles")
        #expect(offered[7] == [.reportSpam, .keep], "A sender offering nothing keeps its fallbacks")
        #expect(offered[8] == [.reportSpam, .keep], "Keep and Report spam never wait on verification")
    }

    /// Support is advertised per account; an older server advertises none of it.
    @Test func testSupportIsPerAccountAndAbsentOnAnOlderServer() async throws {
        let model = try makeEmailModel { request in
            guard request.url!.path.hasSuffix("accounts") else { return (200, self.emptyPage) }
            return (200, """
            {"items":[{"id":"personal","label":"Personal","status":"ready","history_complete":false,\
            "history_processed":0,"read_server_id":"gmail_read","unsubscribe_supported":true},\
            {"id":"work","label":"Work","status":"ready","history_complete":false,\
            "history_processed":0,"read_server_id":"gmail_work_read"}],"next_cursor":null}
            """)
        }
        let older = try makeEmailModel { request in
            guard request.url!.path.hasSuffix("accounts") else { return (200, self.emptyPage) }
            return (200, """
            {"items":[{"id":"personal","label":"Personal","status":"ready","history_complete":false,\
            "history_processed":0,"read_server_id":"gmail_read"}],"next_cursor":null}
            """)
        }
        defer { model.resetConnection(); older.resetConnection() }
        await model.reload()
        await older.reload()

        #expect(model.unsubscribeAvailable)
        #expect(model.unsubscribeSupported(for: "personal"))
        #expect(!model.unsubscribeSupported(for: "work"))
        #expect(!older.unsubscribeAvailable)
        #expect(!older.unsubscribeSupported(for: "personal"))
    }

    // MARK: - fixtures

    private var emptyPage: String { "{\"items\":[],\"next_cursor\":null}" }

    private func id(_ index: Int) -> String {
        String(String(format: "%064d", index).suffix(64))
    }

    private func page(
        ids: [String], verified: Bool = true, state: String = "active", cursor: String? = nil,
        operation: String? = nil
    ) -> String {
        let rows = ids.map {
            row(id: $0, mechanism: verified ? "one_click" : "none", verified: verified,
                digest: verified ? Self.digest : nil, state: state, operation: operation)
        }
        return "{\"items\":[\(rows.joined(separator: ","))],\"next_cursor\":\(cursor.map { "\"\($0)\"" } ?? "null")}"
    }

    private func row(
        id: String, mechanism: String = "one_click", verified: Bool = true, digest: String? = Self.digest,
        state: String = "active", protected: Bool = false, revision: Int = 3, operation: String? = nil
    ) -> String {
        let block = operation.map {
            """
            {"operation_id":"\(operationID)","run_id":"\(runID)","action":"unsubscribe","status":"\($0)",\
            "code":"unsubscribe.rejected","requested_at":"2026-09-19T00:00:00Z","prior_state":"active"}
            """
        } ?? "null"
        return """
            {"id":"\(id)","account_id":"personal","display_name":"Example News",\
            "address":"news@example.com","list_id":"news.example.com","thread_count":12,\
            "thread_count_overflow":false,"first_seen_at":"2026-07-01T00:00:00Z",\
            "last_received_at":"2026-09-18T00:00:00Z","mechanism":"\(mechanism)","verified":\(verified),\
            "link_only":false,"destination":"\(mechanism == "none" ? "" : "news.example.com")",\
            "mailto":null,"evidence_digest":\(digest.map { "\"\($0)\"" } ?? "null"),"state":"\(state)",\
            "protected":\(protected),"protected_reason":\(protected ? "\"correspondent\"" : "null"),\
            "requested_at":null,"operation":\(block),"revision":\(revision)}
            """
    }

    private func operationJSON(status: String) -> String {
        "{\"operation_id\":\"\(operationID)\",\"run_id\":\"\(runID)\",\"status\":\"\(status)\",\"replayed\":false}"
    }

    private func thread(state: String, digest: String?) throws -> EmailThreadView {
        var payload: [String: Any] = [
            "id": threadID.uuidString, "account_id": "personal", "subject": "Weekly digest",
            "senders": ["Example News <news@example.com>"], "updated_at": "2026-09-18T00:00:00Z",
            "revision": 3, "summary": "A weekly digest.", "reason": "A newsletter.",
            "needs_reply": false, "priority": 0.2, "complete": true
        ]
        payload["subscription"] = [
            "id": id(1), "state": state, "mechanism": "one_click",
            "destination": "news.example.com",
            "evidence_digest": digest as Any? ?? NSNull(), "revision": 4
        ]
        return try JSONDecoder.server.decode(
            EmailThreadView.self, from: JSONSerialization.data(withJSONObject: payload))
    }

    private func waitFor(_ condition: () -> Bool) async throws {
        for _ in 0..<800 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        #expect(condition(), "Expected a subscription transport boundary within four seconds")
    }

    private func makeAPI(
        _ handler: @escaping (URLRequest) throws -> (Int, String, EmailSubscriptionGate?)
    ) throws -> VeetbotAPIClient {
        let id = EmailSubscriptionURLProtocol.register(handler)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [EmailSubscriptionURLProtocol.self]
        configuration.httpAdditionalHeaders = ["X-Email-Subscription-Test": id]
        return VeetbotAPIClient(transport: HTTPTransport(
            configuration: try ConnectionConfiguration(baseURLString: "https://email.test"),
            tokenStore: InMemoryTokenStore(token: "test-token"),
            session: URLSession(configuration: configuration)))
    }

    private func makeModel(
        statusBackoff: @escaping @Sendable (UInt64) async throws -> Void = { _ in },
        handler: @escaping (URLRequest) throws -> (Int, String)
    ) throws -> EmailSubscriptionsViewModel {
        let api = try makeAPI { request in
            let (status, body) = try handler(request)
            return (status, body, nil)
        }
        return EmailSubscriptionsViewModel(makeAPIClient: { api }, statusBackoff: statusBackoff)
    }

    private func makeGatedModel(
        handler: @escaping (URLRequest) throws -> (Int, String, EmailSubscriptionGate?)
    ) throws -> EmailSubscriptionsViewModel {
        let api = try makeAPI(handler)
        return EmailSubscriptionsViewModel(makeAPIClient: { api }, statusBackoff: { _ in })
    }

    private func makeEmailModel(
        handler: @escaping (URLRequest) throws -> (Int, String)
    ) throws -> EmailViewModel {
        let api = try makeAPI { request in
            let (status, body) = try handler(request)
            return (status, body, nil)
        }
        return EmailViewModel(makeAPIClient: { api }, statusBackoff: { _ in })
    }
}

extension URLRequest {
    /// `URLProtocol` streams a body, so tests read the bytes URLSession kept.
    fileprivate var veetbotTestBody: Data {
        if let body = httpBody { return body }
        guard let stream = httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: buffer.count)
            if read <= 0 { break }
            data.append(contentsOf: buffer[0..<read])
        }
        return data
    }
}

/// Holds one response so an optimistic row can be observed before it settles.
private final class EmailSubscriptionGate: @unchecked Sendable {
    private let lock = NSLock()
    private var delivery: (() -> Void)?
    private var released = false

    var isWaiting: Bool { lock.withLock { delivery != nil } }

    func install(_ callback: @escaping () -> Void) {
        let deliverNow = lock.withLock {
            if released { return true }
            delivery = callback
            return false
        }
        if deliverNow { callback() }
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

/// A one-way flag letting a handler answer differently once the test says so.
private final class EmailSubscriptionSignal: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    var isSignalled: Bool { lock.withLock { value } }
    func signal() { lock.withLock { value = true } }
}

private final class EmailSubscriptionPollCounter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    var count: Int { lock.withLock { value } }
    func increment() -> Int { lock.withLock { value += 1; return value } }
}

private final class EmailSubscriptionRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [URLRequest] = []
    func append(_ value: URLRequest) { lock.withLock { values.append(value) } }
    var snapshot: [URLRequest] { lock.withLock { values } }
}

private final class EmailSubscriptionBodyRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [Data] = []
    func append(_ value: Data) { lock.withLock { values.append(value) } }
    var snapshot: [Data] { lock.withLock { values } }
}

/// Routes each model's requests through the production transport and JSON paths.
private final class EmailSubscriptionURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var handlers: [String: (URLRequest) throws -> (Int, String, EmailSubscriptionGate?)] = [:]

    static func register(
        _ handler: @escaping (URLRequest) throws -> (Int, String, EmailSubscriptionGate?)
    ) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let id = request.value(forHTTPHeaderField: "X-Email-Subscription-Test"),
              let handler = Self.lock.withLock({ Self.handlers[id] }) else { return }
        do {
            let (status, body, gate) = try handler(request)
            let deliver = {
                let response = HTTPURLResponse(url: self.request.url!, statusCode: status, httpVersion: nil,
                                               headerFields: ["Content-Type": "application/json"])!
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
