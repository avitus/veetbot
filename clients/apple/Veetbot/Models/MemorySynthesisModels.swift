import Foundation

/// Separate wire projections preserve the distinction between originals and synthesis.
public struct MemorySynthesisSource: Codable, Equatable, Identifiable, Sendable {
    public let beliefID: UUID
    public let contentRevision: Int
    public let subject: String
    public let statement: String
    public let sessionID: UUID
    public let eventIDs: [Int]
    public let omitted: Bool
    public var id: UUID { beliefID }
    enum CodingKeys: String, CodingKey {
        case subject, statement, omitted
        case beliefID = "belief_id", contentRevision = "content_revision"
        case sessionID = "session_id", eventIDs = "event_ids"
    }
}

public struct MemorySynthesisClause: Codable, Equatable, Sendable {
    public let text: String
    public let sourceIDs: [UUID]
    enum CodingKeys: String, CodingKey { case text, sourceIDs = "source_ids" }
}

public struct MemorySynthesisContent: Codable, Equatable, Sendable {
    public let memoryID: UUID
    public let subject: String
    public let statement: String
    public let clauses: [MemorySynthesisClause]
    enum CodingKeys: String, CodingKey { case subject, statement, clauses, memoryID = "memory_id" }
}

public struct MemorySynthesisOperation: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let kind: String
    public let state: String
    public let revision: Int
    public let reason: String
    public let policy: String
    public let modelIdentity: String
    public let createdAt: Date
    public let committedAt: Date
    public let invalidatedAt: Date?
    public let undoneAt: Date?
    public let content: MemorySynthesisContent?
    public let sources: [MemorySynthesisSource]
    enum CodingKeys: String, CodingKey {
        case id, kind, state, revision, reason, policy, content, sources
        case modelIdentity = "model_identity", createdAt = "created_at", committedAt = "committed_at"
        case invalidatedAt = "invalidated_at", undoneAt = "undone_at"
    }
    public var title: String {
        switch kind {
        case "merge": return "Equivalent memories"
        case "summary": return "Related summary"
        case "hypothesis": return "Tentative connection"
        case "conflict": return "Unresolved conflict"
        default: return "Memory operation"
        }
    }
    public var canUndo: Bool {
        kind == "merge" && state == "committed" && revision > 0
            && content != nil && Set(sources.map(\.id)).count >= 2
    }
}

public struct MemorySummaryContent: Codable, Equatable, Sendable {
    public let subject: String
    public let statement: String
    public let clauses: [MemorySynthesisClause]
    public let beliefTypes: [String]
    public let scope: String
    public let portability: String
    public let sensitivity: String
    public let authority: String
    public let confidence: Double
    public let lastEvidenceAt: Date
    public let validFrom: Date
    public let expiresAt: Date?
    enum CodingKeys: String, CodingKey {
        case subject, statement, clauses, scope, portability, sensitivity, authority, confidence
        case beliefTypes = "belief_types", lastEvidenceAt = "last_evidence_at"
        case validFrom = "valid_from", expiresAt = "expires_at"
    }
}

public struct MemorySummaryView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let recordKind: String
    public let operationID: UUID
    public let revision: Int
    public let status: String
    public let flaggedForReview: Bool
    public let createdAt: Date
    public let updatedAt: Date
    public let content: MemorySummaryContent?
    public let sources: [MemorySynthesisSource]
    enum CodingKeys: String, CodingKey {
        case id, revision, status, content, sources
        case recordKind = "record_kind", operationID = "operation_id"
        case flaggedForReview = "flagged_for_review", createdAt = "created_at", updatedAt = "updated_at"
    }
}
