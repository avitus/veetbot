import Foundation
import Testing

@testable import VeetbotCore

// ADR-0255 decisions 4 and 5: Settings → MCP servers. The wire views and
// bodies, the sign-in callback, the paste parser and the form's checks, the
// six requests, and the view model's add, access, remove and sign-in flows,
// including the calm state a server without the feature answers with.

private enum MCPFixture {
    static let magicURL = "https://api.magicpath.ai/mcp"

    static func view(
        _ id: String,
        url: String? = MCPFixture.magicURL,
        managedBy: String = "owner",
        access: String = "changes",
        authentication: String = "sign_in",
        status: String = "sign_in_required",
        signInHost: String? = "auth.magicpath.ai",
        signedInAt: String? = nil
    ) -> String {
        func json(_ value: String?) -> String { value.map { "\"\($0)\"" } ?? "null" }
        return #"{"server_id":"\#(id)","url":\#(json(url)),"managed_by":"\#(managedBy)","access":"\#(access)","authentication":"\#(authentication)","status":"\#(status)","sign_in_host":\#(json(signInHost)),"signed_in_at":\#(json(signedInAt))}"#
    }

    static func builtIn(_ id: String) -> String {
        view(
            id, url: nil, managedBy: "operator", access: "read_only", authentication: "operator",
            status: "ready", signInHost: nil
        )
    }

    static func list(_ views: [String]) -> String {
        #"{"servers":[\#(views.joined(separator: ","))]}"#
    }

    static func error(_ code: String, _ message: String = "refused") -> String {
        #"{"error":{"code":"\#(code)","message":"\#(message)","details":{},"request_id":"t"}}"#
    }

    static let notMounted = error("not_found", "The requested resource was not found.")

    static func signInStart(_ id: String) -> String {
        #"{"authorization_url":"https://auth.magicpath.ai/authorize?client_id=c&state=s&server=\#(id)","callback_scheme":"veetbot","expires_at":"2026-10-26T12:10:00Z"}"#
    }

    static func client(_ server: DeviceFlowServer) throws -> VeetbotAPIClient {
        let configuration = try ConnectionConfiguration(baseURLString: "https://veetbot.test")
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: "test-token"),
            session: URLSession(configuration: server.configuration(base: .ephemeral))
        )
        return VeetbotAPIClient(transport: transport)
    }
}

/// A value the stub server's handler reads and the test changes.
private final class Box<Value>: @unchecked Sendable {
    private let lock = NSLock()
    private var stored: Value

    init(_ value: Value) { stored = value }

    var value: Value {
        get { lock.withLock { stored } }
        set { lock.withLock { stored = newValue } }
    }
}

@MainActor
private final class FakeSignInPresenter: MCPSignInPresenting {
    var result: Result<URL, Error>
    private(set) var requests: [(url: URL, scheme: String)] = []

    init(result: Result<URL, Error>) { self.result = result }

    func authenticate(url: URL, callbackScheme: String) async throws -> URL {
        requests.append((url, callbackScheme))
        return try result.get()
    }
}

// MARK: - Wire views, callback, parser and checks

@Suite struct MCPServerWireTests {
    @Test
    func testAnOwnerViewDecodesEveryField() throws {
        let json = MCPFixture.view(
            "magicpath", status: "ready", signedInAt: "2026-10-26T09:30:00Z"
        )
        let view = try JSONDecoder.server.decode(MCPServerView.self, from: Data(json.utf8))

        #expect(view.id == "magicpath")
        #expect(view.url == MCPFixture.magicURL)
        #expect(view.host == "api.magicpath.ai")
        #expect(view.managedBy == .owner)
        #expect(view.isOwnerManaged)
        #expect(view.access == .changes)
        #expect(view.authentication == .signIn)
        #expect(view.usesSignIn)
        #expect(view.status == .ready)
        #expect(view.signInHost == "auth.magicpath.ai")
        #expect(view.signedInAt == ISO8601DateFormatter().date(from: "2026-10-26T09:30:00Z"))
    }

    @Test
    func testAnOperatorRowHasNoURLAndNoSignIn() throws {
        let list = try JSONDecoder.server.decode(
            MCPServerList.self, from: Data(MCPFixture.list([MCPFixture.builtIn("gmail_personal")]).utf8)
        )
        let view = try #require(list.servers.first)

        #expect(view.url == nil)
        #expect(view.host == nil)
        #expect(view.signInHost == nil)
        #expect(view.signedInAt == nil)
        #expect(view.managedBy == .operatorManaged)
        #expect(!view.isOwnerManaged)
        #expect(view.authentication == .operatorManaged)
        #expect(view.access == .readOnly)
    }

    @Test
    func testUnknownValuesDecodeWithoutFailingTheList() throws {
        let json = MCPFixture.view(
            "future", managedBy: "tenant", access: "ask_once", authentication: "api_key",
            status: "degraded"
        )
        let view = try JSONDecoder.server.decode(MCPServerView.self, from: Data(json.utf8))

        #expect(view.managedBy == .unknown("tenant"))
        #expect(view.access == .unknown("ask_once"))
        #expect(view.authentication == .unknown("api_key"))
        #expect(view.status == .unknown("degraded"))
        #expect(!view.isOwnerManaged)
    }

    @Test
    func testTheSignInStartDecodes() throws {
        let start = try JSONDecoder.server.decode(
            MCPSignInStart.self, from: Data(MCPFixture.signInStart("magicpath").utf8)
        )

        #expect(start.authorizationURL.host == "auth.magicpath.ai")
        #expect(start.callbackScheme == "veetbot")
        #expect(start.expiresAt == ISO8601DateFormatter().date(from: "2026-10-26T12:10:00Z"))
    }

    @Test
    func testTheBodiesCarryExactlyTheContractsKeys() throws {
        let add = try JSONSerialization.jsonObject(
            with: JSONEncoder.server.encode(
                AddMCPServerBody(serverID: "magicpath", url: MCPFixture.magicURL, access: .readOnly)
            )
        ) as? [String: String]
        #expect(add == ["server_id": "magicpath", "url": MCPFixture.magicURL, "access": "read_only"])

        let patch = try JSONSerialization.jsonObject(
            with: JSONEncoder.server.encode(UpdateMCPServerAccessBody(access: .changes))
        ) as? [String: String]
        #expect(patch == ["access": "changes"])
    }

    @Test(arguments: [
        ("veetbot://mcp-sign-in?server_id=magicpath&outcome=signed_in", MCPSignInOutcome.signedIn),
        ("veetbot://mcp-sign-in?server_id=magicpath&outcome=failed", .failed),
        ("veetbot://mcp-sign-in?outcome=expired", .expired),
        ("veetbot://mcp-sign-in?server_id=magicpath&outcome=later", .unknown("later")),
        ("veetbot://mcp-sign-in?server_id=magicpath", .unknown(nil)),
    ])
    func testTheCallbackOutcomeIsRead(url: String, outcome: MCPSignInOutcome) throws {
        let parsed = try #require(URL(string: url))
        let callback = try #require(MCPSignInCallback.parse(parsed))

        #expect(callback.outcome == outcome)
    }

    @Test
    func testTheCallbackNamesItsServer() throws {
        let url = try #require(URL(string: "veetbot://mcp-sign-in?server_id=magicpath&outcome=signed_in"))

        #expect(MCPSignInCallback.parse(url)?.serverID == "magicpath")
    }

    @Test(arguments: [
        "https://mcp-sign-in?server_id=magicpath&outcome=signed_in",
        "veetbot://device-sign-in?outcome=signed_in",
        "other://mcp-sign-in?outcome=signed_in",
    ])
    func testAnotherURLIsNotTheCallback(url: String) throws {
        let parsed = try #require(URL(string: url))
        #expect(MCPSignInCallback.parse(parsed) == nil)
    }

    @Test
    func testAClaudeStyleConfigurationGivesItsServer() {
        let parsed = MCPServerConfiguration.parse(
            #"{"mcpServers": {"magicpath": {"url": "https://api.magicpath.ai/mcp"}}}"#
        )

        #expect(parsed == .success([MCPServerDraft(name: "magicpath", url: MCPFixture.magicURL)]))
    }

    @Test
    func testSeveralServersArriveInNameOrderWithNormalizedNames() {
        let parsed = MCPServerConfiguration.parse(
            """
            {"mcpServers": {
              "Magic Path": {"type": "http", "url": "https://api.magicpath.ai/mcp"},
              "linear-app": {"url": " https://mcp.linear.app/mcp ", "headers": {}}
            }}
            """
        )

        #expect(
            parsed
                == .success([
                    MCPServerDraft(name: "linear_app", url: "https://mcp.linear.app/mcp"),
                    MCPServerDraft(name: "magic_path", url: MCPFixture.magicURL),
                ])
        )
    }

    @Test
    func testTheServersMapAndOneUnnamedServerAreReadToo() {
        #expect(
            MCPServerConfiguration.parse(#"{"servers": {"docs": {"url": "https://docs.example.com/mcp"}}}"#)
                == .success([MCPServerDraft(name: "docs", url: "https://docs.example.com/mcp")])
        )
        #expect(
            MCPServerConfiguration.parse(#"{"url": "https://api.magicpath.ai/mcp"}"#)
                == .success([MCPServerDraft(name: "magicpath", url: MCPFixture.magicURL)])
        )
        #expect(
            MCPServerConfiguration.parse(#"{"magicpath": {"url": "https://api.magicpath.ai/mcp"}}"#)
                == .success([MCPServerDraft(name: "magicpath", url: MCPFixture.magicURL)])
        )
    }

    @Test(arguments: [
        ("not json", MCPServerConfigurationError.notJSON),
        ("", .notJSON),
        (#"["https://api.magicpath.ai/mcp"]"#, .notJSON),
        (#"{"mcpServers": {}}"#, .noServers),
        (#"{"mcpServers": {"magicpath": {"type": "http"}}}"#, .missingURL("magicpath")),
        (#"{"mcpServers": {"files": {"command": "npx", "args": ["server"]}}}"#, .localCommand("files")),
        (#"{"mcpServers": {"magicpath": "https://api.magicpath.ai/mcp"}}"#, .notJSON),
    ])
    func testAConfigurationWithoutAUsableServerIsRefused(
        text: String, error: MCPServerConfigurationError
    ) {
        #expect(MCPServerConfiguration.parse(text) == .failure(error))
    }

    @Test(arguments: [
        ("MagicPath", "magicpath"),
        ("Magic Path!", "magic_path"),
        (" my-server ", "my_server"),
        ("__docs__", "docs"),
        ("123tools", "mcp_123tools"),
        ("Café", "caf"),
        ("", ""),
        ("!!!", ""),
        (String(repeating: "a", count: 40), String(repeating: "a", count: 32)),
    ])
    func testNamesAreNormalizedToTheContractsGrammar(raw: String, normalized: String) {
        #expect(MCPServerInput.normalizedName(raw) == normalized)
    }

    @Test
    func testTheFormChecksNamesBeforeSending() {
        #expect(MCPServerInput.validateName("magicpath") == nil)
        #expect(MCPServerInput.validateName("a") == nil)
        #expect(MCPServerInput.validateName("") == .missingName)
        #expect(MCPServerInput.validateName("Magic") == .invalidName)
        #expect(MCPServerInput.validateName("9lives") == .invalidName)
        #expect(MCPServerInput.validateName("my-server") == .invalidName)
        #expect(MCPServerInput.validateName(String(repeating: "a", count: 33)) == .invalidName)
        #expect(MCPServerInput.validateName("gmail_work") == .reservedName)
        #expect(MCPServerInput.validateName("bland_calls") == .reservedName)
        #expect(MCPServerInput.validateName("svp_deals") == .reservedName)
        #expect(MCPServerInput.validateName("gmail") == nil)
    }

    @Test(arguments: [
        ("https://api.magicpath.ai/mcp", nil),
        ("https://api.magicpath.ai:443/mcp", nil),
        ("https://api.magicpath.ai/mcp?team=1", nil),
        ("", MCPServerInputError.missingURL),
        ("api.magicpath.ai/mcp", .invalidURL),
        ("http://api.magicpath.ai/mcp", .httpsRequired),
        ("https://user:secret@api.magicpath.ai/mcp", .credentialsNotAllowed),
        ("https://api.magicpath.ai/mcp#tools", .fragmentNotAllowed),
        ("https://api.magicpath.ai:8443/mcp", .portNotAllowed),
    ] as [(String, MCPServerInputError?)])
    func testTheFormChecksURLsBeforeSending(url: String, error: MCPServerInputError?) {
        #expect(MCPServerInput.validateURL(url) == error)
    }

    @Test
    func testANameThatCouldLeaveItsPathSegmentIsNeverSent() throws {
        #expect(try mcpServerPath("magicpath") == "/v1/mcp-servers/magicpath")
        #expect(throws: MCPServerClientError.invalidServerID) { try mcpServerPath("../settings") }
        #expect(throws: MCPServerClientError.invalidServerID) { try mcpServerPath("a/b") }
        #expect(throws: MCPServerClientError.invalidServerID) { try mcpServerPath("a%2Fb") }
    }
}

// MARK: - The six requests

@Suite(.serialized) struct MCPServerAPIClientTests {
    @Test
    func testListingReadsTheCollection() async throws {
        let server = DeviceFlowServer { _ in (200, MCPFixture.list([MCPFixture.view("magicpath")])) }

        let list = try await MCPFixture.client(server).listMCPServers()

        #expect(server.routes == ["GET veetbot.test /v1/mcp-servers"])
        #expect(list.servers.map(\.id) == ["magicpath"])
    }

    @Test
    func testAddingPostsTheNameURLAndAccessAndReportsANewServer() async throws {
        let server = DeviceFlowServer { _ in (201, MCPFixture.view("magicpath")) }

        let result = try await MCPFixture.client(server).addMCPServer(
            serverID: "magicpath", url: MCPFixture.magicURL, access: .readOnly
        )

        #expect(server.routes == ["POST veetbot.test /v1/mcp-servers"])
        let body = try #require(server.json(of: "POST veetbot.test /v1/mcp-servers") as? [String: String])
        #expect(body == ["server_id": "magicpath", "url": MCPFixture.magicURL, "access": "read_only"])
        #expect(result.created)
        #expect(result.server.id == "magicpath")
    }

    @Test
    func testAnIdenticalReplayIsNotANewServer() async throws {
        let server = DeviceFlowServer { _ in (200, MCPFixture.view("magicpath")) }

        let result = try await MCPFixture.client(server).addMCPServer(
            serverID: "magicpath", url: MCPFixture.magicURL, access: .changes
        )

        #expect(!result.created)
    }

    @Test
    func testChangingAccessPatchesOnlyTheAccess() async throws {
        let server = DeviceFlowServer { _ in (200, MCPFixture.view("magicpath", access: "read_only")) }

        let view = try await MCPFixture.client(server).updateMCPServerAccess("magicpath", access: .readOnly)

        #expect(server.routes == ["PATCH veetbot.test /v1/mcp-servers/magicpath"])
        let body = try #require(server.json(of: "PATCH veetbot.test /v1/mcp-servers/magicpath") as? [String: String])
        #expect(body == ["access": "read_only"])
        #expect(view.access == .readOnly)
    }

    @Test
    func testRemovingSigningInAndSigningOutUseTheServersPath() async throws {
        let server = DeviceFlowServer { request in
            request.httpMethod == "POST" ? (200, MCPFixture.signInStart("magicpath")) : (204, "")
        }
        let client = try MCPFixture.client(server)

        let start = try await client.beginMCPServerSignIn("magicpath")
        try await client.signOutOfMCPServer("magicpath")
        try await client.removeMCPServer("magicpath")

        #expect(
            server.routes == [
                "POST veetbot.test /v1/mcp-servers/magicpath/sign-in",
                "DELETE veetbot.test /v1/mcp-servers/magicpath/sign-in",
                "DELETE veetbot.test /v1/mcp-servers/magicpath",
            ]
        )
        #expect(server.entry(of: "POST veetbot.test /v1/mcp-servers/magicpath/sign-in")?.body.isEmpty == true)
        #expect(start.callbackScheme == "veetbot")
    }

    @Test
    func testAServerWithoutTheFeatureIsUnavailableNotAnError() async throws {
        let server = DeviceFlowServer { _ in (404, MCPFixture.notMounted) }
        let client = try MCPFixture.client(server)

        await #expect(throws: MCPServerClientError.unavailable) { try await client.listMCPServers() }
        await #expect(throws: MCPServerClientError.unavailable) {
            try await client.addMCPServer(serverID: "magicpath", url: MCPFixture.magicURL, access: .changes)
        }
    }

    @Test
    func testAnUnknownServerStaysAnAPIErrorOnItsOwnRoute() async throws {
        let server = DeviceFlowServer { _ in (404, MCPFixture.error("not_found", "No such MCP server.")) }

        do {
            _ = try await MCPFixture.client(server).updateMCPServerAccess("gone", access: .readOnly)
            Issue.record("expected a 404")
        } catch HTTPTransportError.api(let error) {
            #expect(error.statusCode == 404)
        }
    }
}

// MARK: - The view model

@Suite(.serialized) @MainActor struct MCPServerSettingsViewModelTests {
    private func makeModel(
        _ server: DeviceFlowServer,
        presenter: FakeSignInPresenter? = nil
    ) throws -> (MCPServerSettingsViewModel, ToastCenter) {
        let presenter = presenter ?? FakeSignInPresenter(result: .failure(MCPSignInPresentationError.cancelled))
        let client = try MCPFixture.client(server)
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = MCPServerSettingsViewModel(
            makeAPIClient: { client }, presenter: presenter, toasts: toasts
        )
        return (model, toasts)
    }

    @Test
    func testLoadingListsOwnerServersFirstThenBuiltInOnes() async throws {
        let server = DeviceFlowServer { _ in
            (200, MCPFixture.list([
                MCPFixture.builtIn("gmail_personal"), MCPFixture.view("zeta"), MCPFixture.view("alpha"),
            ]))
        }
        let (model, _) = try makeModel(server)

        await model.load()

        #expect(model.servers.map(\.id) == ["alpha", "zeta", "gmail_personal"])
        #expect(model.hasLoaded)
        #expect(!model.notEnabled)
        #expect(model.errorMessage == nil)
        #expect(model.ownerServerCount == 2)
        #expect(model.canAddMore)
    }

    @Test
    func testAServerWithoutTheFeatureShowsTheCalmState() async throws {
        let server = DeviceFlowServer { _ in (404, MCPFixture.notMounted) }
        let (model, _) = try makeModel(server)

        await model.load()

        #expect(model.notEnabled)
        #expect(model.servers.isEmpty)
        #expect(model.errorMessage == nil)
        #expect(MCPServerSettingsViewModel.notEnabledMessage == "MCP servers aren't enabled on this Veetbot server.")
    }

    @Test
    func testAMissingScopeSaysWhichScopes() async throws {
        let server = DeviceFlowServer { _ in (403, MCPFixture.error("authorization_error")) }
        let (model, _) = try makeModel(server)

        await model.load()

        #expect(model.errorMessage == MCPServerSettingsViewModel.scopeMessage)
        #expect(!model.notEnabled)
    }

    @Test
    func testAddingANewServerListsItAndConfirms() async throws {
        let server = DeviceFlowServer { request in
            request.httpMethod == "POST" ? (201, MCPFixture.view("magicpath")) : (200, MCPFixture.list([]))
        }
        let (model, toasts) = try makeModel(server)
        await model.load()

        let added = await model.add(name: "MagicPath", url: " https://api.magicpath.ai/mcp ", access: .changes)

        #expect(added)
        let body = try #require(server.json(of: "POST veetbot.test /v1/mcp-servers") as? [String: String])
        #expect(body == ["server_id": "magicpath", "url": MCPFixture.magicURL, "access": "changes"])
        #expect(model.servers.map(\.id) == ["magicpath"])
        #expect(model.addErrorMessage == nil)
        #expect(toasts.current?.text == "Added magicpath. Sign in to use it.")
    }

    @Test
    func testAnIdenticalReplaySaysTheServerIsAlreadyAdded() async throws {
        let existing = MCPFixture.view("magicpath")
        let server = DeviceFlowServer { request in
            request.httpMethod == "POST" ? (200, existing) : (200, MCPFixture.list([existing]))
        }
        let (model, toasts) = try makeModel(server)
        await model.load()

        let added = await model.add(name: "magicpath", url: MCPFixture.magicURL, access: .changes)

        #expect(added)
        #expect(model.servers.map(\.id) == ["magicpath"])
        #expect(toasts.current?.text == "magicpath is already added.")
    }

    @Test
    func testATakenNameIsRefusedWithoutClaimingSuccess() async throws {
        let server = DeviceFlowServer { request in
            request.httpMethod == "POST" ? (409, MCPFixture.error("conflict")) : (200, MCPFixture.list([]))
        }
        let (model, toasts) = try makeModel(server)
        await model.load()

        let added = await model.add(name: "magicpath", url: MCPFixture.magicURL, access: .changes)

        #expect(!added)
        #expect(model.servers.isEmpty)
        #expect(model.addErrorMessage == "The name magicpath is already used or reserved. Choose another name.")
        #expect(toasts.current == nil)
    }

    @Test
    func testAConflictAtSixteenServersIsTheLimit() async throws {
        let sixteen = (1...16).map { MCPFixture.view("server_\($0)") }
        let server = DeviceFlowServer { request in
            request.httpMethod == "POST" ? (409, MCPFixture.error("conflict")) : (200, MCPFixture.list(sixteen))
        }
        let (model, _) = try makeModel(server)
        await model.load()

        let added = await model.add(name: "magicpath", url: MCPFixture.magicURL, access: .changes)

        #expect(!added)
        #expect(!model.canAddMore)
        #expect(model.addErrorMessage == "You can add up to 16 MCP servers. Remove one to add another.")
    }

    @Test(arguments: [
        (502, "mcp_server_unavailable",
         "Veetbot couldn't reach api.magicpath.ai. Check the URL and try again."),
        (422, "mcp_sign_in_unsupported",
         "api.magicpath.ai asks for a sign-in Veetbot can't do yet, such as an API key. The server wasn't added."),
        (400, "malformed_request",
         "The server didn't accept that name or URL. Check both and try again."),
    ])
    func testAFailedAddSaysWhy(status: Int, code: String, message: String) async throws {
        let server = DeviceFlowServer { request in
            request.httpMethod == "POST" ? (status, MCPFixture.error(code)) : (200, MCPFixture.list([]))
        }
        let (model, toasts) = try makeModel(server)
        await model.load()

        let added = await model.add(name: "magicpath", url: MCPFixture.magicURL, access: .changes)

        #expect(!added)
        #expect(model.addErrorMessage == message)
        #expect(model.servers.isEmpty)
        #expect(toasts.current == nil)
        #expect(server.routes.filter { $0.hasPrefix("POST") }.count == 1, "an add is never retried")
    }

    @Test
    func testAnInvalidNameOrURLIsRefusedBeforeSending() async throws {
        let server = DeviceFlowServer { _ in (200, MCPFixture.list([])) }
        let (model, _) = try makeModel(server)
        await model.load()

        #expect(!(await model.add(name: "gmail_work", url: MCPFixture.magicURL, access: .changes)))
        #expect(model.addErrorMessage == MCPServerInputError.reservedName.message)
        #expect(!(await model.add(name: "magicpath", url: "http://api.magicpath.ai/mcp", access: .changes)))
        #expect(model.addErrorMessage == MCPServerInputError.httpsRequired.message)
        #expect(!(await model.add(name: "!!!", url: MCPFixture.magicURL, access: .changes)))
        #expect(model.addErrorMessage == MCPServerInputError.missingName.message)
        #expect(!server.routes.contains { $0.hasPrefix("POST") })
    }

    @Test
    func testAPastedConfigurationAddsEachServerWithTheChosenAccess() async throws {
        let server = DeviceFlowServer { request in
            guard request.httpMethod == "POST" else { return (200, MCPFixture.list([])) }
            let body = try JSONSerialization.jsonObject(with: Self.body(of: request)) as? [String: String]
            return (201, MCPFixture.view(body?["server_id"] ?? "", access: "read_only", authentication: "none", status: "ready"))
        }
        let (model, toasts) = try makeModel(server)
        await model.load()

        let added = await model.addConfiguration(
            #"{"mcpServers": {"magicpath": {"url": "https://api.magicpath.ai/mcp"}, "Docs": {"url": "https://docs.example.com/mcp"}}}"#,
            access: .readOnly
        )

        #expect(added)
        #expect(model.servers.map(\.id) == ["docs", "magicpath"])
        #expect(model.addErrorMessage == nil)
        #expect(toasts.current?.text == "Added docs and magicpath.")
        #expect(server.routes.filter { $0 == "POST veetbot.test /v1/mcp-servers" }.count == 2)
    }

    @Test
    func testAPartlyFailedPasteNamesWhatFailedAndOnlyConfirmsWhatWasAdded() async throws {
        let server = DeviceFlowServer { request in
            guard request.httpMethod == "POST" else { return (200, MCPFixture.list([])) }
            let body = try JSONSerialization.jsonObject(with: Self.body(of: request)) as? [String: String]
            if body?["server_id"] == "broken" { return (502, MCPFixture.error("mcp_server_unavailable")) }
            return (201, MCPFixture.view(body?["server_id"] ?? "", authentication: "none", status: "ready"))
        }
        let (model, toasts) = try makeModel(server)
        await model.load()

        let added = await model.addConfiguration(
            #"{"mcpServers": {"magicpath": {"url": "https://api.magicpath.ai/mcp"}, "broken": {"url": "https://down.example.com/mcp"}, "gmail_x": {"url": "https://x.example.com/mcp"}}}"#,
            access: .changes
        )

        #expect(!added)
        #expect(model.servers.map(\.id) == ["magicpath"])
        #expect(
            model.addErrorMessage == """
                broken: Veetbot couldn't reach down.example.com. Check the URL and try again.
                gmail_x: \(MCPServerInputError.reservedName.message)
                """
        )
        #expect(toasts.current?.text == "Added magicpath.")
    }

    @Test
    func testAnUnreadablePasteSendsNothing() async throws {
        let server = DeviceFlowServer { _ in (200, MCPFixture.list([])) }
        let (model, _) = try makeModel(server)
        await model.load()

        let added = await model.addConfiguration(#"{"mcpServers": {"x": {"command": "npx"}}}"#, access: .changes)

        #expect(!added)
        #expect(model.addErrorMessage == MCPServerConfigurationError.localCommand("x").message)
        #expect(!server.routes.contains { $0.hasPrefix("POST") })
    }

    @Test
    func testChangingAccessSavesItAndShowsTheServersAnswer() async throws {
        let server = DeviceFlowServer { request in
            request.httpMethod == "PATCH"
                ? (200, MCPFixture.view("magicpath", access: "read_only"))
                : (200, MCPFixture.list([MCPFixture.view("magicpath")]))
        }
        let (model, toasts) = try makeModel(server)
        await model.load()
        let magicpath = try #require(model.servers.first)

        await model.setAccess(.changes, for: magicpath)
        #expect(!server.routes.contains { $0.hasPrefix("PATCH") }, "the current level sends nothing")

        await model.setAccess(.readOnly, for: magicpath)

        let body = try #require(server.json(of: "PATCH veetbot.test /v1/mcp-servers/magicpath") as? [String: String])
        #expect(body == ["access": "read_only"])
        #expect(model.servers.first?.access == .readOnly)
        #expect(model.pendingServerID == nil)
        #expect(toasts.current?.text == "magicpath now runs read-only.")
    }

    @Test
    func testBuiltInServersOfferNoActions() async throws {
        let server = DeviceFlowServer { _ in (200, MCPFixture.list([MCPFixture.builtIn("gmail_personal")])) }
        let (model, _) = try makeModel(server)
        await model.load()
        let builtIn = try #require(model.servers.first)

        await model.setAccess(.changes, for: builtIn)
        await model.remove(builtIn)
        await model.signIn(builtIn)
        await model.signOut(builtIn)

        #expect(server.routes == ["GET veetbot.test /v1/mcp-servers"])
    }

    @Test
    func testRemovingAServerDropsItsRow() async throws {
        let server = DeviceFlowServer { request in
            request.httpMethod == "DELETE"
                ? (204, "")
                : (200, MCPFixture.list([MCPFixture.view("magicpath"), MCPFixture.view("docs")]))
        }
        let (model, toasts) = try makeModel(server)
        await model.load()

        await model.remove(try #require(model.servers.first { $0.id == "magicpath" }))

        #expect(server.routes.contains("DELETE veetbot.test /v1/mcp-servers/magicpath"))
        #expect(model.servers.map(\.id) == ["docs"])
        #expect(toasts.current?.text == "Removed magicpath.")
    }

    @Test
    func testAServerRemovedElsewhereReloadsTheList() async throws {
        let listed = Box([MCPFixture.view("magicpath")])
        let server = DeviceFlowServer { request in
            if request.httpMethod == "PATCH" {
                listed.value = []
                return (404, MCPFixture.error("not_found", "No such MCP server."))
            }
            return (200, MCPFixture.list(listed.value))
        }
        let (model, _) = try makeModel(server)
        await model.load()

        await model.setAccess(.readOnly, for: try #require(model.servers.first))

        #expect(model.servers.isEmpty)
        #expect(model.errorMessage == "magicpath was already removed.")
        #expect(!model.notEnabled)
    }

    @Test
    func testAFeatureSwitchedOffMidSessionShowsTheCalmState() async throws {
        let enabled = Box(true)
        let server = DeviceFlowServer { request in
            if request.httpMethod == "DELETE" { enabled.value = false }
            return enabled.value
                ? (200, MCPFixture.list([MCPFixture.view("magicpath")]))
                : (404, MCPFixture.notMounted)
        }
        let (model, _) = try makeModel(server)
        await model.load()

        await model.remove(try #require(model.servers.first))

        #expect(model.notEnabled)
        #expect(model.servers.isEmpty)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testSigningOutForgetsTheTokensAndReloads() async throws {
        let signedIn = Box(true)
        let server = DeviceFlowServer { request in
            if request.httpMethod == "DELETE" {
                signedIn.value = false
                return (204, "")
            }
            return (200, MCPFixture.list([
                MCPFixture.view(
                    "magicpath", status: signedIn.value ? "ready" : "sign_in_required",
                    signedInAt: signedIn.value ? "2026-10-26T09:30:00Z" : nil
                ),
            ]))
        }
        let (model, toasts) = try makeModel(server)
        await model.load()

        await model.signOut(try #require(model.servers.first))

        #expect(server.routes.contains("DELETE veetbot.test /v1/mcp-servers/magicpath/sign-in"))
        #expect(model.servers.first?.status == .signInRequired)
        #expect(model.servers.first?.signedInAt == nil)
        #expect(toasts.current?.text == "Signed out of magicpath.")
    }

    @Test
    func testSigningInOpensTheAuthorizationPageAndReloadsWhenItReturns() async throws {
        let signedIn = Box(false)
        let presenter = FakeSignInPresenter(
            result: .success(URL(string: "veetbot://mcp-sign-in?server_id=magicpath&outcome=signed_in")!)
        )
        let server = DeviceFlowServer { request in
            if request.httpMethod == "POST" {
                signedIn.value = true
                return (200, MCPFixture.signInStart("magicpath"))
            }
            return (200, MCPFixture.list([
                MCPFixture.view("magicpath", status: signedIn.value ? "ready" : "sign_in_required"),
            ]))
        }
        let (model, toasts) = try makeModel(server, presenter: presenter)
        await model.load()

        await model.signIn(try #require(model.servers.first))

        let request = try #require(presenter.requests.first)
        #expect(request.url.absoluteString.hasPrefix("https://auth.magicpath.ai/authorize"))
        #expect(request.scheme == "veetbot")
        #expect(server.routes.filter { $0 == "GET veetbot.test /v1/mcp-servers" }.count == 2)
        #expect(model.servers.first?.status == .ready)
        #expect(model.errorMessage == nil)
        #expect(model.pendingServerID == nil)
        #expect(toasts.current?.text == "Signed in to magicpath.")
    }

    @Test(arguments: [
        ("failed", "Signing in to magicpath didn't finish. Try again."),
        ("expired", "The sign-in for magicpath expired. Try again."),
    ])
    func testAnUnfinishedSignInSaysSo(outcome: String, message: String) async throws {
        let presenter = FakeSignInPresenter(
            result: .success(URL(string: "veetbot://mcp-sign-in?server_id=magicpath&outcome=\(outcome)")!)
        )
        let server = DeviceFlowServer { request in
            request.httpMethod == "POST"
                ? (200, MCPFixture.signInStart("magicpath"))
                : (200, MCPFixture.list([MCPFixture.view("magicpath")]))
        }
        let (model, toasts) = try makeModel(server, presenter: presenter)
        await model.load()

        await model.signIn(try #require(model.servers.first))

        #expect(model.errorMessage == message)
        #expect(toasts.current == nil)
    }

    @Test
    func testCancellingTheSignInWindowIsNotAnError() async throws {
        let presenter = FakeSignInPresenter(result: .failure(MCPSignInPresentationError.cancelled))
        let server = DeviceFlowServer { request in
            request.httpMethod == "POST"
                ? (200, MCPFixture.signInStart("magicpath"))
                : (200, MCPFixture.list([MCPFixture.view("magicpath")]))
        }
        let (model, toasts) = try makeModel(server, presenter: presenter)
        await model.load()

        await model.signIn(try #require(model.servers.first))

        #expect(presenter.requests.count == 1)
        #expect(model.errorMessage == nil)
        #expect(toasts.current == nil)
        #expect(model.pendingServerID == nil)
    }

    @Test
    func testAnUnreachableServerNeverOpensTheSignInWindow() async throws {
        let presenter = FakeSignInPresenter(result: .failure(MCPSignInPresentationError.cancelled))
        let server = DeviceFlowServer { request in
            request.httpMethod == "POST"
                ? (502, MCPFixture.error("mcp_server_unavailable"))
                : (200, MCPFixture.list([MCPFixture.view("magicpath")]))
        }
        let (model, _) = try makeModel(server, presenter: presenter)
        await model.load()

        await model.signIn(try #require(model.servers.first))

        #expect(presenter.requests.isEmpty)
        #expect(model.errorMessage == "Veetbot couldn't reach api.magicpath.ai. Check the URL and try again.")
    }

    private nonisolated static func body(of request: URLRequest) -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var collected = Data()
        var buffer = [UInt8](repeating: 0, count: 4_096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            collected.append(buffer, count: count)
        }
        return collected
    }
}

// MARK: - The Settings page

@Suite struct MCPServerSettingsViewStructureTests {
    @Test
    func testTheGroupSitsAfterWebsiteAccessAndRendersTheCard() throws {
        let settings = try source(at: "Veetbot/Views/ConnectionSettingsView.swift")

        #expect(ConnectionSettingsSection.mcpServers.title == "MCP servers")
        #expect(ConnectionSettingsSection.mcpServers.systemImage == "puzzlepiece.extension")
        #expect(settings.contains("case .mcpServers:"))
        let section = try #require(settings.range(of: "case .mcpServers:"))
        let next = try #require(settings.range(of: "case .notifications:", range: section.upperBound..<settings.endIndex))
        let page = settings[section.lowerBound..<next.lowerBound]
        #expect(page.contains("if model.isConfigured {"))
        #expect(page.contains("MCPServerSettingsSection()"))
        #expect(settings.contains("Models, website access and MCP servers are saved on the server"))
    }

    @Test
    func testTheCardAvoidsTheKnownMacLayoutTraps() throws {
        let card = try source(at: "Veetbot/Views/MCPServerSettingsView.swift")

        #expect(!card.contains("LazyVStack"), "a lazy stack under a max-width column hung Settings")
        #expect(!card.contains(".pickerStyle(.segmented)"), "segmented labels wrap in the sidebar")
        #expect(card.contains(".labelsHidden()"))
        #expect(card.contains(".a2Field("))
        #expect(card.contains("MCPServerSettingsViewModel.notEnabledMessage"))
        #expect(card.contains(".accessibilityIdentifier(\"mcp-servers.add\")"))
        #expect(card.contains(".accessibilityIdentifier(\"mcp-servers.remove.\\(server.id)\")"))
        #expect(card.contains(".onChange(of: scenePhase)"))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
