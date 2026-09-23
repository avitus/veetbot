import Combine
import Foundation

extension Notification.Name { static let peopleConnectionChanged = Notification.Name("veetbot.people.connectionChanged") }

public enum PeopleAvailability: Equatable, Sendable { case available, disabled, denied, changed, failed }

public enum PeopleCollection: String, CaseIterable, Identifiable, Sendable {
    case people = "People", pinned = "Pinned", review = "Needs review", all = "All identities"
    public var id: String { rawValue }
    /// People holds everyone the owner knows or writes to, confirmed or not;
    /// Needs review is the server's own filter (ADR-0121).
    var states: [String] { self == .people || self == .pinned ? ["active", "provisional"] : [] }
    var review: Bool { self == .review }
    var pinnedOnly: Bool? { self == .pinned ? true : nil }
}

public enum PeopleRelationshipFilter: String, CaseIterable, Identifiable, Sendable {
    case all = "", family, partner, friend, work, other
    public var id: String { rawValue }
    public var label: String {
        switch self {
        case .all: return "All relationships"
        case .family: return "Family"
        case .partner: return "Partner or spouse"
        case .friend: return "Friends"
        case .work: return "Work"
        case .other: return "Other relationships"
        }
    }
    var queryValue: String? { self == .all ? nil : rawValue }
}

/// In-memory presentation state; revisions and source truth always come from the server.
@MainActor
public final class PeopleViewModel: ObservableObject {
    @Published public private(set) var items: [PersonView] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var isLoadingMore = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var availability: PeopleAvailability = .available
    @Published public private(set) var searchText = ""
    @Published public private(set) var collection: PeopleCollection
    @Published public private(set) var recentFirst = false
    @Published public private(set) var relationship: PeopleRelationshipFilter = .all
    private var isConnectionValid = true
    private var connectionObserver: AnyCancellable?
    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private var nextCursor: String?
    private var seenCursors: Set<String> = []
    private var failedPage = false
    private var listRequest = UUID()
    private var searchTask: Task<Void, Never>?
    private let asOf: Date?

    public init(searchText: String = "", asOf: Date? = nil, notifications: NotificationCenter = .default, makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = { await MemoryViewModel.makeDefaultAPIClient() }) {
        self.searchText = searchText
        self.collection = searchText.isEmpty ? .people : .all
        self.asOf = asOf
        self.makeAPIClient = makeAPIClient
        connectionObserver = notifications.publisher(for: .peopleConnectionChanged).sink { [weak self] _ in
            Task { @MainActor in self?.invalidateConnection() }
        }
    }
    private func invalidateConnection() {
        isConnectionValid = false; listRequest = UUID(); searchTask?.cancel(); searchTask = nil
        items = []; searchText = ""; nextCursor = nil; seenCursors = []
        isLoading = false; isLoadingMore = false; errorMessage = nil
    }
    deinit { searchTask?.cancel() }

    public func selectCollection(_ value: PeopleCollection) async {
        guard isConnectionValid else { return }
        guard value != collection else { return }
        collection = value
        items = []; nextCursor = nil
        await reload()
    }

    public func selectRelationship(_ value: PeopleRelationshipFilter) async {
        guard isConnectionValid else { return }
        guard value != relationship else { return }
        relationship = value
        items = []; nextCursor = nil
        await reload()
    }

    public func setRecentFirst(_ value: Bool) async {
        guard isConnectionValid else { return }
        guard value != recentFirst else { return }
        recentFirst = value
        items = []; nextCursor = nil
        await reload()
    }

    public func setSearchText(_ value: String) {
        guard isConnectionValid else { return }
        guard value != searchText else { return }
        searchText = value
        searchTask?.cancel()
        listRequest = UUID()
        items = []
        nextCursor = nil
        searchTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 300_000_000) } catch { return }
            guard let self, !Task.isCancelled else { return }
            self.searchTask = nil
            await self.reload()
        }
    }

    public func reload() async {
        guard isConnectionValid else { return }
        searchTask?.cancel()
        searchTask = nil
        let request = UUID()
        listRequest = request
        isLoading = true
        isLoadingMore = false
        defer { if listRequest == request { isLoading = false } }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let page = try await api.listPeople(text: searchText.trimmingCharacters(in: .whitespacesAndNewlines), asOf: asOf, states: collection.states, review: collection.review, pinned: collection.pinnedOnly, sort: recentFirst ? "recent" : "id", relationship: relationship.queryValue)
            guard isConnectionValid else { return }
            guard listRequest == request else { return }
            var seen: Set<UUID> = []
            items = page.items.filter { seen.insert($0.id).inserted }
            seenCursors = []
            nextCursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
            errorMessage = nil
            availability = .available
        } catch {
            guard isConnectionValid else { return }
            guard listRequest == request else { return }
            failedPage = false
            report(error)
        }
    }

    public func loadMore() async {
        guard isConnectionValid else { return }
        guard !isLoading, !isLoadingMore, let cursor = nextCursor else { return }
        let request = listRequest
        isLoadingMore = true
        defer { if listRequest == request { isLoadingMore = false } }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let page = try await api.listPeople(text: searchText.trimmingCharacters(in: .whitespacesAndNewlines), cursor: cursor, asOf: asOf, states: collection.states, review: collection.review, pinned: collection.pinnedOnly, sort: recentFirst ? "recent" : "id", relationship: relationship.queryValue)
            guard isConnectionValid else { return }
            guard listRequest == request else { return }
            var seen = Set(items.map(\.id))
            items += page.items.filter { seen.insert($0.id).inserted }
            nextCursor = try? nextPageCursor(page.nextCursor, seen: &seenCursors)
            errorMessage = nil
            availability = .available
        } catch {
            guard isConnectionValid else { return }
            guard listRequest == request else { return }
            failedPage = true
            report(error)
        }
    }

    public func retry() async {
        guard isConnectionValid else { return }
        if !failedPage || items.isEmpty || availability == .changed { await reload() } else { await loadMore() }
    }

    private func report(_ error: Error) {
        errorMessage = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        availability = .failed
        if case HTTPTransportError.authorizationDenied = error { availability = .denied }
        if case HTTPTransportError.api(let apiError) = error {
            switch apiError.statusCode {
            case 404, 405: availability = .disabled; errorMessage = "People is not enabled on this server."
            case 403: availability = .denied
            case 409: availability = .changed; errorMessage = "People changed on another device. Refresh to review the latest version."
            default: break
            }
        }
    }
}

@MainActor
public final class PeopleImportViewModel: ObservableObject {
    @Published public private(set) var sessions: [SessionView] = []
    @Published public private(set) var accounts: [EmailAccountView] = []
    @Published public private(set) var emailError: String?
    @Published public private(set) var job: PeopleImportView?
    @Published public private(set) var existingImports: [PeopleImportView] = []
    @Published public private(set) var hasMoreImports = false
    private var importCursor: String?
    @Published public private(set) var isBusy = false
    @Published public private(set) var errorMessage: String?
    private var isConnectionValid = true
    private var connectionObserver: AnyCancellable?
    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private var auditID: UUID?
    private var scope: [String: JSONValue]?
    private var pending: (body: [String: JSONValue], key: String)?
    private var sessionCursor: String?
    private var loadedSessions = false
    private var pendingCancellation: (id: UUID, revision: Int, key: String)?

    public init(notifications: NotificationCenter = .default, makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = { await MemoryViewModel.makeDefaultAPIClient() }) {
        self.makeAPIClient = makeAPIClient
        connectionObserver = notifications.publisher(for: .peopleConnectionChanged).sink { [weak self] _ in
            Task { @MainActor in self?.invalidateConnection() }
        }
    }
    private func invalidateConnection() {
        isConnectionValid = false; sessions = []; accounts = []; job = nil; existingImports = []
        pending = nil; pendingCancellation = nil; scope = nil; auditID = nil
        importCursor = nil; sessionCursor = nil; hasMoreImports = false; loadedSessions = false
        isBusy = false; errorMessage = nil; emailError = nil
    }
    public var canRetry: Bool { pending != nil || pendingCancellation != nil }
    public var hasMoreSessions: Bool { !loadedSessions || sessionCursor != nil }

    public func loadSessions() async {
        guard isConnectionValid else { return }
        guard !isBusy, hasMoreSessions else { return }
        isBusy = true; defer { isBusy = false }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let page = try await api.listSessions(limit: 100, cursor: sessionCursor)
            guard isConnectionValid else { return }
            var seen = Set(sessions.map(\.id))
            sessions += page.items.filter { seen.insert($0.id).inserted && $0.metadata["purpose"] == nil }
            sessionCursor = page.nextCursor; loadedSessions = true; errorMessage = nil
        } catch { if isConnectionValid { errorMessage = error.localizedDescription } }
    }

    public func loadAccounts() async {
        guard isConnectionValid else { return }
        guard !isBusy else { return }
        isBusy = true; defer { isBusy = false }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let loaded = try await api.emailAccounts().items.filter { $0.status == "ready" }
            guard isConnectionValid else { return }
            accounts = loaded
            emailError = nil
        } catch { if isConnectionValid { emailError = "Email accounts could not be loaded. Conversation imports remain available." } }
    }

    public func preview(sessionIDs: Set<UUID>, accountIDs: Set<String> = [], fetchMailbox: Bool = false, since: Date, until: Date, maxRecords: Int, maxCost: String) async {
        guard isConnectionValid else { return }
        guard !isBusy, !canRetry, !sessionIDs.isEmpty || !accountIDs.isEmpty,
              sessionIDs.count <= 100, accountIDs.count <= 10,
              since < until, (1...10000).contains(maxRecords),
              let amount = Decimal(string: maxCost), amount >= Decimal(string: "0.01")!, amount <= 100 else {
            errorMessage = "Choose conversations or email accounts, a valid date range, a record limit, and a budget from $0.01 to $100."
            return
        }
        isBusy = true
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            if auditID == nil {
                let created = try await api.createSession(metadata: ["purpose": .string("people-management")])
                guard isConnectionValid else { return }
                auditID = created.id
            }
            let format = ISO8601DateFormatter()
            format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            scope = ["session_ids": .array(sessionIDs.sorted { $0.uuidString < $1.uuidString }.map { .string($0.uuidString) }),
                     "account_ids": .array(accountIDs.sorted().map { .string($0) }),
                     "email_source": .string(fetchMailbox && !accountIDs.isEmpty ? "mailbox" : "retained"),
                     "since": .string(format.string(from: since)), "until": .string(format.string(from: until)),
                     "max_records": .number(Double(maxRecords)), "max_cost_usd": .string(maxCost)]
            pending = (body: ["phase": .string("preview"), "session_id": .string(auditID!.uuidString), "scope": .object(scope!)], key: UUID().uuidString)
        } catch { if isConnectionValid { errorMessage = error.localizedDescription } }
        isBusy = false
        if pending != nil { await retry() }
    }

    public func start(maxCost: String? = nil) async {
        guard isConnectionValid else { return }
        guard let job, let scope, let auditID, !isBusy, !canRetry else { return }
        let resuming = ["budget_paused", "failed", "cancelled"].contains(job.state)
        guard job.state == "preview" || (resuming && Decimal(string: job.reservedUSD) == 0) else { return }
        var approvedScope = scope
        if let maxCost {
            guard let amount = Decimal(string: maxCost), amount > 0, amount <= 100 else {
                errorMessage = "Enter a total budget greater than zero and no more than $100."
                return
            }
            approvedScope["max_cost_usd"] = .string(maxCost)
        }
        pending = (body: ["phase": .string(resuming ? "resume" : "apply"), "session_id": .string(auditID.uuidString), "scope": .object(approvedScope),
                          "operation_id": .string(job.id.uuidString), "expected_revision": .number(Double(job.revision))], key: UUID().uuidString)
        self.scope = approvedScope
        await retry()
    }
    public func retry() async {
        guard isConnectionValid else { return }
        if pendingCancellation != nil { await cancel(); return }
        guard let pending, !isBusy else { return }
        isBusy = true; defer { isBusy = false }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let loaded = try await api.submitPeopleImport(body: pending.body, key: pending.key)
            guard isConnectionValid else { return }
            job = loaded
            self.pending = nil; errorMessage = nil
        } catch { if isConnectionValid { errorMessage = error.localizedDescription } }
    }
    public func loadImports(more: Bool = false) async {
        guard isConnectionValid else { return }
        guard !isBusy, !more || hasMoreImports else { return }
        isBusy = true; defer { isBusy = false }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let page = try await api.listPeopleImports(cursor: more ? importCursor : nil)
            guard isConnectionValid else { return }
            if !more { existingImports = [] }
            var seen = Set(existingImports.map(\.id))
            existingImports += page.items.filter { seen.insert($0.id).inserted }
            importCursor = page.nextCursor
            hasMoreImports = page.nextCursor != nil
            errorMessage = nil
        } catch {
            guard isConnectionValid else { return }
            if case HTTPTransportError.api(let failure) = error, failure.statusCode == 409 {
                importCursor = nil; hasMoreImports = false
            }
            errorMessage = error.localizedDescription
        }
    }

    public func restore(_ id: UUID) async {
        guard isConnectionValid else { return }
        guard !isBusy, !canRetry else { return }
        isBusy = true; defer { isBusy = false }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let restored = try await api.peopleImport(id)
            guard isConnectionValid else { return }
            job = restored
            scope = restored.scope
            auditID = restored.auditSessionID
            errorMessage = scope == nil || auditID == nil ? "This server does not supply the saved import scope. Update the server to resume from this device." : nil
        } catch { if isConnectionValid { errorMessage = error.localizedDescription } }
    }

    public func showImportSelection() {
        guard isConnectionValid else { return }
        guard !isBusy, !canRetry else { return }
        job = nil; scope = nil; auditID = nil
        errorMessage = nil
    }

    public func refresh() async {
        guard isConnectionValid else { return }
        guard let job, !isBusy else { return }
        isBusy = true; defer { isBusy = false }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let loaded = try await api.peopleImport(job.id)
            guard isConnectionValid else { return }
            self.job = loaded; errorMessage = nil
        } catch { if isConnectionValid { errorMessage = error.localizedDescription } }
    }
    public func cancel() async {
        guard isConnectionValid else { return }
        guard !isBusy else { return }
        if pendingCancellation == nil {
            guard let job, job.isActive else { return }
            pendingCancellation = (id: job.id, revision: job.revision, key: UUID().uuidString)
        }
        guard let target = pendingCancellation else { return }
        isBusy = true; defer { isBusy = false }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            guard isConnectionValid else { return }
            let loaded = try await api.cancelPeopleImport(target.id, revision: target.revision, key: target.key)
            guard isConnectionValid else { return }
            self.job = loaded
            pendingCancellation = nil; errorMessage = nil
        } catch {
            guard isConnectionValid else { return }
            if case HTTPTransportError.api(let failure) = error, failure.statusCode == 409 {
                pendingCancellation = nil
            }
            errorMessage = error.localizedDescription
        }
    }
}
