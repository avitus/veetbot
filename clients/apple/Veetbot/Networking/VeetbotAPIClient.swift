import Foundation

public enum ArtifactContentResponse: Sendable {
    case content(data: Data, etag: String?)
    case notModified
}

public enum VeetbotAPIClientError: Error, LocalizedError, Sendable {
    case serverUpgradeRequired
    case memoryBrowsingUnavailable
    case scheduleBrowsingUnavailable

    public var errorDescription: String? {
        switch self {
        case .serverUpgradeRequired:
            return "This server is running an older Veetbot API that does not support synchronized conversation history or Delete Everywhere. Update the server and try again."
        case .memoryBrowsingUnavailable:
            return "This server does not support memory browsing yet."
        case .scheduleBrowsingUnavailable:
            return "This server does not support schedule browsing yet."
        }
    }
}

// The native client's declared viewing ceiling: full parity, not a lower
// default, the owner's recorded ADR-0070 decision 5 trade-off.
public let memoryBrowsingCeiling: MemorySensitivityKind = .restricted

public struct VeetbotAPIClient: Sendable {
    public let transport: HTTPTransport

    public init(transport: HTTPTransport) {
        self.transport = transport
    }

    public func createSession(
        agentID: String = "general",
        metadata: [String: JSONValue] = [:],
        browserProfileID: UUID? = nil
    ) async throws -> SessionView {
        let body = CreateSessionBody(
            agentID: agentID,
            metadata: metadata,
            browserProfileID: browserProfileID
        )
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

    public func listSessions(
        limit: Int = 100,
        cursor: String? = nil
    ) async throws -> Page<SessionView> {
        var query = [
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200)))
        ]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        do {
            return try await transport.send(
                TransportRequest(method: .get, path: "/v1/sessions", queryItems: query)
            )
        } catch {
            throw historyCompatibilityError(from: error) ?? error
        }
    }

    public func deleteSession(_ sessionID: UUID) async throws {
        do {
            _ = try await transport.sendData(
                TransportRequest(
                    method: .delete,
                    path: "/v1/sessions/\(sessionID.uuidString)",
                    retryAttempts: 2
                )
            )
        } catch {
            throw historyCompatibilityError(from: error) ?? error
        }
    }

    public func listSessionMessages(
        sessionID: UUID,
        limit: Int = 200,
        cursor: String? = nil
    ) async throws -> Page<SessionMessageView> {
        var query = [
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200)))
        ]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        do {
            return try await transport.send(
                TransportRequest(
                    method: .get,
                    path: "/v1/sessions/\(sessionID.uuidString)/messages",
                    queryItems: query,
                    retryAttempts: 3
                )
            )
        } catch {
            throw historyCompatibilityError(from: error) ?? error
        }
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
        // This route is idempotent on (run_id, question_id); the server does not
        // consume Idempotency-Key here, so retries reuse the same question ID.
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
        // Approval resolution is first-decision-wins. A replay returns the
        // stored conflict, which ChatViewModel reconciles by reloading the item.
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

    public func registerDevice(
        _ body: AppleDeviceRegistration,
        idempotencyKey: String
    ) async throws -> DeviceView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/devices",
                body: try JSONEncoder.server.encode(body),
                headers: ["Idempotency-Key": idempotencyKey],
                retryAttempts: 3
            )
        )
    }

    public func listDevices(
        limit: Int = 200,
        cursor: String? = nil
    ) async throws -> Page<DeviceView> {
        var query = [
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200)))
        ]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await transport.send(
            TransportRequest(method: .get, path: "/v1/devices", queryItems: query)
        )
    }

    public func revokeDevice(_ deviceID: UUID) async throws -> DeviceView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/devices/\(deviceID.uuidString)/revoke",
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

    public func createBrowserProfile(
        allowedOrigins: [String],
        idempotencyKey: String = UUID().uuidString.lowercased()
    ) async throws -> BrowserProfileView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/browser-profiles",
                body: try JSONEncoder.server.encode(
                    CreateBrowserProfileBody(allowedOrigins: allowedOrigins)
                ),
                headers: ["Idempotency-Key": idempotencyKey],
                retryAttempts: 3
            )
        )
    }

    public func listBrowserProfiles(
        limit: Int = 200,
        cursor: String? = nil
    ) async throws -> Page<BrowserProfileView> {
        var query = [
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200)))
        ]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await transport.send(
            TransportRequest(method: .get, path: "/v1/browser-profiles", queryItems: query)
        )
    }

    public func beginBrowserAuthentication(
        profileID: UUID,
        loginURL: String
    ) async throws -> BrowserAuthenticationView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies",
                body: try JSONEncoder.server.encode(
                    BeginBrowserAuthenticationBody(loginURL: loginURL)
                )
            )
        )
    }

    public func getBrowserAuthentication(
        _ authenticationID: UUID
    ) async throws -> BrowserAuthenticationView {
        try await transport.send(
            TransportRequest(
                method: .get,
                path: "/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)"
            )
        )
    }

    public func cancelBrowserAuthentication(
        _ authenticationID: UUID
    ) async throws -> BrowserAuthenticationView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/browser-authentication-ceremonies/\(authenticationID.uuidString)/cancel"
            )
        )
    }

    public func revokeBrowserProfile(_ profileID: UUID) async throws -> BrowserProfileView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/browser-profiles/\(profileID.uuidString)/revoke"
            )
        )
    }

    public func deleteBrowserProfile(_ profileID: UUID) async throws {
        _ = try await transport.sendData(
            TransportRequest(
                method: .delete,
                path: "/v1/browser-profiles/\(profileID.uuidString)",
                retryAttempts: 2
            )
        )
    }

    public func listMemories(
        ceiling: MemorySensitivityKind,
        limit: Int = 50,
        cursor: String? = nil,
        statuses: [MemoryStatusKind]? = nil,
        beliefTypes: [MemoryBeliefTypeKind]? = nil,
        subject: String? = nil,
        sessionID: UUID? = nil,
        text: String? = nil
    ) async throws -> Page<MemoryView> {
        var query = [
            URLQueryItem(name: "ceiling", value: ceiling.rawValue),
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200))),
        ]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        for status in statuses ?? [] {
            query.append(URLQueryItem(name: "status", value: status.rawValue))
        }
        for beliefType in beliefTypes ?? [] {
            query.append(URLQueryItem(name: "belief_type", value: beliefType.rawValue))
        }
        if let subject { query.append(URLQueryItem(name: "subject", value: subject)) }
        if let sessionID {
            query.append(URLQueryItem(name: "session_id", value: sessionID.uuidString))
        }
        if let text { query.append(URLQueryItem(name: "text", value: text)) }
        do {
            return try await transport.send(
                TransportRequest(method: .get, path: "/v1/memories", queryItems: query)
            )
        } catch {
            throw memoryBrowsingCompatibilityError(from: error) ?? error
        }
    }

    public func getMemory(_ id: UUID, ceiling: MemorySensitivityKind) async throws -> MemoryView {
        try await transport.send(
            TransportRequest(
                method: .get,
                path: "/v1/memories/\(id.uuidString)",
                queryItems: [URLQueryItem(name: "ceiling", value: ceiling.rawValue)]
            )
        )
    }

    public func getPersona() async throws -> PersonaView {
        try await transport.send(TransportRequest(method: .get, path: "/v1/persona"))
    }

    public func updatePersona(
        expectedVersion: Int,
        entries: [UpdatePersonaEntryBody]
    ) async throws -> PersonaView {
        try await transport.send(
            TransportRequest(
                method: .put,
                path: "/v1/persona",
                body: try JSONEncoder.server.encode(
                    UpdatePersonaBody(expectedVersion: expectedVersion, entries: entries)
                )
            )
        )
    }

    public func personaHistory(limit: Int = 20) async throws -> Page<PersonaView> {
        try await transport.send(
            TransportRequest(
                method: .get,
                path: "/v1/persona/history",
                queryItems: [URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200)))]
            )
        )
    }

    public func listPersonaNominations(state: String? = nil) async throws
        -> Page<PersonaNominationView>
    {
        var query: [URLQueryItem] = []
        if let state { query.append(URLQueryItem(name: "state", value: state)) }
        return try await transport.send(
            TransportRequest(method: .get, path: "/v1/persona/nominations", queryItems: query)
        )
    }

    public func affirmPersonaNomination(_ id: UUID) async throws -> PersonaView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/persona/nominations/\(id.uuidString)/affirm"
            )
        )
    }

    public func declinePersonaNomination(_ id: UUID) async throws -> PersonaNominationView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/persona/nominations/\(id.uuidString)/decline"
            )
        )
    }

    public func listSchedules(
        limit: Int = 50,
        cursor: String? = nil
    ) async throws -> Page<ScheduleListItemView> {
        var query = [
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200)))
        ]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        do {
            return try await transport.send(
                TransportRequest(method: .get, path: "/v1/schedules", queryItems: query)
            )
        } catch {
            throw scheduleBrowsingCompatibilityError(from: error) ?? error
        }
    }

    public func getSchedule(_ id: UUID) async throws -> ScheduleRecordView {
        try await transport.send(
            TransportRequest(method: .get, path: "/v1/schedules/\(id.uuidString)")
        )
    }
}

private func historyCompatibilityError(from error: Error) -> VeetbotAPIClientError? {
    guard case HTTPTransportError.api(let apiError) = error else { return nil }
    if apiError.statusCode == 405
        || (apiError.statusCode == 400
            && apiError.code == .malformedRequest
            && apiError.message == "The HTTP request is not supported.")
    {
        return .serverUpgradeRequired
    }
    return nil
}

private func memoryBrowsingCompatibilityError(from error: Error) -> VeetbotAPIClientError? {
    guard case HTTPTransportError.api(let apiError) = error else { return nil }
    if apiError.statusCode == 404 || apiError.statusCode == 405 {
        return .memoryBrowsingUnavailable
    }
    return nil
}

private func scheduleBrowsingCompatibilityError(from error: Error) -> VeetbotAPIClientError? {
    guard case HTTPTransportError.api(let apiError) = error else { return nil }
    if apiError.statusCode == 404 || apiError.statusCode == 405 {
        return .scheduleBrowsingUnavailable
    }
    return nil
}

private struct CreateSessionBody: Encodable {
    let agentID: String
    let metadata: [String: JSONValue]
    let browserProfileID: UUID?

    enum CodingKeys: String, CodingKey {
        case agentID = "agent_id"
        case metadata
        case browserProfileID = "browser_profile_id"
    }
}

private struct CreateBrowserProfileBody: Encodable {
    let allowedOrigins: [String]

    enum CodingKeys: String, CodingKey {
        case allowedOrigins = "allowed_origins"
    }
}

private struct BeginBrowserAuthenticationBody: Encodable {
    let loginURL: String

    enum CodingKeys: String, CodingKey {
        case loginURL = "login_url"
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
