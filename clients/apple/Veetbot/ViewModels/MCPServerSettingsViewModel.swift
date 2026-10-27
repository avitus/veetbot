import Foundation

/// Opens a server's authorization page and returns the callback URL it ends
/// at. Throws `MCPSignInPresentationError.cancelled` when the owner closes it.
@MainActor
public protocol MCPSignInPresenting: AnyObject {
    func authenticate(url: URL, callbackScheme: String) async throws -> URL
}

public enum MCPSignInPresentationError: Error, Equatable {
    case cancelled
    case couldNotStart
}

/// Settings → MCP servers (ADR-0255): the servers saved on the Veetbot
/// server, owner-added ones first. An owner row can change its access, sign
/// in or out, and be removed; built-in rows only show what is there. Adding
/// takes a name, a URL and an access level, or a pasted JSON configuration
/// naming one or more servers. A server without the feature answers 404 and
/// the page says so calmly. Failures stay beside the control that failed;
/// only a change the server confirmed is toasted.
@MainActor
public final class MCPServerSettingsViewModel: ObservableObject {
    public static let maximumServers = 16
    public static let notEnabledMessage = "MCP servers aren't enabled on this Veetbot server."
    public static let scopeMessage =
        "MCP servers need the settings.read and settings.write scopes on the server."
    static let limitMessage = "You can add up to 16 MCP servers. Remove one to add another."
    static let rejectedMessage = "The server didn't accept that name or URL. Check both and try again."
    static let presenterFailedMessage = "Couldn't open the sign-in window. Try again."

    @Published public private(set) var servers: [MCPServerView] = []
    @Published public private(set) var hasLoaded = false
    @Published public private(set) var isLoading = false
    @Published public private(set) var isAdding = false
    /// The server a row action is acting on, so its buttons wait for it.
    @Published public private(set) var pendingServerID: String?
    /// The server does not mount the routes: the feature is off.
    @Published public private(set) var notEnabled = false
    /// A failed load or row action.
    @Published public private(set) var errorMessage: String?
    /// A failed add, beside the form.
    @Published public private(set) var addErrorMessage: String?

    private let makeAPIClient: () async -> VeetbotAPIClient?
    private let presenter: MCPSignInPresenting
    private let toasts: ToastCenter

    public init(
        makeAPIClient: @escaping () async -> VeetbotAPIClient? = {
            await MemoryViewModel.makeDefaultAPIClient()
        },
        presenter: MCPSignInPresenting? = nil,
        toasts: ToastCenter? = nil
    ) {
        self.makeAPIClient = makeAPIClient
        self.presenter = presenter ?? WebAuthenticationSessionPresenter()
        self.toasts = toasts ?? .shared
    }

    public var ownerServerCount: Int { servers.filter(\.isOwnerManaged).count }
    public var canAddMore: Bool { ownerServerCount < Self.maximumServers }
    public var isBusy: Bool { isAdding || pendingServerID != nil }

    // MARK: Loading

    public func load() async {
        guard let api = await makeAPIClient() else { return }
        isLoading = true
        defer { isLoading = false }
        do {
            let list = try await api.listMCPServers()
            servers = Self.ordered(list.servers)
            notEnabled = false
            errorMessage = nil
            hasLoaded = true
        } catch is CancellationError {
            return
        } catch MCPServerClientError.unavailable {
            markNotEnabled()
        } catch HTTPTransportError.authorizationDenied {
            errorMessage = Self.scopeMessage
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    // MARK: Adding

    /// Adds one server. The name is normalized first, so `MagicPath` is sent
    /// as `magicpath`; nothing is sent for a name or URL the contract refuses.
    @discardableResult
    public func add(name rawName: String, url rawURL: String, access: MCPServerAccess) async -> Bool {
        guard !isBusy else { return false }
        addErrorMessage = nil
        let draft = MCPServerDraft(
            name: MCPServerInput.normalizedName(rawName),
            url: rawURL.trimmingCharacters(in: .whitespacesAndNewlines)
        )
        if let problem = MCPServerInput.validateName(draft.name) ?? MCPServerInput.validateURL(draft.url) {
            addErrorMessage = problem.message
            return false
        }
        guard let api = await makeAPIClient() else {
            addErrorMessage = HTTPTransportError.notConfigured.errorDescription
            return false
        }
        isAdding = true
        defer { isAdding = false }
        switch await send(draft, access: access, api: api) {
        case .success(let result):
            toasts.show(Self.addedToast(result))
            return true
        case .failure(let failure):
            addErrorMessage = failure.message
            return false
        }
    }

    /// Adds every server a pasted configuration names, each with `access`.
    /// What was added is confirmed; what failed is listed by name.
    @discardableResult
    public func addConfiguration(_ text: String, access: MCPServerAccess) async -> Bool {
        guard !isBusy else { return false }
        addErrorMessage = nil
        let drafts: [MCPServerDraft]
        switch MCPServerConfiguration.parse(text) {
        case .success(let parsed): drafts = parsed
        case .failure(let problem):
            addErrorMessage = problem.message
            return false
        }
        guard let api = await makeAPIClient() else {
            addErrorMessage = HTTPTransportError.notConfigured.errorDescription
            return false
        }
        isAdding = true
        defer { isAdding = false }
        var added: [String] = []
        var failures: [String] = []
        batch: for draft in drafts {
            if let problem = MCPServerInput.validateName(draft.name) ?? MCPServerInput.validateURL(draft.url) {
                failures.append("\(draft.name.isEmpty ? draft.url : draft.name): \(problem.message)")
                continue
            }
            switch await send(draft, access: access, api: api) {
            case .success(let result):
                added.append(result.server.id)
            case .failure(let failure):
                failures.append("\(draft.name): \(failure.message)")
                if failure.stopsTheBatch { break batch }
            }
            if notEnabled { break batch }
        }
        if !added.isEmpty { toasts.show("Added \(Self.joined(added)).") }
        if !failures.isEmpty { addErrorMessage = failures.joined(separator: "\n") }
        return failures.isEmpty && !notEnabled
    }

    private struct AddFailure: Error {
        let message: String
        var stopsTheBatch = false
    }

    private func send(
        _ draft: MCPServerDraft, access: MCPServerAccess, api: VeetbotAPIClient
    ) async -> Result<MCPServerAddResult, AddFailure> {
        do {
            let result = try await api.addMCPServer(serverID: draft.name, url: draft.url, access: access)
            upsert(result.server)
            return .success(result)
        } catch MCPServerClientError.unavailable {
            markNotEnabled()
            return .failure(AddFailure(message: Self.notEnabledMessage, stopsTheBatch: true))
        } catch HTTPTransportError.authorizationDenied {
            return .failure(AddFailure(message: Self.scopeMessage, stopsTheBatch: true))
        } catch HTTPTransportError.api(let error) {
            return .failure(AddFailure(message: addFailureMessage(error, draft: draft)))
        } catch {
            return .failure(AddFailure(message: Self.describe(error)))
        }
    }

    private func addFailureMessage(_ error: APIError, draft: MCPServerDraft) -> String {
        let host = URLComponents(string: draft.url)?.host ?? draft.url
        if Self.isUnreachable(error) { return Self.unreachableMessage(host: host) }
        if error.code.rawValue == "mcp_sign_in_unsupported" || error.statusCode == 422 {
            return "\(host) asks for a sign-in Veetbot can't do yet, such as an API key. The server wasn't added."
        }
        if error.code == .conflict || error.statusCode == 409 {
            let isExisting = servers.contains { $0.id == draft.name }
            return !isExisting && !canAddMore
                ? Self.limitMessage
                : "The name \(draft.name) is already used or reserved. Choose another name."
        }
        if error.code == .malformedRequest || error.statusCode == 400 { return Self.rejectedMessage }
        return error.message
    }

    // MARK: Row actions

    public func setAccess(_ access: MCPServerAccess, for server: MCPServerView) async {
        guard server.isOwnerManaged, access != server.access, pendingServerID == nil,
            let api = await makeAPIClient()
        else { return }
        pendingServerID = server.id
        defer { pendingServerID = nil }
        errorMessage = nil
        do {
            let updated = try await api.updateMCPServerAccess(server.id, access: access)
            upsert(updated)
            toasts.show(
                updated.access == .readOnly
                    ? "\(server.id) now runs read-only."
                    : "\(server.id) now asks before each call."
            )
        } catch {
            await handleRowFailure(error, server: server)
        }
    }

    public func remove(_ server: MCPServerView) async {
        guard server.isOwnerManaged, pendingServerID == nil, let api = await makeAPIClient() else { return }
        pendingServerID = server.id
        defer { pendingServerID = nil }
        errorMessage = nil
        do {
            try await api.removeMCPServer(server.id)
            servers.removeAll { $0.id == server.id }
            toasts.show("Removed \(server.id).")
        } catch {
            await handleRowFailure(error, server: server)
        }
    }

    public func signOut(_ server: MCPServerView) async {
        guard server.isOwnerManaged, server.usesSignIn, pendingServerID == nil,
            let api = await makeAPIClient()
        else { return }
        pendingServerID = server.id
        defer { pendingServerID = nil }
        errorMessage = nil
        do {
            try await api.signOutOfMCPServer(server.id)
            await load()
            if !notEnabled { toasts.show("Signed out of \(server.id).") }
        } catch {
            await handleRowFailure(error, server: server)
        }
    }

    /// Starts the sign-in on the server, opens the authorization page, reads
    /// the outcome the callback reports, and reloads the list. Closing the
    /// window is not an error.
    public func signIn(_ server: MCPServerView) async {
        guard server.isOwnerManaged, server.usesSignIn, pendingServerID == nil,
            let api = await makeAPIClient()
        else { return }
        pendingServerID = server.id
        defer { pendingServerID = nil }
        errorMessage = nil
        let start: MCPSignInStart
        do {
            start = try await api.beginMCPServerSignIn(server.id)
        } catch {
            await handleRowFailure(error, server: server, conflict: "\(server.id) doesn't need a sign-in.")
            return
        }
        let outcome: MCPSignInOutcome?
        do {
            let callbackURL = try await presenter.authenticate(
                url: start.authorizationURL, callbackScheme: start.callbackScheme
            )
            let callback = MCPSignInCallback.parse(callbackURL, expectedScheme: start.callbackScheme)
            if let callback, callback.serverID == nil || callback.serverID == server.id {
                outcome = callback.outcome
            } else {
                outcome = .failed
            }
        } catch MCPSignInPresentationError.cancelled {
            outcome = nil
        } catch {
            errorMessage = Self.presenterFailedMessage
            return
        }
        await load()
        guard !notEnabled else { return }
        switch outcome {
        case nil:
            break
        case .signedIn:
            toasts.show("Signed in to \(server.id).")
        case .expired:
            errorMessage = "The sign-in for \(server.id) expired. Try again."
        case .failed, .unknown:
            errorMessage = "Signing in to \(server.id) didn't finish. Try again."
        }
    }

    /// A 404 on one server's route is either an unknown server or a server
    /// whose feature was switched off; the reloaded list tells them apart.
    private func handleRowFailure(
        _ error: Error, server: MCPServerView, conflict: String? = nil
    ) async {
        switch error {
        case is CancellationError:
            return
        case MCPServerClientError.unavailable:
            markNotEnabled()
        case HTTPTransportError.authorizationDenied:
            errorMessage = Self.scopeMessage
        case HTTPTransportError.api(let apiError) where apiError.statusCode == 404:
            await load()
            if !notEnabled, errorMessage == nil { errorMessage = "\(server.id) was already removed." }
        case HTTPTransportError.api(let apiError) where apiError.statusCode == 409:
            await load()
            if !notEnabled, errorMessage == nil {
                errorMessage = conflict ?? "\(server.id) is built in and can't be changed here."
            }
        case HTTPTransportError.api(let apiError) where Self.isUnreachable(apiError):
            errorMessage = Self.unreachableMessage(host: server.host ?? server.id)
        default:
            errorMessage = Self.describe(error)
        }
    }

    // MARK: Helpers

    private func markNotEnabled() {
        notEnabled = true
        servers = []
        errorMessage = nil
        hasLoaded = true
    }

    private func upsert(_ server: MCPServerView) {
        var updated = servers.filter { $0.id != server.id }
        updated.append(server)
        servers = Self.ordered(updated)
    }

    /// Owner-added servers first, then built-in ones, each by name.
    private static func ordered(_ servers: [MCPServerView]) -> [MCPServerView] {
        servers.sorted { lhs, rhs in
            if lhs.isOwnerManaged != rhs.isOwnerManaged { return lhs.isOwnerManaged }
            return lhs.id < rhs.id
        }
    }

    private static func addedToast(_ result: MCPServerAddResult) -> String {
        let name = result.server.id
        if !result.created { return "\(name) is already added." }
        return result.server.status == .signInRequired
            ? "Added \(name). Sign in to use it."
            : "Added \(name)."
    }

    private static func isUnreachable(_ error: APIError) -> Bool {
        error.code.rawValue == "mcp_server_unavailable" || error.statusCode == 502
    }

    private static func unreachableMessage(host: String) -> String {
        "Veetbot couldn't reach \(host). Check the URL and try again."
    }

    private static func joined(_ names: [String]) -> String {
        guard names.count > 1, let last = names.last else { return names.first ?? "" }
        return names.dropLast().joined(separator: ", ") + " and " + last
    }

    private static func describe(_ error: Error) -> String {
        if case HTTPTransportError.api(let apiError) = error { return apiError.message }
        return error.localizedDescription
    }
}
