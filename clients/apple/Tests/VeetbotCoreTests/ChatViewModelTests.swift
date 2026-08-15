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

    private func configuredModel(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> ChatViewModel {
        ChatViewModelURLProtocol.handler = handler
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.protocolClasses = [ChatViewModelURLProtocol.self]
        let session = URLSession(configuration: sessionConfiguration)
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
