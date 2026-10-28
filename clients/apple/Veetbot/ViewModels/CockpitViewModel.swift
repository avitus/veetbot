import Foundation

/// The Cockpit board's presentation state (docs/plan/cockpit.md,
/// `CockpitViewModel`). It loads the snapshot, then streams over the
/// path-based `SSEReader` only while Cockpit is the visible mode in an active
/// app (gate 21). It reconnects with capped exponential backoff and no give-up
/// limit, because an always-on display outlives any run's limits, and refetches
/// the board on every change and on `stream.reset`. Each time Cockpit becomes
/// visible it asks the server to refresh every feed and reloads the board
/// (ADR-0254). Feed data and private settings stay in memory.
@MainActor
public final class CockpitViewModel: ObservableObject {
    public enum Availability: Equatable, Sendable {
        case unknown
        case available
        /// The server has no Cockpit routes, or this connection cannot read them (gate 15).
        case unavailable
    }

    public enum Connection: Equatable, Sendable {
        case idle
        case connecting
        case live
        case reconnecting(attempt: Int)
    }

    @Published public private(set) var availability: Availability = .unknown
    @Published public private(set) var snapshot: CockpitSnapshot?
    @Published public private(set) var connection: Connection = .idle
    /// When the board last loaded, on this device's clock.
    @Published public private(set) var lastUpdated: Date?
    /// Widgets held out of the board after hanging or navigating the realm.
    @Published public private(set) var quarantined: Set<String> = []
    @Published public var selectedSlug: String?
    /// Confirms a removal from this screen and offers its Undo.
    public var toasts: ToastCenter = .shared

    /// Remove from this screen (MagicPath merrily-city-4157): the widget leaves
    /// this device class's board only and stays live elsewhere; Undo saves the
    /// board as it was.
    public func removeFromScreen(_ slug: String, deviceClass: CockpitDeviceClass) async {
        guard let snapshot else { return }
        let before = CockpitBoard.placements(snapshot, deviceClass: deviceClass)
            .map { CockpitPlacement(slug: $0.widget.slug, size: $0.size) }
        guard before.contains(where: { $0.slug == slug }) else { return }
        let title = snapshot.widgets.first { $0.slug == slug }?.title ?? slug
        guard let removed = await saveLayout(deviceClass, placements: before.filter { $0.slug != slug })
        else { return }
        closeDetail()
        // Undo compares against the revision the removal itself produced, so an
        // arrangement saved in the meantime is refused by the server rather than
        // overwritten, and Undo succeeds only when a layout save did.
        toasts.show("Removed \(title) from this screen", restored: "\(title) is back on this screen") { [weak self] in
            guard let self else { return false }
            return await self.saveLayout(deviceClass, placements: before, expectedRevision: removed.revision) != nil
        }
    }

    /// The arrange sheet, opened from the toolbar on the Mac and the options menu on touch.
    @Published public var isArranging = false
    @Published public private(set) var detail: CockpitWidgetDetail?
    @Published public private(set) var actionMessage: String?
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var isSaving = false
    /// Where a delivered `open` intent navigates; the app routes it to its mode.
    public var onOpen: (CockpitDestination) -> Void = { _ in }
    /// Server clock minus this device's, so freshness renders with corrected time.
    public private(set) var offsetMilliseconds: Double = 0

    static let maximumBackoffSeconds: Double = 60

    private let makeClient: () -> CockpitBoardClient?
    private let now: () -> Date
    private let sleep: @Sendable (Double) async throws -> Void
    private var visible = false
    private var streamTask: Task<Void, Never>?
    private var refreshTask: Task<Void, Never>?
    private var quarantinedRevisions: [String: UUID] = [:]
    /// Bumped by every new connection, so an answer to the old one never lands on the new board.
    private var connectionEpoch = 0

    public init(
        makeClient: @escaping () -> CockpitBoardClient?,
        now: @escaping () -> Date = Date.init,
        sleep: @escaping @Sendable (Double) async throws -> Void = { seconds in
            try await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
        }
    ) {
        self.makeClient = makeClient
        self.now = now
        self.sleep = sleep
    }

    /// Cockpit became, or stopped being, the visible mode of an active app.
    public func setVisible(_ visible: Bool) {
        guard visible != self.visible else { return }
        self.visible = visible
        streamTask?.cancel()
        streamTask = nil
        connection = .idle
        guard visible else { return }
        refreshFeeds()
        streamTask = Task { [weak self] in await self?.run(reloading: true) }
    }

    /// A new connection: forget the board and stream again if visible.
    public func resetConnection() {
        streamTask?.cancel()
        streamTask = nil
        connectionEpoch += 1
        snapshot = nil
        availability = .unknown
        connection = .idle
        quarantined = []
        quarantinedRevisions = [:]
        selectedSlug = nil
        detail = nil
        actionMessage = nil
        errorMessage = nil
        isSaving = false
        offsetMilliseconds = 0
        refreshTask?.cancel()
        refreshTask = nil
        if visible {
            refreshFeeds()
            streamTask = Task { [weak self] in await self?.run() }
        }
    }

    /// Ask the server to refresh every feed (ADR-0254). Best effort: a refresh
    /// that fails leaves the board as it is.
    private func refreshFeeds() {
        refreshTask?.cancel()
        refreshTask = makeClient().map { client in Task { try? await client.refreshCockpit() } }
    }

    public func hostReported(_ message: CockpitHostMessage) {
        switch message {
        case .quarantined(let slug, _):
            quarantine(slug)
        case .select(let slug):
            selectedSlug = slug
        case .open(let slug, let kind, let id):
            // Navigation only, and only to what this widget's own feeds delivered.
            guard let snapshot, CockpitBoard.delivers(id, to: slug, in: snapshot),
                  let uuid = UUID(uuidString: id)
            else { return }
            switch kind {
            case .approval: onOpen(.approval(uuid))
            case .emailThread: onOpen(.emailThread(uuid))
            case .schedule: onOpen(.schedule(uuid))
            case .session: onOpen(.session(uuid))
            }
        case .ready, .loaded, .tileError:
            break
        }
    }

    /// The owner's retry: the widget returns to the board as it is.
    public func retry(_ slug: String) {
        quarantinedRevisions[slug] = nil
        quarantined.remove(slug)
    }

    /// The host stopped answering: hold out the widget that changed most recently.
    public func quarantineNewest() {
        guard let snapshot, let slug = CockpitBoard.newest(snapshot, excluding: quarantined) else { return }
        quarantine(slug)
    }

    /// Take a freshly loaded board; a quarantined widget returns once its revision changes.
    public func apply(_ snapshot: CockpitSnapshot) {
        let revisions = Dictionary(snapshot.widgets.map { ($0.slug, $0.revisionID) }, uniquingKeysWith: { a, _ in a })
        for (slug, revision) in quarantinedRevisions where revisions[slug] != revision {
            quarantinedRevisions[slug] = nil
            quarantined.remove(slug)
        }
        if let serverTime = snapshot.serverTime {
            offsetMilliseconds = ((serverTime.timeIntervalSince(now())) * 1000).rounded()
        }
        self.snapshot = snapshot
        lastUpdated = now()
        availability = .available
    }

    /// One content-addressed asset for the scheme handler, fetched with this connection's token.
    public func asset(_ digest: String) async throws -> CockpitAssetData {
        guard let client = makeClient() else { throw URLError(.userAuthenticationRequired) }
        return try await client.cockpitAsset(digest)
    }

    /// The selected widget's revisions, import failures and settings schema.
    public func openDetail(_ slug: String) async {
        selectedSlug = slug
        guard let client = makeClient() else { return }
        let epoch = connectionEpoch
        do {
            let loaded = try await client.cockpitWidget(slug)
            if epoch == connectionEpoch, selectedSlug == slug { detail = loaded }
        } catch {
            if epoch == connectionEpoch { report(error) }
        }
    }

    public func closeDetail() {
        selectedSlug = nil
        detail = nil
    }

    /// Replace a live widget's private settings; they never leave the owner's own clients.
    public func saveSettings(_ slug: String, values: [String: JSONValue]) async {
        await perform { client in
            try await client.putCockpitSettings(slug, values: values)
            // The form already shows the saved values, so only a toast says it took.
            self.actionMessage = nil
            self.toasts.show("Settings saved.")
            await self.openDetail(slug)
            return nil
        }
    }

    /// Arrange this device class's board, compare-and-set on the layout's revision
    /// (the snapshot's unless the caller anchors the save to an earlier one).
    /// Returns the saved layout, or nil when nothing was saved: a refused
    /// compare-and-set, a failed request, or no connection to send it on.
    @discardableResult
    public func saveLayout(
        _ deviceClass: CockpitDeviceClass, placements: [CockpitPlacement], expectedRevision: Int? = nil
    ) async -> CockpitLayout? {
        let expected = expectedRevision ?? snapshot?.layouts[deviceClass]?.revision
        var saved: CockpitLayout?
        await perform { client in
            let layout = try await client.putCockpitLayout(deviceClass, placements: placements, expectedRevision: expected)
            saved = layout
            // The next save compares against this revision until the stream refetches the board.
            if let current = self.snapshot {
                var layouts = current.layouts
                layouts[deviceClass] = layout
                self.snapshot = current.replacingLayouts(layouts)
            }
            return nil
        }
        return saved
    }

    public func revert(_ slug: String, to revision: UUID) async {
        await change(slug) { try await $0.revertCockpitWidget(slug, to: revision) }
    }

    public func delete(_ slug: String) async {
        await change(slug) { try await $0.deleteCockpitWidget(slug) }
    }

    public func restore(_ slug: String) async {
        await change(slug) { try await $0.restoreCockpitWidget(slug) }
    }

    public func clearMessages() {
        actionMessage = nil
        errorMessage = nil
    }

    private func change(_ slug: String, _ request: (CockpitBoardClient) async throws -> CockpitChange) async {
        await perform { client in
            let change = try await request(client)
            await self.openDetail(slug)
            return Self.describe(change)
        }
    }

    /// One owner write at a time; the stream brings the board's own refresh.
    private func perform(_ work: (CockpitBoardClient) async throws -> String?) async {
        guard let client = makeClient() else { return }
        let epoch = connectionEpoch
        isSaving = true
        errorMessage = nil
        defer { if epoch == connectionEpoch { isSaving = false } }
        do {
            let message = try await work(client)
            if epoch == connectionEpoch, let message { actionMessage = message }
        } catch {
            if epoch == connectionEpoch { report(error) }
        }
    }

    private func report(_ error: Error) {
        guard !(error is CancellationError) else { return }
        errorMessage = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }

    static func describe(_ change: CockpitChange) -> String {
        switch (change.operation, change.state) {
        case ("delete", _):
            return "Removing \(change.slug). It leaves the board once its commit lands."
        case (_, "rejected"):
            return "The restored \(change.slug) was rejected; its diagnostics are in the history."
        case (_, "live"):
            return "\(change.slug) is live."
        default:
            return "\(change.slug) is on its way to the board once its commit lands."
        }
    }

    private func quarantine(_ slug: String) {
        guard let widget = snapshot?.widgets.first(where: { $0.slug == slug }) else { return }
        quarantinedRevisions[slug] = widget.revisionID
        quarantined.insert(slug)
    }

    private func run(reloading: Bool = false) async {
        var failures = 0
        var reload = reloading
        while !Task.isCancelled {
            guard let client = makeClient() else {
                // No connection is configured, so there is no board to show.
                availability = .unavailable
                return
            }
            do {
                if reload || snapshot == nil || failures > 0 {
                    apply(try await client.cockpitSnapshot())
                    reload = false
                }
                let events = client.cockpitEvents(after: snapshot?.cursor)
                connection = .live
                for try await frame in events {
                    try Task.checkCancellation()
                    failures = 0
                    // Events name what changed; the board is refetched to apply it.
                    apply(try await client.cockpitSnapshot())
                    if frame.event == "stream.reset" { break }
                }
                // A stream ends at its lifetime, or after a reset; reopen from the board's cursor.
                try Task.checkCancellation()
                failures = 0
            } catch is CancellationError {
                return
            } catch {
                guard !Task.isCancelled else { return }
                if Self.isUnavailable(error) {
                    availability = .unavailable
                    connection = .idle
                    return
                }
                failures += 1
                connection = .reconnecting(attempt: failures)
                do {
                    try await sleep(Self.backoff(afterFailures: failures))
                } catch {
                    return
                }
            }
        }
    }

    static func backoff(afterFailures failures: Int) -> Double {
        min(pow(2, Double(max(failures, 1) - 1)), maximumBackoffSeconds)
    }

    static func isUnavailable(_ error: Error) -> Bool {
        guard let transport = error as? HTTPTransportError else { return false }
        switch transport {
        case .api(let api), .authorizationDenied(let api):
            return api.statusCode == 404 || api.statusCode == 403 || api.code == .notFound
        default:
            return false
        }
    }
}
