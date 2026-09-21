import Combine
import Foundation

/// Presentation state for the Subscriptions census. Every decision stays
/// authoritative on the server: a row settles from its durable operation and is
/// restored, with what remains possible, whenever that outcome is not a
/// confirmed success. The client holds sender identity and an evidence digest;
/// it never holds, builds or shows the address an unsubscribe is sent to.
@MainActor
public final class EmailSubscriptionsViewModel: ObservableObject {
    @Published public private(set) var items: [EmailSubscriptionView] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var hasMore = false
    @Published public private(set) var unavailable = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var rowErrors: [String: String] = [:]
    /// Optimistic labels for work this client admitted, keyed by subscription.
    @Published private var working: [String: String] = [:]
    @Published public private(set) var selection: [String] = []
    @Published public private(set) var isSelecting = false
    @Published public private(set) var confirmation: EmailUnsubscribeConfirmation?
    @Published public private(set) var accountFilter: String?
    @Published public private(set) var stateFilter: String?
    @Published public private(set) var selectedID: String?
    /// Off until the owner asks for it, on every confirmation.
    @Published public var archiveExisting = false

    private let makeAPIClient: () -> VeetbotAPIClient?
    private let statusBackoff: @Sendable (UInt64) async throws -> Void
    private var generation = UUID()
    private var listRequest = UUID()
    private var nextCursor: String?
    private var seenCursors: Set<String> = []

    public init(
        makeAPIClient: @escaping () -> VeetbotAPIClient?,
        statusBackoff: @escaping @Sendable (UInt64) async throws -> Void = {
            try await Task.sleep(nanoseconds: $0)
        }
    ) {
        self.makeAPIClient = makeAPIClient
        self.statusBackoff = statusBackoff
    }

    /// Reloading is owned by the surface, so a filter change is one request.
    public var filterKey: String { "\(accountFilter ?? "")|\(stateFilter ?? "")" }

    public func isSelected(_ id: String) -> Bool { selection.contains(id) }

    /// A protected sender is skipped only by Select all; an explicit tap still reaches it.
    public func canSelect(_ row: EmailSubscriptionView) -> Bool { row.canUnsubscribe }

    /// One line beside the row: an unsettled outcome, work in flight, or a pending check.
    public func status(for row: EmailSubscriptionView) -> String? {
        if let error = rowErrors[row.id] { return error }
        if let label = working[row.id] { return label }
        if row.state == "pending" { return row.stateDescription }
        return row.isVerifying ? "Checking…" : nil
    }

    /// Exactly what this row still allows. Nothing is ever substituted for a missing action.
    public func actions(for row: EmailSubscriptionView) -> [EmailSubscriptionAction] {
        guard working[row.id] == nil, row.state != "pending" else { return [] }
        switch row.state {
        case "kept": return [.unkeep]
        case "reported_spam": return [.notSpam]
        case "active", "failed", "still_sending":
            var offered: [EmailSubscriptionAction] = []
            if row.canUnsubscribe { offered.append(row.state == "active" ? .unsubscribe : .tryAgain) }
            offered.append(contentsOf: [.reportSpam, .keep])
            return offered
        default: return []
        }
    }

    public func setAccountFilter(_ id: String?) {
        guard accountFilter != id else { return }
        accountFilter = id
        resetPaging()
    }

    public func setStateFilter(_ state: String?) {
        guard stateFilter != state else { return }
        stateFilter = state
        resetPaging()
    }

    public func open(_ id: String?) { selectedID = id }

    public func setSelecting(_ value: Bool) {
        isSelecting = value
        if !value { selection = [] }
    }

    /// The batch bound is the client's own refusal; it never sends a larger consent.
    public func toggleSelection(_ row: EmailSubscriptionView) {
        if let index = selection.firstIndex(of: row.id) {
            selection.remove(at: index)
        } else if canSelect(row), selection.count < EmailSubscriptions.batchLimit {
            selection.append(row.id)
        }
    }

    /// Keeps the owner's valued senders out of a bulk gesture and stops at the bound.
    public func selectAll() {
        selection = items
            .filter { !$0.protected && canSelect($0) }
            .prefix(EmailSubscriptions.batchLimit)
            .map(\.id)
    }

    public func reload() async {
        resetPaging()
        await load()
    }

    public func loadMore() async {
        guard !isLoading, let cursor = nextCursor, seenCursors.insert(cursor).inserted else { return }
        await load(cursor: cursor)
    }

    /// The confirmed multi-select gesture: one consent for exactly these senders.
    public func beginUnsubscribe() {
        let targets = selection.compactMap { id in items.first { $0.id == id } }.compactMap(target)
        guard !targets.isEmpty else { return }
        present(EmailUnsubscribeConfirmation(source: .list, targets: targets))
    }

    public func beginUnsubscribe(_ row: EmailSubscriptionView) {
        guard let target = target(row) else { return }
        present(EmailUnsubscribeConfirmation(source: .list, targets: [target]))
    }

    /// The same confirmation, opened from a bulk conversation with its one sender.
    public func beginUnsubscribe(thread: EmailThreadView) {
        guard let block = thread.subscription, block.canUnsubscribe,
              let digest = block.evidenceDigest else { return }
        let sender = thread.senders.first ?? block.destination
        present(EmailUnsubscribeConfirmation(source: .thread, targets: [
            EmailUnsubscribeTarget(
                id: block.id, displayName: sender, address: sender, accountID: thread.accountID,
                mechanism: block.mechanism, destination: block.destination,
                evidenceDigest: digest, revision: block.revision)
        ]))
    }

    public func cancelConfirmation() { confirmation = nil }

    /// Confirming is the consent. One command carries every consented sender.
    public func confirmUnsubscribe() async {
        guard let confirmation, let api = makeAPIClient() else { return }
        let targets = confirmation.targets
        let ids = targets.map(\.id)
        let archive = archiveExisting
        self.confirmation = nil
        selection = []
        isSelecting = false
        begin(ids, label: "Unsubscribing…")
        let connection = generation
        do {
            let operation = try await api.unsubscribeEmailSubscriptions(
                targets, archiveExisting: archive, idempotencyKey: UUID().uuidString)
            guard accepts(connection) else { return }
            await settle(operation, targets: ids, connection: connection)
        } catch {
            guard accepts(connection) else { return }
            restore(ids, reason: error.localizedDescription)
        }
    }

    /// A durable local decision. It admits no task and changes no mailbox.
    public func setKept(_ row: EmailSubscriptionView, kept: Bool) async {
        guard let api = makeAPIClient(), working[row.id] == nil else { return }
        begin([row.id], label: kept ? "Keeping…" : "Restoring…")
        let connection = generation
        do {
            let updated = try await api.keepEmailSubscription(row, kept: kept)
            guard accepts(connection) else { return }
            working[row.id] = nil
            replace(updated)
        } catch {
            guard accepts(connection) else { return }
            restore([row.id], reason: error.localizedDescription)
        }
    }

    /// Report spam, and its first-class reversal, settle from the same durable operation.
    public func report(_ row: EmailSubscriptionView, spam: Bool) async {
        guard let api = makeAPIClient(), working[row.id] == nil else { return }
        begin([row.id], label: spam ? "Reporting spam…" : "Restoring from spam…")
        let connection = generation
        do {
            let operation = try await api.reportEmailSubscription(
                row, spam: spam, idempotencyKey: UUID().uuidString)
            guard accepts(connection) else { return }
            await settle(operation, targets: [row.id], connection: connection)
        } catch {
            guard accepts(connection) else { return }
            restore([row.id], reason: error.localizedDescription)
        }
    }

    /// Removes every census row and decision of the connection that is going away.
    public func resetConnection() {
        generation = UUID()
        listRequest = UUID()
        resetPaging()
        items = []
        rowErrors = [:]
        working = [:]
        confirmation = nil
        selectedID = nil
        accountFilter = nil
        stateFilter = nil
        isSelecting = false
        archiveExisting = false
        unavailable = false
        errorMessage = nil
        isLoading = false
    }

    // MARK: - reading

    private func resetPaging() {
        nextCursor = nil
        seenCursors = []
        hasMore = false
        selection = []
        listRequest = UUID()
    }

    private func load(cursor: String? = nil) async {
        guard let api = makeAPIClient() else { return }
        let connection = generation
        let request = listRequest
        isLoading = true
        defer { if accepts(connection), listRequest == request { isLoading = false } }
        do {
            let page = try await api.emailSubscriptions(
                accountID: accountFilter, state: stateFilter, cursor: cursor)
            guard accepts(connection), listRequest == request else { return }
            items = cursor == nil ? page.items : items + page.items
            nextCursor = page.nextCursor
            hasMore = page.nextCursor != nil
            unavailable = false
            errorMessage = nil
        } catch {
            guard accepts(connection), listRequest == request, !(error is CancellationError) else { return }
            if case HTTPTransportError.api(let failure) = error, failure.statusCode == 404 {
                unavailable = true
                items = []
                hasMore = false
                return
            }
            errorMessage = error.localizedDescription
        }
    }

    // MARK: - durable outcomes

    private func target(_ row: EmailSubscriptionView) -> EmailUnsubscribeTarget? {
        guard row.canUnsubscribe, let digest = row.evidenceDigest else { return nil }
        return EmailUnsubscribeTarget(
            id: row.id, displayName: row.displayName, address: row.address, accountID: row.accountID,
            mechanism: row.mechanism, destination: row.destination, evidenceDigest: digest,
            revision: row.revision)
    }

    private func present(_ value: EmailUnsubscribeConfirmation) {
        archiveExisting = false
        confirmation = value
    }

    private func begin(_ ids: [String], label: String) {
        for id in ids {
            working[id] = label
            rowErrors[id] = nil
        }
    }

    private func accepts(_ connection: UUID) -> Bool { generation == connection && !Task.isCancelled }

    /// Polls the durable operation with the bounded backoff an archive row uses,
    /// then reads the rows back: only the server says what actually happened.
    private func settle(_ operation: EmailOperationView, targets: [String], connection: UUID) async {
        var consecutiveFailures = 0
        var attempts = 0
        var status = operation.status
        while !status.isTerminal {
            guard attempts < Self.maxStatusPolls else {
                restore(targets, reason: Self.unreadableStatus)
                return
            }
            attempts += 1
            do {
                try await statusBackoff(UInt64(2 << consecutiveFailures) * 1_000_000_000)
                guard accepts(connection) else { return }
                guard let api = makeAPIClient() else {
                    restore(targets, reason: Self.unreadableStatus)
                    return
                }
                status = try await api.emailOperation(operation.operationID).status
                guard accepts(connection) else { return }
                consecutiveFailures = 0
            } catch is CancellationError {
                return
            } catch {
                guard accepts(connection) else { return }
                consecutiveFailures += 1
                guard Self.isTransientReadFailure(error), consecutiveFailures < 3 else {
                    restore(targets, reason: Self.unreadableStatus)
                    return
                }
            }
        }
        await load()
        guard accepts(connection) else { return }
        for id in targets { working[id] = nil }
        for id in targets {
            guard let row = items.first(where: { $0.id == id }) else { continue }
            if status != .completed {
                rowErrors[id] = Self.unsettled(row)
            } else if ["failed", "uncertain"].contains(row.operation?.status ?? "") || row.state == "failed" {
                rowErrors[id] = Self.unsettled(row)
            }
        }
    }

    /// A restored row keeps whatever it still allows: try again, Report spam, or Keep.
    private func restore(_ ids: [String], reason: String) {
        for id in ids {
            working[id] = nil
            rowErrors[id] = reason
        }
    }

    private func replace(_ row: EmailSubscriptionView) {
        rowErrors[row.id] = nil
        guard let index = items.firstIndex(where: { $0.id == row.id }) else { return }
        items[index] = row
    }

    /// Matches the bound the archive pollers in `EmailViewModel` use.
    private static let maxStatusPolls = 60

    private static let unreadableStatus =
        "Veetbot could not confirm this request's outcome. Try again, report the sender as spam, or keep it."

    /// Names the recorded outcome without claiming the sender accepted anything.
    private static func unsettled(_ row: EmailSubscriptionView) -> String {
        row.operation?.status == "uncertain"
            ? "Outcome not confirmed. Try again, report the sender as spam, or keep it."
            : "This request did not complete. Try again, report the sender as spam, or keep it."
    }

    /// Bounded retries for temporary HTTP and network failures, never authorization errors.
    private static func isTransientReadFailure(_ error: Error) -> Bool {
        if case HTTPTransportError.api(let failure) = error, let status = failure.statusCode {
            return status == 429 || (500...599).contains(status)
        }
        if case HTTPTransportError.connection(let failure) = error {
            switch failure.code {
            case .timedOut, .cannotFindHost, .cannotConnectToHost, .networkConnectionLost,
                 .dnsLookupFailed, .notConnectedToInternet: return true
            default: return false
            }
        }
        return false
    }
}
