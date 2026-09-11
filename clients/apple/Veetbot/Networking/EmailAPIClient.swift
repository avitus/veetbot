import Foundation

extension VeetbotAPIClient {
    public func emailAccounts() async throws -> Page<EmailAccountView> {
        try await transport.send(TransportRequest(method: .get, path: "/v1/email/accounts"))
    }

    public func emailThreads(
        accountID: String? = nil, view: String = "priority", text: String = "",
        limit: Int = 5, cursor: String? = nil
    ) async throws -> Page<EmailThreadView> {
        var query = [URLQueryItem(name: "view", value: view), URLQueryItem(name: "limit", value: String(limit))]
        if let accountID { query.append(URLQueryItem(name: "account_id", value: accountID)) }
        if !text.isEmpty { query.append(URLQueryItem(name: "text", value: text)) }
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await transport.send(TransportRequest(method: .get, path: "/v1/email/threads", queryItems: query))
    }

    public func emailThread(_ id: UUID) async throws -> EmailThreadView {
        try await transport.send(TransportRequest(method: .get, path: "/v1/email/threads/\(id.uuidString)"))
    }

    public func refreshEmail(idempotencyKey: String) async throws -> EmailOperationView {
        try await transport.send(TransportRequest(
            method: .post, path: "/v1/email/refresh", body: Data("{}".utf8),
            headers: ["Idempotency-Key": idempotencyKey], retryAttempts: 2
        ))
    }

    public func emailOperation(_ id: UUID) async throws -> EmailOperationView {
        try await transport.send(TransportRequest(method: .get, path: "/v1/email/operations/\(id.uuidString)"))
    }

    public func emailFeedback(
        thread: EmailThreadView, target: EmailFeedbackTarget, judgment: String,
        explanation: String? = nil, targetValue: String? = nil, idempotencyKey: String
    ) async throws -> EmailFeedbackResult {
        var values: [String: JSONValue] = [
            "thread_id": .string(thread.id.uuidString), "target": .string(target.rawValue),
            "judgment": .string(judgment), "expected_revision": .number(Double(thread.revision))
        ]
        if let explanation { values["explanation"] = .string(explanation) }
        if let targetValue, !targetValue.isEmpty { values["target_value"] = .string(targetValue) }
        return try await emailCommand(path: "/feedback", values: values, key: idempotencyKey)
    }

    public func undoEmailFeedback(_ id: UUID, idempotencyKey: String) async throws -> EmailThreadView {
        try await transport.send(TransportRequest(
            method: .delete, path: "/v1/email/feedback/\(id.uuidString)",
            headers: ["Idempotency-Key": idempotencyKey], retryAttempts: 2
        ))
    }

    public func emailDiscussion(_ id: UUID, idempotencyKey: String) async throws -> EmailThreadView {
        try await emailCommand(path: "/threads/\(id.uuidString)/discussion", values: [:], key: idempotencyKey)
    }

    public func dismissEmailThread(_ thread: EmailThreadView, idempotencyKey: String) async throws -> EmailThreadView {
        try await emailCommand(path: "/threads/\(thread.id.uuidString)/dismiss",
                               values: ["expected_revision": .number(Double(thread.revision))], key: idempotencyKey)
    }

    public func generateEmailDraft(
        threadID: UUID, instruction: String?, idempotencyKey: String
    ) async throws -> EmailDraftOperation {
        var values: [String: JSONValue] = [:]
        if let instruction, !instruction.isEmpty { values["instruction"] = .string(instruction) }
        return try await emailCommand(path: "/threads/\(threadID.uuidString)/drafts", values: values, key: idempotencyKey)
    }

    public func emailDraft(_ id: UUID) async throws -> EmailDraftView {
        try await transport.send(TransportRequest(method: .get, path: "/v1/email/drafts/\(id.uuidString)"))
    }

    public func emailDraftRevisions(_ id: UUID) async throws -> Page<EmailDraftView> {
        try await transport.send(TransportRequest(method: .get, path: "/v1/email/drafts/\(id.uuidString)/revisions"))
    }

    public func emailLearning() async throws -> EmailLearningState {
        try await transport.send(TransportRequest(method: .get, path: "/v1/email/learning"))
    }

    public func endorseEmailDraftStyle(_ draft: EmailDraftView) async throws -> EmailLearningState {
        try await emailCommand(
            path: "/drafts/\(draft.id.uuidString)/style-example",
            values: ["expected_revision": .number(Double(draft.revision))],
            key: "style-example-\(draft.id.uuidString)-\(draft.revision)"
        )
    }

    public func setEmailLearningPaused(_ paused: Bool) async throws -> EmailLearningState {
        try await transport.send(TransportRequest(
            method: .put, path: "/v1/email/learning", body: try JSONEncoder.server.encode(["paused": paused]), retryAttempts: 2
        ))
    }

    public func resetEmailLearning(scope: String, idempotencyKey: String) async throws -> EmailLearningState {
        try await emailCommand(path: "/learning/reset", values: ["scope": .string(scope)], key: idempotencyKey)
    }

    public func excludeEmailThread(_ thread: EmailThreadView, idempotencyKey: String) async throws {
        _ = try await transport.sendData(TransportRequest(
            method: .post, path: "/v1/email/threads/\(thread.id.uuidString)/exclude",
            body: try JSONEncoder.server.encode(["expected_revision": thread.revision]),
            headers: ["Idempotency-Key": idempotencyKey], retryAttempts: 2
        ))
    }

    public func saveEmailDraft(_ edit: EmailDraftEdit, idempotencyKey: String) async throws -> EmailDraftView {
        var values: [String: JSONValue] = [
            "expected_revision": .number(Double(edit.base.revision)),
            "to": .array(EmailDraftEdit.addresses(edit.to).map(JSONValue.string)),
            "cc": .array(EmailDraftEdit.addresses(edit.cc).map(JSONValue.string)),
            "bcc": .array(EmailDraftEdit.addresses(edit.bcc).map(JSONValue.string)),
            "subject": .string(edit.subject), "body": .string(edit.body)
        ]
        if let revision = edit.reviewedSourceRevision { values["source_revision"] = .number(Double(revision)) }
        return try await transport.send(TransportRequest(
            method: .put, path: "/v1/email/drafts/\(edit.base.id.uuidString)",
            body: try JSONEncoder.server.encode(values), headers: ["Idempotency-Key": idempotencyKey], retryAttempts: 2
        ))
    }

    public func proposeEmailSend(_ draft: EmailDraftView, idempotencyKey: String) async throws -> EmailDraftOperation {
        try await emailCommand(
            path: "/drafts/\(draft.id.uuidString)/send-proposal",
            values: ["expected_revision": .number(Double(draft.revision))], key: idempotencyKey
        )
    }

    public func discardEmailDraft(_ draft: EmailDraftView, idempotencyKey: String) async throws {
        _ = try await transport.sendData(TransportRequest(
            method: .delete, path: "/v1/email/drafts/\(draft.id.uuidString)",
            queryItems: [URLQueryItem(name: "expected_revision", value: String(draft.revision))],
            headers: ["Idempotency-Key": idempotencyKey], retryAttempts: 2
        ))
    }

    private func emailCommand<Result: Decodable>(
        path: String, values: [String: JSONValue], key: String
    ) async throws -> Result {
        try await transport.send(TransportRequest(
            method: .post, path: "/v1/email\(path)", body: try JSONEncoder.server.encode(values),
            headers: ["Idempotency-Key": key], retryAttempts: 2
        ))
    }
}
