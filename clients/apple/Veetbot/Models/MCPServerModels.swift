import Foundation

// Owner-added MCP servers (ADR-0255 decisions 4 and 5): the views
// `/v1/mcp-servers` returns, the bodies it takes, the sign-in start, the
// callback the sign-in ends at, and the client-side checks and paste parser
// the Settings form uses before it sends anything.

/// How a server's tools are classified: read-only tools run without asking,
/// `changes` (the default) asks before every call.
public enum MCPServerAccess: Hashable, Sendable, Identifiable {
    case readOnly
    case changes
    case unknown(String)

    /// The two levels the owner can choose, in the order the menus list them.
    public static let choices: [MCPServerAccess] = [.changes, .readOnly]

    public init(rawValue: String) {
        switch rawValue {
        case "read_only": self = .readOnly
        case "changes": self = .changes
        default: self = .unknown(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .readOnly: return "read_only"
        case .changes: return "changes"
        case .unknown(let value): return value
        }
    }

    public var id: String { rawValue }

    public var label: String {
        switch self {
        case .readOnly: return "Read-only"
        case .changes: return "Asks before each call"
        case .unknown(let value): return value
        }
    }
}

public enum MCPServerStatus: Hashable, Sendable {
    case ready
    case signInRequired
    case unknown(String)

    public init(rawValue: String) {
        switch rawValue {
        case "ready": self = .ready
        case "sign_in_required": self = .signInRequired
        default: self = .unknown(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .ready: return "ready"
        case .signInRequired: return "sign_in_required"
        case .unknown(let value): return value
        }
    }

    public var label: String {
        switch self {
        case .ready: return "Ready"
        case .signInRequired: return "Sign-in required"
        case .unknown(let value): return value
        }
    }
}

public enum MCPServerAuthentication: Hashable, Sendable {
    case none
    case signIn
    case operatorManaged
    case unknown(String)

    public init(rawValue: String) {
        switch rawValue {
        case "none": self = .none
        case "sign_in": self = .signIn
        case "operator": self = .operatorManaged
        default: self = .unknown(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .none: return "none"
        case .signIn: return "sign_in"
        case .operatorManaged: return "operator"
        case .unknown(let value): return value
        }
    }
}

public enum MCPServerManager: Hashable, Sendable {
    case owner
    case operatorManaged
    case unknown(String)

    public init(rawValue: String) {
        switch rawValue {
        case "owner": self = .owner
        case "operator": self = .operatorManaged
        default: self = .unknown(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .owner: return "owner"
        case .operatorManaged: return "operator"
        case .unknown(let value): return value
        }
    }
}

extension MCPServerAccess: Codable {
    public init(from decoder: Decoder) throws {
        self.init(rawValue: try decoder.singleValueContainer().decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

extension MCPServerStatus: Codable {
    public init(from decoder: Decoder) throws {
        self.init(rawValue: try decoder.singleValueContainer().decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

extension MCPServerAuthentication: Codable {
    public init(from decoder: Decoder) throws {
        self.init(rawValue: try decoder.singleValueContainer().decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

extension MCPServerManager: Codable {
    public init(from decoder: Decoder) throws {
        self.init(rawValue: try decoder.singleValueContainer().decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

/// One server as `/v1/mcp-servers` shows it. Operator rows have no URL and
/// are read-only here.
public struct MCPServerView: Codable, Identifiable, Hashable, Sendable {
    public let serverID: String
    public let url: String?
    public let managedBy: MCPServerManager
    public let access: MCPServerAccess
    public let authentication: MCPServerAuthentication
    public let status: MCPServerStatus
    public let signInHost: String?
    public let signedInAt: Date?

    public var id: String { serverID }

    /// Only owner-added rows offer actions; operator rows are built in.
    public var isOwnerManaged: Bool { managedBy == .owner }

    public var host: String? {
        url.flatMap { URLComponents(string: $0)?.host }
    }

    public var usesSignIn: Bool { authentication == .signIn }

    enum CodingKeys: String, CodingKey {
        case url, access, authentication, status
        case serverID = "server_id"
        case managedBy = "managed_by"
        case signInHost = "sign_in_host"
        case signedInAt = "signed_in_at"
    }

    public init(
        serverID: String,
        url: String?,
        managedBy: MCPServerManager,
        access: MCPServerAccess,
        authentication: MCPServerAuthentication,
        status: MCPServerStatus,
        signInHost: String? = nil,
        signedInAt: Date? = nil
    ) {
        self.serverID = serverID
        self.url = url
        self.managedBy = managedBy
        self.access = access
        self.authentication = authentication
        self.status = status
        self.signInHost = signInHost
        self.signedInAt = signedInAt
    }
}

public struct MCPServerList: Codable, Hashable, Sendable {
    public let servers: [MCPServerView]
}

/// `POST /v1/mcp-servers`: a new server is 201, an identical replay 200.
public struct MCPServerAddResult: Hashable, Sendable {
    public let server: MCPServerView
    public let created: Bool
}

/// `POST /v1/mcp-servers/{server_id}/sign-in`: where the authentication
/// session opens and the scheme it ends at.
public struct MCPSignInStart: Codable, Hashable, Sendable {
    public let authorizationURL: URL
    public let callbackScheme: String
    public let expiresAt: Date

    enum CodingKeys: String, CodingKey {
        case authorizationURL = "authorization_url"
        case callbackScheme = "callback_scheme"
        case expiresAt = "expires_at"
    }
}

struct AddMCPServerBody: Encodable, Equatable {
    let serverID: String
    let url: String
    let access: MCPServerAccess

    enum CodingKeys: String, CodingKey {
        case url, access
        case serverID = "server_id"
    }
}

struct UpdateMCPServerAccessBody: Encodable, Equatable {
    let access: MCPServerAccess
}

// MARK: - The sign-in callback

public enum MCPSignInOutcome: Hashable, Sendable {
    case signedIn
    case failed
    case expired
    case unknown(String?)

    init(rawValue: String?) {
        switch rawValue {
        case "signed_in": self = .signedIn
        case "failed": self = .failed
        case "expired": self = .expired
        default: self = .unknown(rawValue)
        }
    }
}

/// The page the server's callback answers with, as the authentication
/// session hands it back: `veetbot://mcp-sign-in?server_id=…&outcome=…`.
public struct MCPSignInCallback: Hashable, Sendable {
    public static let host = "mcp-sign-in"

    public let serverID: String?
    public let outcome: MCPSignInOutcome

    /// Nil for a URL that is not this callback.
    public static func parse(_ url: URL, expectedScheme: String = "veetbot") -> MCPSignInCallback? {
        guard
            let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            components.scheme?.lowercased() == expectedScheme.lowercased(),
            components.host?.lowercased() == host
        else { return nil }
        let items = components.queryItems ?? []
        return MCPSignInCallback(
            serverID: items.first { $0.name == "server_id" }?.value,
            outcome: MCPSignInOutcome(rawValue: items.first { $0.name == "outcome" }?.value)
        )
    }
}

// MARK: - What the form checks before it sends

/// One server to add: a normalized name and the URL as entered.
public struct MCPServerDraft: Hashable, Sendable {
    public let name: String
    public let url: String

    public init(name: String, url: String) {
        self.name = name
        self.url = url
    }
}

/// Why a name or URL was refused before anything was sent.
public enum MCPServerInputError: Error, Hashable, Sendable {
    case missingName
    case invalidName
    case reservedName
    case missingURL
    case invalidURL
    case httpsRequired
    case credentialsNotAllowed
    case fragmentNotAllowed
    case portNotAllowed

    public var message: String {
        switch self {
        case .missingName:
            return "Enter a name for the server."
        case .invalidName:
            return "A name starts with a letter and uses only lowercase letters, digits and underscores, up to 32 characters."
        case .reservedName:
            return "Names starting with gmail_, bland_ or svp_ are reserved for built-in servers."
        case .missingURL:
            return "Enter the server's URL."
        case .invalidURL:
            return "Enter a complete URL, such as https://example.com/mcp."
        case .httpsRequired:
            return "The URL must start with https://."
        case .credentialsNotAllowed:
            return "The URL must not contain a username or password."
        case .fragmentNotAllowed:
            return "The URL must not contain a #fragment."
        case .portNotAllowed:
            return "The URL must use the standard HTTPS port."
        }
    }
}

public enum MCPServerInput {
    public static let maximumNameLength = 32
    public static let reservedPrefixes = ["gmail_", "bland_", "svp_"]

    /// `^[a-z][a-z0-9_]{0,31}$`: the grammar every owner-added name follows.
    public static func isWellFormedName(_ name: String) -> Bool {
        guard let first = name.unicodeScalars.first, isLetter(first),
            name.unicodeScalars.count <= maximumNameLength
        else { return false }
        return name.unicodeScalars.allSatisfy(isNameCharacter)
    }

    public static func validateName(_ name: String) -> MCPServerInputError? {
        if name.isEmpty { return .missingName }
        if !isWellFormedName(name) { return .invalidName }
        if reservedPrefixes.contains(where: name.hasPrefix) { return .reservedName }
        return nil
    }

    /// Absolute HTTPS, no credentials, no fragment, and port 443 or none.
    public static func validateURL(_ raw: String) -> MCPServerInputError? {
        let value = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if value.isEmpty { return .missingURL }
        guard let components = URLComponents(string: value), let scheme = components.scheme,
            let host = components.host, !host.isEmpty
        else { return .invalidURL }
        if scheme.lowercased() != "https" { return .httpsRequired }
        if components.user != nil || components.password != nil { return .credentialsNotAllowed }
        if components.fragment != nil { return .fragmentNotAllowed }
        if let port = components.port, port != 443 { return .portNotAllowed }
        return nil
    }

    /// Lowercased, every other character turned to `_`, outer underscores
    /// trimmed, `mcp_` before a name that would not start with a letter, and
    /// cut to 32 characters.
    public static func normalizedName(_ raw: String) -> String {
        let mapped = String(
            String.UnicodeScalarView(
                raw.trimmingCharacters(in: .whitespacesAndNewlines).lowercased().unicodeScalars.map {
                    isNameCharacter($0) ? $0 : "_"
                }
            )
        )
        var name = mapped.trimmingCharacters(in: CharacterSet(charactersIn: "_"))
        if name.isEmpty { return "" }
        if let first = name.unicodeScalars.first, !isLetter(first) { name = "mcp_" + name }
        return String(name.prefix(maximumNameLength))
    }

    /// A name for a server pasted without one: its host's main label.
    static func name(fromURL raw: String) -> String {
        guard let host = URLComponents(string: raw)?.host else { return "" }
        let labels = host.split(separator: ".").map(String.init)
        let main = labels.count >= 2 ? labels[labels.count - 2] : (labels.first ?? "")
        return normalizedName(main)
    }

    private static func isLetter(_ scalar: Unicode.Scalar) -> Bool {
        ("a"..."z").contains(scalar)
    }

    private static func isNameCharacter(_ scalar: Unicode.Scalar) -> Bool {
        isLetter(scalar) || ("0"..."9").contains(scalar) || scalar == "_"
    }
}

/// Why a pasted configuration could not be read.
public enum MCPServerConfigurationError: Error, Hashable, Sendable {
    case notJSON
    case noServers
    case missingURL(String)
    case localCommand(String)

    public var message: String {
        switch self {
        case .notJSON:
            return "That isn't a JSON configuration. Paste the server's JSON, such as {\"mcpServers\": {\"name\": {\"url\": \"https://…\"}}}."
        case .noServers:
            return "That configuration names no servers."
        case .missingURL(let name):
            return "\(name) has no URL. Veetbot connects to servers by their https URL."
        case .localCommand(let name):
            return "\(name) runs a program on a computer. Veetbot connects only to servers at an https URL."
        }
    }
}

/// Reads the JSON a service gives for its MCP server, as Claude and other
/// clients use it: `{"mcpServers": {"name": {"url": "…"}}}`, the same map
/// under `servers`, the bare map, or one server object without a name.
public enum MCPServerConfiguration {
    public static func parse(_ text: String) -> Result<[MCPServerDraft], MCPServerConfigurationError> {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty,
            let object = try? JSONSerialization.jsonObject(with: Data(trimmed.utf8)),
            let root = object as? [String: Any]
        else { return .failure(.notJSON) }

        let entries: [(String, Any)]
        if let servers = root["mcpServers"] as? [String: Any] {
            entries = Array(servers)
        } else if let servers = root["servers"] as? [String: Any] {
            entries = Array(servers)
        } else if root["url"] != nil || root["serverUrl"] != nil || root["command"] != nil {
            entries = [("", root)]
        } else {
            entries = Array(root)
        }
        guard !entries.isEmpty else { return .failure(.noServers) }

        var drafts: [MCPServerDraft] = []
        for (rawName, value) in entries.sorted(by: { $0.0 < $1.0 }) {
            guard let entry = value as? [String: Any] else { return .failure(.notJSON) }
            let shownName = rawName.isEmpty ? "The server" : rawName
            guard
                let url = ((entry["url"] ?? entry["serverUrl"]) as? String)?
                    .trimmingCharacters(in: .whitespacesAndNewlines),
                !url.isEmpty
            else {
                return .failure(
                    entry["command"] != nil ? .localCommand(shownName) : .missingURL(shownName)
                )
            }
            let name = MCPServerInput.normalizedName(rawName)
            drafts.append(
                MCPServerDraft(name: name.isEmpty ? MCPServerInput.name(fromURL: url) : name, url: url)
            )
        }
        return .success(drafts.sorted { $0.name < $1.name })
    }
}
