import Combine
import Foundation

@MainActor
public final class AppCoordinator: ObservableObject {
    @Published public var mode: ClientMode = .chat
    public let chat: ChatViewModel
    public let email: EmailViewModel
    private var connectionSubscription: AnyCancellable?

    public init(chat: ChatViewModel) {
        self.chat = chat
        email = EmailViewModel(makeAPIClient: { [weak chat] in chat?.currentAPIClient })
        email.authenticationFailure = { [weak chat] error in chat?.reportConnectionError(error) }
        connectionSubscription = chat.$connectionGeneration.dropFirst().sink { [weak self] _ in
            self?.email.resetConnection()
        }
        chat.emailNotificationHandler = { [weak self] threadID, approvalID in
            guard let self else { return }
            self.mode = .email
            await self.email.openThread(threadID, approvalID: approvalID)
        }
    }

    public func discussSelectedThread() async {
        guard let sessionID = await email.discussionSession() else { return }
        await chat.openSharedSession(sessionID)
        mode = .chat
    }
}

/// Presentation state only. Every accepted edit, decision and send remains
/// authoritative in the shared core; mailbox content is never cached on disk.
@MainActor
public final class EmailViewModel: ObservableObject {
    @Published public private(set) var accounts: [EmailAccountView] = []
    @Published public private(set) var items: [EmailThreadView] = []
    @Published public private(set) var thread: EmailThreadView?
    @Published public private(set) var draft: EmailDraftView?
    @Published public private(set) var edits: [UUID: EmailDraftEdit] = [:]
    @Published public private(set) var conflict: EmailDraftView?
    @Published public private(set) var review: ApprovalView?
    @Published public private(set) var reviewDraft: EmailDraftView?
    @Published public private(set) var isLoading = false
    @Published public private(set) var isRefreshing = false
    @Published public private(set) var isLoadingThread = false
    @Published public private(set) var isSaving = false
    @Published public private(set) var isPerformingAction = false
    @Published public private(set) var unavailable = false
    @Published public private(set) var errorMessage: String?
    @Published private var draftActionError: String?
    @Published private var threadReadError: String?
    @Published public private(set) var feedbackMessage: String?
    @Published public private(set) var styleExampleMessage: String?
    @Published public private(set) var feedbackID: UUID?
    @Published public private(set) var hasMore = false
    @Published public private(set) var newImportantCount = 0
    @Published public private(set) var selectedAccountID: String?
    @Published public private(set) var listView = "priority"
    @Published public private(set) var searchText = ""
    @Published public private(set) var selectedThreadID: UUID?
    @Published public private(set) var learning: EmailLearningState?
    @Published public private(set) var revisions: [EmailDraftView] = []
    @Published public private(set) var archiveErrors: [UUID: String] = [:]
    @Published private var archiveSubmitting: Set<UUID> = []
    @Published private var archiveUnsupportedAccounts: Set<String> = []
    @Published private var archiveUnavailableThreads: Set<UUID> = []

    public var authenticationFailure: ((Error) -> Void)?
    private let makeAPIClient: () -> VeetbotAPIClient?
    private let refreshNanoseconds: UInt64
    private var active = false
    private var activation = UUID()
    private var generation = UUID()
    private var listRequest = UUID()
    private var selectionRequest = UUID()
    private var nextCursor: String?
    private var seenCursors: Set<String> = []
    private var refreshTask: Task<Void, Never>?
    private var operationTask: Task<Void, Never>?
    private var autosaveTask: Task<Void, Never>?
    private var searchTask: Task<Void, Never>?
    private var refreshKey: String?
    private var refreshFailure: String?
    private var pendingNewItems: [EmailThreadView]?
    private var saveKeys: [UUID: (EmailDraftEdit, String)] = [:]
    private var sendKeys: [UUID: (Int, String)] = [:]
    private struct ArchiveRequest {
        let thread: EmailThreadView
        let archived: Bool
        let key: String
        var operationID: UUID?
    }
    private var archiveRequests: [UUID: ArchiveRequest] = [:]
    private var archiveTasks: [UUID: Task<Void, Never>] = [:]
    private struct ArchiveMailboxState {
        var inInbox: Bool?
        var operation: EmailArchiveOperation?
        var dismissedRevision: Int?
    }
    private var archiveVersions: [UUID: UUID] = [:]
    private var archiveStates: [UUID: ArchiveMailboxState] = [:]
    private var archiveReadErrors: Set<UUID> = []

    public init(
        makeAPIClient: @escaping () -> VeetbotAPIClient?,
        refreshNanoseconds: UInt64 = 60_000_000_000
    ) {
        self.makeAPIClient = makeAPIClient
        self.refreshNanoseconds = refreshNanoseconds
    }

    deinit {
        refreshTask?.cancel()
        operationTask?.cancel()
        autosaveTask?.cancel()
        searchTask?.cancel()
        archiveTasks.values.forEach { $0.cancel() }
    }

    public var currentEdit: EmailDraftEdit? { draft.flatMap { edits[$0.id] } }
    /// Keeps an unresolved owner-action failure visible even when a later thread read fails or recovers.
    public var draftError: String? { draftActionError ?? threadReadError }
    public var canReview: Bool {
        guard let draft else { return false }
        return draft.canEdit && !draft.stale && conflict == nil && !isSaving && !isPerformingAction
    }

    /// Starts foreground refreshes for a new visit or invalidates reads when Email is hidden.
    public func setActive(_ value: Bool) {
        guard active != value else { return }
        active = value
        activation = UUID()
        isRefreshing = false
        refreshTask?.cancel()
        operationTask?.cancel()
        archiveTasks.values.forEach { $0.cancel() }
        archiveTasks = [:]
        guard value else { return }
        let foreground = activation
        let connection = generation
        refreshTask = Task { [weak self] in
            guard let self else { return }
            await self.reload(activation: foreground)
            while self.acceptsRead(connection: connection, activation: foreground) {
                await self.refresh(activation: foreground)
                do { try await Task.sleep(nanoseconds: self.refreshNanoseconds) }
                catch { return }
            }
        }
    }

    /// Cancels obsolete work and removes all mail, local edits and errors from the old connection.
    public func resetConnection() {
        generation = UUID()
        activation = UUID()
        listRequest = UUID()
        selectionRequest = UUID()
        refreshTask?.cancel()
        operationTask?.cancel()
        autosaveTask?.cancel()
        searchTask?.cancel()
        archiveTasks.values.forEach { $0.cancel() }
        archiveTasks = [:]
        archiveRequests = [:]
        archiveErrors = [:]
        archiveSubmitting = []
        archiveUnsupportedAccounts = []
        archiveUnavailableThreads = []
        archiveReadErrors = []
        archiveVersions = [:]
        archiveStates = [:]
        active = false
        accounts = []
        items = []
        thread = nil
        draft = nil
        edits = [:]
        conflict = nil
        review = nil
        reviewDraft = nil
        selectedThreadID = nil
        selectedAccountID = nil
        nextCursor = nil
        hasMore = false
        unavailable = false
        errorMessage = nil
        draftActionError = nil
        threadReadError = nil
        feedbackMessage = nil
        styleExampleMessage = nil
        feedbackID = nil
        learning = nil
        revisions = []
        refreshKey = nil
        refreshFailure = nil
        saveKeys = [:]
        sendKeys = [:]
        isLoading = false
        isRefreshing = false
        isLoadingThread = false
        isSaving = false
        isPerformingAction = false
        pendingNewItems = nil
        newImportantCount = 0
    }

    /// Clears thread selection and reloads the chosen account within the current foreground visit.
    public func setAccount(_ id: String?) {
        guard id != selectedAccountID else { return }
        selectedAccountID = id
        clearSelection()
        let foreground = active ? activation : nil
        Task { await reload(activation: foreground) }
    }

    /// Reloads the chosen inbox view without allowing results from an obsolete foreground visit.
    public func setListView(_ value: String) {
        guard value != listView else { return }
        listView = value
        let foreground = active ? activation : nil
        Task { await reload(activation: foreground) }
    }

    /// Debounces search changes and binds the eventual reload to the visit that scheduled it.
    public func setSearchText(_ text: String) {
        searchText = text
        searchTask?.cancel()
        listRequest = UUID()
        let foreground = active ? activation : nil
        searchTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 300_000_000) } catch { return }
            guard let self, !Task.isCancelled else { return }
            await self.reload(activation: foreground)
        }
    }

    /// Allows explicit inactive reads while binding visible inbox work to its current foreground visit.
    public func reload(preserveOrder: Bool = false) async {
        await reload(preserveOrder: preserveOrder, activation: active ? activation : nil)
    }

    /// Retains the initiating visit through every asynchronous projection read and error path.
    private func reload(preserveOrder: Bool = false, activation foreground: UUID?) async {
        let connection = generation
        let mailboxVersions = archiveVersions
        guard acceptsRead(connection: connection, activation: foreground), let api = makeAPIClient() else { return }
        let requestID = UUID()
        listRequest = requestID
        let visibleCount = preserveOrder ? max(5, items.count) : 5
        isLoading = !preserveOrder
        if !preserveOrder {
            items = []
            nextCursor = nil
            hasMore = false
            pendingNewItems = nil
            newImportantCount = 0
        }
        errorMessage = refreshFailure
        defer { if listRequest == requestID { isLoading = false } }
        do {
            async let accountPage = api.emailAccounts()
            async let threadPage = readInbox(api: api,
                accountID: selectedAccountID, view: searchText.isEmpty ? listView : "all",
                text: searchText, count: visibleCount, connection: connection, activation: foreground
            )
            let (loadedAccounts, page) = try await (accountPage, threadPage)
            guard acceptsRead(connection: connection, activation: foreground), listRequest == requestID else { return }
            let loadedThreads = page.items.map { preservingArchiveState($0, since: mailboxVersions) }
            accounts = loadedAccounts.items
            unavailable = false
            seenCursors = []
            nextCursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
            hasMore = nextCursor != nil
            if preserveOrder && !items.isEmpty {
                let oldIDs = Set(items.map(\.id))
                let additions = loadedThreads.filter { !oldIDs.contains($0.id) }
                if !additions.isEmpty {
                    pendingNewItems = loadedThreads
                    newImportantCount = additions.count
                    let current = Dictionary(loadedThreads.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
                    items = items.compactMap { current[$0.id] }
                } else {
                    let current = Dictionary(loadedThreads.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
                    items = items.compactMap { current[$0.id] }
                }
            } else {
                items = loadedThreads
                pendingNewItems = nil
                newImportantCount = 0
            }
            for value in loadedThreads { observeArchive(value) }
        } catch {
            guard acceptsRead(connection: connection, activation: foreground), listRequest == requestID else { return }
            if isUnavailable(error) {
                unavailable = true
                items = []
                accounts = []
            } else { report(error, readAccess: true) }
        }
    }

    /// Stops paging when the original connection or foreground visit no longer owns the read.
    private func readInbox(api: VeetbotAPIClient, accountID: String?, view: String, text: String, count: Int,
                           connection: UUID, activation foreground: UUID?) async throws -> Page<EmailThreadView> {
        var rows: [EmailThreadView] = []
        var ids: Set<UUID> = []
        var cursor: String?
        var cursors: Set<String> = []
        repeat {
            guard acceptsRead(connection: connection, activation: foreground) else { throw CancellationError() }
            let page = try await api.emailThreads(accountID: accountID, view: view, text: text,
                                                 limit: min(100, count - rows.count), cursor: cursor)
            guard acceptsRead(connection: connection, activation: foreground) else { throw CancellationError() }
            rows.append(contentsOf: page.items.filter { ids.insert($0.id).inserted })
            cursor = try nextPageCursor(page.nextCursor, seen: &cursors)
        } while count > 100 && rows.count < count && cursor != nil
        return Page(items: rows, nextCursor: cursor)
    }

    public func showNewItems() {
        if let pendingNewItems { items = pendingNewItems }
        pendingNewItems = nil
        newImportantCount = 0
    }

    /// Applies a page only while the requesting connection, list and foreground visit remain current.
    public func loadMore() async {
        let connection = generation
        let foreground = active ? activation : nil
        guard acceptsRead(connection: connection, activation: foreground), !isLoading,
              let cursor = nextCursor, let api = makeAPIClient() else { return }
        isLoading = true
        let requestID = listRequest
        defer { if listRequest == requestID { isLoading = false } }
        do {
            let page = try await api.emailThreads(
                accountID: selectedAccountID, view: searchText.isEmpty ? listView : "all",
                text: searchText, limit: 5, cursor: cursor
            )
            guard acceptsRead(connection: connection, activation: foreground), listRequest == requestID else { return }
            var ids = Set(items.map(\.id))
            items.append(contentsOf: page.items.filter { ids.insert($0.id).inserted })
            nextCursor = nil
            hasMore = false
            nextCursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
            hasMore = nextCursor != nil
        } catch {
            guard acceptsRead(connection: connection, activation: foreground), listRequest == requestID else { return }
            report(error, readAccess: true)
        }
    }

    /// Admits one foreground refresh and ignores results after its connection or visibility expires.
    public func refresh() async {
        await refresh(activation: activation)
    }

    /// Keeps admission ownership distinct from a later visit while retaining uncertain admission keys.
    private func refresh(activation foreground: UUID) async {
        let connection = generation
        guard acceptsRead(connection: connection, activation: foreground), !unavailable, !isRefreshing,
              let api = makeAPIClient() else { return }
        isRefreshing = true
        let key = refreshKey ?? UUID().uuidString
        refreshKey = key
        defer { if generation == connection, activation == foreground { isRefreshing = false } }
        do {
            let operation = try await api.refreshEmail(idempotencyKey: key)
            guard acceptsRead(connection: connection, activation: foreground) else { return }
            refreshKey = nil
            recordRefreshStatus(operation.status)
            await reload(preserveOrder: true, activation: foreground)
            guard acceptsRead(connection: connection, activation: foreground) else { return }
            if !operation.status.isTerminal { watchRefresh(operation.operationID, activation: foreground) }
            if let selectedThreadID { await openThread(selectedThreadID, refreshOnly: true, activation: foreground) }
        } catch {
            guard acceptsRead(connection: connection, activation: foreground) else { return }
            if isUnavailable(error) { unavailable = true }
            else { recordRefreshFailure(error) }
        }
    }

    /// Retries status reads for the same admitted operation, bounded by failures and foreground visibility.
    private func watchRefresh(_ id: UUID, activation foreground: UUID) {
        let connection = generation
        guard acceptsRead(connection: connection, activation: foreground) else { return }
        operationTask?.cancel()
        operationTask = Task { [weak self] in
            guard let self else { return }
            var consecutiveFailures = 0
            while self.acceptsRead(connection: connection, activation: foreground) {
                do {
                    let delay = UInt64(2 << consecutiveFailures) * 1_000_000_000
                    try await Task.sleep(nanoseconds: delay)
                    guard self.acceptsRead(connection: connection, activation: foreground), let api = self.makeAPIClient() else { return }
                    let operation = try await api.emailOperation(id)
                    guard self.acceptsRead(connection: connection, activation: foreground) else { return }
                    consecutiveFailures = 0
                    self.recordRefreshStatus(operation.status)
                    await self.reload(preserveOrder: true, activation: foreground)
                    guard self.acceptsRead(connection: connection, activation: foreground) else { return }
                    if let selected = self.selectedThreadID { await self.openThread(selected, refreshOnly: true, activation: foreground) }
                    if operation.status.isTerminal { return }
                } catch is CancellationError { return }
                catch {
                    guard self.acceptsRead(connection: connection, activation: foreground) else { return }
                    self.recordRefreshFailure(error, readAccess: true)
                    consecutiveFailures += 1
                    guard Self.isTransientReadFailure(error), consecutiveFailures < 3 else { return }
                }
            }
        }
    }

    /// Foreground work belongs to one visit; standalone inactive reads still honor cancellation and connection identity.
    private func acceptsRead(connection: UUID, activation foreground: UUID?) -> Bool {
        generation == connection && !Task.isCancelled
            && (foreground == nil || (active && foreground == activation))
    }

    /// Retains refresh failures across successful cache reads until an operation completes.
    private func recordRefreshFailure(_ error: Error, readAccess: Bool = false) {
        guard !(error is CancellationError), !Task.isCancelled else { return }
        refreshFailure = error.localizedDescription
        report(error, readAccess: readAccess)
    }

    /// Allows bounded retries for temporary HTTP and network failures, never authorization errors.
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

    private func recordRefreshStatus(_ status: RunStatus) {
        if status == .failed {
            refreshFailure = "Email refresh failed. Previously loaded mail remains available. Try refreshing again."
        } else if status == .completed {
            refreshFailure = nil
        }
    }

    /// Clears thread presentation and its errors without discarding draft edits stored by draft ID.
    public func clearSelection() {
        selectionRequest = UUID()
        selectedThreadID = nil
        thread = nil
        draft = nil
        conflict = nil
        review = nil
        reviewDraft = nil
        draftActionError = nil
        threadReadError = nil
        revisions = []
        feedbackMessage = nil
        styleExampleMessage = nil
        feedbackID = nil
    }

    /// Refreshes a thread without overwriting local edits or clearing unrelated action failures.
    public func openThread(_ id: UUID, approvalID: UUID? = nil, refreshOnly: Bool = false) async {
        await openThread(id, approvalID: approvalID, refreshOnly: refreshOnly, activation: active ? activation : nil)
    }

    /// Validates foreground ownership for both the thread read and an optional separate draft read.
    private func openThread(_ id: UUID, approvalID: UUID? = nil, refreshOnly: Bool = false, activation foreground: UUID?) async {
        let connection = generation
        let mailboxVersions = archiveVersions
        guard acceptsRead(connection: connection, activation: foreground), let api = makeAPIClient() else { return }
        if !refreshOnly {
            clearSelection()
            selectedThreadID = id
        }
        guard selectedThreadID == id else { return }
        let requestID = UUID()
        selectionRequest = requestID
        isLoadingThread = thread == nil
        defer { if selectionRequest == requestID { isLoadingThread = false } }
        do {
            let response = try await api.emailThread(id)
            guard acceptsRead(connection: connection, activation: foreground), selectionRequest == requestID else { return }
            let result = preservingArchiveState(response, since: mailboxVersions)
            thread = result
            observeArchive(result)
            if let returnedDraft = result.draft { mergeDraft(returnedDraft) }
            else if let draftID = result.draftID {
                let value = try await api.emailDraft(draftID)
                guard acceptsRead(connection: connection, activation: foreground), selectionRequest == requestID else { return }
                mergeDraft(value)
            } else { draft = nil }
            threadReadError = nil
            if let approvalID, draft?.approvalID == approvalID { await loadReview(activation: foreground) }
        } catch {
            guard acceptsRead(connection: connection, activation: foreground), selectionRequest == requestID else { return }
            report(error, draft: true, readAccess: true)
        }
    }

    private func mergeDraft(_ value: EmailDraftView) {
        guard value.threadID == selectedThreadID else { return }
        draft = value
        if let local = edits[value.id], local.isDirty {
            if local.base.revision != value.revision { conflict = value }
        } else {
            edits[value.id] = EmailDraftEdit(value)
            conflict = nil
        }
        if reviewDraft?.revision != value.revision || value.stale || value.status != "awaiting_approval" {
            review = nil
            reviewDraft = nil
        }
    }

    public func changeEdit(_ keyPath: WritableKeyPath<EmailDraftEdit, String>, to text: String) {
        guard let draft, draft.canEdit, var edit = edits[draft.id] else { return }
        edit[keyPath: keyPath] = text
        edits[draft.id] = edit
        review = nil
        reviewDraft = nil
        autosaveTask?.cancel()
        let id = draft.id
        autosaveTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 700_000_000) } catch { return }
            guard let self, !Task.isCancelled else { return }
            _ = await self.saveDraft(id)
        }
    }

    @discardableResult
    /// Saves the expected draft revision and preserves both versions when another device has edited it.
    public func saveDraft(_ id: UUID? = nil) async -> Bool {
        guard let id = id ?? draft?.id, let edit = edits[id], let api = makeAPIClient() else { return false }
        guard edit.isDirty else { return true }
        guard !isSaving, !(conflict?.id == id) else { return false }
        isSaving = true
        draftActionError = nil
        let connection = generation
        let key: String
        if let previous = saveKeys[id], previous.0 == edit { key = previous.1 }
        else { key = UUID().uuidString; saveKeys[id] = (edit, key) }
        defer { if generation == connection { isSaving = false } }
        do {
            let saved = try await api.saveEmailDraft(edit, idempotencyKey: key)
            guard generation == connection else { return false }
            saveKeys.removeValue(forKey: id)
            if var latest = edits[id] {
                // Typing can continue during a save. Advance only its base,
                // retaining every character entered after this request began.
                if latest == edit { latest = EmailDraftEdit(saved) } else { latest.base = saved }
                edits[id] = latest
                if latest.isDirty {
                    autosaveTask = Task { [weak self] in
                        do { try await Task.sleep(nanoseconds: 50_000_000) } catch { return }
                        guard let self, self.generation == connection else { return }
                        _ = await self.saveDraft(id)
                    }
                }
            }
            if draft?.id == id { draft = saved }
            return true
        } catch {
            guard generation == connection else { return false }
            if case HTTPTransportError.api(let failure) = error, failure.statusCode == 409,
                let server = try? await api.emailDraft(id), generation == connection {
                if draft?.id == id { conflict = server; draft = server }
            }
            report(error, draft: true)
            return false
        }
    }

    /// The owner chooses the current server version explicitly; no conflict
    /// path silently throws away local text.
    public func useServerDraft() {
        guard let conflict else { return }
        edits[conflict.id] = EmailDraftEdit(conflict)
        draft = conflict
        self.conflict = nil
        draftActionError = nil
    }

    public func keepLocalDraft() async {
        guard let conflict, var local = edits[conflict.id] else { return }
        local.base = conflict
        edits[conflict.id] = local
        self.conflict = nil
        _ = await saveDraft(conflict.id)
    }

    public func giveFeedback(target: EmailFeedbackTarget, judgment: String, explanation: String? = nil, targetValue: String? = nil) async {
        guard let thread, let api = makeAPIClient(), !isPerformingAction else { return }
        if target == .topic, !thread.feedbackTopics.contains(targetValue ?? "") {
            draftActionError = "Choose an available content topic, or apply feedback to This thread."
            return
        }
        draftActionError = nil
        isPerformingAction = true
        let connection = generation
        defer { if generation == connection { isPerformingAction = false } }
        do {
            let result = try await api.emailFeedback(thread: thread, target: target, judgment: judgment,
                                                    explanation: explanation, targetValue: targetValue, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            feedbackID = result.feedbackID
            switch judgment {
            case "needs_reply": feedbackMessage = "This thread needs a reply."
            case "no_reply_needed": feedbackMessage = "This thread needs no reply."
            default:
                let scope = target == .topic ? (targetValue ?? target.title.lowercased()) : target.title.lowercased()
                feedbackMessage = "Marked \(scope) as \(judgment.replacingOccurrences(of: "_", with: " "))."
            }
            await reload(preserveOrder: true)
            if selectedThreadID == thread.id { await openThread(thread.id, refreshOnly: true) }
        } catch { if generation == connection { report(error, draft: true) } }
    }

    public func undoFeedback() async {
        guard let feedbackID, let api = makeAPIClient() else { return }
        let connection = generation
        do {
            _ = try await api.undoEmailFeedback(feedbackID, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            self.feedbackID = nil
            feedbackMessage = "Feedback undone."
            await reload(preserveOrder: true)
        } catch { if generation == connection { report(error) } }
    }

    public func generateDraft(instruction: String? = nil) async {
        guard let id = selectedThreadID, let api = makeAPIClient(), !isPerformingAction else { return }
        if currentEdit?.isDirty == true, !(await saveDraft()) { return }
        isPerformingAction = true
        let connection = generation
        defer { if generation == connection { isPerformingAction = false } }
        do {
            let result = try await api.generateEmailDraft(threadID: id, instruction: instruction, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            if let value = result.draft { mergeDraft(value) }
            await watchDraftRun(result.runID, threadID: id, connection: connection)
        } catch { if generation == connection { report(error, draft: true) } }
    }

    /// Saves edits and proposes the exact draft for approval, retaining accepted work if Email is hidden.
    public func prepareSend() async {
        guard canReview, let api = makeAPIClient() else { return }
        autosaveTask?.cancel()
        if currentEdit?.isDirty == true, !(await saveDraft()) { return }
        guard let draft, currentEdit?.isDirty == false, !draft.stale else { return }
        isPerformingAction = true
        let connection = generation
        defer { if generation == connection { isPerformingAction = false } }
        let key: String
        if let previous = sendKeys[draft.id], previous.0 == draft.revision { key = previous.1 }
        else { key = UUID().uuidString; sendKeys[draft.id] = (draft.revision, key) }
        do {
            let result = try await api.proposeEmailSend(draft, idempotencyKey: key)
            guard generation == connection else { return }
            if let value = result.draft { mergeDraft(value) }
            await watchDraftRun(result.runID, threadID: draft.threadID, connection: connection)
            await loadReview(activation: nil)
        } catch { if generation == connection { report(error, draft: true) } }
    }

    private func watchDraftRun(_ runID: UUID, threadID: UUID, connection: UUID) async {
        guard let api = makeAPIClient() else { return }
        // Stop local waiting after a bounded interval; subsequent active refresh
        // recovers the same durable action instead of submitting another one.
        for _ in 0..<60 {
            guard generation == connection, selectedThreadID == threadID, !Task.isCancelled else { return }
            do {
                let run = try await api.getRun(runID)
                await openThread(threadID, refreshOnly: true)
                if run.status.isTerminal || run.status == .waitingForApproval || run.status == .waitingForUser { return }
                try await Task.sleep(nanoseconds: 1_000_000_000)
            } catch { if generation == connection { report(error, draft: true) }; return }
        }
    }

    /// Opens approval only after current account authority and the exact displayed draft match.
    public func loadReview() async {
        await loadReview(activation: active ? activation : nil)
    }

    /// Foreground reads keep their visit identity; an accepted send proposal may finish review preparation while hidden.
    private func loadReview(activation foreground: UUID?) async {
        let connection = generation
        guard acceptsRead(connection: connection, activation: foreground),
              let draft, let id = draft.approvalID, let api = makeAPIClient(),
              !draft.stale, currentEdit?.isDirty == false else { return }
        do {
            let accountPage = try await api.emailAccounts()
            guard acceptsRead(connection: connection, activation: foreground) else { return }
            accounts = accountPage.items
            let approval = try await api.getApproval(id)
            guard acceptsRead(connection: connection, activation: foreground), self.draft?.id == draft.id,
                self.draft?.revision == draft.revision, currentEdit?.isDirty == false,
                approval.runID == draft.runID, approval.status == .pending else { return }
            guard approvalMatchesDraft(approval, draft: draft) else {
                draftActionError = "The approval does not match this draft. Refresh the thread and review again."
                return
            }
            review = approval
            reviewDraft = draft
        } catch { if acceptsRead(connection: connection, activation: foreground) { report(error, draft: true) } }
    }

    public func closeReview() { review = nil; reviewDraft = nil }

    private func approvalMatchesDraft(_ approval: ApprovalView, draft: EmailDraftView) -> Bool {
        func addressesMatch(_ key: String, _ expected: [String]) -> Bool {
            guard let value = approval.arguments[key], value != .null else { return key != "to" && expected.isEmpty }
            if let text = value.stringValue { return text == expected.joined(separator: ", ") }
            guard let entries = value.arrayValue else { return false }
            let strings = entries.compactMap(\.stringValue)
            return strings.count == entries.count && strings == expected
        }
        guard let account = accounts.first(where: { $0.id == draft.accountID }),
            let serverID = account.sendServerID,
            let toolName = draft.sendToolName, toolName == "mcp.\(serverID).send_message", approval.toolName == toolName,
            let providerThreadID = draft.providerThreadID,
            approval.arguments["thread_id"]?.stringValue == providerThreadID else { return false }
        return approval.arguments["subject"]?.stringValue == draft.subject
            && approval.arguments["body"]?.stringValue == draft.body
            && addressesMatch("to", draft.to) && addressesMatch("cc", draft.cc) && addressesMatch("bcc", draft.bcc)
    }

    public func discussionSession() async -> UUID? {
        guard let thread, let api = makeAPIClient() else { return nil }
        if let id = thread.sessionID ?? draft?.sessionID { return id }
        let connection = generation
        do {
            let result = try await api.emailDiscussion(thread.id, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return nil }
            if selectedThreadID == thread.id { self.thread = result }
            return result.sessionID
        } catch { if generation == connection { report(error, draft: true) }; return nil }
    }

    /// Marks the selected source revision handled through the reversible attention command.
    public func dismissSelectedThread() async {
        guard let thread else { return }
        await setThreadHandled(thread, handled: true)
    }

    /// Applies confirmed attention state while retaining open message content and unsaved draft text.
    public func setThreadHandled(_ thread: EmailThreadView, handled: Bool) async {
        guard let api = makeAPIClient(), !isPerformingAction else { return }
        isPerformingAction = true
        let connection = generation
        defer { if generation == connection { isPerformingAction = false } }
        do {
            let result = try await api.dismissEmailThread(thread, dismissed: handled, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            // The command returns a summary. Preserve the open messages and
            // unsaved draft, updating only server-confirmed attention state.
            if self.thread?.id == result.id, self.thread?.revision == result.revision {
                self.thread?.dismissedRevision = result.dismissedRevision
            }
            await reload(preserveOrder: true)
        } catch { if generation == connection { report(error, draft: selectedThreadID == thread.id) } }
    }

    /// Requires explicit support from this thread's account and known mailbox state.
    public func archiveUnavailableReason(for thread: EmailThreadView) -> String? {
        guard !unavailable, !archiveUnsupportedAccounts.contains(thread.accountID),
              let account = accounts.first(where: { $0.id == thread.accountID }),
              account.archiveSupported == true, account.writeServerID != nil else {
            return "Gmail archiving is unavailable for this account. Update or reconnect the server."
        }
        if archiveUnavailableThreads.contains(thread.id) { return "This thread or its Gmail archive endpoint is unavailable. Reconnect to check again." }
        return thread.inInbox == nil ? "Refresh this thread to check its Gmail Inbox state." : nil
    }

    /// Pending and uncertain writes cannot be replaced by another checkbox action.
    public func canArchive(_ thread: EmailThreadView) -> Bool {
        guard archiveUnavailableReason(for: thread) == nil, !archiveSubmitting.contains(thread.id) else { return false }
        if let operation = thread.archiveOperation {
            return operation.status == "completed" || operation.status == "failed"
        }
        return true
    }

    /// Describes this mailbox action without replacing unrelated refresh, editing or send errors.
    public func archiveMessage(for thread: EmailThreadView) -> String? {
        if let error = archiveErrors[thread.id] { return error }
        if archiveSubmitting.contains(thread.id) { return "Requesting the Gmail change…" }
        guard let operation = thread.archiveOperation else { return nil }
        switch operation.status {
        case "pending": return operation.targetArchived ? "Archiving in Gmail…" : "Moving to Inbox…"
        case "failed": return operation.error ?? "Gmail could not complete this action. Try again."
        case "uncertain": return "Outcome not confirmed. Check Gmail; recorded status may update after a later email refresh."
        case "completed": return nil
        default: return "Checking the Gmail action's status…"
        }
    }

    /// Supplies one-action consent and retains the same request after an uncertain admission response.
    public func setThreadArchived(_ thread: EmailThreadView, archived: Bool) async {
        guard let api = makeAPIClient(), !archiveSubmitting.contains(thread.id) else { return }
        guard archiveUnavailableReason(for: thread) == nil else {
            archiveErrors[thread.id] = archiveUnavailableReason(for: thread)
            return
        }
        if archiveRequests[thread.id]?.operationID != nil || !canArchive(thread) {
            await checkArchiveStatus(thread.id)
            return
        }
        let connection = generation
        let request = archiveRequests[thread.id] ?? ArchiveRequest(thread: thread, archived: archived, key: UUID().uuidString)
        archiveRequests[thread.id] = request
        archiveErrors[thread.id] = nil
        archiveReadErrors.remove(thread.id)
        archiveVersions[thread.id] = UUID()
        archiveSubmitting.insert(thread.id)
        defer { if generation == connection { archiveSubmitting.remove(thread.id) } }
        do {
            let operation = try await api.archiveEmailThread(request.thread, archived: request.archived, idempotencyKey: request.key)
            guard generation == connection else { return }
            archiveRequests[thread.id]?.operationID = operation.operationID
            let pending = EmailArchiveOperation(operationID: operation.operationID, runID: operation.runID,
                targetArchived: request.archived, status: "pending", error: nil)
            if let index = items.firstIndex(where: { $0.id == thread.id }) { items[index].archiveOperation = pending }
            if self.thread?.id == thread.id { self.thread?.archiveOperation = pending }
            let current = self.thread?.id == thread.id ? self.thread : items.first { $0.id == thread.id }
            archiveStates[thread.id] = ArchiveMailboxState(inInbox: current?.inInbox ?? thread.inInbox,
                operation: pending, dismissedRevision: current?.dismissedRevision ?? thread.dismissedRevision)
            archiveVersions[thread.id] = UUID()
            await checkArchiveStatus(thread.id)
        } catch {
            guard generation == connection, !(error is CancellationError), !Task.isCancelled else { return }
            if isUnavailable(error) {
                if case HTTPTransportError.api(let failure) = error, failure.statusCode == 405 {
                    archiveUnsupportedAccounts.insert(thread.accountID)
                } else { archiveUnavailableThreads.insert(thread.id) }
                archiveRequests[thread.id] = nil
                archiveErrors[thread.id] = archiveUnavailableReason(for: thread)
            } else {
                if case HTTPTransportError.api(let failure) = error,
                   let status = failure.statusCode, (400...499).contains(status), status != 408, status != 429 {
                    archiveRequests[thread.id] = nil
                }
                archiveErrors[thread.id] = error.localizedDescription
                if case HTTPTransportError.reauthenticationRequired = error { report(error) }
                if case HTTPTransportError.authorizationDenied = error { archiveRequests[thread.id] = nil }
            }
        }
    }

    /// Reads durable action state without admitting a replacement Gmail mutation.
    public func checkArchiveStatus(_ threadID: UUID) async {
        let connection = generation
        let foreground = active ? activation : nil
        let mailboxVersions = archiveVersions
        guard acceptsRead(connection: connection, activation: foreground), let api = makeAPIClient() else { return }
        do {
            let value = try await api.emailThread(threadID)
            guard acceptsRead(connection: connection, activation: foreground), archiveVersions[threadID] == mailboxVersions[threadID] else { return }
            if archiveReadErrors.remove(threadID) != nil { archiveErrors[threadID] = nil }
            guard acceptsArchiveOperation(value) else { return }
            mergeArchive(value)
            if value.archiveOperation?.status == "completed" {
                await reload(preserveOrder: true, activation: foreground)
            }
            observeArchive(value)
        } catch {
            guard acceptsRead(connection: connection, activation: foreground), archiveVersions[threadID] == mailboxVersions[threadID] else { return }
            archiveReadErrors.insert(threadID)
            archiveErrors[threadID] = error.localizedDescription
            if case HTTPTransportError.reauthenticationRequired = error { report(error) }
            if case HTTPTransportError.authorizationDenied = error { report(error, readAccess: true) }
        }
    }

    /// Applies fresh source context and mailbox state while retaining local drafts and independent errors.
    private func mergeArchive(_ value: EmailThreadView) {
        archiveVersions[value.id] = UUID()
        archiveStates[value.id] = ArchiveMailboxState(inInbox: value.inInbox, operation: value.archiveOperation,
            dismissedRevision: value.dismissedRevision)
        if let index = items.firstIndex(where: { $0.id == value.id }), value.revision >= items[index].revision {
            items[index] = value
        }
        if thread?.id == value.id, let revision = thread?.revision, value.revision >= revision {
            thread = value
            if let returnedDraft = value.draft,
               draft?.id != returnedDraft.id || returnedDraft.revision >= (draft?.revision ?? 0) {
                mergeDraft(returnedDraft)
            }
        }
    }

    /// Retains newer mailbox changes without discarding an otherwise useful conversation or inbox read.
    private func preservingArchiveState(_ value: EmailThreadView, since versions: [UUID: UUID]) -> EmailThreadView {
        if archiveVersions[value.id] != versions[value.id] || !acceptsArchiveOperation(value) {
            if let state = archiveStates[value.id] {
                var preserved = value
                preserved.inInbox = state.inInbox
                preserved.archiveOperation = state.operation
                preserved.dismissedRevision = state.dismissedRevision
                return preserved
            }
        } else {
            archiveStates[value.id] = ArchiveMailboxState(inInbox: value.inInbox,
                operation: value.archiveOperation, dismissedRevision: value.dismissedRevision)
        }
        return value
    }

    /// An older operation cannot settle or replace a newer request whose admission response is missing.
    private func acceptsArchiveOperation(_ value: EmailThreadView) -> Bool {
        guard let request = archiveRequests[value.id] else { return true }
        guard let operation = value.archiveOperation else { return false }
        if let id = request.operationID { return operation.operationID == id }
        return operation.operationID != request.thread.archiveOperation?.operationID
            && operation.targetArchived == request.archived
    }

    /// Resumes pending actions learned from another device and clears only confirmed action failures.
    private func observeArchive(_ value: EmailThreadView) {
        guard acceptsArchiveOperation(value), let operation = value.archiveOperation else { return }
        if operation.status == "completed" || operation.status == "failed" || operation.status == "uncertain" {
            archiveRequests[value.id] = nil
            archiveErrors[value.id] = nil
            archiveReadErrors.remove(value.id)
            archiveTasks[value.id]?.cancel()
            archiveTasks[value.id] = nil
        } else if active, archiveTasks[value.id] == nil {
            let connection = generation
            let foreground = activation
            archiveTasks[value.id] = Task { [weak self] in
                guard let self else { return }
                defer { if self.activation == foreground, self.generation == connection { self.archiveTasks[value.id] = nil } }
                for _ in 0..<60 {
                    do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
                    guard self.acceptsRead(connection: connection, activation: foreground) else { return }
                    await self.checkArchiveStatus(value.id)
                    if self.archiveErrors[value.id] != nil { return }
                }
            }
        }
    }

    public func confirmCurrentSourceReviewed() async {
        guard let thread, let draft, var edit = edits[draft.id], draft.stale else { return }
        edit.reviewedSourceRevision = thread.revision
        edits[draft.id] = edit
        _ = await saveDraft()
    }

    /// Reads draft history without letting an obsolete panel erase or replace the current visit's state.
    public func loadRevisions() async {
        let connection = generation
        let foreground = active ? activation : nil
        guard acceptsRead(connection: connection, activation: foreground), let draft, let api = makeAPIClient() else { return }
        do {
            let page = try await api.emailDraftRevisions(draft.id)
            guard acceptsRead(connection: connection, activation: foreground), self.draft?.id == draft.id else { return }
            revisions = page.items
        } catch { if acceptsRead(connection: connection, activation: foreground) { report(error, draft: true, readAccess: true) } }
    }

    public func useRevision(_ value: EmailDraftView) {
        guard let draft, draft.id == value.id, var edit = edits[draft.id], draft.canEdit else { return }
        edit.to = value.to.joined(separator: ", ")
        edit.cc = value.cc.joined(separator: ", ")
        edit.bcc = value.bcc.joined(separator: ", ")
        edit.subject = value.subject
        edit.body = value.body
        edits[draft.id] = edit
        closeReview()
    }

    /// Refreshes learning coverage for the current visit, while still allowing explicit inactive reads.
    public func loadLearning() async {
        let connection = generation
        let foreground = active ? activation : nil
        guard acceptsRead(connection: connection, activation: foreground), let api = makeAPIClient() else { return }
        do {
            let value = try await api.emailLearning()
            if acceptsRead(connection: connection, activation: foreground) { learning = value }
        } catch { if acceptsRead(connection: connection, activation: foreground) { report(error, readAccess: true) } }
    }

    /// Saves local changes before explicitly endorsing that exact revision as a writing example.
    public func endorseDraftStyle() async {
        guard !isPerformingAction, conflict == nil, !unavailable, let draftID = draft?.id else { return }
        let connection = generation
        let selection = selectedThreadID
        if currentEdit?.isDirty == true, !(await saveDraft(draftID)) { return }
        guard generation == connection, selectedThreadID == selection,
              let draft, draft.id == draftID, let api = makeAPIClient() else { return }
        isPerformingAction = true
        draftActionError = nil
        styleExampleMessage = nil
        defer { if generation == connection { isPerformingAction = false } }
        do {
            let value = try await api.endorseEmailDraftStyle(draft)
            guard generation == connection else { return }
            learning = value
            if self.draft?.id == draft.id {
                styleExampleMessage = "Saved revision \(draft.revision) as a writing example."
            }
        } catch { if generation == connection { report(error, draft: true) } }
    }

    public func setLearningPaused(_ paused: Bool) async {
        guard let api = makeAPIClient() else { return }
        let connection = generation
        do {
            let value = try await api.setEmailLearningPaused(paused)
            if generation == connection { learning = value }
        } catch { if generation == connection { report(error) } }
    }

    public func resetLearning(scope: String) async {
        guard let api = makeAPIClient() else { return }
        let connection = generation
        do {
            let value = try await api.resetEmailLearning(scope: scope, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            learning = value
            await reload(preserveOrder: true)
        } catch { if generation == connection { report(error) } }
    }

    public func excludeSelectedThread() async {
        guard let thread, let api = makeAPIClient() else { return }
        let connection = generation
        do {
            try await api.excludeEmailThread(thread, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            edits = edits.filter { $0.value.base.threadID != thread.id }
            clearSelection()
            await reload(preserveOrder: true)
        } catch { if generation == connection { report(error, draft: true) } }
    }

    public func resolveSend(_ decision: ApprovalDecision) async {
        guard let review, let frozen = reviewDraft, let draft,
            draft.id == frozen.id, draft.revision == frozen.revision, !draft.stale,
            currentEdit?.isDirty == false, let api = makeAPIClient(), !isPerformingAction else { return }
        isPerformingAction = true
        let connection = generation
        defer { if generation == connection { isPerformingAction = false } }
        do {
            _ = try await api.resolveApproval(review.id, decision: decision)
            guard generation == connection else { return }
            closeReview()
            await watchDraftRun(review.runID, threadID: draft.threadID, connection: connection)
        } catch {
            guard generation == connection else { return }
            // A lost response or another device may have resolved this action.
            // Read the same approval and draft; never create a new send here.
            if let latest = try? await api.getApproval(review.id), !latest.status.isPending {
                closeReview()
                await openThread(draft.threadID, refreshOnly: true)
            }
            report(error, draft: true)
        }
    }

    public func discardDraft() async {
        guard let draft, let api = makeAPIClient(), !isPerformingAction else { return }
        let connection = generation
        do {
            try await api.discardEmailDraft(draft, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            edits.removeValue(forKey: draft.id)
            closeReview()
            await openThread(draft.threadID, refreshOnly: true)
        } catch { if generation == connection { report(error, draft: true) } }
    }

    private func isUnavailable(_ error: Error) -> Bool {
        guard case HTTPTransportError.api(let failure) = error else { return false }
        return failure.statusCode == 404 || failure.statusCode == 405
    }

    /// Routes visible errors by their source; cancellation is silent and revoked read access clears mail.
    private func report(_ error: Error, draft: Bool = false, readAccess: Bool = false) {
        if error is CancellationError || Task.isCancelled { return }
        if case HTTPTransportError.reauthenticationRequired = error {
            resetConnection()
            authenticationFailure?(error)
        } else if readAccess, case HTTPTransportError.authorizationDenied = error {
            resetConnection()
        }
        if draft {
            if readAccess { threadReadError = error.localizedDescription }
            else { draftActionError = error.localizedDescription }
        } else { errorMessage = error.localizedDescription }
    }
}
