import Foundation
import Testing

@testable import VeetbotCore

@Suite(.serialized) @MainActor struct ChatViewModelTests {
    @Test
    func testConfigureReportsTheCurrentAttemptFailure() async {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "existing-token"),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore()
        )

        let configured = await model.configure(
            baseURLString: "not a server URL",
            token: "replacement-token"
        )

        #expect(configured == false)
        #expect(model.errorMessage != nil)
    }

    @Test
    func testHistoryPaginationHasNoArbitraryPageCapAndRejectsLoops() throws {
        var seen: Set<String> = []

        for page in 1 ... 101 {
            let cursor = "cursor-\(page)"
            #expect(try ChatViewModel.nextHistoryCursor(cursor, seen: &seen) == cursor)
        }
        #expect(seen.count == 101)
        #expect(throws: HTTPTransportError.self) {
            try ChatViewModel.nextHistoryCursor("cursor-101", seen: &seen)
        }
        #expect(try ChatViewModel.nextHistoryCursor(nil, seen: &seen) == nil)
    }

    @Test
    func testConfigureFailsWhenInitialHistoryNeedsAServerUpgrade() async throws {
        let model = try configuredModel { request in
            try response(
                for: request,
                statusCode: 400,
                body: #"{"error":{"code":"malformed_request","message":"The HTTP request is not supported.","details":{},"request_id":"old-server"}}"#
            )
        }
        defer { ChatViewModelURLProtocol.handler = nil }

        let configured = await model.configure(
            baseURLString: "https://veetbot.test",
            token: "replacement-token"
        )

        #expect(configured == false)
        #expect(model.isConfigured == false)
        #expect(model.baseURL == nil)
        #expect(model.errorMessage?.contains("Update the server") == true)
    }

    @Test
    func testConfigureFailsWhenInitialHistoryRequiresReauthentication() async throws {
        let model = try configuredModel { request in
            try response(
                for: request,
                statusCode: 401,
                body: #"{"error":{"code":"authentication_error","message":"expired","details":{},"request_id":"expired-token"}}"#
            )
        }
        defer { ChatViewModelURLProtocol.handler = nil }

        let configured = await model.configure(
            baseURLString: "https://veetbot.test",
            token: "replacement-token"
        )

        #expect(configured == false)
        #expect(model.isConfigured == false)
        #expect(model.requiresReauthentication == true)
        #expect(model.errorMessage == "expired")
    }

    @Test
    func testConfigureFailsOnAnyOtherInitialHistoryError() async throws {
        let model = try configuredModel { request in
            try response(
                for: request,
                statusCode: 500,
                body: #"{"error":{"code":"internal_error","message":"temporarily unavailable","details":{},"request_id":"server-error"}}"#
            )
        }
        defer { ChatViewModelURLProtocol.handler = nil }

        let configured = await model.configure(
            baseURLString: "https://veetbot.test",
            token: "replacement-token"
        )

        #expect(configured == false)
        #expect(model.isConfigured == false)
        #expect(model.baseURL == nil)
        #expect(model.errorMessage == "temporarily unavailable")
    }

    @Test
    func testSuccessfulServerDeleteRemovesVisibleRowWhenCacheDeleteFails() async throws {
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000123")
        )
        let store = DeleteFailingHistoryStore()
        let session = urlSession { request in
            if request.httpMethod == HTTPMethod.delete.rawValue {
                return try response(for: request, statusCode: 204, body: "")
            }
            return try response(
                for: request,
                statusCode: 200,
                body: """
                    {"items":[{"id":"\(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Delete me","metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:01:00Z","active_run_id":null,"last_run_id":null}],"next_cursor":null}
                    """
            )
        }
        defer { ChatViewModelURLProtocol.handler = nil }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(
                defaults: try #require(UserDefaults(suiteName: "com.veetbot.tests.\(UUID())"))
            ),
            historyStore: store,
            urlSession: session
        )
        #expect(
            await model.configure(
                baseURLString: "https://veetbot.test",
                token: "replacement-token"
            )
        )
        let entry = try #require(model.history.first)

        await model.deleteSessionEverywhere(entry)

        #expect(model.history.isEmpty)
        #expect(model.errorMessage == DeleteFailingHistoryStore.message)
        #expect(await store.list().map(\.sessionID) == [sessionID])
    }

    private func configuredModel(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> ChatViewModel {
        let session = urlSession(handler: handler)
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        return ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: session
        )
    }

    private func urlSession(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) -> URLSession {
        ChatViewModelURLProtocol.handler = handler
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.protocolClasses = [ChatViewModelURLProtocol.self]
        return URLSession(configuration: sessionConfiguration)
    }

    private func response(
        for request: URLRequest,
        statusCode: Int,
        body: String
    ) throws -> (HTTPURLResponse, Data) {
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
}

private enum DeleteFailingHistoryStoreError: Error, LocalizedError {
    case syntheticFailure

    var errorDescription: String? { DeleteFailingHistoryStore.message }
}

private actor DeleteFailingHistoryStore: SessionHistoryStore {
    static let message = "Synthetic history deletion failure."
    private var entries: [UUID: SessionHistoryEntry] = [:]

    func list() -> [SessionHistoryEntry] {
        entries.values.sortedForHistoryList()
    }

    func upsert(_ entry: SessionHistoryEntry) {
        entries[entry.sessionID] = entry
    }

    func delete(sessionID: UUID) throws {
        throw DeleteFailingHistoryStoreError.syntheticFailure
    }
}

private final class ChatViewModelURLProtocol: URLProtocol {
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
