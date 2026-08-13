import Foundation

public enum SessionStatus: String, Codable, Sendable {
    case active = "ACTIVE"
    case closed = "CLOSED"
}

public enum RunStatus: String, Codable, CaseIterable, Sendable {
    case queued = "QUEUED"
    case running = "RUNNING"
    case waitingForApproval = "WAITING_FOR_APPROVAL"
    case waitingForUser = "WAITING_FOR_USER"
    case completed = "COMPLETED"
    case failed = "FAILED"
    case cancelled = "CANCELLED"

    public var isTerminal: Bool {
        self == .completed || self == .failed || self == .cancelled
    }

    public var isActive: Bool { !isTerminal }
}

public enum FailureReason: String, Codable, Sendable {
    case maxAttemptsExceeded = "max_attempts_exceeded"
    case budgetExceeded = "budget_exceeded"
    case deadlineExceeded = "deadline_exceeded"
    case maxStepsExceeded = "max_steps_exceeded"
    case toolLoopDetected = "tool_loop_detected"
    case repeatedDenial = "repeated_denial"
    case approvalExpired = "approval_expired"
    case inputDeadlineExceeded = "input_deadline_exceeded"
    case contextOverflow = "context_overflow"
    case modelPermanentError = "model_permanent_error"
    case emptyModelTurn = "empty_model_turn"
    case authorizationError = "authorization_error"
    case childRunFailed = "child_run_failed"
    case internalError = "internal_error"
}

public struct SessionView: Codable, Identifiable, Sendable {
    public let id: UUID
    public let status: SessionStatus
    public let agentID: String
    public let agentVersion: String
    public let title: String?
    public let metadata: [String: JSONValue]
    public let createdAt: Date
    public let updatedAt: Date
    public let activeRunID: UUID?

    enum CodingKeys: String, CodingKey {
        case id, status, title, metadata
        case agentID = "agent_id"
        case agentVersion = "agent_version"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case activeRunID = "active_run_id"
    }
}

public struct RunUsageView: Codable, Sendable {
    public let inputTokens: Int
    public let outputTokens: Int
    public let costUSD: String

    enum CodingKeys: String, CodingKey {
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case costUSD = "cost_usd"
    }
}

public struct RunLimitsView: Codable, Sendable {
    public let maxSteps: Int
    public let deadlineAt: Date?
    public let maxCostUSD: String?

    enum CodingKeys: String, CodingKey {
        case maxSteps = "max_steps"
        case deadlineAt = "deadline_at"
        case maxCostUSD = "max_cost_usd"
    }
}

public struct RunFailureView: Codable, Sendable {
    public let reason: FailureReason
    public let message: String
    public let stepNumber: Int?
    public let attemptNumber: Int?
    public let occurredAt: Date

    enum CodingKeys: String, CodingKey {
        case reason, message
        case stepNumber = "step_number"
        case attemptNumber = "attempt_number"
        case occurredAt = "occurred_at"
    }
}

public struct RunView: Codable, Identifiable, Sendable {
    public let id: UUID
    public let sessionID: UUID
    public let parentRunID: UUID?
    public let status: RunStatus
    public let stepCount: Int
    public let modelCallCount: Int
    public let toolCallCount: Int
    public let usage: RunUsageView
    public let limits: RunLimitsView
    public let failure: RunFailureView?
    public let cancelRequestedAt: Date?
    public let createdAt: Date
    public let updatedAt: Date

    enum CodingKeys: String, CodingKey {
        case id, status, usage, limits, failure
        case sessionID = "session_id"
        case parentRunID = "parent_run_id"
        case stepCount = "step_count"
        case modelCallCount = "model_call_count"
        case toolCallCount = "tool_call_count"
        case cancelRequestedAt = "cancel_requested_at"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

public enum ApprovalDecision: String, Codable, CaseIterable, Sendable {
    case approveOnce = "approve_once"
    case deny
}

public enum ApprovalStatus: String, Codable, CaseIterable, Sendable {
    case pending = "PENDING"
    case approved = "APPROVED"
    case denied = "DENIED"
    case expired = "EXPIRED"
    case cancelled = "CANCELLED"

    public var isPending: Bool { self == .pending }
    public var displayName: String { rawValue.lowercased() }
}

public struct ApprovalView: Codable, Identifiable, Sendable {
    public let id: UUID
    public let runID: UUID
    public let sessionID: UUID
    public let status: ApprovalStatus
    public let toolName: String?
    public let actionSummary: String
    public let arguments: [String: JSONValue]
    public let risk: String
    public let policyReason: String
    public let expiresAt: Date?
    public let createdAt: Date
    public let resolvedAt: Date?
    public let resolvedBy: String?
    public let decision: ApprovalDecision?

    enum CodingKeys: String, CodingKey {
        case id, status, arguments, risk, decision
        case runID = "run_id"
        case sessionID = "session_id"
        case toolName = "tool_name"
        case actionSummary = "action_summary"
        case policyReason = "policy_reason"
        case expiresAt = "expires_at"
        case createdAt = "created_at"
        case resolvedAt = "resolved_at"
        case resolvedBy = "resolved_by"
    }
}

public struct ArtifactView: Codable, Identifiable, Sendable {
    public let id: UUID
    public let sessionID: UUID
    public let runID: UUID
    public let name: String
    public let mediaType: String
    public let sha256: String
    public let sizeBytes: Int
    public let metadata: [String: JSONValue]
    public let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case id, name, sha256, metadata
        case sessionID = "session_id"
        case runID = "run_id"
        case mediaType = "media_type"
        case sizeBytes = "size_bytes"
        case createdAt = "created_at"
    }
}

public struct SubmitResult: Codable, Sendable {
    public let runID: UUID
    public let status: RunStatus

    enum CodingKeys: String, CodingKey {
        case runID = "run_id"
        case status
    }
}

public struct Page<Item: Codable & Sendable>: Codable, Sendable {
    public let items: [Item]
    public let nextCursor: String?

    enum CodingKeys: String, CodingKey {
        case items
        case nextCursor = "next_cursor"
    }
}

public enum ContentBlock: Codable, Hashable, Sendable {
    case text(String)
    case image(artifactID: UUID, mediaType: String, detail: String)
    case file(artifactID: UUID, mediaType: String, filename: String?)

    private enum CodingKeys: String, CodingKey {
        case type, kind, text, detail, filename
        case artifactID = "artifact_id"
        case mediaType = "media_type"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let discriminator: String
        let discriminatorKey: CodingKeys
        if let type = try container.decodeIfPresent(String.self, forKey: .type) {
            discriminator = type
            discriminatorKey = .type
        } else {
            discriminator = try container.decode(String.self, forKey: .kind)
            discriminatorKey = .kind
        }
        switch discriminator {
        case "text":
            self = .text(try container.decode(String.self, forKey: .text))
        case "image":
            self = .image(
                artifactID: try container.decode(UUID.self, forKey: .artifactID),
                mediaType: try container.decode(String.self, forKey: .mediaType),
                detail: try container.decodeIfPresent(String.self, forKey: .detail) ?? "auto"
            )
        case "file":
            self = .file(
                artifactID: try container.decode(UUID.self, forKey: .artifactID),
                mediaType: try container.decode(String.self, forKey: .mediaType),
                filename: try container.decodeIfPresent(String.self, forKey: .filename)
            )
        default:
            throw DecodingError.dataCorruptedError(
                forKey: discriminatorKey,
                in: container,
                debugDescription: "Unknown content block type: \(discriminator)"
            )
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case let .text(text):
            try container.encode("text", forKey: .type)
            try container.encode(text, forKey: .text)
        case let .image(artifactID, mediaType, detail):
            try container.encode("image", forKey: .type)
            try container.encode(artifactID, forKey: .artifactID)
            try container.encode(mediaType, forKey: .mediaType)
            try container.encode(detail, forKey: .detail)
        case let .file(artifactID, mediaType, filename):
            try container.encode("file", forKey: .type)
            try container.encode(artifactID, forKey: .artifactID)
            try container.encode(mediaType, forKey: .mediaType)
            try container.encodeIfPresent(filename, forKey: .filename)
        }
    }

    public var text: String? {
        guard case let .text(value) = self else { return nil }
        return value
    }

    public var artifactID: UUID? {
        switch self {
        case .text:
            return nil
        case let .image(artifactID, _, _), let .file(artifactID, _, _):
            return artifactID
        }
    }
}
