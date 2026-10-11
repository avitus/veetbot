import Combine
import Foundation

/// Ephemeral owner history. No prose is persisted or used as a source of truth.
@MainActor public final class MemorySynthesisViewModel: ObservableObject {
    @Published public private(set) var items: [MemorySynthesisOperation] = []
    @Published public private(set) var operation: MemorySynthesisOperation?
    @Published public private(set) var summary: MemorySummaryView?
    @Published public private(set) var isLoading = false
    @Published public private(set) var isSaving = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var message: String?
    @Published public private(set) var unavailable = false
    @Published public private(set) var kind: String?
    @Published public private(set) var state: String?
    @Published public private(set) var hasMore = false
    @Published public private(set) var canRetryWrite = false
    let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private var generation = UUID()
    private var cursors: Set<String> = []
    private var nextCursor: String?
    private var connectionValid = true
    private var observer: AnyCancellable?
    private enum WriteKind { case undo, review(MemoryReviewOutcome), delete }
    private struct PendingWrite {
        let kind: WriteKind
        let id: UUID
        let operationID: UUID
        let revision: Int
        let key: String
    }
    private var pendingWrite: PendingWrite?

    public init(notifications: NotificationCenter = .default, makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = { await MemoryViewModel.makeDefaultAPIClient() }) {
        self.makeAPIClient = makeAPIClient
        observer = notifications.publisher(for: .peopleConnectionChanged).sink { [weak self] _ in
            Task { @MainActor in self?.invalidateConnection() }
        }
    }

    public func select(kind: String?, state: String?) async {
        self.kind = kind
        self.state = state
        await reload()
    }

    public func reload() async {
        clearContent()
        await loadMore(first: true)
    }

    public func loadMore(first: Bool = false) async {
        guard connectionValid, !isLoading, first || nextCursor != nil else { return }
        let token = generation
        isLoading = true
        errorMessage = nil
        defer { if generation == token { isLoading = false } }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard generation == token else { return }
            let page = try await api.memorySynthesis(cursor: nextCursor, kind: kind, state: state)
            guard generation == token else { return }
            var seen = Set(items.map(\.id))
            items += page.items.filter { seen.insert($0.id).inserted }
            nextCursor = try? nextPageCursor(page.nextCursor, seen: &cursors)
            hasMore = nextCursor != nil
            unavailable = false
        } catch {
            guard generation == token else { return }
            if synthesisStatus(error) == 401 || synthesisStatus(error) == 403 { clearContent() }
            unavailable = synthesisStatus(error) == 404 || synthesisStatus(error) == 405
            errorMessage = unavailable ? "This server does not support synthesis browsing yet." : synthesisErrorMessage(error)
        }
    }

    public func open(_ id: UUID) async {
        clearContent()
        let token = generation
        isLoading = true
        defer { if generation == token { isLoading = false } }
        do {
            guard connectionValid, let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard generation == token else { return }
            let fresh = try await api.memorySynthesisOperation(id)
            guard generation == token else { return }
            guard fresh.id == id else { throw HTTPTransportError.invalidResponse }
            var derived: MemorySummaryView?
            if ["summary", "hypothesis"].contains(fresh.kind), fresh.state == "committed", let memoryID = fresh.content?.memoryID {
                derived = try await api.memorySummary(memoryID)
                guard generation == token else { return }
                guard derived?.operationID == id, derived?.id == memoryID, derived?.recordKind == fresh.kind else {
                    throw HTTPTransportError.invalidResponse
                }
            }
            operation = fresh
            summary = derived
        } catch {
            guard generation == token else { return }
            errorMessage = synthesisStatus(error) == 404 ? "This memory is no longer available." : synthesisErrorMessage(error)
        }
    }

    /// Retains only the identity of an uncertain action on this connection.
    public func clearContent() {
        generation = UUID()
        items = []; operation = nil; summary = nil
        isLoading = false; errorMessage = nil; message = nil
        nextCursor = nil; cursors = []; hasMore = false; unavailable = false
    }

    public func invalidateConnection() {
        connectionValid = false
        clearContent()
        canRetryWrite = false
        pendingWrite = nil
    }

    public func undo() async {
        guard pendingWrite == nil, let operation, operation.canUndo else { return }
        pendingWrite = PendingWrite(kind: .undo, id: operation.id, operationID: operation.id,
                                    revision: operation.revision, key: UUID().uuidString)
        await retryWrite()
    }
    public func review(_ outcome: MemoryReviewOutcome) async {
        guard pendingWrite == nil, let summary, summary.status == "active", summary.content != nil else { return }
        pendingWrite = PendingWrite(kind: .review(outcome), id: summary.id, operationID: summary.operationID,
                                    revision: summary.revision, key: UUID().uuidString)
        await retryWrite()
    }
    public func deleteSummary() async {
        guard pendingWrite == nil, let summary else { return }
        pendingWrite = PendingWrite(kind: .delete, id: summary.id, operationID: summary.operationID,
                                    revision: summary.revision, key: UUID().uuidString)
        await retryWrite()
    }
    public func retryWrite() async {
        guard connectionValid, !isSaving, let pending = pendingWrite else { return }
        let token = generation
        isSaving = true
        canRetryWrite = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard generation == token else { return }
            switch pending.kind {
            case .undo:
                let result = try await api.undoMemorySynthesis(pending.id, revision: pending.revision, key: pending.key)
                guard generation == token else { return }
                guard result.id == pending.id else { throw HTTPTransportError.invalidResponse }
                clearContent()
                operation = result
                message = "Merge undone. Original memories remain available."
            case .review(let outcome):
                let result = try await api.reviewMemorySummary(pending.id, outcome: outcome, key: pending.key)
                guard generation == token else { return }
                guard result.id == pending.id, result.operationID == pending.operationID else {
                    throw HTTPTransportError.invalidResponse
                }
                clearContent()
                summary = result
                message = outcome == .untrue ? "Synthesis rejected. Original memories are kept." : "Synthesis updated."
            case .delete:
                try await api.deleteMemory(pending.id, ceiling: memoryBrowsingCeiling, key: pending.key)
                guard generation == token else { return }
                clearContent()
                message = "Synthesis deleted. Original memories are kept."
            }
            pendingWrite = nil
            canRetryWrite = false
            NotificationCenter.default.post(name: .memorySynthesisChanged, object: nil)
        } catch {
            guard generation == token else { return }
            let status = synthesisStatus(error)
            clearContent()
            if let status, (400..<500).contains(status), status != 408, status != 429 {
                pendingWrite = nil
                canRetryWrite = false
                if status == 409 {
                    await open(pending.operationID)
                    errorMessage = "This memory changed. Review the refreshed details before trying again."
                } else {
                    errorMessage = synthesisErrorMessage(error)
                }
            } else {
                // The server may have committed before a timeout or decoding failure.
                // Keep this exact key, target and revision for the explicit Retry action.
                errorMessage = "The result could not be confirmed. Retry the same action safely."
            }
        }
    }
}

func synthesisStatus(_ error: Error) -> Int? {
    switch error {
    case VeetbotAPIClientError.memoryChangesUnavailable: return 405
    case HTTPTransportError.api(let error), HTTPTransportError.authorizationDenied(let error), HTTPTransportError.reauthenticationRequired(let error):
        return error.statusCode
    default: return nil
    }
}

func synthesisErrorMessage(_ error: Error) -> String {
    switch synthesisStatus(error) {
    case 401: return "Reconnect to view your memories."
    case 403: return "Your connection does not have permission for this memory action."
    case 404: return "This memory is no longer available."
    case 405: return "This server does not support this memory action yet."
    default: return "Memory could not be refreshed. Please retry."
    }
}

extension Notification.Name {
    static let memorySynthesisChanged = Notification.Name("VeetbotMemorySynthesisChanged")
}
