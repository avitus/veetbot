import Foundation

public struct EmailAccountView: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let label: String
    public let emailAddress: String?
    public let status: String
    /// A recorded refresh failure; absence does not prove the account has completed its first update.
    public let error: String?
    public let lastSyncedAt: Date?
    public let historyComplete: Bool
    public let historyProcessed: Int
    public let readServerID: String?
    public let sendServerID: String?
    /// Absent support on an older server never authorizes a Gmail mutation.
    public let archiveSupported: Bool?
    public let writeServerID: String?
    /// Absent support on an older server shows no Subscriptions surface at all.
    public let unsubscribeSupported: Bool?
    /// Absent support on an older server shows no spam tag action and never falls back to Archive.
    public let spamSupported: Bool?
    /// Whether the inbox catch-up has reached its floor; absent on an older server.
    public let inboxComplete: Bool?
    /// The oldest date the automatic catch-up covers.
    public let catchUpSince: Date?
    /// The oldest inbox mail the current catch-up pass has read so far.
    public let inboxReachedAt: Date?

    enum CodingKeys: String, CodingKey {
        case id, label, status, error
        case emailAddress = "email_address"
        case lastSyncedAt = "last_synced_at"
        case historyComplete = "history_complete"
        case historyProcessed = "history_processed"
        case readServerID = "read_server_id"
        case sendServerID = "send_server_id"
        case archiveSupported = "archive_supported"
        case writeServerID = "write_server_id"
        case unsubscribeSupported = "unsubscribe_supported"
        case spamSupported = "spam_supported"
        case inboxComplete = "inbox_complete"
        case catchUpSince = "catch_up_since"
        case inboxReachedAt = "inbox_reached_at"
    }

    /// Distinguishes a failed attempt from an account still waiting for its initial synchronization.
    public var hasRefreshFailure: Bool { status == "unavailable" && error != nil }

    /// Describes pending, incomplete or failed refreshes without claiming cached mail is current.
    public var updateMessage: String? {
        if hasRefreshFailure { return "Account could not be updated. Try refreshing again." }
        if status == "unavailable" { return "Waiting for an email update." }
        if status == "syncing" { return "Updating — results may be incomplete." }
        return nil
    }

    /// Explains how far back the automatic catch-up has read, so older mail surfacing makes sense.
    public func coverageMessage(locale: Locale = .current, timeZone: TimeZone = .current) -> String? {
        guard let inboxComplete else { return nil }
        var style = Date.FormatStyle.dateTime.month(.abbreviated).day().locale(locale)
        style.timeZone = timeZone
        guard let since = catchUpSince?.formatted(style) else {
            return inboxComplete ? nil : "Checking recent inbox mail. New mail still arrives first."
        }
        if inboxComplete { return "Inbox mail back to \(since) has been checked." }
        if let reached = inboxReachedAt?.formatted(style) {
            return "Checking inbox mail back to \(since) — reached \(reached). New mail still arrives first."
        }
        return "Checking inbox mail back to \(since). New mail still arrives first."
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
    /// Inert reading Markdown derived from the retained source; absent on older servers.
    public let readerBody: String?
    public let sentAt: Date
    public let complete: Bool
    public let attachments: [EmailAttachmentView]?
    enum CodingKeys: String, CodingKey {
        case id, sender, to, cc, subject, body, complete, attachments
        case sentAt = "sent_at"
        case readerBody = "reader_body"
    }
}

/// The list and point read share their bounded summary fields. Message bodies
/// and the current draft are present only on a point read.
public struct EmailThreadView: Codable, Identifiable, Equatable, Sendable {
    public let id: UUID
    public let accountID: String
    public let subject: String
    public let senders: [String]
    /// Server-assessed content categories; older responses may omit this field.
    public let topics: [String]?
    public let updatedAt: Date
    public let revision: Int
    /// The source revision the owner handled; later source revisions reopen the thread.
    public var dismissedRevision: Int?
    /// Missing Inbox state is unknown, rather than evidence of an archived conversation.
    public var inInbox: Bool?
    public var archiveOperation: EmailArchiveOperation?
    /// Present only when this conversation's newest received message belongs to a bulk sender.
    public let subscription: EmailThreadSubscription?
    /// The server flagged this conversation as suspected spam; absent on older servers.
    public var suspectedSpam: Bool?
    /// The owner said this flagged conversation is not spam.
    public var spamCleared: Bool?
    /// The owner reported this conversation as spam; it sits in Gmail's Spam.
    public var inSpam: Bool?
    public let summary: String
    public let reason: String
    public let needsReply: Bool
    public let draftID: UUID?
    public let sessionID: UUID?
    public let priority: Double
    public let complete: Bool
    public let messages: [EmailMessageView]?
    public let draft: EmailDraftView?

    /// Reflects confirmed attention state only while it still matches the displayed source revision.
    public var isHandled: Bool { dismissedRevision == revision }

    /// Only the server's confirmed mailbox projection determines the checkbox state.
    public var isArchived: Bool { inInbox == false }

    /// A flag never moves mail; a reported conversation shows its Spam state instead.
    public var isSuspectedSpam: Bool { suspectedSpam == true && inSpam != true }

    public var isInSpam: Bool { inSpam == true }

    /// Preserve exact server values while hiding empty or duplicate picker entries.
    public var feedbackTopics: [String] {
        var seen = Set<String>()
        return (topics ?? []).filter {
            !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && seen.insert($0).inserted
        }
    }

    enum CodingKeys: String, CodingKey {
        case id, subject, senders, topics, revision, summary, reason, priority, complete, messages, draft
        case subscription
        case accountID = "account_id"
        case updatedAt = "updated_at"
        case needsReply = "needs_reply"
        case dismissedRevision = "dismissed_revision"
        case inInbox = "in_inbox"
        case archiveOperation = "archive_operation"
        case suspectedSpam = "suspected_spam"
        case spamCleared = "spam_cleared"
        case inSpam = "in_spam"
        case draftID = "draft_id"
        case sessionID = "session_id"
    }
}

/// Durable archive progress is separate from both source revision and legacy attention state.
public struct EmailArchiveOperation: Codable, Equatable, Sendable {
    public let operationID: UUID
    public let runID: UUID
    public let targetArchived: Bool
    /// A spam report or its reversal; absent on older servers and for a plain archive.
    public var targetSpam: Bool?
    public let status: String
    public let error: String?

    public var isSpamAction: Bool { targetSpam == true }

    enum CodingKeys: String, CodingKey {
        case status, error
        case operationID = "operation_id"
        case runID = "run_id"
        case targetArchived = "target_archived"
        case targetSpam = "target_spam"
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

/// Server collections of cached Inbox mail, independent of Gmail write actions.
public enum EmailCollection: String, CaseIterable, Codable, Sendable {
    case inbox
    case needsYou = "needs_you"
    case handled

    public var title: String {
        switch self {
        case .inbox: "Inbox"
        case .needsYou: "Needs you"
        case .handled: "Handled"
        }
    }
}

/// Counts cover the authorized account/search result before pagination.
public struct EmailCollectionCounts: Codable, Equatable, Sendable {
    public let inbox: Int
    public let needsYou: Int
    public let handled: Int

    public func count(for collection: EmailCollection) -> Int {
        switch collection {
        case .inbox: inbox
        case .needsYou: needsYou
        case .handled: handled
        }
    }

    enum CodingKeys: String, CodingKey {
        case inbox, handled
        case needsYou = "needs_you"
    }
}

/// Older servers omit collection counts; absence must not be displayed as zero.
public struct EmailThreadPage: Codable, Sendable {
    public let items: [EmailThreadView]
    public let nextCursor: String?
    public let collectionCounts: EmailCollectionCounts?

    enum CodingKeys: String, CodingKey {
        case items
        case nextCursor = "next_cursor"
        case collectionCounts = "collection_counts"
    }
}

/// How a row names the people in a conversation. The server hands senders over
/// as RFC 5322 mailboxes (`Name <address>`, the name sometimes quoted), so the
/// name is shown when the mail carried one and the address only otherwise.
public enum EmailCorrespondent {
    /// The display name of one mailbox, or its address when it has no name.
    public static func displayName(_ sender: String) -> String {
        let mailbox = parts(of: sender)
        return mailbox.name.isEmpty ? mailbox.address : mailbox.name
    }

    /// One line naming each distinct correspondent once, in the server's order.
    public static func line(for senders: [String]) -> String {
        var addresses = Set<String>()
        var shown = Set<String>()
        var names: [String] = []
        for sender in senders {
            let mailbox = parts(of: sender)
            if !mailbox.address.isEmpty, !addresses.insert(mailbox.address.lowercased()).inserted { continue }
            let name = mailbox.name.isEmpty ? mailbox.address : mailbox.name
            guard !name.isEmpty, shown.insert(name.lowercased()).inserted else { continue }
            names.append(name)
        }
        return names.joined(separator: ", ")
    }

    static func parts(of sender: String) -> (name: String, address: String) {
        var text = sender.trimmingCharacters(in: .whitespacesAndNewlines)
        var address = ""
        if let open = text.lastIndex(of: "<"), let close = text[open...].firstIndex(of: ">") {
            address = text[text.index(after: open)..<close].trimmingCharacters(in: .whitespaces)
            text.removeSubrange(open...close)
        } else if text.contains("@"), !text.contains(where: \.isWhitespace) {
            address = text
            text = ""
        }
        var name = text.trimmingCharacters(in: .whitespacesAndNewlines)
        // A quoted name, possibly quoted again with escaped quotes inside.
        while name.count >= 2, name.hasPrefix("\""), name.hasSuffix("\"") {
            name = String(name.dropFirst().dropLast())
                .replacingOccurrences(of: "\\\"", with: "\"")
                .trimmingCharacters(in: .whitespacesAndNewlines)
        }
        return (name.replacingOccurrences(of: "\\\\", with: "\\"), address)
    }
}

extension EmailThreadView {
    /// The people who wrote, by name where the mail gave one, each once.
    public var correspondents: String { EmailCorrespondent.line(for: senders) }
}

/// The recency label on a row: the time today, Yesterday, the weekday within
/// the week, then the date, with the year once it differs from this one.
public enum EmailRowDate {
    public static func label(
        for date: Date, relativeTo now: Date, calendar: Calendar = .current, locale: Locale = .current
    ) -> String {
        let days = calendar.dateComponents(
            [.day], from: calendar.startOfDay(for: date), to: calendar.startOfDay(for: now)).day ?? 0
        let style = Date.FormatStyle(locale: locale, calendar: calendar, timeZone: calendar.timeZone)
        switch days {
        case 0: return date.formatted(style.hour().minute())
        case 1: return "Yesterday"
        case 2...6: return date.formatted(style.weekday(.abbreviated))
        default:
            let sameYear = calendar.component(.year, from: date) == calendar.component(.year, from: now)
            return date.formatted(sameYear ? style.month(.abbreviated).day() : style.month(.abbreviated).day().year())
        }
    }
}
