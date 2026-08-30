#if DEBUG && os(iOS)
import Foundation

enum ConversationNavigationUITestFixture {
    static let launchArgument = "--ui-testing-conversation-navigation"
    static let firstSessionID = "00000000-0000-0000-0000-000000000123"
    static let secondSessionID = "00000000-0000-0000-0000-000000000456"
    static let memoryID = "00000000-0000-0000-0000-000000000321"
    static let scheduleID = "00000000-0000-0000-0000-000000000654"

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

    @MainActor
    static func makeMemoryAPIClientIfRequested() -> VeetbotAPIClient? {
        guard ProcessInfo.processInfo.arguments.contains(launchArgument) else { return nil }
        guard
            let configuration = try? ConnectionConfiguration(
                baseURLString: "https://ui-testing.veetbot.invalid"
            )
        else { return nil }

        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.protocolClasses = [ConversationNavigationUITestURLProtocol.self]
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: "ui-test-token"),
            session: URLSession(configuration: sessionConfiguration)
        )
        return VeetbotAPIClient(transport: transport)
    }

    @MainActor
    static func makeScheduleAPIClientIfRequested() -> VeetbotAPIClient? {
        makeMemoryAPIClientIfRequested()
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
        let statusCode: Int
        switch (request.httpMethod, url.path) {
        case ("GET", "/v1/sessions"):
            statusCode = 200
            body = """
                {"items":[\(Self.firstSessionJSON),\(Self.secondSessionJSON)],"next_cursor":null}
                """
        case ("GET", "/v1/sessions/\(ConversationNavigationUITestFixture.firstSessionID)"):
            statusCode = 200
            body = Self.firstSessionJSON
        case (
            "GET",
            "/v1/sessions/\(ConversationNavigationUITestFixture.firstSessionID)/messages"
        ):
            statusCode = 200
            body = """
                {"items":[
                  {"sequence":1,"role":"user","content":[{"type":"text","text":"Historical question"}]},
                  {"sequence":2,"role":"assistant","content":[{"type":"text","text":"Historical answer loaded"}]}
                ],"next_cursor":null}
                """
        case (
            "POST",
            "/v1/sessions/\(ConversationNavigationUITestFixture.firstSessionID)/messages"
        ):
            statusCode = 202
            body =
                "{\"run_id\":\"\(Self.runID)\",\"status\":\"QUEUED\"}"
        case ("GET", "/v1/runs/\(Self.runID)/events"):
            statusCode = 200
            body = """
                id: 1
                event: run.completed
                data: {"run_id":"\(Self.runID)"}

                """
        case ("GET", "/v1/sessions/\(ConversationNavigationUITestFixture.secondSessionID)"):
            statusCode = 200
            body = Self.secondSessionJSON
        case (
            "GET",
            "/v1/sessions/\(ConversationNavigationUITestFixture.secondSessionID)/messages"
        ):
            statusCode = 200
            body = """
                {"items":[
                  {"sequence":1,"role":"user","content":[{"type":"text","text":"Second historical question"}]},
                  {"sequence":2,"role":"assistant","content":[{"type":"text","text":"Second historical answer loaded"}]}
                ],"next_cursor":null}
                """
        case ("GET", "/v1/memories"):
            let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            guard query.contains(URLQueryItem(name: "ceiling", value: "restricted")) else {
                client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
                return
            }
            statusCode = 200
            body = """
                {"items":[\(Self.memoryJSON)],"next_cursor":null}
                """
        case ("GET", "/v1/schedules"):
            statusCode = 200
            body = """
                {"items":[\(Self.scheduleSummaryJSON)],"next_cursor":null}
                """
        case ("GET", "/v1/schedules/\(ConversationNavigationUITestFixture.scheduleID)"):
            statusCode = 200
            body = Self.scheduleDetailJSON
        case ("GET", "/v1/browser-profiles"):
            statusCode = 200
            body = #"{"items":[],"next_cursor":null}"#
        case ("POST", "/v1/browser-profiles"):
            statusCode = 201
            body = Self.browserProfileJSON
        case (
            "POST",
            "/v1/browser-profiles/\(Self.browserProfileID)/authentication-ceremonies"
        ):
            statusCode = 201
            body = """
                {"id":"\(Self.authenticationID)","profile_id":"\(Self.browserProfileID)","status":"authentication_required","expires_at":"2026-08-23T12:05:00Z","launch_url":"https://browser.example/authentication/\(Self.authenticationID)#capability=opaque"}
                """
        default:
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }

        guard let response = HTTPURLResponse(
            url: url,
            statusCode: statusCode,
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

    private static let memoryJSON = """
        {"id":"\(ConversationNavigationUITestFixture.memoryID)","subject":"the user","statement":"The user prefers dark mode.","belief_type":"preference","status":"active","polarity":"assert","scope":"session","portability":"portable","authority":"user","sensitivity":"restricted","confidence":0.87,"corroboration_count":3,"flagged_for_review":false,"conflicts_with":[],"superseded_by":null,"source_session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","source_event_ids":[10,11],"formation_run_id":"00000000-0000-0000-0000-000000000900","consolidation_policy_version":"formation@1","origin_scopes":["session"],"valid_from":"2026-08-01T00:00:00Z","valid_to":null,"expires_at":null,"last_reinforced_at":"2026-08-15T00:00:00Z","created_at":"2026-07-01T00:00:00Z","updated_at":"2026-08-20T00:00:00Z"}
        """

    private static let scheduleSummaryJSON = """
        {"id":"\(ConversationNavigationUITestFixture.scheduleID)","state":"ACTIVE","pause_reason":null,"current_revision":1,"next_fire_at":"2026-08-30T16:00:00Z","title":"Daily review","instruction_preview":"Preview from the schedule index.","cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"}
        """

    private static let scheduleDetailJSON = """
        {"schedule":{"id":"\(ConversationNavigationUITestFixture.scheduleID)","tenant_id":"local","principal_id":"principal","state":"ACTIVE","pause_reason":null,"current_revision":1,"next_fire_at":"2026-08-30T16:00:00Z","consecutive_failures":0,"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"},"revision":{"schedule_id":"\(ConversationNavigationUITestFixture.scheduleID)","revision":1,"title":"Daily review","instruction":"Full instruction from the schedule point read.","agent_id":"00000000-0000-0000-0000-000000000655","agent_version":"1","policy_profile":"default","requested_scopes":[],"limits":{"max_steps":12,"max_model_calls":12,"max_tool_calls":24,"max_input_tokens":null,"max_output_tokens":null,"max_cost":"1","deadline_at":null,"synthesis_reserve_steps":0,"synthesis_reserve_model_calls":0,"synthesis_reserve_cost":"0"},"run_timeout_seconds":300,"cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"timezone":"America/Los_Angeles","misfire_grace_seconds":3600,"max_consecutive_failures":1,"created_by_principal_id":"principal","created_at":"2026-08-29T00:00:00Z"},"replayed":false}
        """

    private static let browserProfileID = "00000000-0000-0000-0000-000000000789"
    private static let authenticationID = "00000000-0000-0000-0000-000000000790"
    private static let runID = "00000000-0000-0000-0000-000000000791"
    private static let browserProfileJSON = """
        {"id":"\(browserProfileID)","allowed_origins":["https://example.org"],"status":"authentication_required","generation":1,"created_at":"2026-08-23T12:00:00Z","updated_at":"2026-08-23T12:00:00Z","last_used_at":null}
        """
}
#endif
