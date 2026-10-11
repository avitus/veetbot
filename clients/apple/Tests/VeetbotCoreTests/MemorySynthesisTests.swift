import Foundation
import Testing
@testable import VeetbotCore

@Suite(.serialized) @MainActor struct MemorySynthesisTests {
    @Test func connectionLoadsGovernedReviewControls() async throws {
        let api = try synthesisAPI { request in
            if request.url!.path.hasPrefix("/v1/memories/") {
                return (200, synthesisSummary().replacingOccurrences(of: "\"record_kind\":\"summary\"", with: "\"record_kind\":\"hypothesis\""))
            }
            return (200, synthesisOperation(kind: "hypothesis"))
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        #expect(model.operation?.title == "Tentative connection")
        #expect(model.summary?.recordKind == "hypothesis")
        #expect(model.summary?.sources.count == 2)
        #expect(model.operation?.canUndo == false)
    }

    @Test func journalLoadsAndRevalidatesDetail() async throws {
        let api = try synthesisAPI { request in
            if request.url!.path == "/v1/memory-reconsolidations" {
                return (200, "{\"items\":[\(synthesisOperation())],\"next_cursor\":null}")
            }
            if request.url!.path.hasPrefix("/v1/memories/") { return (200, synthesisSummary()) }
            return (200, synthesisOperation())
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.reload()
        #expect(model.items.map(\.id) == [synthesisID])
        await model.open(synthesisID)
        #expect(model.operation?.kind == "summary")
        #expect(model.summary?.content?.statement == "Enjoys quiet mornings.")
        #expect(model.summary?.sources.count == 2, "omitted inputs remain visible support")
    }

    @Test func undoOnlyForKnownCommittedMergesWithVisibleSupport() throws {
        for (kind, state, visible, expected) in [
            ("merge", "committed", true, true), ("summary", "committed", true, false),
            ("future", "committed", true, false), ("merge", "undone", true, false),
            ("merge", "invalidated", false, false), ("merge", "committed", false, false)
        ] {
            let operation = try JSONDecoder.server.decode(MemorySynthesisOperation.self, from: Data(synthesisOperation(kind: kind, state: state, visible: visible).utf8))
            #expect(operation.canUndo == expected)
        }
    }
    @Test func paginationFiltersDeduplicateAndStopRepeatedCursors() async throws {
        let log = SynthesisRequestLog()
        let api = try synthesisAPI { request in
            log.add(request)
            #expect(request.url!.query!.contains("kind=merge"))
            #expect(request.url!.query!.contains("state=committed"))
            return (200, "{\"items\":[\(synthesisOperation(kind: "merge")),\(synthesisOperation(kind: "merge"))],\"next_cursor\":\"again\"}")
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.select(kind: "merge", state: "committed")
        await model.loadMore()
        await model.loadMore()
        #expect(log.requests.count == 2)
        #expect(model.items.count == 1)
        #expect(!model.hasMore)
    }

    @Test func emptyPageStillContinuesAndFailureCanRetry() async throws {
        let log = SynthesisRequestLog()
        let api = try synthesisAPI { request in
            let count = log.add(request)
            if count == 2 { return (503, synthesisError(503)) }
            return count == 1 ? (200, "{\"items\":[],\"next_cursor\":\"next\"}") : (200, "{\"items\":[\(synthesisOperation())],\"next_cursor\":null}")
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.reload()
        #expect(model.hasMore)
        await model.loadMore()
        #expect(model.errorMessage != nil)
        #expect(model.hasMore)
        await model.loadMore()
        #expect(model.items.count == 1)
        #expect(model.errorMessage == nil)
    }

    @Test(arguments: [401, 403, 404, 405, 500])
    func readFailuresClearPrivateContent(code: Int) async throws {
        let api = try synthesisAPI { _ in (code, synthesisError(code)) }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.reload()
        #expect(model.items.isEmpty)
        #expect(model.unavailable == (code == 404 || code == 405))
        #expect(model.errorMessage != nil)
        #expect(model.errorMessage?.contains("SECRET") == false)
        await model.open(synthesisID)
        #expect(model.operation == nil)
        #expect(model.summary == nil)
    }

    @Test func vanishedSummaryNeverPublishesCachedOperationContent() async throws {
        let api = try synthesisAPI { request in
            if request.url!.path.hasPrefix("/v1/memories/") { return (404, synthesisError(404)) }
            return (200, synthesisOperation())
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        #expect(model.operation == nil)
        #expect(model.summary == nil)
        #expect(model.errorMessage != nil)
    }

    @Test func opaqueHistoryAndUnknownKindsStayReadableWithoutActions() async throws {
        for kind in ["summary", "merge", "future"] {
            let log = SynthesisRequestLog()
            let api = try synthesisAPI { request in
                log.add(request)
                return (200, synthesisOperation(kind: kind, state: "invalidated", visible: false))
            }
            let model = MemorySynthesisViewModel(makeAPIClient: { api })
            await model.open(synthesisID)
            #expect(model.operation?.content == nil)
            #expect(model.operation?.sources == [])
            #expect(model.operation?.canUndo == false)
            #expect(log.requests.count == 1)
        }
    }

    @Test func undoRetainsExactRevisionAndKeyAcrossUncertainRetry() async throws {
        let log = SynthesisRequestLog()
        let api = try synthesisAPI { request in
            log.add(request)
            if request.httpMethod == "POST" {
                if log.requests.filter({ $0.httpMethod == "POST" }).count <= 2 { throw URLError(.timedOut) }
                return (200, synthesisOperation(kind: "merge", state: "undone", revision: 4))
            }
            return (200, synthesisOperation(kind: "merge", revision: 3))
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        await model.undo()
        #expect(model.canRetryWrite)
        await model.retryWrite()
        let writes = log.requests.filter { $0.httpMethod == "POST" }
        #expect(writes.count == 3)
        #expect(Set(writes.compactMap { $0.value(forHTTPHeaderField: "Idempotency-Key") }).count == 1)
        for request in writes {
            #expect(request.url!.path.hasSuffix("/undo"))
            let body = try JSONSerialization.jsonObject(with: requestBody(request)) as! [String: Int]
            #expect(body == ["expected_revision": 3])
        }
        #expect(model.operation?.state == "undone")
        #expect(!model.canRetryWrite)
    }

    @Test func conflictRefreshesAndRequiresANewPreview() async throws {
        let log = SynthesisRequestLog()
        let api = try synthesisAPI { request in
            log.add(request)
            if request.httpMethod == "POST" { return (409, synthesisError(409)) }
            return (200, synthesisOperation(kind: "merge", state: log.requests.count == 1 ? "committed" : "invalidated", visible: log.requests.count == 1))
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        await model.undo()
        #expect(model.operation?.state == "invalidated")
        #expect(model.operation?.content == nil)
        #expect(!model.canRetryWrite)
        #expect(model.errorMessage?.contains("changed") == true)
    }

    @Test(arguments: [MemoryReviewOutcome.dismiss, .notHere, .untrue])
    func summaryReviewUsesTheDerivedIdentity(outcome: MemoryReviewOutcome) async throws {
        let log = SynthesisRequestLog()
        let api = try synthesisAPI { request in
            log.add(request)
            if request.httpMethod == "POST" {
                #expect(request.url!.path == "/v1/memories/\(summaryID)/review")
                let body = try JSONSerialization.jsonObject(with: requestBody(request)) as! [String: String]
                #expect(body["outcome"] == outcome.rawValue)
                return (200, synthesisSummary(flagged: outcome != .dismiss, visible: outcome != .untrue, portability: outcome == .notHere ? "local" : "portable"))
            }
            if request.url!.path.hasPrefix("/v1/memories/") { return (200, synthesisSummary()) }
            return (200, synthesisOperation())
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        await model.review(outcome)
        #expect(log.requests.filter { $0.httpMethod == "POST" }.count == 1)
        #expect(model.summary?.flaggedForReview == (outcome != .dismiss))
        #expect((model.summary?.content == nil) == (outcome == .untrue))
        if outcome == .notHere { #expect(model.summary?.content?.portability == "local") }
        #expect(model.operation?.content == nil, "a review must not retain the older derived text in history")
    }

    @Test func deletingSummaryKeepsOriginalsAndClearsRenderedCopies() async throws {
        let log = SynthesisRequestLog()
        let api = try synthesisAPI { request in
            log.add(request)
            if request.httpMethod == "DELETE" { return (204, "") }
            if request.url!.path.hasPrefix("/v1/memories/") { return (200, synthesisSummary()) }
            return (200, synthesisOperation())
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        await model.deleteSummary()
        #expect(log.requests.filter { $0.httpMethod == "DELETE" }.map { $0.url!.path } == ["/v1/memories/\(summaryID)"])
        #expect(model.summary == nil)
        #expect(model.operation == nil)
        #expect(model.message != nil)
    }

    @Test func connectionChangeAndBackgroundDiscardLateReads() async throws {
        for connectionChange in [false, true] {
            let gate = SynthesisGate()
            defer { gate.release() }
            let api = try synthesisAPI { _ in
                gate.wait()
                return (200, "{\"items\":[\(synthesisOperation())],\"next_cursor\":null}")
            }
            let model = MemorySynthesisViewModel(makeAPIClient: { api })
            let task = Task { await model.reload() }
            while !gate.started { await Task.yield() }
            if connectionChange { model.invalidateConnection() } else { model.clearContent() }
            gate.release()
            await task.value
            #expect(model.items.isEmpty)
            #expect(!model.isLoading)
            if connectionChange {
                await model.reload()
                #expect(model.items.isEmpty)
            }
        }
    }

    @Test(arguments: [401, 403])
    func expiredAuthorizationClearsEarlierPages(code: Int) async throws {
        let api = try synthesisAPI { request in
            if request.url!.query!.contains("cursor=") { return (code, synthesisError(code)) }
            return (200, "{\"items\":[\(synthesisOperation())],\"next_cursor\":\"next\"}")
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.reload()
        #expect(model.items.count == 1)
        await model.loadMore()
        #expect(model.items.isEmpty)
        #expect(!model.hasMore)
    }

    @Test func changingFilterDiscardsALatePage() async throws {
        let gate = SynthesisGate()
        defer { gate.release() }
        let api = try synthesisAPI { request in
            if request.url!.query!.contains("kind=merge") { return (200, "{\"items\":[],\"next_cursor\":null}") }
            if request.url!.query!.contains("cursor=") { gate.wait() }
            return (200, "{\"items\":[\(synthesisOperation())],\"next_cursor\":\"next\"}")
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.reload()
        let page = Task { await model.loadMore() }
        while !gate.started { await Task.yield() }
        await model.select(kind: "merge", state: nil)
        gate.release()
        await page.value
        #expect(model.items.isEmpty)
        #expect(!model.hasMore)
        #expect(!model.isLoading)
    }

    @Test(arguments: [false, true])
    func backgroundedWriteKeepsOnlyItsRetryIdentity(connectionChange: Bool) async throws {
        let gate = SynthesisGate()
        let log = SynthesisRequestLog()
        defer { gate.release() }
        let api = try synthesisAPI { request in
            log.add(request)
            if request.httpMethod == "POST" {
                if log.requests.filter({ $0.httpMethod == "POST" }).count == 1 { gate.wait() }
                return (200, synthesisOperation(kind: "merge", state: "undone"))
            }
            return (200, synthesisOperation(kind: "merge"))
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        let save = Task { await model.undo() }
        while !gate.started { await Task.yield() }
        await model.undo() // A second click cannot admit a competing mutation.
        if connectionChange { model.invalidateConnection() } else { model.clearContent() }
        gate.release()
        await save.value
        #expect(model.operation == nil)
        #expect(model.summary == nil)
        #expect(model.canRetryWrite == !connectionChange)
        await model.retryWrite()
        let writes = log.requests.filter { $0.httpMethod == "POST" }
        #expect(writes.count == (connectionChange ? 1 : 2))
        #expect(Set(writes.compactMap { $0.value(forHTTPHeaderField: "Idempotency-Key") }).count == 1)
    }

    @Test(arguments: [401, 403, 404, 405])
    func terminalWriteErrorsClearContentAndDoNotOfferBlindRetry(code: Int) async throws {
        let api = try synthesisAPI { request in
            request.httpMethod == "POST" ? (code, synthesisError(code)) : (200, synthesisOperation(kind: "merge"))
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        await model.undo()
        #expect(model.operation == nil)
        #expect(!model.canRetryWrite)
        #expect(model.errorMessage != nil)
        #expect(model.errorMessage?.contains("SECRET") == false)
    }

    @Test func operationIdentityCannotChangeDuringDetailRead() async throws {
        let api = try synthesisAPI { _ in (200, synthesisOperation(kind: "merge").replacingOccurrences(of: synthesisID.uuidString, with: UUID().uuidString)) }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        #expect(model.operation == nil)
        #expect(model.errorMessage != nil)
    }

    @Test func anOlderServerRejectsDeleteWithoutAnUncertainRetry() async throws {
        let api = try synthesisAPI { request in
            if request.httpMethod == "DELETE" { return (405, synthesisError(405)) }
            if request.url!.path.hasPrefix("/v1/memories/") { return (200, synthesisSummary()) }
            return (200, synthesisOperation())
        }
        let model = MemorySynthesisViewModel(makeAPIClient: { api })
        await model.open(synthesisID)
        await model.deleteSummary()
        #expect(!model.canRetryWrite)
        #expect(model.errorMessage?.contains("does not support") == true)
    }

}

let synthesisID = UUID(uuidString: "00000000-0000-0000-0000-000000003201")!
let summaryID = UUID(uuidString: "00000000-0000-0000-0000-000000003202")!
let synthesisSourceID = UUID(uuidString: "00000000-0000-0000-0000-000000003203")!
let synthesisSourceJSON = """
{"belief_id":"\(synthesisSourceID)","content_revision":1,"subject":"the user","statement":"Enjoys quiet mornings.","session_id":"00000000-0000-0000-0000-000000003204","event_ids":[1],"omitted":false}
"""
let synthesisSourcesJSON = "[\(synthesisSourceJSON),\(synthesisSourceJSON.replacingOccurrences(of: synthesisSourceID.uuidString, with: "00000000-0000-0000-0000-000000003205").replacingOccurrences(of: "false", with: "true"))]"
let synthesisClausesJSON = "[{\"text\":\"Enjoys quiet mornings.\",\"source_ids\":[\"\(synthesisSourceID)\"]}]"
func synthesisOperation(kind: String = "summary", state: String = "committed", revision: Int = 1, visible: Bool = true) -> String {
    let content = "{\"memory_id\":\"\(summaryID)\",\"subject\":\"the user\",\"statement\":\"Enjoys quiet mornings.\",\"clauses\":\(synthesisClausesJSON)}"
    return """
    {"id":"\(synthesisID)","kind":"\(kind)","state":"\(state)","revision":\(revision),"reason":"summarized","policy":"reconsolidation@1","model_identity":"extractive@1","created_at":"2026-10-06T01:00:00Z","committed_at":"2026-10-06T01:00:00Z","content":\(visible ? content : "null"),"sources":\(visible ? synthesisSourcesJSON : "[]")}
    """
}
func synthesisSummary(flagged: Bool = true, visible: Bool = true, portability: String = "portable") -> String {
    let content = """
    {"subject":"the user","statement":"Enjoys quiet mornings.","clauses":\(synthesisClausesJSON),"belief_types":["preference"],"scope":"user","portability":"\(portability)","sensitivity":"restricted","authority":"user","confidence":0.8,"last_evidence_at":"2026-09-01T00:00:00Z","valid_from":"2026-09-01T00:00:00Z","expires_at":null}
    """
    return """
    {"id":"\(summaryID)","record_kind":"summary","operation_id":"\(synthesisID)","revision":1,"status":"\(visible ? "active" : "retired")","flagged_for_review":\(flagged),"created_at":"2026-10-06T01:00:00Z","updated_at":"2026-10-06T01:00:00Z","content":\(visible ? content : "null"),"sources":\(visible ? synthesisSourcesJSON : "[]")}
    """
}

func synthesisAPI(_ handler: @escaping (URLRequest) throws -> (Int, String)) throws -> VeetbotAPIClient {
    let config = URLSessionConfiguration.ephemeral
    config.protocolClasses = [SynthesisTestProtocol.self]
    let key = SynthesisTestProtocol.register(handler)
    config.httpAdditionalHeaders = ["X-Synthesis-Test": key]
    return VeetbotAPIClient(transport: HTTPTransport(
        configuration: try ConnectionConfiguration(baseURLString: "https://veetbot.test"),
        tokenStore: InMemoryTokenStore(token: "test"), session: URLSession(configuration: config)))
}
private final class SynthesisTestProtocol: URLProtocol {
    typealias Handler = (URLRequest) throws -> (Int, String)
    static let lock = NSLock()
    static var handlers: [String: Handler] = [:]
    static func register(_ handler: @escaping Handler) -> String {
        let key = UUID().uuidString
        lock.withLock { handlers[key] = handler }
        return key
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let handler = Self.lock.withLock { Self.handlers[request.value(forHTTPHeaderField: "X-Synthesis-Test") ?? ""] }
        DispatchQueue.global().async { [self] in
            do {
                guard let handler else { throw URLError(.unknown) }
                #expect(request.url!.query!.contains("ceiling=restricted"))
                #expect(request.value(forHTTPHeaderField: "Authorization") == "Bearer test")
                let (code, text) = try handler(request)
                let response = HTTPURLResponse(url: request.url!, statusCode: code, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: Data(text.utf8))
                client?.urlProtocolDidFinishLoading(self)
            } catch { client?.urlProtocol(self, didFailWithError: error) }
        }
    }
    override func stopLoading() {}
}

func synthesisError(_ code: Int) -> String {
    "{\"error\":{\"code\":\"not_found\",\"message\":\"SECRET\",\"details\":{},\"request_id\":\"test\"}}"
}
final class SynthesisRequestLog: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [URLRequest] = []
    var requests: [URLRequest] { lock.withLock { values } }
    @discardableResult func add(_ request: URLRequest) -> Int { lock.withLock { values.append(request); return values.count } }
}
final class SynthesisGate: @unchecked Sendable {
    private let lock = NSLock()
    private var didStart = false
    private let semaphore = DispatchSemaphore(value: 0)
    var started: Bool { lock.withLock { didStart } }
    func wait() { lock.withLock { didStart = true }; semaphore.wait() }
    func release() { semaphore.signal() }
}
private func requestBody(_ request: URLRequest) throws -> Data {
    if let data = request.httpBody { return data }
    guard let stream = request.httpBodyStream else { return Data() }
    stream.open(); defer { stream.close() }
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 4096)
    while stream.hasBytesAvailable {
        let count = stream.read(&buffer, maxLength: buffer.count)
        if count <= 0 { break }
        data.append(buffer, count: count)
    }
    return data
}
