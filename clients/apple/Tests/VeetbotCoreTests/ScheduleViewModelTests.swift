import Foundation
import Testing

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

@testable import VeetbotCore

@Suite(.serialized) @MainActor struct ScheduleViewModelTests {
    @Test
    func testReloadAndLoadMoreKeepUniqueRowsAndStopARepeatedCursor() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000721"))
        let secondID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000722"))
        let lock = NSLock()
        var requestedCursors: [String?] = []
        let model = try makeModel { request in
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "cursor" })?.value
            lock.withLock { requestedCursors.append(cursor) }
            if cursor == nil {
                return Self.response(
                    request,
                    body: #"{"items":[\#(Self.summaryJSON(id: firstID))],"next_cursor":"page-2"}"#
                )
            }
            return Self.response(
                request,
                body: #"{"items":[\#(Self.summaryJSON(id: firstID)),\#(Self.summaryJSON(id: secondID))],"next_cursor":"page-2"}"#
            )
        }

        await model.reload()
        await model.loadMore()
        await model.loadMore()

        #expect(model.items.map(\.id) == [firstID, secondID])
        #expect(lock.withLock { requestedCursors.count } == 2)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testLaterPageFailureRetainsRowsAndRetryContinuesThatPage() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000731"))
        let secondID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000732"))
        let lock = NSLock()
        var pageTwoAttempts = 0
        let model = try makeModel { request in
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "cursor" })?.value
            if cursor == nil {
                return Self.response(
                    request,
                    body: #"{"items":[\#(Self.summaryJSON(id: firstID))],"next_cursor":"page-2"}"#
                )
            }
            let attempt = lock.withLock {
                pageTwoAttempts += 1
                return pageTwoAttempts
            }
            if attempt == 1 {
                return Self.response(
                    request,
                    statusCode: 503,
                    body: #"{"error":{"code":"service_unavailable","message":"Try again.","details":{},"request_id":"page-2"}}"#
                )
            }
            return Self.response(
                request,
                body: #"{"items":[\#(Self.summaryJSON(id: secondID))],"next_cursor":null}"#
            )
        }

        await model.reload()
        await model.loadMore()

        #expect(model.items.map(\.id) == [firstID])
        #expect(model.errorMessage == "Try again.")

        await model.retry()

        #expect(model.items.map(\.id) == [firstID, secondID])
        #expect(model.errorMessage == nil)
    }

    @Test
    func testDetailUsesPointReadAndCanRetryWithoutReplacingTheList() async throws {
        let scheduleID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000741")
        )
        let lock = NSLock()
        var detailAttempts = 0
        let model = try makeModel { request in
            if request.url?.path == "/v1/schedules" {
                return Self.response(
                    request,
                    body: #"{"items":[\#(Self.summaryJSON(id: scheduleID))],"next_cursor":null}"#
                )
            }
            let attempt = lock.withLock {
                detailAttempts += 1
                return detailAttempts
            }
            if attempt == 1 {
                return Self.response(
                    request,
                    statusCode: 503,
                    body: #"{"error":{"code":"service_unavailable","message":"Detail unavailable.","details":{},"request_id":"detail"}}"#
                )
            }
            return Self.response(request, body: Self.detailJSON(id: scheduleID))
        }

        await model.reload()
        await model.loadDetail(scheduleID)

        #expect(model.items.map(\.id) == [scheduleID])
        #expect(model.detailRecords[scheduleID] == nil)
        #expect(model.detailError(for: scheduleID) == "Detail unavailable.")

        await model.retryDetail(scheduleID)

        #expect(model.detailRecords[scheduleID]?.revision.instruction == "Full instruction")
        #expect(model.detailError(for: scheduleID) == nil)
        #expect(model.items.map(\.id) == [scheduleID])
    }

    @Test
    func testMissingConnectionAndOldServerProduceUnavailableState() async throws {
        let missingConnection = ScheduleViewModel(makeAPIClient: { nil })
        await missingConnection.reload()
        #expect(missingConnection.unavailable)

        let oldServer = try makeModel { request in
            Self.response(
                request,
                statusCode: 404,
                body: #"{"error":{"code":"not_found","message":"Not found.","details":{},"request_id":"old"}}"#
            )
        }
        await oldServer.reload()
        #expect(oldServer.unavailable)
        #expect(oldServer.items.isEmpty)
    }

    private func makeModel(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> ScheduleViewModel {
        let configuration = try ConnectionConfiguration(
            baseURLString: "https://schedule-view-model.invalid"
        )
        let handlerID = ScheduleViewModelURLProtocol.register(handler)
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.httpAdditionalHeaders = [
            ScheduleViewModelURLProtocol.handlerHeader: handlerID
        ]
        sessionConfiguration.protocolClasses = [ScheduleViewModelURLProtocol.self]
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: "valid"),
            session: URLSession(configuration: sessionConfiguration)
        )
        let client = VeetbotAPIClient(transport: transport)
        return ScheduleViewModel(makeAPIClient: { client })
    }

    nonisolated private static func response(
        _ request: URLRequest,
        statusCode: Int = 200,
        body: String
    ) -> (HTTPURLResponse, Data) {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: statusCode,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        return (response, Data(body.utf8))
    }

    nonisolated private static func summaryJSON(id: UUID) -> String {
        #"{"id":"\#(id.uuidString)","state":"ACTIVE","pause_reason":null,"current_revision":1,"next_fire_at":"2026-08-30T16:00:00Z","title":"Daily review","instruction_preview":"Preview","cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"}"#
    }

    nonisolated private static func detailJSON(id: UUID) -> String {
        #"{"schedule":{"id":"\#(id.uuidString)","tenant_id":"local","principal_id":"principal","state":"ACTIVE","pause_reason":null,"current_revision":1,"next_fire_at":"2026-08-30T16:00:00Z","consecutive_failures":0,"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"},"revision":{"schedule_id":"\#(id.uuidString)","revision":1,"title":"Daily review","instruction":"Full instruction","agent_id":"00000000-0000-0000-0000-000000000742","agent_version":"1","policy_profile":"default","requested_scopes":[],"limits":{"max_steps":12,"max_model_calls":12,"max_tool_calls":24,"max_input_tokens":null,"max_output_tokens":null,"max_cost":"1","deadline_at":null,"synthesis_reserve_steps":0,"synthesis_reserve_model_calls":0,"synthesis_reserve_cost":"0"},"run_timeout_seconds":300,"cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"timezone":"America/Los_Angeles","misfire_grace_seconds":3600,"max_consecutive_failures":1,"created_by_principal_id":"principal","created_at":"2026-08-29T00:00:00Z"},"replayed":false}"#
    }
}

private final class ScheduleViewModelURLProtocol: URLProtocol {
    static let handlerHeader = "X-Veetbot-Schedule-Test-Handler-ID"
    private static let store = ScheduleViewModelURLProtocolHandlerStore()

    static func register(
        _ handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) -> String {
        store.register(handler)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard
            let handlerID = request.value(forHTTPHeaderField: Self.handlerHeader),
            let handler = Self.store.handler(for: handlerID)
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
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

private final class ScheduleViewModelURLProtocolHandlerStore: @unchecked Sendable {
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
