import Foundation

public enum ClientMode: String, CaseIterable, Identifiable, Sendable {
    case chat, email
    public var id: String { rawValue }
    public var title: String { self == .chat ? "Chat" : "Email" }
    public var symbol: String { self == .chat ? "bubble.left.and.bubble.right" : "envelope" }
}

public struct EmailAccountView: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let label: String
    public let emailAddress: String?
    public let status: String
    public let error: String?
    public let lastSyncedAt: Date?
    public let historyComplete: Bool
    public let historyProcessed: Int
    public let readServerID: String?
    public let sendServerID: String?

    enum CodingKeys: String, CodingKey {
        case id, label, status, error
        case emailAddress = "email_address"
        case lastSyncedAt = "last_synced_at"
        case historyComplete = "history_complete"
        case historyProcessed = "history_processed"
        case readServerID = "read_server_id"
        case sendServerID = "send_server_id"
    }

    public var hasRefreshFailure: Bool { status == "unavailable" && error != nil }

    public var updateMessage: String? {
        if hasRefreshFailure { return "Account could not be updated. Try refreshing again." }
        if status == "unavailable" { return "Waiting for an email update." }
        if status == "syncing" { return "Updating — results may be incomplete." }
        return nil
    }
}

public struct EmailAttachmentView: Codable, Equatable, Sendable {
    public let filename: String
    public let mimeType: String
    public let size: Int?
    enum CodingKeys: String, CodingKey {
        case filename, size
        case mimeType = "mime_type"
    }
}

public struct EmailMessageView: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let sender: String
    public let to: [String]
    public let cc: [String]
    public let subject: String
    public let body: String
    public let sentAt: Date
    public let complete: Bool
    public let attachments: [EmailAttachmentView]?
    enum CodingKeys: String, CodingKey {
        case id, sender, to, cc, subject, body, complete, attachments
        case sentAt = "sent_at"
    }
}

/// The list and point read share their bounded summary fields. Message bodies
/// and the current draft are present only on a point read.
public struct EmailThreadView: Codable, Identifiable, Equatable, Sendable {
    public let id: UUID
    public let accountID: String
    public let subject: String
    public let senders: [String]
    public let updatedAt: Date
    public let revision: Int
    public var dismissedRevision: Int?
    public let summary: String
    public let reason: String
    public let needsReply: Bool
    public let draftID: UUID?
    public let sessionID: UUID?
    public let priority: Double
    public let complete: Bool
    public let messages: [EmailMessageView]?
    public let draft: EmailDraftView?

    public var isHandled: Bool { dismissedRevision == revision }

    enum CodingKeys: String, CodingKey {
        case id, subject, senders, revision, summary, reason, priority, complete, messages, draft
        case accountID = "account_id"
        case updatedAt = "updated_at"
        case needsReply = "needs_reply"
        case dismissedRevision = "dismissed_revision"
        case draftID = "draft_id"
        case sessionID = "session_id"
    }
}

public struct EmailDraftView: Codable, Identifiable, Equatable, Sendable {
    public let id: UUID
    public let threadID: UUID
    public let accountID: String
    public let revision: Int
    public let sourceRevision: Int
    public let providerThreadID: String?
    public let sendToolName: String?
    public let to: [String]
    public let cc: [String]
    public let bcc: [String]
    public let subject: String
    public let body: String
    /// Raw strings keep future server states readable without granting actions.
    public let status: String
    public let stale: Bool
    public let runID: UUID?
    public let approvalID: UUID?
    public let sessionID: UUID?
    public let updatedAt: Date

    public var canEdit: Bool { ["ready", "awaiting_approval", "failed"].contains(status) }
    public var isProcessing: Bool { ["generating", "sending"].contains(status) }

    enum CodingKeys: String, CodingKey {
        case id, revision, to, cc, bcc, subject, body, status, stale
        case threadID = "thread_id"
        case accountID = "account_id"
        case sourceRevision = "source_revision"
        case providerThreadID = "provider_thread_id"
        case sendToolName = "send_tool_name"
        case runID = "run_id"
        case approvalID = "approval_id"
        case sessionID = "session_id"
        case updatedAt = "updated_at"
    }
}

public struct EmailOperationView: Codable, Sendable {
    public let operationID: UUID
    public let runID: UUID
    public let status: RunStatus
    public let replayed: Bool?
    enum CodingKeys: String, CodingKey {
        case status, replayed
        case operationID = "operation_id"
        case runID = "run_id"
    }
}

public struct EmailDraftOperation: Codable, Sendable {
    public let draft: EmailDraftView?
    public let runID: UUID
    public let status: RunStatus
    public let approvalID: UUID?
    enum CodingKeys: String, CodingKey {
        case draft, status
        case runID = "run_id"
        case approvalID = "approval_id"
    }
}

public struct EmailFeedbackResult: Codable, Sendable {
    public let feedbackID: UUID
    public let thread: EmailThreadView
    enum CodingKeys: String, CodingKey {
        case thread
        case feedbackID = "feedback_id"
    }
}

public enum EmailFeedbackTarget: String, CaseIterable, Sendable {
    case thread, person, topic
    public var title: String {
        switch self {
        case .thread: "This thread"
        case .person: "This person"
        case .topic: "This kind of content"
        }
    }
}

public struct EmailLearningState: Codable, Sendable {
    public let paused: Bool
    public let profileRevision: Int
    public let excludedSources: Int
    public let styleExamples: Int
    public let historyProcessed: Int
    public let historyComplete: Bool
    enum CodingKeys: String, CodingKey {
        case paused
        case profileRevision = "profile_revision"
        case excludedSources = "excluded_sources"
        case styleExamples = "style_examples"
        case historyProcessed = "history_processed"
        case historyComplete = "history_complete"
    }
}

public struct EmailDraftEdit: Equatable, Sendable {
    public var base: EmailDraftView
    public var to: String
    public var cc: String
    public var bcc: String
    public var subject: String
    public var body: String
    public var reviewedSourceRevision: Int?

    public init(_ draft: EmailDraftView) {
        base = draft
        to = draft.to.joined(separator: ", ")
        cc = draft.cc.joined(separator: ", ")
        bcc = draft.bcc.joined(separator: ", ")
        subject = draft.subject
        body = draft.body
        reviewedSourceRevision = nil
    }

    public var isDirty: Bool {
        to != base.to.joined(separator: ", ") || cc != base.cc.joined(separator: ", ")
            || bcc != base.bcc.joined(separator: ", ") || subject != base.subject || body != base.body
            || (reviewedSourceRevision != nil && reviewedSourceRevision != base.sourceRevision)
    }

    static func addresses(_ text: String) -> [String] {
        text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }
}
