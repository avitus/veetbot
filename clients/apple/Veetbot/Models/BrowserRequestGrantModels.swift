import Foundation

/// One approval for the rest of a request on a signed-in website
/// (ADR-0301). Its summary is server-authored text.
public struct RequestGrantOfferView: Codable, Equatable, Sendable {
    public let profileID: UUID
    public let profileGeneration: Int
    public let origins: [String]
    public let durationSeconds: Int
    public let summary: String

    enum CodingKeys: String, CodingKey {
        case origins, summary
        case profileID = "profile_id"
        case profileGeneration = "profile_generation"
        case durationSeconds = "duration_seconds"
    }
}

/// The offered profile, repeated in an `approve_for_request` body.
public struct RequestGrantEcho: Codable, Equatable, Sendable {
    public let profileID: UUID

    public init(offer: RequestGrantOfferView) {
        profileID = offer.profileID
    }

    enum CodingKeys: String, CodingKey {
        case profileID = "profile_id"
    }
}
