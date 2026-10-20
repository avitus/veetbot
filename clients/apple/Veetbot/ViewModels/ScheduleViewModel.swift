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
    @Published private var lifecycleErrors: [UUID: String] = [:]
    @Published private var lifecycleChangingIDs: Set<UUID> = []
    @Published private var recentRunsByID: [UUID: [ScheduleRecentRun]] = [:]
    @Published private var recentRunErrors: [UUID: String] = [:]

    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private let toasts: ToastCenter
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
        },
        toasts: ToastCenter? = nil
    ) {
        self.makeAPIClient = makeAPIClient
        self.toasts = toasts ?? .shared
    }

    // MARK: Lifecycle (ADR-0215 part A)

    private enum Lifecycle { case pause, resume, stop }

    /// Stops future runs until resumed; the toast's Undo resumes.
    public func pause(_ scheduleID: UUID) async {
        guard await change(scheduleID, .pause) else { return }
        toasts.show("Paused. Nothing runs until you resume it.", restored: "Resumed.") { [weak self] in
            await self?.change(scheduleID, .resume) ?? false
        }
    }

    public func resume(_ scheduleID: UUID) async {
        guard await change(scheduleID, .resume) else { return }
        toasts.show("Resumed.")
    }

    /// Cancels future runs; a run in progress finishes, and the record stays
    /// in Recent History for 30 days (ADR-0080, ADR-0089). Not undoable.
    public func stop(_ scheduleID: UUID) async {
        guard await change(scheduleID, .stop) else { return }
        toasts.show("Stopped. It stays in Recent History for 30 days.")
    }

    public func lifecycleError(for scheduleID: UUID) -> String? { lifecycleErrors[scheduleID] }

    public func isChangingLifecycle(_ scheduleID: UUID) -> Bool { lifecycleChangingIDs.contains(scheduleID) }

    /// Sends one lifecycle change at the revision on screen. A revision that
    /// moved on is read again and retried once, as the website binding does.
    @discardableResult
    private func change(_ scheduleID: UUID, _ action: Lifecycle) async -> Bool {
        guard lifecycleChangingIDs.insert(scheduleID).inserted else { return false }
        defer { lifecycleChangingIDs.remove(scheduleID) }
        lifecycleErrors[scheduleID] = nil
        guard let api = await makeAPIClient() else {
            lifecycleErrors[scheduleID] = "Connect to a Veetbot server to change this schedule."
            return false
        }
        var retried = false
        while true {
            do {
                let revision: Int
                if let current = detailRecords[scheduleID] {
                    revision = current.schedule.currentRevision
                } else {
                    revision = try await api.getSchedule(scheduleID).schedule.currentRevision
                }
                let record: ScheduleRecordView
                switch action {
                case .pause: record = try await api.pauseSchedule(scheduleID, expectedRevision: revision)
                case .resume: record = try await api.resumeSchedule(scheduleID, expectedRevision: revision)
                case .stop: record = try await api.cancelSchedule(scheduleID, expectedRevision: revision)
                }
                detailRecords[scheduleID] = record
                updateListRow(from: record)
                return true
            } catch HTTPTransportError.api(let apiError)
                where !retried && apiError.details.reason == "schedule.revision_conflict"
            {
                retried = true
                if let fresh = try? await api.getSchedule(scheduleID) { detailRecords[scheduleID] = fresh }
            } catch {
                lifecycleErrors[scheduleID] = displayMessage(for: error)
                return false
            }
        }
    }

    /// The list row follows the record so its state reads right without a reload.
    private func updateListRow(from record: ScheduleRecordView) {
        guard let index = items.firstIndex(where: { $0.id == record.schedule.id }) else { return }
        let row = items[index]
        items[index] = ScheduleListItemView(
            id: row.id, state: record.schedule.state, pauseReason: record.schedule.pauseReason,
            currentRevision: record.schedule.currentRevision, nextFireAt: record.schedule.nextFireAt,
            title: row.title, instructionPreview: row.instructionPreview, cadence: row.cadence,
            createdAt: row.createdAt, updatedAt: record.schedule.updatedAt
        )
    }

    // MARK: Delivery (ADR-0215 part D)

    /// "Only notify when something needs you": a completed report stays quiet;
    /// failures, approvals and questions still notify.
    public func setNotifyOnlyWhenNeeded(_ scheduleID: UUID, _ onlyWhenNeeded: Bool) async {
        guard lifecycleChangingIDs.insert(scheduleID).inserted else { return }
        defer { lifecycleChangingIDs.remove(scheduleID) }
        lifecycleErrors[scheduleID] = nil
        guard let api = await makeAPIClient() else {
            lifecycleErrors[scheduleID] = "Connect to a Veetbot server to change this schedule."
            return
        }
        do {
            detailRecords[scheduleID] = try await api.setScheduleNotify(
                scheduleID, notify: onlyWhenNeeded ? "attention_only" : "every_report"
            )
            toasts.show("Saved. Applies from the next run.")
        } catch {
            lifecycleErrors[scheduleID] = displayMessage(for: error)
        }
    }

    // MARK: Recent runs (ADR-0215 part A)

    /// The newest occurrences and, for each that started a run, its status.
    public func loadRecentRuns(_ scheduleID: UUID) async {
        guard let api = await makeAPIClient() else { return }
        recentRunErrors[scheduleID] = nil
        do {
            let page = try await api.listScheduleOccurrences(scheduleID, limit: 10)
            var runs: [ScheduleRecentRun] = []
            for occurrence in page.items {
                var status: RunStatus?
                if let runID = occurrence.runID { status = try? await api.getRun(runID).status }
                runs.append(ScheduleRecentRun(occurrence: occurrence, runStatus: status))
            }
            recentRunsByID[scheduleID] = runs
        } catch {
            recentRunErrors[scheduleID] = displayMessage(for: error)
        }
    }

    public func recentRuns(for scheduleID: UUID) -> [ScheduleRecentRun] { recentRunsByID[scheduleID] ?? [] }

    public func recentRunsError(for scheduleID: UUID) -> String? { recentRunErrors[scheduleID] }

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

/// One line of Recent runs: what happened at a scheduled instant, and the
/// conversation that holds its report when one exists.
public struct ScheduleRecentRun: Identifiable, Equatable, Sendable {
    public let occurrence: ScheduleOccurrenceView
    public let runStatus: RunStatus?
    public var id: UUID { occurrence.id }

    /// A report opens only from a run whose conversation still exists.
    public var reportSessionID: UUID? {
        occurrence.disposition == "MATERIALIZED" ? occurrence.sessionID : nil
    }

    public var title: String {
        switch occurrence.disposition {
        case "MATERIALIZED":
            if occurrence.linksErasedAt != nil { return "Ran; the conversation was deleted" }
            switch runStatus {
            case .completed: return "Completed"
            case .failed: return "Failed"
            case .cancelled: return "Stopped"
            case .waitingForApproval, .waitingForUser: return "Waiting for you"
            case .queued, .running: return "Running"
            default: return "Ran"
            }
        case "SKIPPED_OVERLAP": return "Skipped: the last run was still going"
        case "MISSED": return "Missed: Veetbot was offline past the grace period"
        case "AUTHORIZATION_FAILED": return "Did not run: permission was missing"
        case "CONFIGURATION_FAILED": return "Did not run: the schedule needs attention"
        default: return "Did not run"
        }
    }
}
