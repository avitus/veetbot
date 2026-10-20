import Combine
import Foundation

/// Browses the calling principal's beliefs over the memory API
/// (memory-read-api-and-browser.md) and applies the two writes of ADR-0117,
/// review and deletion. The view model holds no memory state of its own: it
/// re-fetches from the server on every reload and discards its page cache
/// whenever a filter, the search text, or the connection changes; a write
/// edits the row from the server's own response.
@MainActor
public final class MemoryViewModel: ObservableObject {
    @Published public private(set) var items: [MemoryView] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var isLoadingMore = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var unavailable = false
    @Published public private(set) var searchText = ""
    @Published public private(set) var statusFilter: MemoryStatusKind?
    @Published public private(set) var typeFilter: MemoryBeliefTypeKind?
    /// True asks the server for the review queue only (ADR-0117).
    @Published public private(set) var flaggedOnly = false
    /// Set when a write answered method-not-allowed: the server browses but
    /// predates review and deletion.
    @Published public private(set) var changesUnavailable = false
    /// The identifier of the row a write is acting on, so the views can
    /// disable a second action on it until the first settles.
    @Published public private(set) var pendingActionID: UUID?
    /// The sidebar's numbers (ADR-0214); nil while unknown or when the server
    /// predates the counts route, so a row then shows no number rather than 0.
    @Published public private(set) var counts: MemoryCountsView?

    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private let toasts: ToastCenter
    private var nextCursor: String?
    private var seenCursors: Set<String> = []
    private var reloadRequestID: UUID?
    private var searchDebounceTask: Task<Void, Never>?
    private var lastFailedOperation: FailedOperation?

    /// Which fetch last failed, so a single `retry()` can re-run the right
    /// one: `loadMore()` no-ops once `reload()` has already reset the
    /// cursor to nil, and re-running the wrong operation is exactly how a
    /// footer's Retry button goes dead.
    private enum FailedOperation {
        case reload
        case loadMore
    }

    private static let searchDebounceNanoseconds: UInt64 = 300_000_000

    /// The API client is produced by a closure rather than held directly so
    /// tests can stub it; the default mirrors how `ChatViewModel` builds its
    /// own `VeetbotAPIClient` from the already-persisted connection.
    public init(
        makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = {
            await MemoryViewModel.makeDefaultAPIClient()
        },
        toasts: ToastCenter? = nil
    ) {
        self.makeAPIClient = makeAPIClient
        self.toasts = toasts ?? .shared
    }

    deinit {
        searchDebounceTask?.cancel()
    }

    public static func makeDefaultAPIClient() async -> VeetbotAPIClient? {
        #if DEBUG && !SWIFT_PACKAGE
        if let fixtureClient = ConversationNavigationUITestFixture.makeMemoryAPIClientIfRequested()
        {
            return fixtureClient
        }
        #endif
        let configurationStore = ConnectionConfigurationStore()
        guard let configuration = await configurationStore.load() else { return nil }
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: KeychainTokenStore(),
            session: nil
        )
        return VeetbotAPIClient(transport: transport)
    }

    public func setSearchText(_ text: String) {
        guard searchText != text else { return }
        searchText = text
        searchDebounceTask?.cancel()
        searchDebounceTask = Task { [weak self] in
            do {
                try await Task.sleep(nanoseconds: Self.searchDebounceNanoseconds)
            } catch {
                return
            }
            guard !Task.isCancelled, let self else { return }
            // Clear the stored task before reload() runs: reload() cancels
            // any pending debounce on entry, and without this the debounce
            // task would cancel itself here, racing the in-flight request's
            // own cancellation against its response.
            self.searchDebounceTask = nil
            await self.reload()
        }
    }

    public func setStatusFilter(_ status: MemoryStatusKind?) {
        guard statusFilter != status else { return }
        statusFilter = status
        searchDebounceTask?.cancel()
        Task { [weak self] in await self?.reload() }
    }

    public func setTypeFilter(_ type: MemoryBeliefTypeKind?) {
        guard typeFilter != type else { return }
        typeFilter = type
        searchDebounceTask?.cancel()
        Task { [weak self] in await self?.reload() }
    }

    func applyGroupFilters(flagged: Bool, type: MemoryBeliefTypeKind?) {
        guard flaggedOnly != flagged || typeFilter != type else { return }
        flaggedOnly = flagged
        typeFilter = type
        searchDebounceTask?.cancel()
        Task { [weak self] in await self?.reload() }
    }

    public func setFlaggedOnly(_ flagged: Bool) {
        guard flaggedOnly != flagged else { return }
        flaggedOnly = flagged
        searchDebounceTask?.cancel()
        Task { [weak self] in await self?.reload() }
    }

    /// Deletes one belief. On success the row leaves the list; a not-found
    /// answer means it is already gone and is treated the same way. Returns
    /// whether the belief is gone.
    @discardableResult
    public func delete(_ memory: MemoryView) async -> Bool {
        guard pendingActionID == nil else { return false }
        pendingActionID = memory.id
        defer { pendingActionID = nil }
        errorMessage = nil
        guard let api = await makeAPIClient() else {
            errorMessage = displayMessage(for: VeetbotAPIClientError.memoryChangesUnavailable)
            return false
        }
        do {
            try await api.deleteMemory(memory.id, ceiling: memoryBrowsingCeiling)
            items.removeAll { $0.id == memory.id }
            toasts.show(Self.deletedConfirmation)
            await refreshCounts(api)
            return true
        } catch VeetbotAPIClientError.memoryChangesUnavailable {
            changesUnavailable = true
            errorMessage = displayMessage(for: VeetbotAPIClientError.memoryChangesUnavailable)
            return false
        } catch HTTPTransportError.api(let apiError) where apiError.statusCode == 404 {
            items.removeAll { $0.id == memory.id }
            return true
        } catch {
            errorMessage = displayMessage(for: error)
            return false
        }
    }

    static let deletedConfirmation = "Deleted. The same statement will not form again."

    /// What each review outcome did, said once the server confirms it. None
    /// can be undone over HTTP, so none offers Undo.
    static func confirmation(for outcome: MemoryReviewOutcome) -> String {
        switch outcome {
        case .dismiss: "Marked as reviewed."
        case .untrue: "Marked not true. Veetbot will not form it again from the same evidence."
        case .notHere: "Marked not relevant here."
        }
    }

    static let correctedConfirmation = "Updated. The earlier wording is kept as history."
    static let refusedWordingMessage =
        "That wording can't be saved. It may match the current one, something you removed, or look like a password."

    /// Supersedes a belief with the owner's wording (ADR-0214). The server
    /// answers with the replacement, which takes the original's row; the
    /// original stays on the server as history. Returns the replacement, or
    /// nil when nothing changed.
    @discardableResult
    public func correct(_ memory: MemoryView, wording: String) async -> MemoryView? {
        let text = wording.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        guard pendingActionID == nil, !text.isEmpty else { return nil }
        pendingActionID = memory.id
        defer { pendingActionID = nil }
        errorMessage = nil
        guard let api = await makeAPIClient() else {
            errorMessage = displayMessage(for: VeetbotAPIClientError.memoryChangesUnavailable)
            return nil
        }
        do {
            let replacement = try await api.correctMemory(
                memory.id,
                wording: text,
                expectedUpdatedAt: memory.updatedAt,
                ceiling: memoryBrowsingCeiling
            )
            if let index = items.firstIndex(where: { $0.id == memory.id }) {
                items[index] = replacement
            }
            toasts.show(Self.correctedConfirmation)
            await refreshCounts(api)
            return replacement
        } catch VeetbotAPIClientError.memoryChangesUnavailable {
            changesUnavailable = true
            errorMessage = displayMessage(for: VeetbotAPIClientError.memoryChangesUnavailable)
            return nil
        } catch HTTPTransportError.api(let apiError) where apiError.statusCode == 400 {
            errorMessage = Self.refusedWordingMessage
            return nil
        } catch HTTPTransportError.api(let apiError) where apiError.statusCode == 409 {
            errorMessage = "This memory changed on the server. The list has been refreshed."
            await reload()
            return nil
        } catch {
            errorMessage = displayMessage(for: error)
            return nil
        }
    }

    /// The sources the belief cites, or nil when the server cannot say.
    public func evidence(for memory: MemoryView) async -> [MemoryEvidenceItem]? {
        guard let api = await makeAPIClient() else { return nil }
        return try? await api.memoryEvidence(memory.id, ceiling: memoryBrowsingCeiling)
    }

    /// The number a sidebar group shows, when the server reported one.
    public func count(for group: MemoryBrowseGroup) -> Int? {
        guard let counts else { return nil }
        switch group {
        case .review: return counts.flagged
        case .all: return counts.live
        case .type(let type): return counts.byType[type.rawValue] ?? 0
        }
    }

    /// Fetches the sidebar's numbers. Counts are a label, never a blocker:
    /// any failure, including a server without the route, leaves them unknown.
    public func loadCounts() async {
        guard let api = await makeAPIClient() else { return }
        counts = try? await api.memoryCounts(ceiling: memoryBrowsingCeiling)
    }

    /// After a write, numbers already on screen are fetched again; a surface
    /// that never showed them costs no request.
    private func refreshCounts(_ api: VeetbotAPIClient) async {
        guard counts != nil else { return }
        counts = try? await api.memoryCounts(ceiling: memoryBrowsingCeiling)
    }

    /// Applies one review outcome. The row is replaced by the server's view of
    /// the belief; under the review-queue filter a dismissed belief leaves the
    /// list, and a retired one leaves it under the live default. Returns the
    /// reviewed belief, or nil when the write did not happen.
    @discardableResult
    public func review(_ memory: MemoryView, outcome: MemoryReviewOutcome) async -> MemoryView? {
        guard pendingActionID == nil else { return nil }
        pendingActionID = memory.id
        defer { pendingActionID = nil }
        errorMessage = nil
        guard let api = await makeAPIClient() else {
            errorMessage = displayMessage(for: VeetbotAPIClientError.memoryChangesUnavailable)
            return nil
        }
        do {
            let reviewed = try await api.reviewMemory(
                memory.id, outcome: outcome, ceiling: memoryBrowsingCeiling
            )
            let stillListed =
                (!flaggedOnly || reviewed.flaggedForReview)
                && (statusFilter.map { $0.rawValue == reviewed.status }
                    ?? (reviewed.status == "active" || reviewed.status == "provisional"))
            if let index = items.firstIndex(where: { $0.id == memory.id }) {
                if stillListed { items[index] = reviewed } else { items.remove(at: index) }
            }
            toasts.show(Self.confirmation(for: outcome))
            await refreshCounts(api)
            return reviewed
        } catch VeetbotAPIClientError.memoryChangesUnavailable {
            changesUnavailable = true
            errorMessage = displayMessage(for: VeetbotAPIClientError.memoryChangesUnavailable)
            return nil
        } catch HTTPTransportError.api(let apiError) where apiError.statusCode == 409 {
            // The belief changed under us; the reload shows what the server holds now.
            errorMessage = "This memory changed on the server. The list has been refreshed."
            await reload()
            return nil
        } catch {
            errorMessage = displayMessage(for: error)
            return nil
        }
    }

    /// Resets pagination and fetches page one under the current filters.
    /// Only the most recently started reload is allowed to publish its
    /// result: every await below re-checks `reloadRequestID` so a slow
    /// response for an abandoned query cannot overwrite a newer one.
    public func reload() async {
        searchDebounceTask?.cancel()
        let requestID = UUID()
        reloadRequestID = requestID
        seenCursors = []
        nextCursor = nil
        isLoading = true
        errorMessage = nil
        defer {
            if reloadRequestID == requestID { isLoading = false }
        }

        guard let api = await makeAPIClient() else {
            guard reloadRequestID == requestID else { return }
            items = []
            unavailable = true
            lastFailedOperation = .reload
            return
        }

        do {
            let page = try await api.listMemories(
                ceiling: memoryBrowsingCeiling,
                statuses: statusFilter.map { [$0] },
                beliefTypes: typeFilter.map { [$0] },
                text: normalizedSearchText,
                flagged: flaggedOnly ? true : nil
            )
            guard reloadRequestID == requestID else { return }
            unavailable = false
            lastFailedOperation = nil
            items = page.items
            nextCursor = consumeNextCursor(page.nextCursor)
        } catch VeetbotAPIClientError.memoryBrowsingUnavailable {
            guard reloadRequestID == requestID else { return }
            items = []
            unavailable = true
            lastFailedOperation = .reload
        } catch {
            guard reloadRequestID == requestID else { return }
            unavailable = false
            // A reload is always the result of the initial load or a filter
            // or search-text change (reload() is never called to merely
            // refresh the current page): on failure, the previous selection's
            // rows must not sit under the new one, so they're cleared rather
            // than left stale. The full-screen error state this produces
            // carries its own retry.
            items = []
            errorMessage = displayMessage(for: error)
            lastFailedOperation = .reload
        }
    }

    /// Re-runs whichever fetch last failed. Both the full-screen error
    /// state's retry and the inline loadMore footer's retry call this rather
    /// than hard-coding which underlying method to call.
    public func retry() async {
        switch lastFailedOperation {
        case .loadMore:
            await loadMore()
        case .reload, nil:
            await reload()
        }
    }

    /// Fetches the next page via the stored cursor, guarding against a
    /// server that echoes back the cursor it was given and against a stale
    /// response for a reload that has since been superseded.
    ///
    /// A failure here — including a mid-scroll `memoryBrowsingUnavailable`
    /// — never clears `items`: the caller already has a populated list, and
    /// a page-2+ failure is exactly the case where throwing that list away
    /// would be the worse outcome. `errorMessage` carries the failure so the
    /// browser can show it inline with a retry, and is cleared optimistically
    /// on every new attempt so it cannot outlive the failure that set it.
    public func loadMore() async {
        guard !isLoading, !isLoadingMore else { return }
        guard let cursor = nextCursor, let requestID = reloadRequestID else { return }

        isLoadingMore = true
        errorMessage = nil
        defer { isLoadingMore = false }

        guard let api = await makeAPIClient() else { return }

        do {
            let page = try await api.listMemories(
                ceiling: memoryBrowsingCeiling,
                cursor: cursor,
                statuses: statusFilter.map { [$0] },
                beliefTypes: typeFilter.map { [$0] },
                text: normalizedSearchText,
                flagged: flaggedOnly ? true : nil
            )
            guard reloadRequestID == requestID else { return }
            unavailable = false
            lastFailedOperation = nil
            let existingIDs = Set(items.map(\.id))
            items.append(contentsOf: page.items.filter { !existingIDs.contains($0.id) })
            nextCursor = consumeNextCursor(page.nextCursor)
        } catch VeetbotAPIClientError.memoryBrowsingUnavailable {
            guard reloadRequestID == requestID else { return }
            unavailable = true
            errorMessage = displayMessage(for: VeetbotAPIClientError.memoryBrowsingUnavailable)
            lastFailedOperation = .loadMore
        } catch {
            guard reloadRequestID == requestID else { return }
            errorMessage = displayMessage(for: error)
            lastFailedOperation = .loadMore
        }
    }

    /// Validates a returned cursor against the ones already seen this
    /// session; a repeat means the server echoed a cursor back, so pagination
    /// stops silently rather than spinning the client in a fetch loop.
    private func consumeNextCursor(_ cursor: String?) -> String? {
        do {
            return try nextPageCursor(cursor, seen: &seenCursors)
        } catch {
            return nil
        }
    }

    private var normalizedSearchText: String? {
        let trimmed = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }

    private func displayMessage(for error: Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}

/// A group of the Mac sidebar (MagicPath coolly-breeze-5701): the review
/// queue, everything, or one belief type. Each maps onto the list route's own
/// `flagged` and `belief_type` filters.
public enum MemoryBrowseGroup: Hashable, Identifiable, Sendable {
    case review
    case all
    case type(MemoryBeliefTypeKind)

    public var id: String {
        switch self {
        case .review: return "review"
        case .all: return "all"
        case .type(let type): return "type.\(type.rawValue)"
        }
    }

    public var title: String {
        switch self {
        case .review: return "Needs review"
        case .all: return "All memories"
        case .type(.preference): return "Preferences"
        case .type(.fact): return "Facts"
        case .type(.relationship): return "Relationships"
        case .type(.userModelAttr): return "About you"
        case .type(.procedurePointer): return "Procedures"
        }
    }

    public static let browseTypes: [MemoryBrowseGroup] = [
        .type(.preference), .type(.fact), .type(.relationship), .type(.userModelAttr), .type(.procedurePointer),
    ]
}

extension MemoryViewModel {
    /// The sidebar group the current filters describe.
    public var group: MemoryBrowseGroup {
        if flaggedOnly { return .review }
        if let typeFilter { return .type(typeFilter) }
        return .all
    }

    /// Sets the review flag and the type filter together, so a group change
    /// costs one request.
    public func selectGroup(_ group: MemoryBrowseGroup) {
        switch group {
        case .review: applyGroupFilters(flagged: true, type: nil)
        case .all: applyGroupFilters(flagged: false, type: nil)
        case .type(let type): applyGroupFilters(flagged: false, type: type)
        }
    }
}
