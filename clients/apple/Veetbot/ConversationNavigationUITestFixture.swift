#if DEBUG && os(iOS)
import Foundation

enum ConversationNavigationUITestFixture {
    static let launchArgument = "--ui-testing-conversation-navigation"
    static let firstSessionID = "00000000-0000-0000-0000-000000000123"
    static let secondSessionID = "00000000-0000-0000-0000-000000000456"

    @MainActor
    static func makeModelIfRequested() -> ChatViewModel? {
        guard ProcessInfo.processInfo.arguments.contains(launchArgument) else { return nil }

        let suiteName = "com.veetbot.apple.ui-tests"
        guard let defaults = UserDefaults(suiteName: suiteName) else { return nil }
        defaults.removePersistentDomain(forName: suiteName)
        defaults.set(
            "https://ui-testing.veetbot.invalid",
            forKey: "veetbot.connection.baseURL"
        )

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConversationNavigationUITestURLProtocol.self]
        return ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "ui-test-token"),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: URLSession(configuration: configuration)
        )
    }
}

private final class ConversationNavigationUITestURLProtocol: URLProtocol {
    override static func canInit(with request: URLRequest) -> Bool { true }

    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }

        let body: String
        switch (request.httpMethod, url.path) {
        case ("GET", "/v1/sessions"):
            body = """
                {"items":[\(Self.firstSessionJSON),\(Self.secondSessionJSON)],"next_cursor":null}
                """
        case ("GET", "/v1/sessions/\(ConversationNavigationUITestFixture.firstSessionID)"):
            body = Self.firstSessionJSON
        case (
            "GET",
            "/v1/sessions/\(ConversationNavigationUITestFixture.firstSessionID)/messages"
        ):
            body = """
                {"items":[
                  {"sequence":1,"role":"user","content":[{"type":"text","text":"Historical question"}]},
                  {"sequence":2,"role":"assistant","content":[{"type":"text","text":"Historical answer loaded"}]}
                ],"next_cursor":null}
                """
        case ("GET", "/v1/sessions/\(ConversationNavigationUITestFixture.secondSessionID)"):
            body = Self.secondSessionJSON
        case (
            "GET",
            "/v1/sessions/\(ConversationNavigationUITestFixture.secondSessionID)/messages"
        ):
            body = """
                {"items":[
                  {"sequence":1,"role":"user","content":[{"type":"text","text":"Second historical question"}]},
                  {"sequence":2,"role":"assistant","content":[{"type":"text","text":"Second historical answer loaded"}]}
                ],"next_cursor":null}
                """
        default:
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }

        guard let response = HTTPURLResponse(
            url: url,
            statusCode: 200,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        ) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

    private static let firstSessionJSON = """
        {"id":"\(ConversationNavigationUITestFixture.firstSessionID)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Historical chat","metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:04:00Z","active_run_id":null,"last_run_id":null}
        """

    private static let secondSessionJSON = """
        {"id":"\(ConversationNavigationUITestFixture.secondSessionID)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Second historical chat","metadata":{},"created_at":"2026-08-13T00:00:00Z","updated_at":"2026-08-13T00:04:00Z","active_run_id":null,"last_run_id":null}
        """
}
#endif
