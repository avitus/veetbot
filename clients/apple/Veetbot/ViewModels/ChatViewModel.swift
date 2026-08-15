import Combine
import Foundation

public struct LoadedArtifact: Sendable {
    public let metadata: ArtifactView
    public let data: Data
}

@MainActor
public final class ChatViewModel: ObservableObject {
    @Published public private(set) var history: [SessionHistoryEntry] = []
    @Published public private(set) var selectedSessionID: UUID?
    @Published public private(set) var baseURL: URL?
    @Published public private(set) var isConfigured = false
    @Published public private(set) var requiresReauthentication = false
    @Published public private(set) var isSending = false
    @Published public private(set) var sessionBusy = false
    @Published public private(set) var errorMessage: String?

    public let runState: RunStateReducer

    private let tokenStore: any TokenStore
    private let configurationStore: ConnectionConfigurationStore
    private let historyStore: any SessionHistoryStore
    private let artifactCache: ArtifactCache
    private let urlSession: URLSession?
    private var api: VeetbotAPIClient?
    private var eventStream: ReconnectingEventStream?
    private let watchTasks = WatchTaskBox()
    private var loadedApprovalIDs: Set<UUID> = []
    private var selectionRequestID: UUID?
    private var removedHistorySessionIDs: Set<UUID> = []
    private var deletingHistorySessionIDs: Set<UUID> = []
    private var historyReconciliationID: UUID?
    private var pendingSubmission: (text: String, key: String)?

    public init(
        tokenStore: any TokenStore = KeychainTokenStore(),
        configurationStore: ConnectionConfigurationStore = ConnectionConfigurationStore(),
        historyStore: (any SessionHistoryStore)? = nil,
        artifactCache: ArtifactCache = ArtifactCache(),
        runState: RunStateReducer? = nil,
        urlSession: URLSession? = nil
    ) {
        self.tokenStore = tokenStore
        self.configurationStore = configurationStore
        self.historyStore = historyStore ?? SessionHistoryStoreFactory.makeDefault()
        self.artifactCache = artifactCache
        self.runState = runState ?? RunStateReducer()
        self.urlSession = urlSession
        Task { await bootstrap() }
    }

    @discardableResult
    public func configure(baseURLString: String, token: String) async -> Bool {
        do {
            let configuration = try ConnectionConfiguration(baseURLString: baseURLString)
            let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmedToken.isEmpty {
                try await tokenStore.saveToken(trimmedToken)
            }
            guard try await tokenStore.readToken() != nil else {
                throw HTTPTransportError.missingToken
            }
            try await install(configuration)
            await configurationStore.save(configuration)
            requiresReauthentication = false
            errorMessage = nil
            return true
        } catch {
            present(error)
            return false
        }
    }

    public func forgetCredentials() async {
        selectionRequestID = nil
        historyReconciliationID = nil
        watchTasks.cancel()
        do { try await tokenStore.deleteToken() } catch { present(error) }
        api = nil
        eventStream = nil
        await artifactCache.removeAll()
        isConfigured = false
        requiresReauthentication = true
    }

    public func newSession() {
        selectionRequestID = nil
        watchTasks.cancel()
        selectedSessionID = nil
        sessionBusy = false
        loadedApprovalIDs.removeAll()
        pendingSubmission = nil
        runState.reset()
    }

    public func selectSession(_ entry: SessionHistoryEntry) async {
        guard let api, !removedHistorySessionIDs.contains(entry.sessionID) else { return }
        let requestID = UUID()
        selectionRequestID = requestID
        watchTasks.cancel()
        selectedSessionID = entry.sessionID
        sessionBusy = false
        loadedApprovalIDs.removeAll()
        pendingSubmission = nil
        runState.reset()
        do {
            let session = try await api.getSession(entry.sessionID)
            guard selectionRequestID == requestID,
                !removedHistorySessionIDs.contains(entry.sessionID)
            else { return }
            try await store(
                session: session,
                lastRunID: session.activeRunID ?? session.lastRunID ?? entry.lastRunID
            )
            guard selectionRequestID == requestID else {
                if removedHistorySessionIDs.contains(entry.sessionID) {
                    try? await historyStore.delete(sessionID: entry.sessionID)
                    history =
                        (try? await historyStore.list())
                        ?? history.filter {
                            $0.sessionID != entry.sessionID
                        }
                }
                return
            }
            if let runID = session.activeRunID ?? session.lastRunID ?? entry.lastRunID {
                let run = try await api.getRun(runID)
                guard selectionRequestID == requestID else { return }
                runState.seed(run: run)
                watch(runID: run.id, touchHistoryOnCompletion: run.status.isActive)
            }
        } catch {
            if selectionRequestID == requestID {
                present(error)
            }
        }
    }

    public func deleteSessionEverywhere(_ entry: SessionHistoryEntry) async {
        guard let api else { return }
        historyReconciliationID = nil
        deletingHistorySessionIDs.insert(entry.sessionID)
        defer { deletingHistorySessionIDs.remove(entry.sessionID) }
        do {
            try await api.deleteSession(entry.sessionID)
            removedHistorySessionIDs.insert(entry.sessionID)
            historyReconciliationID = nil
            if selectedSessionID == entry.sessionID { newSession() }
            try await historyStore.delete(sessionID: entry.sessionID)
            history = try await historyStore.list()
            await artifactCache.removeAll()
        } catch {
            present(error)
        }
    }

    public func synchronizeHistory() async {
        do {
            try await reconcileHistory()
        } catch let error as VeetbotAPIClientError {
            present(error)
        } catch let error as HTTPTransportError {
            if case .reauthenticationRequired = error {
                present(error)
            }
        } catch {
            // Background reconciliation is best effort; direct user actions
            // still surface their own failures.
        }
    }

    private func reconcileHistory() async throws {
        guard let api else { return }
        let reconciliationID = UUID()
        historyReconciliationID = reconciliationID
        defer {
            if historyReconciliationID == reconciliationID {
                historyReconciliationID = nil
            }
        }
        let locallyKnownAtStart = Set(history.map(\.sessionID))
        var cursor: String?
        var seenCursors: Set<String> = []
        var serverSessions: [SessionView] = []
        repeat {
            let page = try await api.listSessions(cursor: cursor)
            guard historyReconciliationID == reconciliationID else { return }
            serverSessions.append(contentsOf: page.items)
            cursor = try Self.nextHistoryCursor(page.nextCursor, seen: &seenCursors)
        } while cursor != nil
        guard historyReconciliationID == reconciliationID else { return }

        let serverIDs = Set(serverSessions.map(\.id))
        for session in serverSessions
        where !removedHistorySessionIDs.contains(session.id)
            && !deletingHistorySessionIDs.contains(session.id)
        {
            guard historyReconciliationID == reconciliationID else { return }
            let existing = history.first { $0.sessionID == session.id }
            let entry = Self.mergedHistoryEntry(
                session: session,
                existing: existing,
                lastRunID: session.lastRunID,
                suggestedTitle: nil,
                touchedAt: session.updatedAt
            )
            guard historyReconciliationID == reconciliationID else { return }
            try await historyStore.upsert(entry)
            guard historyReconciliationID == reconciliationID else {
                await discardSuccessfullyDeletedHistory()
                return
            }
        }
        var prunedHistory = false
        for sessionID in locallyKnownAtStart.subtracting(serverIDs) {
            guard historyReconciliationID == reconciliationID else { return }
            do {
                let session = try await api.getSession(sessionID)
                guard historyReconciliationID == reconciliationID else { return }
                guard !removedHistorySessionIDs.contains(session.id),
                    !deletingHistorySessionIDs.contains(session.id)
                else { continue }
                let existing = history.first { $0.sessionID == session.id }
                let entry = Self.mergedHistoryEntry(
                    session: session,
                    existing: existing,
                    lastRunID: session.lastRunID,
                    suggestedTitle: nil,
                    touchedAt: session.updatedAt
                )
                guard historyReconciliationID == reconciliationID else { return }
                try await historyStore.upsert(entry)
                guard historyReconciliationID == reconciliationID else {
                    await discardSuccessfullyDeletedHistory()
                    return
                }
            } catch let error as HTTPTransportError {
                guard historyReconciliationID == reconciliationID else { return }
                if case .api(let apiError) = error, apiError.statusCode == 404 {
                    guard historyReconciliationID == reconciliationID else { return }
                    try await historyStore.delete(sessionID: sessionID)
                    guard historyReconciliationID == reconciliationID else { return }
                    prunedHistory = true
                    if selectedSessionID == sessionID { newSession() }
                } else {
                    throw error
                }
            }
        }
        guard historyReconciliationID == reconciliationID else { return }
        let reconciledHistory = try await historyStore.list()
        guard historyReconciliationID == reconciliationID else { return }
        history = reconciledHistory
        if prunedHistory {
            await artifactCache.removeAll()
            guard historyReconciliationID == reconciliationID else { return }
        }
    }

    private func discardSuccessfullyDeletedHistory() async {
        for sessionID in removedHistorySessionIDs {
            try? await historyStore.delete(sessionID: sessionID)
        }
    }

    static func nextHistoryCursor(
        _ cursor: String?,
        seen: inout Set<String>
    ) throws -> String? {
        guard let cursor else { return nil }
        guard seen.insert(cursor).inserted else {
            throw HTTPTransportError.invalidResponse
        }
        return cursor
    }

    @discardableResult
    public func send(_ rawText: String) async -> Bool {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let api else { return false }
        guard !isSending else { return false }

        if runState.isRunActive {
            if runState.runStatus == .waitingForUser,
                let prompt = runState.clarifyingQuestion
            {
                return await answerQuestion(prompt, answer: text)
            } else {
                sessionBusy = true
                errorMessage = "This session already has an active run."
                return false
            }
        }

        isSending = true
        sessionBusy = false
        defer { isSending = false }
        let idempotencyKey: String
        if let pendingSubmission, pendingSubmission.text == text {
            idempotencyKey = pendingSubmission.key
        } else {
            idempotencyKey = UUID().uuidString.lowercased()
            pendingSubmission = (text, idempotencyKey)
        }
        do {
            let session: SessionView
            if let selectedSessionID {
                session = try await api.getSession(selectedSessionID)
            } else {
                session = try await api.createSession()
                selectedSessionID = session.id
                try await store(session: session, lastRunID: nil, suggestedTitle: text)
            }
            let submit = try await api.submitMessage(
                sessionID: session.id,
                content: [.text(text)],
                idempotencyKey: idempotencyKey
            )
            runState.begin(runID: submit.runID, status: submit.status)
            do {
                try await store(
                    session: session,
                    lastRunID: submit.runID,
                    suggestedTitle: text,
                    touchedNow: true
                )
            } catch {
                // The server accepted the message; a local cache failure must
                // not invite a second submission with a new idempotency key.
                present(error)
            }
            watch(runID: submit.runID)
            pendingSubmission = nil
            return true
        } catch {
            if let apiError = apiError(from: error),
                apiError.code == .conflict,
                apiError.details.reason == "active_run_exists",
                let runID = apiError.details.runID
            {
                pendingSubmission = nil
                sessionBusy = true
                errorMessage = "This session is busy; attached to its active run."
                do {
                    let run = try await api.getRun(runID)
                    runState.seed(run: run)
                    watch(runID: runID, touchHistoryOnCompletion: run.status.isActive)
                } catch {
                    present(error)
                }
            } else {
                present(error)
            }
            return false
        }
    }

    @discardableResult
    public func answerQuestion(
        _ prompt: ClarifyingQuestionPrompt,
        answer rawAnswer: String
    ) async -> Bool {
        guard let api, runState.runStatus == .waitingForUser else { return false }
        let answer = rawAnswer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !answer.isEmpty else { return false }
        isSending = true
        defer { isSending = false }
        do {
            let result = try await api.deliverInput(
                runID: prompt.runID,
                content: [.text(answer)],
                questionID: prompt.questionID
            )
            runState.begin(runID: result.runID, status: result.status)
            try await touchSelectedHistory(lastRunID: result.runID)
            // The existing SSE connection remains open while the run is suspended.
            return true
        } catch {
            present(error)
            return false
        }
    }

    public func cancelActiveRun() async {
        guard let api, let runID = runState.activeRunID, runState.isRunActive else { return }
        do {
            let run = try await api.cancelRun(runID)
            runState.seed(run: run)
        } catch {
            present(error)
        }
    }

    public func resolveApproval(
        _ approval: ApprovalView,
        decision: ApprovalDecision,
        reason: String? = nil
    ) async {
        guard let api else { return }
        do {
            let stored = try await api.resolveApproval(
                approval.id,
                decision: decision,
                reason: reason
            )
            runState.mergeApproval(stored)
        } catch {
            if let apiError = apiError(from: error),
                apiError.code == .conflict,
                apiError.details.reason == "approval_already_resolved"
            {
                do {
                    runState.mergeApproval(try await api.getApproval(approval.id))
                    errorMessage =
                        "This approval was already resolved; the first decision is shown."
                } catch {
                    present(error)
                }
            } else {
                present(error)
            }
        }
    }

    public func refreshPendingApprovals() async {
        guard let api else { return }
        do {
            var cursor: String?
            var pageCount = 0
            repeat {
                let page = try await api.listPendingApprovals(
                    runID: runState.activeRunID,
                    sessionID: selectedSessionID,
                    cursor: cursor
                )
                for approval in page.items {
                    loadedApprovalIDs.insert(approval.id)
                    runState.mergeApproval(approval)
                }
                cursor = page.nextCursor
                pageCount += 1
            } while cursor != nil && pageCount < 20
        } catch {
            present(error)
        }
    }

    public func loadArtifact(_ artifactID: UUID) async throws -> LoadedArtifact {
        guard let api else { throw HTTPTransportError.notConfigured }
        let metadata = try await api.getArtifact(artifactID)
        let cached = await artifactCache.value(for: artifactID)
        let response = try await api.getArtifactContent(artifactID, etag: cached?.etag)
        switch response {
        case .content(let data, let etag):
            let value = CachedArtifactContent(data: data, etag: etag)
            await artifactCache.insert(value, for: artifactID)
            return LoadedArtifact(metadata: metadata, data: data)
        case .notModified:
            if let cached { return LoadedArtifact(metadata: metadata, data: cached.data) }
            let retry = try await api.getArtifactContent(artifactID)
            guard case .content(let data, let etag) = retry else {
                throw HTTPTransportError.invalidResponse
            }
            await artifactCache.insert(
                CachedArtifactContent(data: data, etag: etag),
                for: artifactID
            )
            return LoadedArtifact(metadata: metadata, data: data)
        }
    }

    public func clearError() { errorMessage = nil }

    public func clearCachedArtifacts() async {
        await artifactCache.removeAll()
    }

    private func bootstrap() async {
        do {
            history = try await historyStore.list()
        } catch {
            present(error)
        }
        do {
            if let configuration = await configurationStore.load(),
                try await tokenStore.readToken() != nil
            {
                try await install(configuration)
            }
        } catch {
            present(error)
        }
    }

    private func install(_ configuration: ConnectionConfiguration) async throws {
        await artifactCache.removeAll()
        pendingSubmission = nil
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: tokenStore,
            session: urlSession
        )
        api = VeetbotAPIClient(transport: transport)
        eventStream = ReconnectingEventStream(reader: SSEReader(transport: transport))
        baseURL = configuration.baseURL
        do {
            try await reconcileHistory()
        } catch let error as VeetbotAPIClientError {
            clearInstalledConnection()
            throw error
        } catch let error as HTTPTransportError {
            if case .reauthenticationRequired = error {
                clearInstalledConnection()
                throw error
            }
        }
        isConfigured = true
    }

    private func clearInstalledConnection() {
        api = nil
        eventStream = nil
        baseURL = nil
        isConfigured = false
    }

    private func watch(runID: UUID, touchHistoryOnCompletion: Bool = true) {
        guard let eventStream else { return }
        watchTasks.cancel()
        watchTasks.task = Task { [weak self] in
            do {
                for try await frame in eventStream.frames(runID: runID) {
                    guard let self else { return }
                    self.runState.reduce(frame)
                    if frame.event == "approval.requested"
                        || frame.event == "run.waiting_for_approval"
                    {
                        await self.loadApprovalsRequiredByReducer()
                        await self.refreshPendingApprovals()
                    }
                }
                guard let self else { return }
                if touchHistoryOnCompletion {
                    await self.refreshHistoryAfterRun(runID)
                }
            } catch is CancellationError {
                return
            } catch {
                self?.present(error)
            }
        }
    }

    private func loadApprovalsRequiredByReducer() async {
        guard let api else { return }
        for approvalID in runState.needsApprovalIDs where !loadedApprovalIDs.contains(approvalID) {
            do {
                let approval = try await api.getApproval(approvalID)
                loadedApprovalIDs.insert(approvalID)
                runState.mergeApproval(approval)
            } catch {
                present(error)
            }
        }
    }

    private func refreshHistoryAfterRun(_ runID: UUID) async {
        do {
            try await updateSelectedHistory(lastRunID: runID)
        } catch {
            present(error)
        }
    }

    private func store(
        session: SessionView,
        lastRunID: UUID?,
        suggestedTitle: String? = nil,
        touchedNow: Bool = false
    ) async throws {
        let existing = history.first { $0.sessionID == session.id }
        let entry = Self.mergedHistoryEntry(
            session: session,
            existing: existing,
            lastRunID: lastRunID,
            suggestedTitle: suggestedTitle,
            touchedAt: touchedNow ? Date() : nil
        )
        try await historyStore.upsert(entry)
        history = try await historyStore.list()
    }

    static func mergedHistoryEntry(
        session: SessionView,
        existing: SessionHistoryEntry?,
        lastRunID: UUID?,
        suggestedTitle: String?,
        touchedAt: Date?
    ) -> SessionHistoryEntry {
        let title =
            session.title
            ?? existing?.title
            ?? suggestedTitle.map(Self.title(from:))
            ?? "New conversation"
        return SessionHistoryEntry(
            sessionID: session.id,
            title: title,
            agentID: session.agentID,
            createdAt: session.createdAt,
            updatedAt: touchedAt ?? existing?.updatedAt ?? session.updatedAt,
            lastRunID: lastRunID ?? session.lastRunID ?? existing?.lastRunID
        )
    }

    private func touchSelectedHistory(lastRunID: UUID) async throws {
        try await updateSelectedHistory(lastRunID: lastRunID)
    }

    private func updateSelectedHistory(lastRunID: UUID) async throws {
        guard let selectedSessionID,
            var entry = history.first(where: { $0.sessionID == selectedSessionID })
        else { return }
        entry.updatedAt = Date()
        entry.lastRunID = lastRunID
        try await historyStore.upsert(entry)
        history = try await historyStore.list()
    }

    private static func title(from text: String) -> String {
        let collapsed = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        return String(collapsed.prefix(64))
    }

    private func apiError(from error: Error) -> APIError? {
        switch error {
        case HTTPTransportError.api(let value),
            HTTPTransportError.reauthenticationRequired(let value),
            HTTPTransportError.authorizationDenied(let value):
            return value
        default:
            return nil
        }
    }

    private func present(_ error: Error) {
        if case HTTPTransportError.reauthenticationRequired = error {
            requiresReauthentication = true
        }
        errorMessage = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}

/// Main-actor callers own all task reads and writes; `deinit` may only cancel off-actor.
private final class WatchTaskBox: @unchecked Sendable {
    var task: Task<Void, Never>?

    func cancel() {
        task?.cancel()
        task = nil
    }

    deinit { task?.cancel() }
}
