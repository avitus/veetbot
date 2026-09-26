import Foundation

public enum ArtifactContentResponse: Sendable {
    case content(data: Data, etag: String?)
    case notModified
}

public enum VeetbotAPIClientError: Error, LocalizedError, Sendable {
    case serverUpgradeRequired
    case memoryBrowsingUnavailable
    case memoryChangesUnavailable
    case scheduleBrowsingUnavailable
    case foldersUnavailable
    case attachmentsUnavailable
    case modelSettingsUnavailable

    public var errorDescription: String? {
        switch self {
        case .serverUpgradeRequired:
            return "This server is running an older Veetbot API that does not support synchronized conversation history or Delete Everywhere. Update the server and try again."
        case .memoryBrowsingUnavailable:
            return "This server does not support memory browsing yet."
        case .memoryChangesUnavailable:
            return "This server does not support memory changes yet."
        case .scheduleBrowsingUnavailable:
            return "This server does not support schedule browsing yet."
        case .foldersUnavailable:
            return "This server does not support conversation folders yet."
        case .attachmentsUnavailable:
            return "This server does not accept attachments yet."
        case .modelSettingsUnavailable:
            return "This server doesn't support model settings yet."
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

    public func pendingInvocations(deviceID: UUID) async throws -> DeviceInvocationList {
        try await transport.send(
            TransportRequest(
                method: .get,
                path: "/v1/devices/\(deviceID.uuidString)/invocations"
            )
        )
    }

    public func postInvocationResult(
        deviceID: UUID,
        invocationID: UUID,
        result: DeviceInvocationResult
    ) async throws -> DeviceInvocationResultView {
        // No retries: a 409 here means the invocation row is already
        // terminally settled server-side, so a retry would only replay the
        // same conflict. The single attempt either lands or the caller
        // re-fetches the pending queue to see what actually happened.
        try await transport.send(
            TransportRequest(
                method: .post,
                path:
                    "/v1/devices/\(deviceID.uuidString)/invocations/\(invocationID.uuidString)/result",
                body: try JSONEncoder.server.encode(DeviceInvocationResultBody(status: result))
            )
        )
    }

    public func postDeviceMessage(
        deviceID: UUID,
        channel: String,
        sender: String,
        body: String,
        receivedAt: Date
    ) async throws -> DeviceIngestResult {
        // Ingest is digest-idempotent server-side, so retries here are safe
        // (unlike the result post above).
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/devices/\(deviceID.uuidString)/messages",
                body: try JSONEncoder.server.encode(
                    DeviceMessageBody(
                        channel: channel,
                        sender: sender,
                        body: body,
                        receivedAt: receivedAt
                    )
                ),
                retryAttempts: 3
            )
        )
    }

    /// Upload one file for a session; sending a message later claims it (ADR-0120).
    ///
    /// The key makes a retry replay the stored upload instead of storing it twice.
    public func uploadArtifact(
        sessionID: UUID,
        data: Data,
        filename: String,
        mediaType: String,
        idempotencyKey: String,
        progress: (@Sendable (Double) -> Void)? = nil
    ) async throws -> ArtifactView {
        do {
            let (body, _) = try await transport.sendData(
                TransportRequest(
                    method: .post,
                    path: "/v1/sessions/\(sessionID.uuidString)/artifacts",
                    headers: [
                        "Content-Type": mediaType,
                        "X-Filename": encodedHeaderFilename(filename),
                        "Idempotency-Key": idempotencyKey,
                    ],
                    retryAttempts: 3
                ),
                uploading: data,
                progress: progress
            )
            return try JSONDecoder.server.decode(ArtifactView.self, from: body)
        } catch {
            throw attachmentCompatibilityError(from: error) ?? error
        }
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
        loginURL: String,
        mode: BrowserAuthenticationMode = .remote
    ) async throws -> BrowserAuthenticationView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies",
                body: try JSONEncoder.server.encode(
                    BeginBrowserAuthenticationBody(loginURL: loginURL, mode: mode)
                )
            )
        )
    }

    /// The profile's ceremonies, without launch URLs. Begin recovery and the
    /// remote reconcile read it (ADR-0128).
    public func listBrowserAuthentications(
        profileID: UUID
    ) async throws -> [BrowserAuthenticationView] {
        try await transport.send(
            TransportRequest(
                method: .get,
                path: "/v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies"
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
        text: String? = nil,
        flagged: Bool? = nil
    ) async throws -> Page<MemoryView> {
        var query = [
            URLQueryItem(name: "ceiling", value: ceiling.rawValue),
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200))),
        ]
        if let flagged { query.append(URLQueryItem(name: "flagged", value: flagged ? "true" : "false")) }
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

    /// Deletes one belief through the governed path (ADR-0117). The key makes a
    /// retried request replay rather than repeat; a server without the route
    /// degrades to `memoryChangesUnavailable`.
    public func deleteMemory(
        _ id: UUID, ceiling: MemorySensitivityKind, key: String = UUID().uuidString
    ) async throws {
        do {
            _ = try await transport.sendData(
                TransportRequest(
                    method: .delete,
                    path: "/v1/memories/\(id.uuidString)",
                    queryItems: [URLQueryItem(name: "ceiling", value: ceiling.rawValue)],
                    headers: ["Idempotency-Key": key],
                    retryAttempts: 2
                )
            )
        } catch {
            throw memoryChangesCompatibilityError(from: error) ?? error
        }
    }

    /// Applies one review outcome to a flagged belief and returns its new view.
    public func reviewMemory(
        _ id: UUID,
        outcome: MemoryReviewOutcome,
        ceiling: MemorySensitivityKind,
        key: String = UUID().uuidString
    ) async throws -> MemoryView {
        do {
            return try await transport.send(
                TransportRequest(
                    method: .post,
                    path: "/v1/memories/\(id.uuidString)/review",
                    queryItems: [URLQueryItem(name: "ceiling", value: ceiling.rawValue)],
                    body: try JSONEncoder.server.encode(["outcome": outcome.rawValue]),
                    headers: ["Idempotency-Key": key],
                    retryAttempts: 2
                )
            )
        } catch {
            throw memoryChangesCompatibilityError(from: error, missingRouteIsUnsupported: true)
                ?? error
        }
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

    /// The owner's chat and memory model choices with the options the server
    /// offers. A server that predates the resource degrades to
    /// `modelSettingsUnavailable` instead of a generic error.
    public func getModelSettings() async throws -> ModelSettingsView {
        do {
            return try await transport.send(
                TransportRequest(method: .get, path: "/v1/settings/models")
            )
        } catch {
            throw modelSettingsCompatibilityError(from: error) ?? error
        }
    }

    /// Replaces both choices at once, guarded by the version the caller read.
    /// A PUT that restates the stored values succeeds without a new version,
    /// so a retry after a lost response is safe.
    public func updateModelSettings(
        expectedVersion: Int,
        chat: ModelChoice,
        memory: ModelChoice
    ) async throws -> ModelSettingsView {
        do {
            return try await transport.send(
                TransportRequest(
                    method: .put,
                    path: "/v1/settings/models",
                    body: try JSONEncoder.server.encode(
                        UpdateModelSettingsBody(
                            expectedVersion: expectedVersion, chat: chat, memory: memory
                        )
                    ),
                    retryAttempts: 2
                )
            )
        } catch {
            throw modelSettingsCompatibilityError(from: error) ?? error
        }
    }

    public func listFolders(limit: Int = 200, cursor: String? = nil) async throws -> Page<FolderView> {
        var query = [URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200)))]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        do {
            return try await transport.send(
                TransportRequest(method: .get, path: "/v1/folders", queryItems: query)
            )
        } catch {
            throw folderCompatibilityError(from: error) ?? error
        }
    }

    public func createFolder(name: String) async throws -> FolderView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/folders",
                body: try JSONEncoder.server.encode(FolderNameBody(name: name))
            )
        )
    }

    public func renameFolder(_ id: UUID, name: String) async throws -> FolderView {
        try await transport.send(
            TransportRequest(
                method: .patch,
                path: "/v1/folders/\(id.uuidString)",
                body: try JSONEncoder.server.encode(FolderNameBody(name: name))
            )
        )
    }

    public func deleteFolder(_ id: UUID) async throws {
        _ = try await transport.sendData(
            TransportRequest(
                method: .delete,
                path: "/v1/folders/\(id.uuidString)",
                retryAttempts: 2
            )
        )
    }

    public func setSessionFolder(_ sessionID: UUID, folderID: UUID?) async throws -> SessionView {
        try await transport.send(
            TransportRequest(
                method: .put,
                path: "/v1/sessions/\(sessionID.uuidString)/folder",
                body: try JSONEncoder.server.encode(SetSessionFolderBody(folderID: folderID)),
                retryAttempts: 2
            )
        )
    }

    public func listFolderProposals(
        state: String = "proposed",
        limit: Int = 200,
        cursor: String? = nil
    ) async throws -> Page<FolderProposalView> {
        var query = [
            URLQueryItem(name: "state", value: state),
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200))),
        ]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        do {
            return try await transport.send(
                TransportRequest(method: .get, path: "/v1/folders/proposals", queryItems: query)
            )
        } catch {
            throw folderCompatibilityError(from: error) ?? error
        }
    }

    public func acceptFolderProposal(_ id: UUID, name: String? = nil) async throws
        -> FolderProposalView
    {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/folders/proposals/\(id.uuidString)/accept",
                body: try name.map { try JSONEncoder.server.encode(AcceptFolderProposalBody(name: $0)) }
            )
        )
    }

    public func declineFolderProposal(_ id: UUID) async throws -> FolderProposalView {
        try await transport.send(
            TransportRequest(
                method: .post,
                path: "/v1/folders/proposals/\(id.uuidString)/decline"
            )
        )
    }

    public func listSchedules(
        limit: Int = 50,
        cursor: String? = nil,
        states: [ScheduleStateKind] = []
    ) async throws -> Page<ScheduleListItemView> {
        var query = [
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200)))
        ]
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        query.append(
            contentsOf: states.map { URLQueryItem(name: "state", value: $0.rawValue) }
        )
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

/// A server that predates ADR-0117 but mounts the memory read router answers a
/// write with the same envelope it uses for any unsupported request: a method
/// miss on the GET-only path is rewritten to a 400 `malformed_request` reading
/// "The HTTP request is not supported.", and the absent review route is the
/// generic 404 "The requested resource was not found." A belief that no longer
/// exists carries the service's own message, so it stays a plain API error the
/// caller acts on by its body. A bare 405 is kept for completeness.
private func memoryChangesCompatibilityError(
    from error: Error, missingRouteIsUnsupported: Bool = false
) -> VeetbotAPIClientError? {
    guard case HTTPTransportError.api(let apiError) = error else { return nil }
    if apiError.statusCode == 405 {
        return .memoryChangesUnavailable
    }
    if apiError.statusCode == 400,
        apiError.code == .malformedRequest,
        apiError.message == "The HTTP request is not supported."
    {
        return .memoryChangesUnavailable
    }
    if missingRouteIsUnsupported,
        apiError.statusCode == 404,
        apiError.message == "The requested resource was not found."
    {
        return .memoryChangesUnavailable
    }
    return nil
}

/// Percent-encode a file name for the `X-Filename` header; the server decodes UTF-8.
func encodedHeaderFilename(_ filename: String) -> String {
    var allowed = CharacterSet.alphanumerics.intersection(.init(charactersIn: "\u{0}"..."\u{7F}"))
    allowed.insert(charactersIn: "-._~")
    return filename.addingPercentEncoding(withAllowedCharacters: allowed) ?? "attachment"
}

/// Attachments are default-off (ADR-0120): a server without the upload route
/// answers the generic 404 or a method miss, never the service's own message for
/// a missing conversation, which stays a plain API error.
private func attachmentCompatibilityError(from error: Error) -> VeetbotAPIClientError? {
    guard case HTTPTransportError.api(let apiError) = error else { return nil }
    if apiError.statusCode == 405 {
        return .attachmentsUnavailable
    }
    if apiError.statusCode == 404,
        apiError.message == "The requested resource was not found."
    {
        return .attachmentsUnavailable
    }
    return nil
}

/// Folders are an optional, default-off surface: a server that lacks the
/// router answers 404 or 405 on the list routes, and the client degrades to
/// the flat history rather than demanding an upgrade.
private func folderCompatibilityError(from error: Error) -> VeetbotAPIClientError? {
    guard case HTTPTransportError.api(let apiError) = error else { return nil }
    if apiError.statusCode == 404 || apiError.statusCode == 405 {
        return .foldersUnavailable
    }
    return nil
}

/// Model settings are a single resource with no per-item 404, so any 404 or
/// 405 on it means the server predates the feature.
private func modelSettingsCompatibilityError(from error: Error) -> VeetbotAPIClientError? {
    guard case HTTPTransportError.api(let apiError) = error else { return nil }
    if apiError.statusCode == 404 || apiError.statusCode == 405 {
        return .modelSettingsUnavailable
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

/// The begin body. `mode` is sent only for a device sign-in, so a remote begin
/// keeps today's bytes and stays compatible with an older server (ADR-0128).
struct BeginBrowserAuthenticationBody: Encodable {
    let loginURL: String
    let mode: BrowserAuthenticationMode

    enum CodingKeys: String, CodingKey {
        case loginURL = "login_url"
        case mode
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(loginURL, forKey: .loginURL)
        if mode == .device {
            try container.encode(mode, forKey: .mode)
        }
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

private struct DeviceInvocationResultBody: Encodable {
    let status: DeviceInvocationResult
}

private struct DeviceMessageBody: Encodable {
    let channel: String
    let sender: String
    let body: String
    let receivedAt: Date

    enum CodingKeys: String, CodingKey {
        case channel, sender, body
        case receivedAt = "received_at"
    }
}
