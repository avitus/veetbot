import Foundation

/// One approval for the rest of a request on a signed-in website
/// (ADR-0302). Its summary is server-authored text.
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

/// One approval for the rest of a request that changes one saved job
/// (ADR-0344). Its summary is server-authored text; the access ceilings it
/// also carries are the server's to check, so the card does not decode them.
public struct JobRequestGrantOfferView: Codable, Equatable, Sendable {
    public let slug: String
    public let durationSeconds: Int
    public let summary: String

    enum CodingKeys: String, CodingKey {
        case slug, summary
        case durationSeconds = "duration_seconds"
    }
}

/// What the card offered, repeated in an `approve_for_request` body: the
/// website login's profile (ADR-0302) or the job's slug (ADR-0344).
public struct RequestGrantEcho: Codable, Equatable, Sendable {
    public let profileID: UUID?
    public let jobSlug: String?

    public init(offer: RequestGrantOfferView) {
        profileID = offer.profileID
        jobSlug = nil
    }

    public init(jobOffer: JobRequestGrantOfferView) {
        profileID = nil
        jobSlug = jobOffer.slug
    }

    enum CodingKeys: String, CodingKey {
        case profileID = "profile_id"
        case jobSlug = "job_slug"
    }
}
