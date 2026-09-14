import Foundation

extension VeetbotAPIClient {
    public func callResult(_ id: UUID) async throws -> CallResultViewData {
        try await transport.send(TransportRequest(method: .get, path: "/v1/calls/\(id.uuidString)"))
    }

    public func deleteCallResult(_ id: UUID) async throws -> CallResultViewData {
        try await transport.send(TransportRequest(method: .delete, path: "/v1/calls/\(id.uuidString)"))
    }
}
