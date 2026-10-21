import Foundation

/// Retained only in presentation memory after an authenticated owner fetch.
public struct CallResultViewData: Decodable, Identifiable, Equatable, Sendable {
    public let callID: UUID
    public let direction: String?
    public let status: String?
    public let counterparty: String?
    public let summary: String?
    public let transcript: String?
    public let summaryComplete: Bool?
    public let transcriptComplete: Bool?
    public let erased: Bool?
    public let createdAt: Date?
    public let durationMinutes: Double?
    public let resultPending: Bool?
    /// Duration is provider metadata, never estimated from transcript length.
    public var durationText: String? {
        guard erased != true, resultPending != true, status != "active",
              let durationMinutes, durationMinutes.isFinite, durationMinutes >= 0 else { return nil }
        let seconds = (durationMinutes * 60).rounded()
        guard seconds.isFinite else { return nil }
        if seconds < 60 { return String(format: "%.0f s", seconds) }
        return String(format: "%.0f min %.0f s", floor(seconds / 60), seconds.truncatingRemainder(dividingBy: 60))
    }

    public var canFollowUp: Bool {
        erased != true && resultPending != true && status != "active" && status != "accepted"
    }

    /// Only an identity and the owner's selected intent enter the editable composer.
    /// Content and numbers must be read through the governed call tool, retaining their trust label.
    public func followUpPrompt(for action: CallFollowUpAction) -> String? {
        guard canFollowUp else { return nil }
        switch action {
        case .callAgain:
            return "Read retained call \(callID.uuidString) through the call reader. I would like to call again. Help me confirm the recipient and purpose before arranging a new call."
        case .somethingWrong:
            return "Read retained call \(callID.uuidString) through the call reader. Something is wrong with this result. Help me review it; ask me what needs correcting. Do not place another call unless I request it."
        }
    }
    public init(callID: UUID, direction: String? = nil, status: String? = nil,
                counterparty: String? = nil, summary: String? = nil, transcript: String? = nil,
                summaryComplete: Bool? = nil, transcriptComplete: Bool? = nil, erased: Bool? = nil,
                createdAt: Date? = nil, durationMinutes: Double? = nil, resultPending: Bool? = nil) {
        self.callID = callID
        self.direction = direction
        self.status = status
        self.counterparty = counterparty
        self.summary = summary
        self.transcript = transcript
        self.summaryComplete = summaryComplete
        self.transcriptComplete = transcriptComplete
        self.erased = erased
        self.createdAt = createdAt
        self.durationMinutes = durationMinutes
        self.resultPending = resultPending
    }

    public var id: UUID { callID }

    enum CodingKeys: String, CodingKey {
        case direction, status, counterparty, summary, transcript, erased
        case callID = "call_id"
        case createdAt = "created_at"
        case durationMinutes = "duration_minutes"
        case resultPending = "result_pending"
        case summaryComplete = "summary_complete"
        case transcriptComplete = "transcript_complete"
    }
}

/// An editable Chat handoff, never a direct call or a submitted problem report.
public enum CallFollowUpAction: Sendable {
    case callAgain
    case somethingWrong
}
