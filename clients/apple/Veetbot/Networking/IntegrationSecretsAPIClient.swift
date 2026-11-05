import Foundation

/// Metadata only. A stored value is never a read model or an offline record.
public struct IntegrationKey: Decodable, Identifiable, Equatable, Sendable {
    public let id: UUID
    public let reference: String
    public let label: String
    public let origin: String
    public let authScheme: String
    public let revision: Int
    public let createdAt: Date
    public let updatedAt: Date

    enum CodingKeys: String, CodingKey {
        case id, reference, label, origin, revision
        case authScheme = "auth_scheme"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

public struct IntegrationKeyList: Decodable, Sendable {
    public let secrets: [IntegrationKey]
}

extension VeetbotAPIClient {
    public func listIntegrationKeys() async throws -> IntegrationKeyList {
        try await transport.send(TransportRequest(method: .get, path: "/v1/integration-secrets"))
    }

    /// The form owns the ID across retries. The body exists only for this request.
    public func saveIntegrationKey(
        id: UUID, label: String, origin: String, authScheme: String,
        value: String, expectedRevision: Int
    ) async throws -> IntegrationKey {
        struct Write: Encodable {
            let label: String
            let origin: String
            let authScheme: String
            let value: String
            let expectedRevision: Int
            enum CodingKeys: String, CodingKey {
                case label, origin, value
                case authScheme = "auth_scheme"
                case expectedRevision = "expected_revision"
            }
        }
        return try await transport.send(TransportRequest(
            method: .put, path: "/v1/integration-secrets/\(id.uuidString.lowercased())",
            body: try JSONEncoder.server.encode(Write(
                label: label, origin: origin, authScheme: authScheme,
                value: value, expectedRevision: expectedRevision
            ))
        ))
    }

    public func deleteIntegrationKey(_ key: IntegrationKey) async throws {
        _ = try await transport.sendData(TransportRequest(
            method: .delete, path: "/v1/integration-secrets/\(key.id.uuidString.lowercased())",
            headers: ["If-Match": String(key.revision)]
        ))
    }
}
