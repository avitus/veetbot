import Combine
import Foundation

extension Notification.Name { static let peopleConnectionChanged = Notification.Name("veetbot.people.connectionChanged") }

public enum PeopleAvailability: Equatable, Sendable { case available, disabled, denied, changed, failed }

public enum PeopleCollection: String, CaseIterable, Identifiable, Sendable {
    case review = "Needs you", recent = "Recent", people = "Everyone", pinned = "Pinned"
    case duplicates = "Possible duplicates", all = "All identities"
    public var id: String { rawValue }
    /// Possible duplicates and every identity, merged ones included, are
    /// upkeep rather than the directory (MagicPath keen-earth-6102).
    var isMaintenance: Bool { self == .duplicates || self == .all }
    /// Everyone holds everyone the owner knows or writes to, confirmed or not;
    /// Needs you is the server's review filter (ADR-0121): people known only
    /// by a name or a role, waiting for the owner to say who they are.
    var states: [String] { [.people, .pinned, .recent].contains(self) ? ["active", "provisional"] : [] }
    var review: Bool { self == .review }
    var pinnedOnly: Bool? { self == .pinned ? true : nil }
    /// Recent is the server's newest-interaction order (`sort=recent`).
    var sort: String { self == .recent ? "recent" : "id" }
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
    @Published public private(set) var relationship: PeopleRelationshipFilter = .all
    /// The open possible duplicates, on their own page (ADR-0125).
    @Published public private(set) var suggestions: [PeopleMergeSuggestionView] = []
    /// The sidebar's numbers (ADR-0217); nil while unknown or on an older server.
    @Published public private(set) var counts: PeopleCountsView?
    /// The person whose owed thread is being corrected from its row.
    @Published public private(set) var resolvingThreadOf: UUID?
    private var isConnectionValid = true
    private var connectionObserver: AnyCancellable?
    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private let toasts: ToastCenter
    private var nextCursor: String?
    private var seenCursors: Set<String> = []
    private var failedPage = false
    private var listRequest = UUID()
    private var searchTask: Task<Void, Never>?
    private var suggestionSessionID: UUID?
    private let asOf: Date?

    public init(searchText: String = "", asOf: Date? = nil, notifications: NotificationCenter = .default, makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = { await MemoryViewModel.makeDefaultAPIClient() }, toasts: ToastCenter? = nil) {
        self.toasts = toasts ?? .shared
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
        items = []; suggestions = []; suggestionSessionID = nil; searchText = ""; nextCursor = nil; seenCursors = []; counts = nil
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

    /// Merges a possible duplicate, or keeps the pair apart for good (ADR-0125).
    public func resolveSuggestion(_ suggestion: PeopleMergeSuggestionView, decision: String) async {
        guard isConnectionValid else { return }
        do {
            guard let api = await makeAPIClient() else { throw HTTPTransportError.notConfigured }
            let session: UUID
            if let existing = suggestionSessionID { session = existing } else {
                session = try await api.createSession(metadata: ["purpose": .string("people-management")]).id
                suggestionSessionID = session
            }
            guard isConnectionValid else { return }
            _ = try await api.resolveMergeSuggestion(suggestion.id, body: [
                "session_id": .string(session.uuidString),
                "expected_revision": .number(Double(suggestion.revision)),
                "decision": .string(decision),
            ], key: UUID().uuidString)
            guard isConnectionValid else { return }
            suggestions.removeAll { $0.id == suggestion.id }
            // ADR-0125: an answered pair stays answered.
            toasts.show(decision == "merge" ? "Merged." : "Kept separate. Veetbot will not suggest this pair again.")
            errorMessage = nil
            await reload()
        } catch {
            guard isConnectionValid else { return }
            errorMessage = error.localizedDescription
            await reload()
        }
    }

    public func selectRelationship(_ value: PeopleRelationshipFilter) async {
        guard isConnectionValid else { return }
        guard value != relationship else { return }
        relationship = value
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
            if collection == .duplicates {
                // A server without duplicate handling answers with an error;
                // the page then simply shows no pairs.
                let duplicates = (try? await api.listMergeSuggestions())?.items ?? []
                guard isConnectionValid, listRequest == request else { return }
                suggestions = duplicates
                items = []
                nextCursor = nil
                errorMessage = nil
                availability = .available
                return
            }
            let text = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
            // ADR-0217: Needs you leads with the replies the owner owes soon. The
            // filter stands alone, so it applies only to the unfiltered list; a
            // row without a thread (an older server ignoring the filter) is dropped.
            var owed: [PersonView] = []
            if collection == .review, text.isEmpty, relationship == .all {
                owed = ((try? await api.listPeople(asOf: nil, openThreads: true))?.items ?? []).filter { $0.nextThread != nil }
            }
            let page = try await api.listPeople(text: text, asOf: asOf, states: collection.states, review: collection.review, pinned: collection.pinnedOnly, sort: collection.sort, relationship: relationship.queryValue)
            guard isConnectionValid else { return }
            guard listRequest == request else { return }
            suggestions = []
            var seen: Set<UUID> = []
            items = (owed + page.items).filter { seen.insert($0.id).inserted }
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
            let page = try await api.listPeople(text: searchText.trimmingCharacters(in: .whitespacesAndNewlines), cursor: cursor, asOf: asOf, states: collection.states, review: collection.review, pinned: collection.pinnedOnly, sort: collection.sort, relationship: relationship.queryValue)
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

    // MARK: ADR-0217

    /// Fetches the sidebar's numbers. They label the lists and never block
    /// them: any failure, an older server included, leaves them unknown.
    public func loadCounts() async {
        guard isConnectionValid, let api = await makeAPIClient() else { return }
        let loaded = try? await api.peopleCounts()
        guard isConnectionValid else { return }
        counts = loaded
    }

    /// A collection's name with its number, when the server reported one.
    public func collectionLabel(_ collection: PeopleCollection) -> String {
        count(for: collection).map { "\(collection.rawValue) · \($0)" } ?? collection.rawValue
    }

    /// The number a sidebar row shows, when the server reported one.
    public func count(for collection: PeopleCollection) -> Int? {
        guard let counts else { return nil }
        switch collection {
        case .review: return counts.needsYou
        case .people: return counts.everyone
        case .duplicates: return counts.possibleDuplicates
        case .recent, .pinned, .all: return nil
        }
    }

    /// Why a person is under Needs you: a reply owed soon, or a name with no
    /// confirmed address that waits for the owner to say who it is.
    public func attentionLine(for person: PersonView, now: Date = Date()) -> String? {
        guard collection == .review else { return nil }
        if let thread = person.nextThread, let due = thread.dueAt {
            return "Needs you · \(thread.description) · \(Self.dueText(due, now: now))"
        }
        return "Needs you · Who is this?"
    }

    /// When an owed thread is due, in words.
    static func dueText(_ due: Date, now: Date, calendar: Calendar = .current) -> String {
        let today = calendar.startOfDay(for: now)
        let days = calendar.dateComponents([.day], from: today, to: calendar.startOfDay(for: due)).day ?? 0
        switch days {
        case ..<0: return "overdue"
        case 0: return "due today"
        case 1: return "due tomorrow"
        case 2...6: return "due " + due.formatted(.dateTime.weekday(.wide))
        default: return "due " + due.formatted(.dateTime.month(.abbreviated).day())
        }
    }

    /// When the owner last had contact with someone, in words (Recent rows).
    static func lastContactText(_ at: Date, now: Date = Date(), calendar: Calendar = .current) -> String {
        let days = calendar.dateComponents(
            [.day], from: calendar.startOfDay(for: at), to: calendar.startOfDay(for: now)
        ).day ?? 0
        switch days {
        case ...0: return "Last contact today"
        case 1: return "Last contact yesterday"
        case 2...30: return "Last contact \(days) days ago"
        default: return "Last contact " + at.formatted(.dateTime.month(.abbreviated).day())
        }
    }

    /// Marks the owed thread on a Needs you row completed or cancelled through
    /// the person's existing commitment correction (ADR-0217 part C).
    public func resolveThread(of person: PersonView, state: String) async {
        guard isConnectionValid, resolvingThreadOf == nil, let thread = person.nextThread,
              let beliefID = thread.beliefID else { return }
        resolvingThreadOf = person.id
        defer { resolvingThreadOf = nil }
        let detail = PeopleDetailViewModel(personID: person.id, makeAPIClient: makeAPIClient)
        await detail.reload()
        guard isConnectionValid else { return }
        guard let fact = detail.profile?.facts.first(where: { $0.id == beliefID }) else {
            errorMessage = "Refresh People before changing this thread."
            return
        }
        await detail.correct(fact, operation: "changed", statement: thread.description, sessionID: nil, commitmentState: state)
        guard isConnectionValid else { return }
        if let failure = detail.errorMessage {
            errorMessage = failure
            return
        }
        errorMessage = nil
        toasts.show(state == "completed" ? "Marked done." : "Marked not needed.")
        await reload()
        await refreshCounts()
    }

    private func refreshCounts() async {
        guard counts != nil else { return }
        await loadCounts()
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
