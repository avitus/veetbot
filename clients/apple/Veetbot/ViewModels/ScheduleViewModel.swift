import Combine
import Foundation

/// Read-only presentation state for the existing schedule control plane
/// (scheduling.md#native-apple-schedule-browser). Server records remain
/// authoritative; every presentation reloads and every detail opening performs
/// the point read that is allowed to return the complete instruction.
@MainActor
public final class ScheduleViewModel: ObservableObject {
    @Published public private(set) var items: [ScheduleListItemView] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var isLoadingMore = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var unavailable = false
    @Published public private(set) var detailRecords: [UUID: ScheduleRecordView] = [:]
    @Published private var detailErrors: [UUID: String] = [:]
    @Published private var detailLoadingIDs: Set<UUID> = []

    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private var nextCursor: String?
    private var seenCursors: Set<String> = []
    private var reloadRequestID: UUID?
    private var detailRequestIDs: [UUID: UUID] = [:]
    private var lastFailedListOperation: FailedListOperation?

    private enum FailedListOperation {
        case reload
        case loadMore
    }

    public init(
        makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = {
            await ScheduleViewModel.makeDefaultAPIClient()
        }
    ) {
        self.makeAPIClient = makeAPIClient
    }

    public static func makeDefaultAPIClient() async -> VeetbotAPIClient? {
        #if DEBUG && os(iOS)
        if let fixtureClient =
            ConversationNavigationUITestFixture.makeScheduleAPIClientIfRequested()
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

    public func reload() async {
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
            lastFailedListOperation = .reload
            return
        }

        do {
            let page = try await api.listSchedules()
            guard reloadRequestID == requestID else { return }
            unavailable = false
            lastFailedListOperation = nil
            items = page.items
            nextCursor = consumeNextCursor(page.nextCursor)
        } catch VeetbotAPIClientError.scheduleBrowsingUnavailable {
            guard reloadRequestID == requestID else { return }
            items = []
            unavailable = true
            lastFailedListOperation = .reload
        } catch {
            guard reloadRequestID == requestID else { return }
            items = []
            unavailable = false
            errorMessage = displayMessage(for: error)
            lastFailedListOperation = .reload
        }
    }

    public func loadMore() async {
        guard !isLoading, !isLoadingMore else { return }
        guard let cursor = nextCursor, let requestID = reloadRequestID else { return }

        isLoadingMore = true
        errorMessage = nil
        defer { isLoadingMore = false }

        guard let api = await makeAPIClient() else { return }

        do {
            let page = try await api.listSchedules(cursor: cursor)
            guard reloadRequestID == requestID else { return }
            unavailable = false
            lastFailedListOperation = nil
            let existingIDs = Set(items.map(\.id))
            items.append(contentsOf: page.items.filter { !existingIDs.contains($0.id) })
            nextCursor = consumeNextCursor(page.nextCursor)
        } catch VeetbotAPIClientError.scheduleBrowsingUnavailable {
            guard reloadRequestID == requestID else { return }
            unavailable = true
            errorMessage = displayMessage(for: VeetbotAPIClientError.scheduleBrowsingUnavailable)
            lastFailedListOperation = .loadMore
        } catch {
            guard reloadRequestID == requestID else { return }
            errorMessage = displayMessage(for: error)
            lastFailedListOperation = .loadMore
        }
    }

    public func retry() async {
        switch lastFailedListOperation {
        case .loadMore:
            await loadMore()
        case .reload, nil:
            await reload()
        }
    }

    public func loadDetail(_ scheduleID: UUID) async {
        let requestID = UUID()
        detailRequestIDs[scheduleID] = requestID
        detailLoadingIDs.insert(scheduleID)
        detailErrors[scheduleID] = nil
        detailRecords[scheduleID] = nil
        defer {
            if detailRequestIDs[scheduleID] == requestID {
                detailLoadingIDs.remove(scheduleID)
            }
        }

        guard let api = await makeAPIClient() else {
            guard detailRequestIDs[scheduleID] == requestID else { return }
            detailErrors[scheduleID] = "Connect to a Veetbot server to view this schedule."
            return
        }

        do {
            let record = try await api.getSchedule(scheduleID)
            guard detailRequestIDs[scheduleID] == requestID else { return }
            detailRecords[scheduleID] = record
        } catch {
            guard detailRequestIDs[scheduleID] == requestID else { return }
            detailErrors[scheduleID] = displayMessage(for: error)
        }
    }

    public func retryDetail(_ scheduleID: UUID) async {
        await loadDetail(scheduleID)
    }

    public func detailError(for scheduleID: UUID) -> String? {
        detailErrors[scheduleID]
    }

    public func isLoadingDetail(_ scheduleID: UUID) -> Bool {
        detailLoadingIDs.contains(scheduleID)
    }

    private func consumeNextCursor(_ cursor: String?) -> String? {
        do {
            return try nextPageCursor(cursor, seen: &seenCursors)
        } catch {
            return nil
        }
    }

    private func displayMessage(for error: Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}
