import Foundation

/// A runtime asset or widget bundle the scheme handler serves, fetched natively with the token.
public struct CockpitAssetData: Sendable, Equatable {
    public let content: Data
    public let contentType: String

    public init(content: Data, contentType: String) {
        self.content = content
        self.contentType = contentType
    }
}

/// The Cockpit routes the board uses (docs/plan/cockpit.md, the routes).
public protocol CockpitBoardClient: Sendable {
    func cockpitSnapshot() async throws -> CockpitSnapshot
    func cockpitEvents(after cursor: Int?) -> AsyncThrowingStream<SSEFrame, Error>
    func cockpitAsset(_ digest: String) async throws -> CockpitAssetData
    func cockpitWidget(_ slug: String) async throws -> CockpitWidgetDetail
    func putCockpitSettings(_ slug: String, values: [String: JSONValue]) async throws
    func putCockpitLayout(
        _ deviceClass: CockpitDeviceClass, placements: [CockpitPlacement], expectedRevision: Int?
    ) async throws -> CockpitLayout
    func revertCockpitWidget(_ slug: String, to revision: UUID) async throws -> CockpitChange
    func deleteCockpitWidget(_ slug: String) async throws -> CockpitChange
    func restoreCockpitWidget(_ slug: String) async throws -> CockpitChange
}

extension VeetbotAPIClient: CockpitBoardClient {
    public func cockpitSnapshot() async throws -> CockpitSnapshot {
        try await transport.send(TransportRequest(method: .get, path: "/v1/cockpit"))
    }

    public func cockpitEvents(after cursor: Int?) -> AsyncThrowingStream<SSEFrame, Error> {
        SSEReader(transport: transport).frames(path: "/v1/cockpit/stream", lastEventID: cursor)
    }

    public func cockpitAsset(_ digest: String) async throws -> CockpitAssetData {
        guard CockpitAssetRoute.isDigest(digest) else { throw URLError(.badURL) }
        let (data, response) = try await transport.sendData(
            TransportRequest(method: .get, path: "/v1/cockpit/assets/\(digest)", retryAttempts: 2)
        )
        let type = response.value(forHTTPHeaderField: "Content-Type") ?? "application/octet-stream"
        return CockpitAssetData(content: data, contentType: type)
    }

    public func cockpitWidget(_ slug: String) async throws -> CockpitWidgetDetail {
        try await transport.send(TransportRequest(method: .get, path: "/v1/cockpit/widgets/\(try Self.cockpitSlug(slug))"))
    }

    public func putCockpitSettings(_ slug: String, values: [String: JSONValue]) async throws {
        let body = try JSONEncoder.server.encode(["values": JSONValue.object(values)])
        _ = try await transport.sendData(TransportRequest(
            method: .put, path: "/v1/cockpit/widgets/\(try Self.cockpitSlug(slug))/settings", body: body,
            headers: ["Content-Type": "application/json"]
        ))
    }

    public func putCockpitLayout(
        _ deviceClass: CockpitDeviceClass, placements: [CockpitPlacement], expectedRevision: Int?
    ) async throws -> CockpitLayout {
        let body = try JSONEncoder.server.encode(CockpitLayoutRequest(
            placements: placements, expectedRevision: expectedRevision
        ))
        return try await transport.send(TransportRequest(
            method: .put, path: "/v1/cockpit/layouts/\(deviceClass.rawValue)", body: body,
            headers: ["Content-Type": "application/json"]
        ))
    }

    public func revertCockpitWidget(_ slug: String, to revision: UUID) async throws -> CockpitChange {
        let body = try JSONEncoder.server.encode(["revision_id": revision.uuidString.lowercased()])
        return try await cockpitChange(slug, action: "revert", body: body)
    }

    public func deleteCockpitWidget(_ slug: String) async throws -> CockpitChange {
        try await cockpitChange(slug, action: "delete", body: Data("{}".utf8))
    }

    public func restoreCockpitWidget(_ slug: String) async throws -> CockpitChange {
        try await cockpitChange(slug, action: "restore", body: Data("{}".utf8))
    }

    private func cockpitChange(_ slug: String, action: String, body: Data) async throws -> CockpitChange {
        try await transport.send(TransportRequest(
            method: .post, path: "/v1/cockpit/widgets/\(try Self.cockpitSlug(slug))/\(action)", body: body,
            headers: ["Content-Type": "application/json"]
        ))
    }

    /// A slug follows the manifest grammar, so it never reshapes a route.
    static func cockpitSlug(_ slug: String) throws -> String {
        guard slug.range(of: #"^[a-z][a-z0-9-]{1,47}$"#, options: .regularExpression) != nil else {
            throw URLError(.badURL)
        }
        return slug
    }
}

private struct CockpitLayoutRequest: Encodable {
    let placements: [CockpitPlacement]
    let expectedRevision: Int?

    enum CodingKeys: String, CodingKey {
        case placements
        case expectedRevision = "expected_revision"
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(placements, forKey: .placements)
        // The server takes an explicit null for a first layout.
        try container.encode(expectedRevision, forKey: .expectedRevision)
    }
}
