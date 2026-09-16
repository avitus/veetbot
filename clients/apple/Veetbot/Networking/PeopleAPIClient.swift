import Foundation

/// People always carries the native viewing ceiling; the server clamps it to the surface.
extension VeetbotAPIClient {
    public func listPeople(text: String? = nil, cursor: String? = nil, asOf: Date? = nil, state: String? = nil, pinned: Bool? = nil, sort: String = "id", relationship: String? = nil) async throws -> Page<PersonView> {
        var query = peopleQuery
        query.append(URLQueryItem(name: "limit", value: "50"))
        query.append(URLQueryItem(name: "sort", value: sort))
        if let state { query.append(URLQueryItem(name: "state", value: state)) }
        if let relationship { query.append(URLQueryItem(name: "relationship", value: relationship)) }
        if let pinned { query.append(URLQueryItem(name: "pinned", value: pinned ? "true" : "false")) }
        if let text, !text.isEmpty { query.append(URLQueryItem(name: "text", value: text)) }
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        if let asOf {
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            query.append(URLQueryItem(name: "as_of", value: formatter.string(from: asOf)))
        }
        return try await transport.send(TransportRequest(method: .get, path: "/v1/people", queryItems: query))
    }
    public func person(_ id: UUID) async throws -> PersonProfileView {
        try await transport.send(TransportRequest(method: .get, path: "/v1/people/\(id.uuidString)", queryItems: peopleQuery))
    }
    public func peopleHistory(_ id: UUID, cursor: String? = nil, channel: String? = nil) async throws -> PeopleHistoryPage {
        var query = peopleQuery
        query.append(URLQueryItem(name: "limit", value: "50"))
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        if let channel { query.append(URLQueryItem(name: "channel", value: channel)) }
        return try await transport.send(TransportRequest(method: .get, path: "/v1/people/\(id.uuidString)/history", queryItems: query))
    }
    public func peopleEvidence(_ id: UUID, reference: UUID) async throws -> PeopleEvidenceView {
        try await transport.send(TransportRequest(method: .get, path: "/v1/people/\(id.uuidString)/evidence/\(reference.uuidString)", queryItems: peopleQuery))
    }
    public func peopleIdentityEvidence(_ id: UUID, cursor: String? = nil) async throws -> Page<PeopleIdentityEvidenceView> {
        var query = peopleQuery
        query.append(URLQueryItem(name: "limit", value: "50"))
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await transport.send(TransportRequest(method: .get, path: "/v1/people/\(id.uuidString)/identity-evidence", queryItems: query))
    }
    public func peopleOperation(_ id: UUID) async throws -> PeopleOperationView {
        try await transport.send(TransportRequest(method: .get, path: "/v1/people/operations/\(id.uuidString)", queryItems: peopleQuery))
    }
    public func writePerson(_ id: UUID?, body: [String: JSONValue], key: String) async throws -> PersonView {
        try await transport.send(peopleWrite(path: id.map { "/v1/people/\($0.uuidString)" } ?? "/v1/people", method: id == nil ? .post : .patch, body: body, key: key))
    }
    public func correctPerson(_ id: UUID, body: [String: JSONValue], key: String) async throws -> PeopleCorrectionResult {
        try await transport.send(peopleWrite(path: "/v1/people/\(id.uuidString)/corrections", body: body, key: key))
    }
    public func identityOperation(body: [String: JSONValue], key: String) async throws -> PeopleOperationView {
        try await transport.send(peopleWrite(path: "/v1/people/identity-operations", body: body, key: key))
    }
    public func forgetPerson(_ id: UUID, body: [String: JSONValue], key: String) async throws -> PeopleOperationView {
        try await transport.send(peopleWrite(path: "/v1/people/\(id.uuidString)/forget", body: body, key: key))
    }
    private var peopleQuery: [URLQueryItem] { [URLQueryItem(name: "ceiling", value: memoryBrowsingCeiling.rawValue)] }
    private func peopleWrite(path: String, method: HTTPMethod = .post, body: [String: JSONValue], key: String) throws -> TransportRequest {
        TransportRequest(method: method, path: path, queryItems: peopleQuery, body: try JSONEncoder.server.encode(body),
            headers: ["Idempotency-Key": key], retryAttempts: 2)
    }
}

extension VeetbotAPIClient {
    public func listPeopleImports(cursor: String? = nil) async throws -> Page<PeopleImportView> {
        var query = peopleQuery
        query.append(URLQueryItem(name: "limit", value: "50"))
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await transport.send(TransportRequest(method: .get, path: "/v1/people/imports", queryItems: query))
    }
    public func peopleImport(_ id: UUID) async throws -> PeopleImportView {
        try await transport.send(TransportRequest(method: .get, path: "/v1/people/imports/\(id.uuidString)", queryItems: peopleQuery))
    }
    public func submitPeopleImport(body: [String: JSONValue], key: String) async throws -> PeopleImportView {
        try await transport.send(peopleWrite(path: "/v1/people/imports", body: body, key: key))
    }
    public func cancelPeopleImport(_ id: UUID, revision: Int, key: String) async throws -> PeopleImportView {
        try await transport.send(peopleWrite(path: "/v1/people/imports/\(id.uuidString)/cancel", body: ["expected_revision": .number(Double(revision))], key: key))
    }
}
