import Foundation

/// The task permission a `browser.act` approval offers (ADR-0129,
/// 0129-design §8.2). Its summary is server-authored text.
public struct TaskGrantOfferView: Codable, Equatable, Sendable {
    public let origin: String
    public let pathPrefix: String
    public let durationSeconds: Int
    public let maxActions: Int
    public let maxTypedCharacters: Int
    public let actionKinds: [String]
    public let summary: String

    enum CodingKeys: String, CodingKey {
        case origin, summary
        case pathPrefix = "path_prefix"
        case durationSeconds = "duration_seconds"
        case maxActions = "max_actions"
        case maxTypedCharacters = "max_typed_characters"
        case actionKinds = "action_kinds"
    }
}

/// Why an active task permission did not cover the action an approval asks
/// about. `reason` is a closed server list; an unknown value stays readable.
public struct TaskGrantNotCoveredView: Codable, Equatable, Sendable {
    public let grantID: UUID
    public let reason: String

    enum CodingKeys: String, CodingKey {
        case reason
        case grantID = "grant_id"
    }
}

/// The resolve body's `task_grant`: the offer's scope, repeated exactly.
public struct TaskGrantEcho: Codable, Equatable, Sendable {
    public let origin: String
    public let pathPrefix: String

    public init(origin: String, pathPrefix: String) {
        self.origin = origin
        self.pathPrefix = pathPrefix
    }

    public init(offer: TaskGrantOfferView) {
        self.init(origin: offer.origin, pathPrefix: offer.pathPrefix)
    }

    enum CodingKeys: String, CodingKey {
        case origin
        case pathPrefix = "path_prefix"
    }
}

/// Which grants `GET /v1/browser-task-grants` lists.
public enum BrowserTaskGrantListStatus: String, Sendable {
    case active
    case all
}

/// A task grant's derived status. A value this build does not know decodes
/// and keeps its spelling.
public struct BrowserTaskGrantStatus: RawRepresentable, Codable, Hashable, Sendable {
    public let rawValue: String

    public init(rawValue: String) {
        self.rawValue = rawValue
    }

    public static let active = BrowserTaskGrantStatus(rawValue: "active")
    public static let expired = BrowserTaskGrantStatus(rawValue: "expired")
    public static let exhausted = BrowserTaskGrantStatus(rawValue: "exhausted")
    public static let revoked = BrowserTaskGrantStatus(rawValue: "revoked")
    public static let ended = BrowserTaskGrantStatus(rawValue: "ended")
}

/// `GET /v1/browser-task-grants` and its point read (0129-design §8.3).
public struct BrowserTaskGrantView: Codable, Identifiable, Equatable, Sendable {
    public let id: UUID
    public let sessionID: UUID
    public let profileID: UUID
    public let origin: String
    public let pathPrefix: String
    public let actionKinds: [String]
    public let status: BrowserTaskGrantStatus
    public let endReason: String?
    public let maxActions: Int
    public var actionsUsed: Int
    public let maxTypedCharacters: Int
    public let typedCharacters: Int
    public let createdAt: Date
    public let expiresAt: Date
    public let lastUsedAt: Date?
    public let endedAt: Date?
    public let approvalID: UUID
    public let approvedBy: String

    enum CodingKeys: String, CodingKey {
        case id, origin, status
        case sessionID = "session_id"
        case profileID = "profile_id"
        case pathPrefix = "path_prefix"
        case actionKinds = "action_kinds"
        case endReason = "end_reason"
        case maxActions = "max_actions"
        case actionsUsed = "actions_used"
        case maxTypedCharacters = "max_typed_characters"
        case typedCharacters = "typed_characters"
        case createdAt = "created_at"
        case expiresAt = "expires_at"
        case lastUsedAt = "last_used_at"
        case endedAt = "ended_at"
        case approvalID = "approval_id"
        case approvedBy = "approved_by"
    }

    public var isActive: Bool { status == .active }

    /// "Allowed on www.example.org/lesson · 23 of 200 · 18 min left".
    public func bannerText(now: Date) -> String {
        let host = URL(string: origin)?.host ?? origin
        let remaining = expiresAt.timeIntervalSince(now)
        let time = remaining >= 60
            ? "\(Int((remaining / 60).rounded(.up))) min left"
            : "less than a minute left"
        return "Allowed on \(host)\(pathPrefix) · \(actionsUsed) of \(maxActions) · \(time)"
    }
}

/// The `browser.act` approval view (0129-design §6.2), decoded from an
/// approval's `arguments` when `view` is `browser.act.v1`. Page-authored
/// fields are untrusted website text.
public struct BrowserActionApprovalArguments: Equatable, Sendable {
    public static let viewName = "browser.act.v1"

    public let described: Bool
    public let kind: String
    public let pageOrigin: String?
    public let pagePath: String?
    public let pageTitle: String?
    public let elementRole: String?
    public let elementName: String?
    public let elementText: String?
    public let elementContext: String?
    public let field: String?
    public let text: String?
    public let option: String?
    public let key: String?
    public let scrollDeltaY: Int?
    public let checked: Bool?
    public let consequence: String?
    public let refused: Bool

    public init?(arguments: [String: JSONValue]) {
        guard arguments["view"]?.stringValue == Self.viewName,
            let kind = arguments["kind"]?.stringValue
        else { return nil }
        described = arguments["described"]?.boolValue ?? false
        self.kind = kind
        pageOrigin = arguments["page_origin"]?.stringValue
        pagePath = arguments["page_path"]?.stringValue
        pageTitle = arguments["page_title"]?.stringValue
        elementRole = arguments["element_role"]?.stringValue
        elementName = arguments["element_name"]?.stringValue
        elementText = arguments["element_text"]?.stringValue
        elementContext = arguments["element_context"]?.stringValue
        field = arguments["field"]?.stringValue
        text = arguments["text"]?.stringValue
        option = arguments["option"]?.stringValue
        key = arguments["key"]?.stringValue
        scrollDeltaY = arguments["scroll_delta_y"]?.intValue
        checked = arguments["checked"]?.boolValue
        consequence = arguments["consequence"]?.stringValue
        refused = arguments["refused"]?.boolValue ?? false
    }
}

extension ApprovalView {
    /// The `browser.act` view, or nil for any other approval or view version.
    public var browserAction: BrowserActionApprovalArguments? {
        guard toolName == "browser.act" else { return nil }
        return BrowserActionApprovalArguments(arguments: arguments)
    }
}
