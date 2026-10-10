import Foundation

extension VeetbotAPIClient {
    public func memorySynthesis(cursor: String? = nil, kind: String? = nil, state: String? = nil) async throws -> Page<MemorySynthesisOperation> {
        var query = synthesisQuery
        query.append(URLQueryItem(name: "limit", value: "50"))
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        if let kind { query.append(URLQueryItem(name: "kind", value: kind)) }
        if let state { query.append(URLQueryItem(name: "state", value: state)) }
        return try await transport.send(TransportRequest(method: .get, path: "/v1/memory-reconsolidations", queryItems: query))
    }
    public func memorySynthesisOperation(_ id: UUID) async throws -> MemorySynthesisOperation {
        try await transport.send(TransportRequest(method: .get, path: "/v1/memory-reconsolidations/\(id.uuidString)", queryItems: synthesisQuery))
    }
    public func undoMemorySynthesis(_ id: UUID, revision: Int, key: String) async throws -> MemorySynthesisOperation {
        try await transport.send(TransportRequest(
            method: .post, path: "/v1/memory-reconsolidations/\(id.uuidString)/undo", queryItems: synthesisQuery,
            body: try JSONEncoder.server.encode(["expected_revision": revision]),
            headers: ["Idempotency-Key": key], retryAttempts: 2))
    }
    public func memorySummary(_ id: UUID) async throws -> MemorySummaryView {
        try await transport.send(TransportRequest(method: .get, path: "/v1/memories/\(id.uuidString)", queryItems: synthesisQuery))
    }
    public func reviewMemorySummary(_ id: UUID, outcome: MemoryReviewOutcome, key: String) async throws -> MemorySummaryView {
        try await transport.send(TransportRequest(
            method: .post, path: "/v1/memories/\(id.uuidString)/review", queryItems: synthesisQuery,
            body: try JSONEncoder.server.encode(["outcome": outcome.rawValue]),
            headers: ["Idempotency-Key": key], retryAttempts: 2))
    }
    private var synthesisQuery: [URLQueryItem] {
        [URLQueryItem(name: "ceiling", value: memoryBrowsingCeiling.rawValue)]
    }
}
