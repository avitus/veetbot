import Combine
import Foundation

/// Browses the calling principal's beliefs over the read-only memory API
/// (memory-read-api-and-browser.md). The view model holds no memory state of
/// its own: it re-fetches from the server on every reload and discards its
/// page cache whenever a filter, the search text, or the connection changes.
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

    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
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
        }
    ) {
        self.makeAPIClient = makeAPIClient
    }

    deinit {
        searchDebounceTask?.cancel()
    }

    public static func makeDefaultAPIClient() async -> VeetbotAPIClient? {
        #if DEBUG && os(iOS)
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
                text: normalizedSearchText
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
                text: normalizedSearchText
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
