import Foundation

public enum SessionStatus: String, Codable, Sendable {
    case active = "ACTIVE"
    case closed = "CLOSED"
}

public enum RunStatus: Hashable, Sendable {
    case queued
    case running
    case waitingForApproval
    case waitingForUser
    case completed
    case failed
    case cancelled
    case unknown(String)

    public init(rawValue: String) {
        switch rawValue {
        case "QUEUED": self = .queued
        case "RUNNING": self = .running
        case "WAITING_FOR_APPROVAL": self = .waitingForApproval
        case "WAITING_FOR_USER": self = .waitingForUser
        case "COMPLETED": self = .completed
        case "FAILED": self = .failed
        case "CANCELLED": self = .cancelled
        default: self = .unknown(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .queued: return "QUEUED"
        case .running: return "RUNNING"
        case .waitingForApproval: return "WAITING_FOR_APPROVAL"
        case .waitingForUser: return "WAITING_FOR_USER"
        case .completed: return "COMPLETED"
        case .failed: return "FAILED"
        case .cancelled: return "CANCELLED"
        case .unknown(let value): return value
        }
    }

    public var isTerminal: Bool {
        self == .completed || self == .failed || self == .cancelled
    }

    public var isActive: Bool {
        switch self {
        case .queued, .running, .waitingForApproval, .waitingForUser, .unknown: return true
        case .completed, .failed, .cancelled: return false
        }
    }
}

extension RunStatus: Codable {
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(rawValue: try container.decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

public enum FailureReason: Hashable, Sendable {
    case maxAttemptsExceeded
    case budgetExceeded
    case deadlineExceeded
    case maxStepsExceeded
    case toolLoopDetected
    case repeatedDenial
    case approvalExpired
    case inputDeadlineExceeded
    case contextOverflow
    case modelPermanentError
    case emptyModelTurn
    case authorizationError
    case childRunFailed
    case internalError
    case unknown(String)

    public init(rawValue: String) {
        switch rawValue {
        case "max_attempts_exceeded": self = .maxAttemptsExceeded
        case "budget_exceeded": self = .budgetExceeded
        case "deadline_exceeded": self = .deadlineExceeded
        case "max_steps_exceeded": self = .maxStepsExceeded
        case "tool_loop_detected": self = .toolLoopDetected
        case "repeated_denial": self = .repeatedDenial
        case "approval_expired": self = .approvalExpired
        case "input_deadline_exceeded": self = .inputDeadlineExceeded
        case "context_overflow": self = .contextOverflow
        case "model_permanent_error": self = .modelPermanentError
        case "empty_model_turn": self = .emptyModelTurn
        case "authorization_error": self = .authorizationError
        case "child_run_failed": self = .childRunFailed
        case "internal_error": self = .internalError
        default: self = .unknown(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .maxAttemptsExceeded: return "max_attempts_exceeded"
        case .budgetExceeded: return "budget_exceeded"
        case .deadlineExceeded: return "deadline_exceeded"
        case .maxStepsExceeded: return "max_steps_exceeded"
        case .toolLoopDetected: return "tool_loop_detected"
        case .repeatedDenial: return "repeated_denial"
        case .approvalExpired: return "approval_expired"
        case .inputDeadlineExceeded: return "input_deadline_exceeded"
        case .contextOverflow: return "context_overflow"
        case .modelPermanentError: return "model_permanent_error"
        case .emptyModelTurn: return "empty_model_turn"
        case .authorizationError: return "authorization_error"
        case .childRunFailed: return "child_run_failed"
        case .internalError: return "internal_error"
        case .unknown(let value): return value
        }
    }
}

extension FailureReason: Codable {
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(rawValue: try container.decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
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
    public let lastRunID: UUID?

    enum CodingKeys: String, CodingKey {
        case id, status, title, metadata
        case agentID = "agent_id"
        case agentVersion = "agent_version"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case activeRunID = "active_run_id"
        case lastRunID = "last_run_id"
    }
}

public enum BrowserProfileStatus: String, Codable, Hashable, Sendable {
    case provisioning
    case authenticationRequired = "authentication_required"
    case ready
    case needsUser = "needs_user"
    case revoked
}

public struct BrowserProfileView: Codable, Identifiable, Hashable, Sendable {
    public let id: UUID
    public let allowedOrigins: [String]
    public let status: BrowserProfileStatus
    public let generation: Int
    public let createdAt: Date
    public let updatedAt: Date
    public let lastUsedAt: Date?

    enum CodingKeys: String, CodingKey {
        case id, status, generation
        case allowedOrigins = "allowed_origins"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
        case lastUsedAt = "last_used_at"
    }
}

public enum BrowserAuthenticationStatus: String, Codable, Hashable, Sendable {
    case authenticationRequired = "authentication_required"
    case needsUser = "needs_user"
    case ready
    case expired
    case cancelled

    public var isTerminal: Bool {
        self == .ready || self == .expired || self == .cancelled
    }
}

public struct BrowserAuthenticationView: Codable, Identifiable, Hashable, Sendable {
    public let id: UUID
    public let profileID: UUID
    public let status: BrowserAuthenticationStatus
    public let expiresAt: Date
    public let launchURL: URL?

    enum CodingKeys: String, CodingKey {
        case id, status
        case profileID = "profile_id"
        case expiresAt = "expires_at"
        case launchURL = "launch_url"
    }
}

public enum SessionMessageRole: Hashable, Sendable {
    case user
    case assistant
    case unknown(String)

    public init(rawValue: String) {
        switch rawValue {
        case "user": self = .user
        case "assistant": self = .assistant
        default: self = .unknown(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .user: return "user"
        case .assistant: return "assistant"
        case .unknown(let value): return value
        }
    }
}

extension SessionMessageRole: Codable {
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(rawValue: try container.decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

public struct SessionMessageView: Codable, Sendable {
    public let sequence: Int
    public let role: SessionMessageRole
    public let content: [ContentBlock]
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

    public var userFacingMessage: String {
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return "The run ended because \(reason.displayName.lowercased())."
        }
        return trimmed
    }

    public var diagnosticSummary: String {
        var parts = [reason.displayName]
        if let stepNumber { parts.append("Step \(stepNumber)") }
        if let attemptNumber { parts.append("Attempt \(attemptNumber)") }
        return parts.joined(separator: " · ")
    }

    enum CodingKeys: String, CodingKey {
        case reason, message
        case stepNumber = "step_number"
        case attemptNumber = "attempt_number"
        case occurredAt = "occurred_at"
    }
}

private extension FailureReason {
    var displayName: String {
        switch self {
        case .maxAttemptsExceeded: "Maximum attempts exceeded"
        case .budgetExceeded: "Budget exceeded"
        case .deadlineExceeded: "Deadline exceeded"
        case .maxStepsExceeded: "Maximum steps exceeded"
        case .toolLoopDetected: "Tool loop detected"
        case .repeatedDenial: "Repeated denial"
        case .approvalExpired: "Approval expired"
        case .inputDeadlineExceeded: "Input deadline exceeded"
        case .contextOverflow: "Context overflow"
        case .modelPermanentError: "Model error"
        case .emptyModelTurn: "Empty model response"
        case .authorizationError: "Authorization error"
        case .childRunFailed: "Child run failed"
        case .internalError: "Internal error"
        case .unknown(let value):
            sentenceCase(value.replacingOccurrences(of: "_", with: " "))
        }
    }

    private func sentenceCase(_ value: String) -> String {
        guard let first = value.first else { return "Unknown failure" }
        return first.uppercased() + value.dropFirst()
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

public enum ScheduleStateKind: String, Codable, CaseIterable, Sendable {
    case active = "ACTIVE"
    case paused = "PAUSED"
    case completed = "COMPLETED"
    case cancelled = "CANCELLED"
}

public enum ScheduleCadenceKind: String, Codable, CaseIterable, Sendable {
    case once = "ONCE"
    case daily = "DAILY"
    case weekly = "WEEKLY"
    case monthly = "MONTHLY"
    case yearly = "YEARLY"
}

public struct ScheduleMonthDayView: Codable, Equatable, Sendable {
    public let month: Int
    public let day: Int

    public init(month: Int, day: Int) {
        self.month = month
        self.day = day
    }
}

/// A forward-compatible projection of the server's closed cadence union.
/// `kind` remains a raw string so an additive server value can still render
/// generically on an older client (ADR-0075 decision 7).
public struct ScheduleCadenceView: Codable, Equatable, Sendable {
    public let kind: String
    public let at: Date?
    public let localTime: String?
    public let timezone: String?
    public let weekdays: [Int]?
    public let daysOfMonth: [Int]?
    public let lastDay: Bool?
    public let dates: [ScheduleMonthDayView]?

    enum CodingKeys: String, CodingKey {
        case kind, at, timezone, weekdays, dates
        case localTime = "local_time"
        case daysOfMonth = "days_of_month"
        case lastDay = "last_day"
    }

    public var kindKind: ScheduleCadenceKind? { ScheduleCadenceKind(rawValue: kind) }
}

public struct ScheduleListItemView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let state: String
    public let pauseReason: String?
    public let currentRevision: Int
    public let nextFireAt: Date?
    public let title: String
    public let instructionPreview: String
    public let cadence: ScheduleCadenceView
    public let createdAt: Date
    public let updatedAt: Date

    enum CodingKeys: String, CodingKey {
        case id, state, title, cadence
        case pauseReason = "pause_reason"
        case currentRevision = "current_revision"
        case nextFireAt = "next_fire_at"
        case instructionPreview = "instruction_preview"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public var stateKind: ScheduleStateKind? { ScheduleStateKind(rawValue: state) }
}

public struct ScheduleIdentityView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let tenantID: String
    public let principalID: String
    public let state: String
    public let pauseReason: String?
    public let currentRevision: Int
    public let nextFireAt: Date?
    public let consecutiveFailures: Int
    public let createdAt: Date
    public let updatedAt: Date

    enum CodingKeys: String, CodingKey {
        case id, state
        case tenantID = "tenant_id"
        case principalID = "principal_id"
        case pauseReason = "pause_reason"
        case currentRevision = "current_revision"
        case nextFireAt = "next_fire_at"
        case consecutiveFailures = "consecutive_failures"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public var stateKind: ScheduleStateKind? { ScheduleStateKind(rawValue: state) }
}

public struct ScheduleRunLimitsView: Codable, Equatable, Sendable {
    public let maxSteps: Int
    public let maxModelCalls: Int
    public let maxToolCalls: Int
    public let maxInputTokens: Int?
    public let maxOutputTokens: Int?
    public let maxCost: String?
    public let deadlineAt: Date?
    /// Added after the original scheduling control plane. These stay optional
    /// so a client can inspect schedules on an older server that still exposes
    /// the Milestone 11 routes.
    public let synthesisReserveSteps: Int?
    public let synthesisReserveModelCalls: Int?
    public let synthesisReserveCost: String?

    enum CodingKeys: String, CodingKey {
        case maxSteps = "max_steps"
        case maxModelCalls = "max_model_calls"
        case maxToolCalls = "max_tool_calls"
        case maxInputTokens = "max_input_tokens"
        case maxOutputTokens = "max_output_tokens"
        case maxCost = "max_cost"
        case deadlineAt = "deadline_at"
        case synthesisReserveSteps = "synthesis_reserve_steps"
        case synthesisReserveModelCalls = "synthesis_reserve_model_calls"
        case synthesisReserveCost = "synthesis_reserve_cost"
    }
}

public struct ScheduleRevisionView: Codable, Equatable, Sendable {
    public let scheduleID: UUID
    public let revision: Int
    public let title: String
    public let instruction: String
    public let agentID: UUID
    public let agentVersion: String
    public let policyProfile: String
    public let requestedScopes: [String]
    public let limits: ScheduleRunLimitsView
    public let runTimeoutSeconds: Int
    public let cadence: ScheduleCadenceView
    public let timezone: String?
    public let misfireGraceSeconds: Int
    public let maxConsecutiveFailures: Int
    public let createdByPrincipalID: String
    public let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case revision, title, instruction, limits, cadence, timezone
        case scheduleID = "schedule_id"
        case agentID = "agent_id"
        case agentVersion = "agent_version"
        case policyProfile = "policy_profile"
        case requestedScopes = "requested_scopes"
        case runTimeoutSeconds = "run_timeout_seconds"
        case misfireGraceSeconds = "misfire_grace_seconds"
        case maxConsecutiveFailures = "max_consecutive_failures"
        case createdByPrincipalID = "created_by_principal_id"
        case createdAt = "created_at"
    }
}

public struct ScheduleRecordView: Codable, Equatable, Sendable {
    public let schedule: ScheduleIdentityView
    public let revision: ScheduleRevisionView
}

public enum MemoryStatusKind: String, Codable, CaseIterable, Sendable {
    case candidate
    case provisional
    case active
    case superseded
    case expired
    case retired
}

public enum MemoryBeliefTypeKind: String, Codable, CaseIterable, Sendable {
    case fact
    case preference
    case relationship
    case userModelAttr = "user_model_attr"
    case procedurePointer = "procedure_pointer"
}

public enum MemoryPolarityKind: String, Codable, CaseIterable, Sendable {
    case assert
    case retract
}

public enum MemoryPortabilityKind: String, Codable, CaseIterable, Sendable {
    case portable
    case contextual
    case local
}

public enum MemoryAuthorityKind: String, Codable, CaseIterable, Sendable {
    case user
    case affirmed
    case inferred
}

public enum MemorySensitivityKind: String, Codable, CaseIterable, Sendable {
    case `public`
    case `internal`
    case sensitive
    case restricted
}

/// The public projection of a belief, mirroring the server's `MemoryView`
/// exposure list field-for-field (memory-read-api-and-browser.md). The
/// enum-like fields decode as raw strings with typed known-case accessors
/// below, so a value this client does not yet know about decodes instead of
/// throwing (ADR-0049 decision 4).
public struct MemoryView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let subject: String
    public let statement: String
    public let beliefType: String
    public let claimKind: String
    public let derivation: String
    public let longevity: String
    public let status: String
    public let polarity: String
    public let scope: String
    public let portability: String
    public let authority: String
    public let sensitivity: String
    public let confidence: Double
    public let corroborationCount: Int
    public let flaggedForReview: Bool
    public let conflictsWith: [UUID]
    public let supersededBy: UUID?
    public let sourceSessionID: UUID
    public let sourceEventIDs: [Int]
    public let formationRunID: UUID
    public let consolidationPolicyVersion: String
    public let originScopes: [String]
    public let validFrom: Date
    public let validTo: Date?
    public let expiresAt: Date?
    public let lastEvidenceAt: Date
    public let lastUsedAt: Date?
    public let lastReinforcedAt: Date
    public let createdAt: Date
    public let updatedAt: Date

    enum CodingKeys: String, CodingKey {
        case id, subject, statement, status, polarity, scope, portability, authority, sensitivity,
            confidence
        case beliefType = "belief_type"
        case claimKind = "claim_kind"
        case derivation
        case longevity
        case corroborationCount = "corroboration_count"
        case flaggedForReview = "flagged_for_review"
        case conflictsWith = "conflicts_with"
        case supersededBy = "superseded_by"
        case sourceSessionID = "source_session_id"
        case sourceEventIDs = "source_event_ids"
        case formationRunID = "formation_run_id"
        case consolidationPolicyVersion = "consolidation_policy_version"
        case originScopes = "origin_scopes"
        case validFrom = "valid_from"
        case validTo = "valid_to"
        case expiresAt = "expires_at"
        case lastEvidenceAt = "last_evidence_at"
        case lastUsedAt = "last_used_at"
        case lastReinforcedAt = "last_reinforced_at"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }

    public var statusKind: MemoryStatusKind? { MemoryStatusKind(rawValue: status) }
    public var beliefTypeKind: MemoryBeliefTypeKind? { MemoryBeliefTypeKind(rawValue: beliefType) }
    public var polarityKind: MemoryPolarityKind? { MemoryPolarityKind(rawValue: polarity) }
    public var portabilityKind: MemoryPortabilityKind? {
        MemoryPortabilityKind(rawValue: portability)
    }
    public var authorityKind: MemoryAuthorityKind? { MemoryAuthorityKind(rawValue: authority) }
    public var sensitivityKind: MemorySensitivityKind? {
        MemorySensitivityKind(rawValue: sensitivity)
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
        case .text(let text):
            try container.encode("text", forKey: .type)
            try container.encode(text, forKey: .text)
        case .image(let artifactID, let mediaType, let detail):
            try container.encode("image", forKey: .type)
            try container.encode(artifactID, forKey: .artifactID)
            try container.encode(mediaType, forKey: .mediaType)
            try container.encode(detail, forKey: .detail)
        case .file(let artifactID, let mediaType, let filename):
            try container.encode("file", forKey: .type)
            try container.encode(artifactID, forKey: .artifactID)
            try container.encode(mediaType, forKey: .mediaType)
            try container.encodeIfPresent(filename, forKey: .filename)
        }
    }

    public var text: String? {
        guard case .text(let value) = self else { return nil }
        return value
    }

    public var artifactID: UUID? {
        switch self {
        case .text:
            return nil
        case .image(let artifactID, _, _), .file(let artifactID, _, _):
            return artifactID
        }
    }
}

/// One persona entry as the server renders it: text, provenance, and tier
/// (persona-surface.md). `sourceBeliefID` is set exactly when the entry was
/// affirmed from a nomination rather than typed by the owner.
public struct PersonaEntryView: Codable, Equatable, Sendable {
    public let text: String
    public let source: String
    public let sourceBeliefID: UUID?
    public let sensitivity: String

    enum CodingKeys: String, CodingKey {
        case text, source, sensitivity
        case sourceBeliefID = "source_belief_id"
    }

    public init(text: String, source: String, sourceBeliefID: UUID?, sensitivity: String) {
        self.text = text
        self.source = source
        self.sourceBeliefID = sourceBeliefID
        self.sensitivity = sensitivity
    }
}

/// The persona document head, mirroring the server's `PersonaView` exposure
/// list; version 0 with no entries is the real, unwritten starting state.
public struct PersonaView: Codable, Equatable, Sendable {
    public let version: Int
    public let entries: [PersonaEntryView]
    public let source: String
    public let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case version, entries, source
        case createdAt = "created_at"
    }

    public init(version: Int, entries: [PersonaEntryView], source: String, createdAt: Date) {
        self.version = version
        self.entries = entries
        self.source = source
        self.createdAt = createdAt
    }
}

/// A consolidation-raised persona candidate awaiting the owner's verdict.
public struct PersonaNominationView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let beliefID: UUID
    public let statement: String
    public let beliefType: String
    public let authority: String
    public let confidence: Double
    public let corroborationCount: Int
    public let sensitivity: String
    public let state: String
    public let nominatedAt: Date
    public let resolvedAt: Date?
    public let affirmedVersion: Int?

    enum CodingKeys: String, CodingKey {
        case id, statement, authority, confidence, sensitivity, state
        case beliefID = "belief_id"
        case beliefType = "belief_type"
        case corroborationCount = "corroboration_count"
        case nominatedAt = "nominated_at"
        case resolvedAt = "resolved_at"
        case affirmedVersion = "affirmed_version"
    }
}

/// One entry of a guarded persona replacement (`PUT /v1/persona`).
public struct UpdatePersonaEntryBody: Codable, Equatable, Sendable {
    public let text: String
    public let sensitivity: String
    public let sourceBeliefID: UUID?

    enum CodingKeys: String, CodingKey {
        case text, sensitivity
        case sourceBeliefID = "source_belief_id"
    }

    public init(text: String, sensitivity: String = "internal", sourceBeliefID: UUID? = nil) {
        self.text = text
        self.sensitivity = sensitivity
        self.sourceBeliefID = sourceBeliefID
    }
}

/// The guarded persona replacement request body.
public struct UpdatePersonaBody: Codable, Equatable, Sendable {
    public let expectedVersion: Int
    public let entries: [UpdatePersonaEntryBody]

    enum CodingKeys: String, CodingKey {
        case expectedVersion = "expected_version"
        case entries
    }

    public init(expectedVersion: Int, entries: [UpdatePersonaEntryBody]) {
        self.expectedVersion = expectedVersion
        self.entries = entries
    }
}

/// One pending device-scoped call, as the device's fetch route returns it
/// (device-channel-and-sms.md). Mirrors the server's `DeviceInvocationView`.
public struct DeviceInvocationView: Codable, Sendable, Equatable {
    public let id: UUID
    public let toolName: String
    public let arguments: [String: JSONValue]
    public let createdAt: Date
    public let expiresAt: Date

    enum CodingKeys: String, CodingKey {
        case id, arguments
        case toolName = "tool_name"
        case createdAt = "created_at"
        case expiresAt = "expires_at"
    }
}

/// Everything one device still owes an answer for, oldest first. A device's
/// pending queue is bounded by the invocation timeout rather than a page
/// size, so this is a whole answer rather than a keyset page.
public struct DeviceInvocationList: Codable, Sendable {
    public let invocations: [DeviceInvocationView]
}

/// The recorded terminal state of one device-scoped call, as the server
/// returns it after `postInvocationResult`. `status` stays a raw string here
/// (not `DeviceInvocationResult`) so a value this client does not yet know
/// about still decodes.
public struct DeviceInvocationResultView: Codable, Sendable {
    public let id: UUID
    public let status: String
    public let resolvedAt: Date?

    enum CodingKeys: String, CodingKey {
        case id, status
        case resolvedAt = "resolved_at"
    }
}

/// Where one ingested device message was routed, and whether it was a
/// replay of a message already seen (the ingest route is digest-idempotent).
public struct DeviceIngestResult: Codable, Sendable {
    public let duplicate: Bool
    public let sessionID: UUID
    public let runID: UUID

    enum CodingKeys: String, CodingKey {
        case duplicate
        case sessionID = "session_id"
        case runID = "run_id"
    }
}

/// The single terminal outcome a device may post for one invocation. Four
/// tokens, not three, and frozen: a client that watched its own deadline
/// pass reports `expired` rather than inventing a `failed` the owner never
/// saw. A 409 posting this means the invocation is already terminally
/// settled server-side — callers must never retry.
public enum DeviceInvocationResult: String, Codable, Sendable {
    case sent
    case cancelled
    case failed
    case expired
}
