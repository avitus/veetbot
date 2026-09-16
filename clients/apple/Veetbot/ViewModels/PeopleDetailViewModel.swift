import Combine
import Foundation

@MainActor public final class PeopleDetailViewModel: ObservableObject {
    @Published public private(set) var profile: PersonProfileView?
    @Published public private(set) var history: [PersonInteractionView] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var isLoadingHistory = false
    @Published public private(set) var isSaving = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var historyError: String?
    @Published public private(set) var evidenceError: String?
    @Published public private(set) var preview: PeopleOperationView?
    @Published public private(set) var receipt: PeopleOperationView?
    @Published public private(set) var isForgotten = false
    @Published public private(set) var requiresRefresh = false
    @Published public private(set) var canRetrySave = false
    @Published public private(set) var identityEvidence: [PeopleIdentityEvidenceView] = []
    @Published public private(set) var isLoadingIdentityEvidence = false
    @Published public private(set) var identityEvidenceError: String?
    private var identityEvidenceStarted = false
    private var identityEvidenceCursor: String?
    private var identityEvidenceCursors: Set<String> = []
    public var hasMoreIdentityEvidence: Bool { !identityEvidenceStarted || identityEvidenceCursor != nil }

    public func loadIdentityEvidence(restart: Bool = false) async {
        guard isConnectionValid else { return }
        guard !isLoadingIdentityEvidence else { return }
        if restart { identityEvidenceStarted = false; identityEvidenceCursor = nil; identityEvidenceCursors = []; identityEvidence = [] }
        guard hasMoreIdentityEvidence else { return }
        let id = requestID
        isLoadingIdentityEvidence = true
        defer { if requestID == id { isLoadingIdentityEvidence = false } }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let page = try await api.peopleIdentityEvidence(personID, cursor: identityEvidenceCursor)
            guard requestID == id else { return }
            let cursor = try? nextPageCursor(page.nextCursor, seen: &identityEvidenceCursors)
            var seen = Set(identityEvidence.map(\.id))
            identityEvidence += page.items.filter { seen.insert($0.id).inserted }
            identityEvidenceStarted = true
            identityEvidenceCursor = cursor
            identityEvidenceError = nil
        } catch {
            guard isConnectionValid else { return }
            guard requestID == id else { return }
            identityEvidenceError = error.localizedDescription
            if case HTTPTransportError.api(let error) = error, error.statusCode == 409 {
                requiresRefresh = true
                identityEvidenceError = "This person's evidence changed. Close repair, refresh the person, and select the evidence again."
            }
        }
    }
    public let personID: UUID
    private var isConnectionValid = true
    private var connectionObserver: AnyCancellable?
    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private var requestID = UUID()
    private var nextHistoryCursor: String?
    private var historyStarted = false
    private var seenCursors: Set<String> = []
    private var auditSessionID: UUID?
    private var previewKind: WriteKind?
    private var pending: PendingWrite?
    private enum WriteKind { case update, correction, identity, forget }
    private struct PendingWrite {
        let kind: WriteKind
        let body: [String: JSONValue]
        let key: String
        let api: VeetbotAPIClient
    }

    public init(personID: UUID, notifications: NotificationCenter = .default, makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = { await MemoryViewModel.makeDefaultAPIClient() }) {
        self.personID = personID
        self.makeAPIClient = makeAPIClient
        connectionObserver = notifications.publisher(for: .peopleConnectionChanged).sink { [weak self] _ in
            Task { @MainActor in self?.invalidateConnection() }
        }
    }

    private func invalidateConnection() {
        isConnectionValid = false; requestID = UUID(); pending = nil; auditSessionID = nil
        profile = nil; history = []; identityEvidence = []; preview = nil; receipt = nil
        errorMessage = nil; historyError = nil; evidenceError = nil; identityEvidenceError = nil
        isLoading = false; isLoadingHistory = false; isLoadingIdentityEvidence = false; isSaving = false
        canRetrySave = false; requiresRefresh = false
    }

    public func reload(discardPending: Bool = false) async {
        guard isConnectionValid else { return }
        if discardPending { pending = nil; canRetrySave = false; preview = nil }
        let id = UUID()
        requestID = id
        identityEvidence = []; identityEvidenceStarted = false; identityEvidenceCursor = nil
        identityEvidenceCursors = []; identityEvidenceError = nil; isLoadingIdentityEvidence = false
        isLoadingHistory = false
        isLoading = true
        defer { if requestID == id { isLoading = false } }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let value = try await api.person(personID)
            guard requestID == id else { return }
            profile = value
            history = value.history
            nextHistoryCursor = nil
            seenCursors = []
            historyStarted = false
            historyError = nil
            errorMessage = nil
            requiresRefresh = false
        } catch {
            guard isConnectionValid else { return }
            guard requestID == id else { return }
            errorMessage = error.localizedDescription
        }
    }

    public func loadHistory() async {
        guard isConnectionValid else { return }
        guard !isLoadingHistory, !historyStarted || nextHistoryCursor != nil else { return }
        let id = requestID
        isLoadingHistory = true
        defer { if requestID == id { isLoadingHistory = false } }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let page = try await api.peopleHistory(personID, cursor: nextHistoryCursor)
            guard requestID == id else { return }
            if !historyStarted { history = []; historyStarted = true }
            var seen = Set(history.map(\.id))
            history += page.items.filter { seen.insert($0.id).inserted }
            nextHistoryCursor = try? nextPageCursor(page.nextCursor, seen: &seenCursors)
            historyError = nil
        } catch {
            guard isConnectionValid else { return }
            guard requestID == id else { return }
            historyError = error.localizedDescription
        }
    }

    public var hasMoreHistory: Bool { !historyStarted || nextHistoryCursor != nil }

    public func evidence(_ reference: UUID) async -> PeopleEvidenceView? {
        guard isConnectionValid else { return nil }
        evidenceError = nil
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return nil }
            let source = try await api.peopleEvidence(personID, reference: reference)
            return isConnectionValid ? source : nil
        } catch { guard isConnectionValid else { return nil }; evidenceError = "This source is unavailable: \(error.localizedDescription)"; return nil }
    }

    public func saveName(_ name: String, sessionID: UUID?) async {
        guard isConnectionValid else { return }
        guard let profile, !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        await submit(.update, body: ["display_name": .string(name), "expected_revision": .number(Double(profile.person.revision))], sessionID: sessionID)
    }

    public func setPinned(_ pinned: Bool, sessionID: UUID?) async {
        guard isConnectionValid else { return }
        guard let profile else { return }
        await submit(.update, body: ["pinned": .bool(pinned), "expected_revision": .number(Double(profile.person.revision))], sessionID: sessionID)
    }

    public func confirmIdentity(sessionID: UUID?) async {
        guard isConnectionValid else { return }
        guard let profile, profile.person.state == "provisional" else { return }
        await submit(.update, body: ["confirm": .bool(true), "expected_revision": .number(Double(profile.person.revision))], sessionID: sessionID)
    }

    public func addAlias(value: String, kind: String, context: String, sessionID: UUID?) async {
        guard isConnectionValid else { return }
        guard let profile, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        await submit(.update, body: ["expected_revision": .number(Double(profile.person.revision)),
            "alias": .object(["operation": .string("add"), "identifier_kind": .string(kind),
                "value": .string(value), "namespace": .string("owner"), "context": .string(context)])], sessionID: sessionID)
    }

    public func endAlias(_ alias: PersonAliasView, sessionID: UUID?) async {
        guard isConnectionValid else { return }
        guard let profile else { return }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        await submit(.update, body: ["expected_revision": .number(Double(profile.person.revision)),
            "alias": .object(["operation": .string("end"), "identifier_id": .string(alias.id.uuidString),
                "expected_revision": .number(Double(alias.revision)), "valid_to": .string(formatter.string(from: Date()))])], sessionID: sessionID)
    }

    public func correct(_ fact: MemoryView, operation: String, statement: String?, sessionID: UUID?, effectiveAt: Date? = nil, relationshipPredicate: String? = nil, commitmentState: String? = nil) async {
        guard isConnectionValid else { return }
        guard let profile, let revision = profile.factRevision(fact.id) else { return }
        var body: [String: JSONValue] = ["belief_id": .string(fact.id.uuidString), "expected_revision": .number(Double(profile.person.revision)),
            "expected_position": .number(Double(revision)), "operation": .string(operation)]
        if let statement { body["statement"] = .string(statement) }
        if let relationshipPredicate {
            guard let edge = profile.relationships.first(where: { $0.beliefID == fact.id }) else {
                errorMessage = "Refresh this person before changing the relationship."
                return
            }
            var projection: [String: JSONValue] = [
                "kind": .string("relationship"), "id": .string(edge.id.uuidString),
                "expected_revision": .number(Double(edge.revision)),
                "predicate": .string(relationshipPredicate), "qualifier": .string(edge.qualifier),
                "precision": .string(edge.precision), "subject": edge.subject.correctionValue,
                "object": edge.object.correctionValue
            ]
            if let zone = edge.sourceTimezone { projection["source_timezone"] = .string(zone) }
            body["projection"] = .object(projection)
        } else if let commitmentState {
            guard let edge = profile.commitments.first(where: { $0.beliefID == fact.id }), let statement else {
                errorMessage = "Refresh this person before changing the commitment."
                return
            }
            var projection: [String: JSONValue] = [
                "kind": .string("commitment"), "id": .string(edge.id.uuidString),
                "expected_revision": .number(Double(edge.revision)), "state": .string(commitmentState),
                "description": .string(statement), "debtor": edge.debtor.correctionValue,
                "beneficiary": edge.beneficiary.correctionValue
            ]
            projection["due_precision"] = .string(edge.duePrecision ?? "unknown")
            if let zone = edge.sourceTimezone { projection["source_timezone"] = .string(zone) }
            if let dueAt = edge.dueAt { projection["due_at"] = .string(ISO8601DateFormatter().string(from: dueAt)) }
            body["projection"] = .object(projection)
        }
        if let effectiveAt {
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            body["effective_at"] = .string(formatter.string(from: effectiveAt))
        }
        await submit(.correction, body: body, sessionID: sessionID)
    }

    public func previewIdentity(operation: String, target: PersonView, selectedIDs: Set<UUID>, sessionID: UUID?) async {
        guard isConnectionValid else { return }
        guard let profile, target.id != personID else { return }
        let body: [String: JSONValue] = ["operation": .string(operation), "source_id": .string(personID.uuidString),
            "target_id": .string(target.id.uuidString), "expected_revisions": .object([
                personID.uuidString: .number(Double(profile.person.revision)), target.id.uuidString: .number(Double(target.revision))]),
            "selected_ids": .array(selectedIDs.sorted { $0.uuidString < $1.uuidString }.map { .string($0.uuidString) })]
        await submit(.identity, body: body, sessionID: sessionID)
    }

    public func previewForget(sessionID: UUID?) async {
        guard isConnectionValid else { return }
        guard let profile else { return }
        await submit(.forget, body: ["phase": .string("preview"), "expected_revision": .number(Double(profile.person.revision))], sessionID: sessionID)
    }

    public func applyPreview(_ accepted: PeopleOperationView, sessionID: UUID?) async {
        guard isConnectionValid else { return }
        guard accepted.state == "preview" else { return }
        let preview = accepted
        let kind: WriteKind = preview.operation == nil ? .forget : .identity
        var body: [String: JSONValue] = ["operation_id": .string(preview.id.uuidString), "expected_revision": .number(Double(preview.revision))]
        body[kind == .forget ? "phase" : "operation"] = .string("apply")
        await submit(kind, body: body, sessionID: sessionID)
    }

    public func undoIdentity(sessionID: UUID?) async {
        guard isConnectionValid else { return }
        guard let receipt, receipt.operation != nil, receipt.state == "completed" else { return }
        await submit(.identity, body: ["operation": .string("undo"), "operation_id": .string(receipt.id.uuidString),
            "expected_revision": .number(Double(receipt.revision))], sessionID: sessionID)
    }

    public func cancelPreview() { preview = nil; previewKind = nil }

    public func refreshReceipt() async {
        guard isConnectionValid else { return }
        guard let receipt else { return }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let loaded = try await api.peopleOperation(receipt.id)
            guard isConnectionValid else { return }
            self.receipt = loaded
        } catch { if isConnectionValid { errorMessage = error.localizedDescription } }
    }

    public func retrySave() async {
        guard isConnectionValid else { return }
        guard pending != nil, !isSaving, !requiresRefresh else { return }
        await performPending()
    }

    private func submit(_ kind: WriteKind, body: [String: JSONValue], sessionID: UUID?) async {
        guard isConnectionValid, !isSaving, pending == nil, !requiresRefresh else { return }
        isSaving = true
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let auditID: UUID
            if let existing = sessionID ?? auditSessionID { auditID = existing }
            else {
                let session = try await api.createSession(metadata: ["purpose": .string("people-management")])
                guard isConnectionValid else { return }
                auditID = session.id
                auditSessionID = auditID
            }
            var scoped = body
            scoped["session_id"] = .string(auditID.uuidString)
            pending = PendingWrite(kind: kind, body: scoped, key: UUID().uuidString, api: api)
            isSaving = false
            await performPending()
        } catch { if isConnectionValid { isSaving = false; errorMessage = error.localizedDescription } }
    }

    private func performPending() async {
        guard isConnectionValid, let write = pending else { return }
        isSaving = true
        canRetrySave = false
        errorMessage = nil
        defer { isSaving = false }
        do {
            switch write.kind {
            case .update:
                _ = try await write.api.writePerson(personID, body: write.body, key: write.key)
            case .correction:
                let result = try await write.api.correctPerson(personID, body: write.body, key: write.key)
                guard isConnectionValid else { return }
                if let erasure = result.erasure { receipt = erasure }
            case .identity, .forget:
                let result = try await (write.kind == .identity
                    ? write.api.identityOperation(body: write.body, key: write.key)
                    : write.api.forgetPerson(personID, body: write.body, key: write.key))
                guard isConnectionValid else { return }
                if result.state == "preview" { preview = result; previewKind = write.kind }
                else {
                    preview = nil
                    receipt = result
                    if write.kind == .forget { isForgotten = true; profile = nil; history = [] }
                }
            }
            guard isConnectionValid else { return }
            pending = nil
            if preview == nil && !isForgotten { await reload() }
        } catch {
            guard isConnectionValid else { return }
            errorMessage = error.localizedDescription
            if case HTTPTransportError.api(let error) = error, error.statusCode == 409 {
                requiresRefresh = true
                pending = nil
                preview = nil
                errorMessage = "This person changed. Refresh and review your change before saving again."
            } else if case HTTPTransportError.api(let error) = error,
                      (400..<500).contains(error.statusCode ?? 0), error.statusCode != 408, error.statusCode != 429 {
                pending = nil
            } else if case HTTPTransportError.authorizationDenied = error {
                pending = nil
            } else { canRetrySave = true }
        }
    }
}

/// Reads the exact source through its original permission boundary.
@MainActor public final class PeopleEvidenceViewModel: ObservableObject {
    @Published public private(set) var hasMoreHistory = false
    private var historyCursor: String?
    private var historyCursors: Set<String> = []
    public func loadMoreHistory() async {
        guard isConnectionValid, hasMoreHistory, !isLoading else { return }
        isLoading = true; defer { isLoading = false }
        errorMessage = nil
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            try await readHistory(using: api)
        } catch { if isConnectionValid { errorMessage = error.localizedDescription } }
    }
    @Published public private(set) var text: String?
    @Published public private(set) var heading: String?
    @Published public private(set) var partial = false
    @Published public private(set) var isLoading = false
    @Published public private(set) var errorMessage: String?
    public let source: PeopleEvidenceView
    private var isConnectionValid = true
    private var connectionObserver: AnyCancellable?
    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    public init(source: PeopleEvidenceView, notifications: NotificationCenter = .default, makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = { await MemoryViewModel.makeDefaultAPIClient() }) {
        self.source = source
        self.makeAPIClient = makeAPIClient
        connectionObserver = notifications.publisher(for: .peopleConnectionChanged).sink { [weak self] _ in
            Task { @MainActor in self?.invalidateConnection() }
        }
    }
    private func invalidateConnection() {
        isConnectionValid = false; text = nil; heading = nil; partial = false
        hasMoreHistory = false; historyCursor = nil; historyCursors = []
        errorMessage = nil; isLoading = false
    }
    public func load() async {
        guard isConnectionValid else { return }
        guard !isLoading else { return }
        isLoading = true; defer { isLoading = false }
        text = nil; heading = nil; partial = false; errorMessage = nil
        hasMoreHistory = false; historyCursor = nil; historyCursors = []
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            if source.sourceKind == "email" {
                guard let id = source.emailThreadID else {
                    errorMessage = "The original message is unavailable in retained Email."
                    return
                }
                let thread = try await api.emailThread(id)
                guard isConnectionValid else { return }
                guard thread.accountID == source.accountID,
                      let message = thread.messages?.first(where: { $0.id == source.messageID }) else {
                    errorMessage = "The exact source message is no longer available."
                    return
                }
                heading = "\(message.subject)\nFrom: \(message.sender)"
                text = message.body
                partial = !message.complete
                return
            }
            if let assertion = source.ownerAssertion {
                heading = "Your recorded change"
                text = assertion
                return
            }
            try await readHistory(using: api)
        } catch { if isConnectionValid { errorMessage = error.localizedDescription } }
    }
    private func readHistory(using api: VeetbotAPIClient) async throws {
        for _ in 0..<10 {
            let page = try await api.listSessionMessages(sessionID: source.sessionID, limit: 200, cursor: historyCursor)
            guard isConnectionValid else { return }
            if let message = page.items.first(where: { $0.sequence == source.eventSequence }) {
                heading = source.sourceKind == "sms" ? "Source SMS" : "Source message"
                text = message.content.compactMap(\.text).joined(separator: "\n")
                hasMoreHistory = false; historyCursor = nil
                return
            }
            guard let next = page.nextCursor else {
                hasMoreHistory = false; historyCursor = nil
                errorMessage = "Source text is unavailable in synchronized history."
                return
            }
            guard historyCursors.insert(next).inserted else {
                hasMoreHistory = false; historyCursor = nil
                throw HTTPTransportError.invalidResponse
            }
            historyCursor = next
            hasMoreHistory = true
        }
    }

}
