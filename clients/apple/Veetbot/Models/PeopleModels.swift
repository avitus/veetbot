import Foundation

public struct PeopleLookup: Identifiable, Sendable {
    public let id = UUID()
    public let text: String
    public let asOf: Date?
    public init(text: String = "", asOf: Date? = nil) { self.text = text; self.asOf = asOf }

    /// This is a search hint; choosing a candidate never confirms or combines an identity.
    public static func correspondent(_ sender: String, at: Date) -> PeopleLookup {
        let trimmed = sender.trimmingCharacters(in: .whitespacesAndNewlines)
        if let start = trimmed.lastIndex(of: "<"), let end = trimmed.lastIndex(of: ">"), start < end {
            return PeopleLookup(text: String(trimmed[trimmed.index(after: start)..<end]), asOf: at)
        }
        return PeopleLookup(text: trimmed, asOf: at)
    }
}

public struct PersonView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let revision: Int
    public let displayName: String
    public let state: String
    public let pinned: Bool
    public let sensitivity: String
    public let supportIDs: [UUID]
    enum CodingKeys: String, CodingKey {
        case id, revision, state, pinned, sensitivity
        case displayName = "display_name"
        case supportIDs = "support_ids"
    }
}

public struct PersonAliasView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let revision: Int
    public let value: String
    public let identifierKind: String
    public let verification: String
    public let context: String?
    public let validTo: Date?
    public let supportIDs: [UUID]
    enum CodingKeys: String, CodingKey {
        case id, revision, value, verification, context
        case validTo = "valid_to"
        case identifierKind = "identifier_kind"
        case supportIDs = "support_ids"
    }
}

public struct PersonEndpointView: Codable, Equatable, Sendable {
    public let kind: String
    public let id: UUID?
    var correctionValue: JSONValue {
        var values: [String: JSONValue] = ["kind": .string(kind)]
        if let id { values["id"] = .string(id.uuidString) }
        return .object(values)
    }
}

public struct PersonRelationshipView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let revision: Int
    public let beliefID: UUID?
    public let subject: PersonEndpointView
    public let object: PersonEndpointView
    public let predicate: String
    public let qualifier: String
    public let validFrom: Date?
    public let validTo: Date?
    public let precision: String
    public let sourceTimezone: String?
    public let supportIDs: [UUID]
    public var sinceLabel: String? { validFrom.map { "Since \(peopleDateLabel($0, precision: precision, sourceTimezone: sourceTimezone))" } }
    public var untilLabel: String? { validTo.map { "Until \(peopleDateLabel($0, precision: precision, sourceTimezone: sourceTimezone))" } }
    enum CodingKeys: String, CodingKey {
        case beliefID = "belief_id"
        case id, revision, subject, object, predicate, qualifier, precision
        case validFrom = "valid_from"
        case validTo = "valid_to"
        case sourceTimezone = "source_timezone"
        case supportIDs = "support_ids"
    }
}

public struct PersonInteractionView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let revision: Int
    public let channel: String
    public let interactionKind: String
    public let attribution: String
    public let direction: String
    public let summary: String
    public let occurredAt: Date?
    public let precision: String
    public let sourceTimezone: String?
    public let supportIDs: [UUID]
    public let participants: [Participant]
    public var dateLabel: String { peopleDateLabel(occurredAt, precision: precision, sourceTimezone: sourceTimezone) }
    public struct Participant: Codable, Equatable, Sendable {
        public let personID: UUID
        public let role: String
        enum CodingKeys: String, CodingKey { case personID = "person_id"; case role }
    }
    enum CodingKeys: String, CodingKey {
        case id, revision, channel, attribution, direction, summary, precision, participants
        case interactionKind = "interaction_kind"
        case occurredAt = "occurred_at"
        case sourceTimezone = "source_timezone"
        case supportIDs = "support_ids"
    }
}

public struct PersonCommitmentView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let revision: Int
    public let beliefID: UUID?
    public let debtor: PersonEndpointView
    public let beneficiary: PersonEndpointView
    public let description: String
    public let state: String
    public let dueAt: Date?
    public let duePrecision: String?
    public let sourceTimezone: String?
    public let supportIDs: [UUID]
    public var dueLabel: String? {
        guard let dueAt else { return nil }
        guard let duePrecision, duePrecision != "unknown" else { return "Due date precision unknown" }
        return "Due \(peopleDateLabel(dueAt, precision: duePrecision, sourceTimezone: sourceTimezone))"
    }
    enum CodingKeys: String, CodingKey {
        case beliefID = "belief_id"
        case id, revision, debtor, beneficiary, description, state
        case dueAt = "due_at"
        case duePrecision = "due_precision"
        case sourceTimezone = "source_timezone"
        case supportIDs = "support_ids"
    }
}

/// Calendar dates use their source zone and never claim finer precision than the evidence.
private func peopleDateLabel(_ date: Date?, precision: String, sourceTimezone: String?) -> String {
    guard let date else { return "Date unknown" }
    let formatter = DateFormatter()
    formatter.timeZone = sourceTimezone.flatMap(TimeZone.init(identifier:)) ?? TimeZone(secondsFromGMT: 0)
    switch precision {
    case "year": formatter.setLocalizedDateFormatFromTemplate("yyyy")
    case "month": formatter.setLocalizedDateFormatFromTemplate("MMMMyyyy")
    case "day": formatter.setLocalizedDateFormatFromTemplate("yMMMd")
    case "instant": formatter.dateStyle = .medium; formatter.timeStyle = .short
    default: return "Date precision unknown"
    }
    return formatter.string(from: date)
}

public struct PersonProfileView: Codable, Equatable, Sendable {
    public let person: PersonView
    public let aliases: [PersonAliasView]
    public let relationships: [PersonRelationshipView]
    public let history: [PersonInteractionView]
    public let commitments: [PersonCommitmentView]
    public let facts: [MemoryView]
    public let factRevisions: [String: Int]
    public let relatedLabels: [String: String]?
    public let truncated: Bool
    public let coverage: String
    enum CodingKeys: String, CodingKey {
        case person, aliases, relationships, history, commitments, facts, truncated, coverage
        case factRevisions = "fact_revisions"
        case relatedLabels = "related_labels"
    }
    public func factRevision(_ id: UUID) -> Int? { factRevisions[id.uuidString.lowercased()] }
}

public struct PeopleHistoryPage: Codable, Sendable {
    public let items: [PersonInteractionView]
    public let nextCursor: String?
    public let coverage: String
    enum CodingKeys: String, CodingKey { case items, coverage; case nextCursor = "next_cursor" }
}

public struct PeopleIdentityEvidenceView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let revision: Int
    public let kind: String
    public let label: String
    public let supportIDs: [UUID]
    public let beliefID: UUID?
    public let unresolved: Bool
    enum CodingKeys: String, CodingKey {
        case id, revision, kind, label, unresolved
        case supportIDs = "support_ids", beliefID = "belief_id"
    }
}

public struct PeopleOperationView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let revision: Int
    public let state: String
    public let operation: String?
    public let counts: [String: Int]?
    public let scope: String?
    public let expiresAt: Date?
    public let assignments: [Assignment]?
    public struct Assignment: Codable, Equatable, Identifiable, Sendable {
        public let recordID: UUID
        public let expectedRevision: Int
        public var id: UUID { recordID }
        enum CodingKeys: String, CodingKey { case recordID = "entity_id"; case expectedRevision = "expected_revision" }
    }
    enum CodingKeys: String, CodingKey {
        case id, revision, state, operation, counts, scope, assignments
        case expiresAt = "expires_at"
    }
}

public struct PeopleEvidenceView: Codable, Equatable, Sendable {
    public let reference: UUID
    public let sourceKind: String
    public let sessionID: UUID
    public let eventSequence: Int
    public let evidenceAt: Date
    public let accountID: String?
    public let threadID: String?
    public let messageID: String?
    public let emailThreadID: UUID?
    public let ownerAssertion: String?
    enum CodingKeys: String, CodingKey {
        case reference
        case sourceKind = "source_kind"
        case sessionID = "session_id"
        case eventSequence = "event_sequence"
        case evidenceAt = "evidence_at"
        case accountID = "account_id"
        case threadID = "thread_id"
        case messageID = "message_id"
        case emailThreadID = "email_thread_id"
        case ownerAssertion = "owner_assertion"
    }
}

public struct PeopleCorrectionResult: Codable, Sendable {
    public let personRevision: Int
    public let belief: MemoryView?
    public let removed: Bool
    public let erasure: PeopleOperationView?
    enum CodingKeys: String, CodingKey { case personRevision = "person_revision"; case belief, removed, erasure }
}

public struct PeopleImportView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let auditSessionID: UUID?
    public let scope: [String: JSONValue]?
    public let revision: Int
    public let state: String
    public let recordsRead: Int
    public let mailboxRecordsRead: Int?
    public let mailboxReadComplete: Bool?
    public let recordsProcessed: Int
    public let recordsExcluded: Int
    public let failures: Int
    public let spentUSD: String
    public let reservedUSD: String
    public let sourceReadComplete: Bool
    public let analysisComplete: Bool
    public let knownRecords: Int?
    public let remainingRecords: Int?
    public let coverage: String
    public let errorCode: String?
    public var isActive: Bool { state == "queued" || state == "running" }
    enum CodingKeys: String, CodingKey {
        case id, revision, state, failures, coverage, scope
        case auditSessionID = "audit_session_id"
        case mailboxRecordsRead = "mailbox_records_read", mailboxReadComplete = "mailbox_read_complete"
        case recordsRead = "records_read", recordsProcessed = "records_processed", recordsExcluded = "records_excluded"
        case spentUSD = "spent_usd", reservedUSD = "reserved_usd"
        case sourceReadComplete = "source_read_complete", analysisComplete = "analysis_complete"
        case knownRecords = "known_records", remainingRecords = "remaining_records", errorCode = "error_code"
    }
}
