import Foundation

public enum ArtifactContentResponse: Sendable {
    case content(data: Data, etag: String?)
    case notModified
}

public struct VeetbotAPIClient: Sendable {
    public let transport: HTTPTransport

    public init(transport: HTTPTransport) {
        self.transport = transport
    }

    public func createSession(
        agentID: String = "general",
        metadata: [String: JSONValue] = [:]
    ) async throws -> SessionView {
        let body = CreateSessionBody(agentID: agentID, metadata: metadata)
        return try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/sessions",
                body: try JSONEncoder.server.encode(body)
            )
        )
    }

    public func getSession(_ sessionID: UUID) async throws -> SessionView {
        try await transport.send(
            TransportRequest(method: .get, path: "/v1/sessions/\(sessionID.uuidString)")
        )
    }

    public func submitMessage(
        sessionID: UUID,
        content: [ContentBlock],
        idempotencyKey: String = UUID().uuidString.lowercased()
    ) async throws -> SubmitResult {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/sessions/\(sessionID.uuidString)/messages",
                body: try JSONEncoder.server.encode(MessageBody(content: content)),
                headers: ["Idempotency-Key": idempotencyKey],
                retryAttempts: 3
            )
        )
    }

    public func getRun(_ runID: UUID) async throws -> RunView {
        try await transport.send(
            TransportRequest(method: .get, path: "/v1/runs/\(runID.uuidString)")
        )
    }

    public func deliverInput(
        runID: UUID,
        content: [ContentBlock],
        questionID: UUID
    ) async throws -> SubmitResult {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/runs/\(runID.uuidString)/input",
                body: try JSONEncoder.server.encode(
                    InputBody(content: content, questionID: questionID)
                ),
                retryAttempts: 2
            )
        )
    }

    public func cancelRun(_ runID: UUID) async throws -> RunView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/runs/\(runID.uuidString)/cancel",
                retryAttempts: 2
            )
        )
    }

    public func listPendingApprovals(
        runID: UUID? = nil,
        sessionID: UUID? = nil,
        limit: Int = 50,
        cursor: String? = nil
    ) async throws -> Page<ApprovalView> {
        var query = [
            URLQueryItem(name: "status", value: "pending"),
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200))),
        ]
        if let runID { query.append(URLQueryItem(name: "run_id", value: runID.uuidString)) }
        if let sessionID {
            query.append(URLQueryItem(name: "session_id", value: sessionID.uuidString))
        }
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await transport.send(
            TransportRequest(method: .get, path: "/v1/approvals", queryItems: query)
        )
    }

    public func getApproval(_ approvalID: UUID) async throws -> ApprovalView {
        try await transport.send(
            TransportRequest(method: .get, path: "/v1/approvals/\(approvalID.uuidString)")
        )
    }

    public func resolveApproval(
        _ approvalID: UUID,
        decision: ApprovalDecision,
        reason: String? = nil
    ) async throws -> ApprovalView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/approvals/\(approvalID.uuidString)/resolve",
                body: try JSONEncoder.server.encode(
                    ResolveApprovalBody(decision: decision, reason: reason)
                ),
                retryAttempts: 2
            )
        )
    }

    public func getArtifact(_ artifactID: UUID) async throws -> ArtifactView {
        try await transport.send(
            TransportRequest(method: .get, path: "/v1/artifacts/\(artifactID.uuidString)")
        )
    }

    public func getArtifactContent(
        _ artifactID: UUID,
        etag: String? = nil
    ) async throws -> ArtifactContentResponse {
        var headers: [String: String] = [:]
        if let etag { headers["If-None-Match"] = etag }
        let (data, response) = try await transport.sendData(
            TransportRequest(
                method: .get,
                path: "/v1/artifacts/\(artifactID.uuidString)/content",
                headers: headers
            ),
            accepting: [304]
        )
        if response.statusCode == 304 { return .notModified }
        return .content(data: data, etag: response.value(forHTTPHeaderField: "ETag"))
    }
}

private struct CreateSessionBody: Encodable {
    let agentID: String
    let metadata: [String: JSONValue]

    enum CodingKeys: String, CodingKey {
        case agentID = "agent_id"
        case metadata
    }
}

private struct MessageBody: Encodable {
    let content: [ContentBlock]
}

private struct InputBody: Encodable {
    let content: [ContentBlock]
    let questionID: UUID

    enum CodingKeys: String, CodingKey {
        case content
        case questionID = "question_id"
    }
}

private struct ResolveApprovalBody: Encodable {
    let decision: ApprovalDecision
    let reason: String?
}
