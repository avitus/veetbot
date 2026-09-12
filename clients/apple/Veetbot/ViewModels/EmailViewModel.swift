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

    public var authenticationFailure: ((Error) -> Void)?
    private let makeAPIClient: () -> VeetbotAPIClient?
    private let refreshNanoseconds: UInt64
    private var active = false
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
    }

    public var currentEdit: EmailDraftEdit? { draft.flatMap { edits[$0.id] } }
    /// Keeps an unresolved owner-action failure visible even when a later thread read fails or recovers.
    public var draftError: String? { draftActionError ?? threadReadError }
    public var canReview: Bool {
        guard let draft else { return false }
        return draft.canEdit && !draft.stale && conflict == nil && !isSaving && !isPerformingAction
    }

    public func setActive(_ value: Bool) {
        guard active != value else { return }
        active = value
        refreshTask?.cancel()
        operationTask?.cancel()
        guard value else { return }
        refreshTask = Task { [weak self] in
            guard let self else { return }
            await self.reload()
            while !Task.isCancelled && self.active {
                await self.refresh()
                do { try await Task.sleep(nanoseconds: self.refreshNanoseconds) }
                catch { return }
            }
        }
    }

    /// Cancels obsolete work and removes all mail, local edits and errors from the old connection.
    public func resetConnection() {
        generation = UUID()
        listRequest = UUID()
        selectionRequest = UUID()
        refreshTask?.cancel()
        operationTask?.cancel()
        autosaveTask?.cancel()
        searchTask?.cancel()
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

    public func setAccount(_ id: String?) {
        guard id != selectedAccountID else { return }
        selectedAccountID = id
        clearSelection()
        Task { await reload() }
    }

    public func setListView(_ value: String) {
        guard value != listView else { return }
        listView = value
        Task { await reload() }
    }

    public func setSearchText(_ text: String) {
        searchText = text
        searchTask?.cancel()
        listRequest = UUID()
        searchTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 300_000_000) } catch { return }
            guard let self, !Task.isCancelled else { return }
            await self.reload()
        }
    }

    public func reload(preserveOrder: Bool = false) async {
        guard let api = makeAPIClient() else { return }
        let requestID = UUID()
        let connection = generation
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
                text: searchText, count: visibleCount
            )
            let (loadedAccounts, page) = try await (accountPage, threadPage)
            guard generation == connection, listRequest == requestID else { return }
            accounts = loadedAccounts.items
            unavailable = false
            seenCursors = []
            nextCursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
            hasMore = nextCursor != nil
            if preserveOrder && !items.isEmpty {
                let oldIDs = Set(items.map(\.id))
                let additions = page.items.filter { !oldIDs.contains($0.id) }
                if !additions.isEmpty {
                    pendingNewItems = page.items
                    newImportantCount = additions.count
                    let current = Dictionary(page.items.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
                    items = items.compactMap { current[$0.id] }
                } else {
                    let current = Dictionary(page.items.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
                    items = items.compactMap { current[$0.id] }
                }
            } else {
                items = page.items
                pendingNewItems = nil
                newImportantCount = 0
            }
        } catch {
            guard generation == connection, listRequest == requestID else { return }
            if isUnavailable(error) {
                unavailable = true
                items = []
                accounts = []
            } else { report(error, readAccess: true) }
        }
    }

    private func readInbox(api: VeetbotAPIClient, accountID: String?, view: String, text: String, count: Int) async throws -> Page<EmailThreadView> {
        var rows: [EmailThreadView] = []
        var ids: Set<UUID> = []
        var cursor: String?
        var cursors: Set<String> = []
        repeat {
            try Task.checkCancellation()
            let page = try await api.emailThreads(accountID: accountID, view: view, text: text,
                                                 limit: min(100, count - rows.count), cursor: cursor)
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

    public func loadMore() async {
        guard !isLoading, let cursor = nextCursor, let api = makeAPIClient() else { return }
        isLoading = true
        let requestID = listRequest
        let connection = generation
        defer { if listRequest == requestID { isLoading = false } }
        do {
            let page = try await api.emailThreads(
                accountID: selectedAccountID, view: searchText.isEmpty ? listView : "all",
                text: searchText, limit: 5, cursor: cursor
            )
            guard generation == connection, listRequest == requestID else { return }
            var ids = Set(items.map(\.id))
            items.append(contentsOf: page.items.filter { ids.insert($0.id).inserted })
            nextCursor = nil
            hasMore = false
            nextCursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
            hasMore = nextCursor != nil
        } catch {
            guard generation == connection, listRequest == requestID else { return }
            report(error, readAccess: true)
        }
    }

    /// Admits one foreground refresh and ignores results after its connection or visibility expires.
    public func refresh() async {
        guard active, !unavailable, !isRefreshing, let api = makeAPIClient() else { return }
        isRefreshing = true
        let connection = generation
        let key = refreshKey ?? UUID().uuidString
        refreshKey = key
        defer { if generation == connection { isRefreshing = false } }
        do {
            let operation = try await api.refreshEmail(idempotencyKey: key)
            guard generation == connection, active, !Task.isCancelled else { return }
            refreshKey = nil
            recordRefreshStatus(operation.status)
            await reload(preserveOrder: true)
            guard generation == connection, active, !Task.isCancelled else { return }
            if !operation.status.isTerminal { watchRefresh(operation.operationID) }
            if let selectedThreadID { await openThread(selectedThreadID, refreshOnly: true) }
        } catch {
            guard generation == connection, active, !Task.isCancelled else { return }
            if isUnavailable(error) { unavailable = true }
            else { recordRefreshFailure(error) }
        }
    }

    /// Retries status reads for the same admitted operation, bounded by failures and foreground visibility.
    private func watchRefresh(_ id: UUID) {
        operationTask?.cancel()
        let connection = generation
        operationTask = Task { [weak self] in
            guard let self else { return }
            var consecutiveFailures = 0
            while self.active && self.generation == connection && !Task.isCancelled {
                do {
                    let delay = UInt64(2 << consecutiveFailures) * 1_000_000_000
                    try await Task.sleep(nanoseconds: delay)
                    guard self.active, let api = self.makeAPIClient(), !Task.isCancelled else { return }
                    let operation = try await api.emailOperation(id)
                    guard self.generation == connection, self.active, !Task.isCancelled else { return }
                    consecutiveFailures = 0
                    self.recordRefreshStatus(operation.status)
                    await self.reload(preserveOrder: true)
                    guard self.generation == connection, self.active, !Task.isCancelled else { return }
                    if let selected = self.selectedThreadID { await self.openThread(selected, refreshOnly: true) }
                    if operation.status.isTerminal { return }
                } catch is CancellationError { return }
                catch {
                    guard self.generation == connection, self.active, !Task.isCancelled else { return }
                    self.recordRefreshFailure(error, readAccess: true)
                    consecutiveFailures += 1
                    guard Self.isTransientReadFailure(error), consecutiveFailures < 3 else { return }
                }
            }
        }
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
        guard let api = makeAPIClient() else { return }
        if !refreshOnly {
            clearSelection()
            selectedThreadID = id
        }
        guard selectedThreadID == id else { return }
        let requestID = UUID()
        let connection = generation
        selectionRequest = requestID
        isLoadingThread = thread == nil
        defer { if selectionRequest == requestID { isLoadingThread = false } }
        do {
            let result = try await api.emailThread(id)
            guard generation == connection, selectionRequest == requestID else { return }
            thread = result
            if let returnedDraft = result.draft { mergeDraft(returnedDraft) }
            else if let draftID = result.draftID {
                let value = try await api.emailDraft(draftID)
                guard generation == connection, selectionRequest == requestID else { return }
                mergeDraft(value)
            } else { draft = nil }
            threadReadError = nil
            if let approvalID, draft?.approvalID == approvalID { await loadReview() }
        } catch {
            guard generation == connection, selectionRequest == requestID else { return }
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
            default: feedbackMessage = "Marked \(target.title.lowercased()) as \(judgment.replacingOccurrences(of: "_", with: " "))."
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
            await loadReview()
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
        guard let draft, let id = draft.approvalID, let api = makeAPIClient(),
            !draft.stale, currentEdit?.isDirty == false else { return }
        let connection = generation
        do {
            let accountPage = try await api.emailAccounts()
            guard generation == connection else { return }
            accounts = accountPage.items
            let approval = try await api.getApproval(id)
            guard generation == connection, self.draft?.id == draft.id,
                self.draft?.revision == draft.revision, currentEdit?.isDirty == false,
                approval.runID == draft.runID, approval.status == .pending else { return }
            guard approvalMatchesDraft(approval, draft: draft) else {
                draftActionError = "The approval does not match this draft. Refresh the thread and review again."
                return
            }
            review = approval
            reviewDraft = draft
        } catch { if generation == connection { report(error, draft: true) } }
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

    public func confirmCurrentSourceReviewed() async {
        guard let thread, let draft, var edit = edits[draft.id], draft.stale else { return }
        edit.reviewedSourceRevision = thread.revision
        edits[draft.id] = edit
        _ = await saveDraft()
    }

    public func loadRevisions() async {
        guard let draft, let api = makeAPIClient() else { return }
        let connection = generation
        do {
            let page = try await api.emailDraftRevisions(draft.id)
            guard generation == connection, self.draft?.id == draft.id else { return }
            revisions = page.items
        } catch { if generation == connection { report(error, draft: true, readAccess: true) } }
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

    public func loadLearning() async {
        guard let api = makeAPIClient() else { return }
        let connection = generation
        do {
            let value = try await api.emailLearning()
            if generation == connection { learning = value }
        } catch { if generation == connection { report(error, readAccess: true) } }
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
