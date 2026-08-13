import Foundation

public struct SSEFrame: Equatable, Sendable {
    public let id: Int?
    public let event: String
    public let data: [String: JSONValue]

    public init(id: Int?, event: String, data: [String: JSONValue]) {
        self.id = id
        self.event = event
        self.data = data
    }

    public var isPersisted: Bool { id != nil }

    public var isTerminal: Bool {
        event == "run.completed" || event == "run.failed" || event == "run.cancelled"
    }
}

public enum TrustLabel: String, Codable, Sendable {
    case platform
    case trustedConfiguration = "trusted_configuration"
    case user
    case internalTool = "internal_tool"
    case externalUntrusted = "external_untrusted"
    case memory
    case knowledge
}

public enum SideEffectClass: String, Codable, CaseIterable, Sendable {
    case none
    case workspaceRead = "workspace_read"
    case workspaceWrite = "workspace_write"
    case networkRead = "network_read"
    case codeExecution = "code_execution"
    case packageInstall = "package_install"
    case sandboxNetwork = "sandbox_network"
    case externalMessage = "external_message"
    case externalWrite = "external_write"
    case externalDelete = "external_delete"
    case financial
    case publication
    case credentialAccess = "credential_access"
    case hostAccess = "host_access"
    case privileged
}

public enum RiskLevel: String, Codable, CaseIterable, Sendable {
    case low, medium, high, critical
}

public struct TaskStateView: Codable, Identifiable, Sendable {
    public let taskID: String
    public let description: String
    public let status: String
    public let sourceEventIDs: [Int]
    public let trustLevel: TrustLabel
    public let updatedAt: Date

    public var id: String { taskID }

    enum CodingKeys: String, CodingKey {
        case description, status
        case taskID = "task_id"
        case sourceEventIDs = "source_event_ids"
        case trustLevel = "trust_level"
        case updatedAt = "updated_at"
    }
}

public struct FactView: Codable, Identifiable, Sendable {
    public let statement: String
    public let sourceEventIDs: [Int]
    public let trustLevel: TrustLabel
    public let establishedAt: Date

    public var id: String { "\(sourceEventIDs.map(String.init).joined(separator: ",")):\(statement)" }

    enum CodingKeys: String, CodingKey {
        case statement
        case sourceEventIDs = "source_event_ids"
        case trustLevel = "trust_level"
        case establishedAt = "established_at"
    }
}

public struct WorkingStateView: Codable, Sendable {
    public let objective: String?
    public let constraints: [String]
    public let tasks: [TaskStateView]
    public let establishedFacts: [FactView]
    public let openQuestions: [String]
    public let nextAction: String?

    enum CodingKeys: String, CodingKey {
        case objective, constraints, tasks
        case establishedFacts = "established_facts"
        case openQuestions = "open_questions"
        case nextAction = "next_action"
    }
}

public struct ToolResultView: Sendable {
    public var content: [ContentBlock]
    public var trust: TrustLabel?
    public var isError: Bool
    public var structured: [String: JSONValue]?

    public init(
        content: [ContentBlock] = [],
        trust: TrustLabel? = nil,
        isError: Bool = false,
        structured: [String: JSONValue]? = nil
    ) {
        self.content = content
        self.trust = trust
        self.isError = isError
        self.structured = structured
    }
}
