import Combine
import Foundation

public enum ScheduleBrowserSection: String, CaseIterable, Identifiable, Sendable {
    case current
    case recentHistory

    public var id: String { rawValue }

    var states: [ScheduleStateKind] {
        switch self {
        case .current: return [.active, .paused]
        case .recentHistory: return [.completed, .cancelled]
        }
    }
}

/// One choice in a schedule's website access picker (ADR-0154).
public struct ScheduleWebsiteAccessOption: Identifiable, Equatable, Sendable {
    public let id: UUID
    public let label: String
}

/// Ready sign-ins in server order, plus the one the schedule already uses
/// whatever its state, so a binding that stopped working stays visible.
public func scheduleWebsiteAccessOptions(
    profiles: [BrowserProfileView],
    boundProfileID: UUID?
) -> [ScheduleWebsiteAccessOption] {
    var options = profiles
        .filter { $0.status == .ready || $0.id == boundProfileID }
        .map { profile in
            let sites = profile.allowedOrigins
                .map { URLComponents(string: $0)?.host ?? $0 }
                .joined(separator: ", ")
            return ScheduleWebsiteAccessOption(
                id: profile.id,
                label: profile.status == .ready
                    ? sites : "\(sites) · \(profile.status.displayName)"
            )
        }
    if let boundProfileID, !profiles.contains(where: { $0.id == boundProfileID }) {
        options.append(ScheduleWebsiteAccessOption(id: boundProfileID, label: "Unavailable sign-in"))
    }
    return options
}

/// The server refuses updates to a terminal schedule, and an unknown state is
/// not assumed to accept one.
public func scheduleAllowsWebsiteAccessChange(_ state: ScheduleStateKind?) -> Bool {
    state == .active || state == .paused
}

/// Presentation state for the existing schedule control plane
/// (scheduling.md#native-apple-schedule-browser). Server records remain
/// authoritative; every presentation reloads and every detail opening performs
/// the point read that is allowed to return the complete instruction. The only
/// change it can make is a schedule's website access (ADR-0154).
@MainActor
public final class ScheduleViewModel: ObservableObject {
    @Published public private(set) var items: [ScheduleListItemView] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var isLoadingMore = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var unavailable = false
    @Published public private(set) var section = ScheduleBrowserSection.current
    @Published public private(set) var detailRecords: [UUID: ScheduleRecordView] = [:]
    /// Nil until the sign-ins load, so a failed load is not shown as "none".
    @Published public private(set) var websiteAccessProfiles: [BrowserProfileView]?
    @Published public private(set) var websiteAccessProfilesError: String?
    @Published private var detailErrors: [UUID: String] = [:]
    @Published private var detailLoadingIDs: Set<UUID> = []
    @Published private var websiteAccessErrors: [UUID: String] = [:]
    @Published private var websiteAccessSavingIDs: Set<UUID> = []

    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private var nextCursor: String?
    private var seenCursors: Set<String> = []
    private var reloadRequestID: UUID?
    private var detailRequestIDs: [UUID: UUID] = [:]
    private var websiteAccessRequestID: UUID?
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

    public func reload(_ section: ScheduleBrowserSection = .current) async {
        let requestID = UUID()
        self.section = section
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
            let page = try await api.listSchedules(states: section.states)
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
            let page = try await api.listSchedules(cursor: cursor, states: section.states)
            guard reloadRequestID == requestID else { return }
            unavailable = false
            lastFailedListOperation = nil
            var existingIDs = Set(items.map(\.id))
            items.append(contentsOf: page.items.filter { existingIDs.insert($0.id).inserted })
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
            await reload(section)
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

    public func loadWebsiteAccessProfiles() async {
        let requestID = UUID()
        websiteAccessRequestID = requestID
        websiteAccessProfilesError = nil
        guard let api = await makeAPIClient() else {
            guard websiteAccessRequestID == requestID else { return }
            websiteAccessProfilesError = "Connect to a Veetbot server to choose website access."
            return
        }
        var profiles: [BrowserProfileView] = []
        var cursor: String?
        var seenCursors: Set<String> = []
        do {
            repeat {
                let page = try await api.listBrowserProfiles(cursor: cursor)
                profiles.append(contentsOf: page.items)
                cursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
            } while cursor != nil
            guard websiteAccessRequestID == requestID else { return }
            websiteAccessProfiles = profiles
        } catch {
            guard websiteAccessRequestID == requestID else { return }
            websiteAccessProfilesError = displayMessage(for: error)
        }
    }

    /// Saves the owner's choice and shows the record the server returns; on
    /// failure the server's current binding stays displayed with the reason.
    public func setWebsiteAccess(_ scheduleID: UUID, browserProfileID: UUID?) async {
        guard websiteAccessSavingIDs.insert(scheduleID).inserted else { return }
        defer { websiteAccessSavingIDs.remove(scheduleID) }
        websiteAccessErrors[scheduleID] = nil
        guard let api = await makeAPIClient() else {
            websiteAccessErrors[scheduleID] = "Connect to a Veetbot server to change website access."
            return
        }
        do {
            detailRecords[scheduleID] = try await api.setScheduleWebsiteAccess(
                scheduleID,
                browserProfileID: browserProfileID
            )
        } catch {
            websiteAccessErrors[scheduleID] = websiteAccessMessage(for: error)
        }
    }

    public func websiteAccessError(for scheduleID: UUID) -> String? {
        websiteAccessErrors[scheduleID]
    }

    public func isSavingWebsiteAccess(_ scheduleID: UUID) -> Bool {
        websiteAccessSavingIDs.contains(scheduleID)
    }

    private func websiteAccessMessage(for error: Error) -> String {
        if case HTTPTransportError.api(let apiError) = error,
            apiError.details.reason == "schedule.browser_profile_unavailable"
        {
            return "That sign-in isn't ready. Sign in again in Website Access, then choose it here."
        }
        return displayMessage(for: error)
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
