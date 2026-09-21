import Foundation

/// The census rows, the gesture the owner confirms, and the additive thread
/// block. The unsubscribe address never reaches a client: a row carries only
/// the destination host, or the recipient a `mailto:` sender specified, and a
/// digest naming the evidence the server dispatches from.
public enum EmailSubscriptions {
    /// States a sender can still be unsubscribed from.
    public static let actionableStates = ["active", "failed", "still_sending"]
    /// One confirmed gesture consents for at most this many senders.
    public static let batchLimit = 25
}

/// The sender's own message, as the read server normalized it.
public struct EmailSubscriptionMailto: Codable, Equatable, Sendable {
    public let to: String
    public let subject: String
    public let body: String
}

/// The latest durable action. Pending or uncertain never implies success.
public struct EmailSubscriptionOperation: Codable, Equatable, Sendable {
    public let operationID: UUID
    public let runID: UUID
    /// Raw strings keep future server values readable without granting actions.
    public let action: String
    public let status: String
    public let code: String?
    public let requestedAt: Date
    public let priorState: String

    enum CodingKeys: String, CodingKey {
        case action, status, code
        case operationID = "operation_id"
        case runID = "run_id"
        case requestedAt = "requested_at"
        case priorState = "prior_state"
    }
}

/// One bulk sender as one account sees it.
public struct EmailSubscriptionView: Codable, Identifiable, Equatable, Sendable {
    public let id: String
    public let accountID: String
    public let displayName: String
    public let address: String
    public let listID: String
    public let threadCount: Int
    public let threadCountOverflow: Bool
    public let firstSeenAt: Date
    public let lastReceivedAt: Date
    /// Raw strings keep future server values readable without granting actions.
    public let mechanism: String
    public let verified: Bool
    public let linkOnly: Bool
    /// The one-click host, or the recipient a `mailto:` sender named. Never an address to dial.
    public let destination: String
    public let mailto: EmailSubscriptionMailto?
    /// Absent until the evidence is verified, and again once a decision is recorded.
    public let evidenceDigest: String?
    public let state: String
    public let protected: Bool
    public let protectedReason: String?
    public let requestedAt: Date?
    public let operation: EmailSubscriptionOperation?
    public let revision: Int

    enum CodingKeys: String, CodingKey {
        case id, address, mechanism, verified, destination, mailto, state, protected, revision
        case operation
        case accountID = "account_id"
        case displayName = "display_name"
        case listID = "list_id"
        case threadCount = "thread_count"
        case threadCountOverflow = "thread_count_overflow"
        case firstSeenAt = "first_seen_at"
        case lastReceivedAt = "last_received_at"
        case linkOnly = "link_only"
        case evidenceDigest = "evidence_digest"
        case protectedReason = "protected_reason"
        case requestedAt = "requested_at"
    }

    /// Only verified evidence in an actionable state can be unsubscribed from.
    public var canUnsubscribe: Bool {
        verified && mechanism != "none" && evidenceDigest != nil
            && EmailSubscriptions.actionableStates.contains(state)
    }

    /// Evidence this refresh has not read yet. Keep and Report spam never wait on it.
    public var isVerifying: Bool {
        !verified && EmailSubscriptions.actionableStates.contains(state)
    }

    /// Names the host that will be called, or the mailbox that will be written to.
    public var mechanismSentence: String {
        if isVerifying { return "Checking…" }
        let recipient = destination.isEmpty ? (mailto?.to ?? "") : destination
        if let sentence = emailUnsubscribeSentence(mechanism: mechanism, destination: recipient) {
            return sentence
        }
        return linkOnly ? "Unsubscribe link only" : "No automated unsubscribe"
    }

    /// States what the server recorded without claiming an unsubscribe succeeded.
    public var stateDescription: String {
        switch state {
        case "active": "Active"
        case "kept": "Kept"
        case "pending":
            switch operation?.action {
            case "report_spam": "Reporting spam…"
            case "not_spam": "Restoring from spam…"
            default: "Unsubscribing…"
            }
        case "unsubscribed": "Unsubscribed"
        case "failed": "Unsubscribe failed"
        case "still_sending": "Still sending after unsubscribing"
        case "reported_spam": "Reported as spam"
        default: state
        }
    }

    /// Distinct conversations inside the ninety-day window, counted to a bound.
    public var volumeDescription: String {
        threadCountOverflow
            ? "More than \(threadCount) conversations"
            : "\(threadCount) conversation\(threadCount == 1 ? "" : "s")"
    }
}

/// The shared plain-words wording. Anything but a usable mechanism has no sentence.
func emailUnsubscribeSentence(mechanism: String, destination: String) -> String? {
    guard !destination.isEmpty else { return nil }
    switch mechanism {
    case "one_click": return "One-click request to \(destination)"
    case "mailto": return "Sends an email to \(destination)"
    default: return nil
    }
}

/// The additive block that places the action on a bulk conversation.
public struct EmailThreadSubscription: Codable, Equatable, Sendable {
    public let id: String
    public let state: String
    public let mechanism: String
    public let destination: String
    public let evidenceDigest: String?
    public let revision: Int

    enum CodingKeys: String, CodingKey {
        case id, state, mechanism, destination, revision
        case evidenceDigest = "evidence_digest"
    }

    /// A digest is only ever published for verified evidence with a mechanism.
    public var canUnsubscribe: Bool {
        mechanism != "none" && evidenceDigest != nil
            && EmailSubscriptions.actionableStates.contains(state)
    }
}

/// What a row still allows. A state that allows nothing offers nothing, and
/// the client never substitutes a different command for a missing one.
public enum EmailSubscriptionAction: String, Identifiable, CaseIterable, Sendable {
    case unsubscribe
    case tryAgain
    case reportSpam
    case notSpam
    case keep
    case unkeep

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .unsubscribe: "Unsubscribe"
        case .tryAgain: "Try again"
        case .reportSpam: "Report spam"
        case .notSpam: "Not spam"
        case .keep: "Keep"
        case .unkeep: "Stop keeping"
        }
    }

    public var symbol: String {
        switch self {
        case .unsubscribe, .tryAgain: "envelope.badge.shield.half.filled"
        case .reportSpam: "exclamationmark.octagon"
        case .notSpam: "arrow.uturn.backward"
        case .keep: "tray.and.arrow.down"
        case .unkeep: "tray.and.arrow.up"
        }
    }
}

/// Which surface opened a confirmation, so only that surface presents it.
public enum EmailUnsubscribeSource: String, Sendable {
    case list
    case thread
}

/// One sender inside a confirmation, named with the mechanism in plain words.
public struct EmailUnsubscribeTarget: Identifiable, Equatable, Sendable {
    public let id: String
    public let displayName: String
    public let address: String
    public let accountID: String
    public let mechanism: String
    public let destination: String
    public let evidenceDigest: String
    public let revision: Int

    public var sentence: String {
        emailUnsubscribeSentence(mechanism: mechanism, destination: destination)
            ?? "No automated unsubscribe"
    }
}

/// The consent the owner confirms: exactly these senders, and nothing else.
public struct EmailUnsubscribeConfirmation: Identifiable, Equatable, Sendable {
    public static let warning = "An unsubscribe cannot be undone."
    public let id: UUID
    public let source: EmailUnsubscribeSource
    public let targets: [EmailUnsubscribeTarget]

    public init(id: UUID = UUID(), source: EmailUnsubscribeSource, targets: [EmailUnsubscribeTarget]) {
        self.id = id
        self.source = source
        self.targets = targets
    }
}
