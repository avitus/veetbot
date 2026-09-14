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
    public var id: UUID { callID }

    enum CodingKeys: String, CodingKey {
        case direction, status, counterparty, summary, transcript, erased
        case callID = "call_id"
        case summaryComplete = "summary_complete"
        case transcriptComplete = "transcript_complete"
    }
}
