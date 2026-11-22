import Combine
import CryptoKit
import Foundation

@MainActor
public final class AppCoordinator: ObservableObject {
    @Published public var mode: ClientMode = .chat {
        didSet {
            if mode == .email, oldValue != .email {
                email.setListView("priority")
            }
        }
    }
    /// A schedule a Cockpit widget asked to open; the window shows the schedules browser.
    @Published public var requestedSchedule: UUID?
    public let chat: ChatViewModel
    public let email: EmailViewModel
    public let cockpit: CockpitViewModel
    private var connectionSubscription: AnyCancellable?
    private var emailAccountsSubscription: AnyCancellable?

    /// Coordinate authenticated Chat and Email navigation while retaining each mode's edits.
    public init(chat: ChatViewModel) {
        self.chat = chat
        email = EmailViewModel(makeAPIClient: { [weak chat] in chat?.currentAPIClient })
        cockpit = CockpitViewModel(makeClient: { [weak chat] in chat?.currentAPIClient })
        email.authenticationFailure = { [weak chat] error in chat?.reportConnectionError(error) }
        // Window-level controls read account capabilities through this coordinator.
        emailAccountsSubscription = email.$accounts.dropFirst().sink { [weak self] _ in
            self?.objectWillChange.send()
        }
        connectionSubscription = chat.$connectionGeneration.dropFirst().sink { [weak self] _ in
            self?.email.resetConnection()
            self?.cockpit.resetConnection()
        }
        cockpit.onOpen = { [weak self] destination in self?.open(destination) }
        chat.chatNotificationHandler = { [weak self] in self?.mode = .chat }
        chat.emailNotificationHandler = { [weak self] threadID, approvalID in
            guard let self else { return }
            if threadID == nil { self.email.clearSelection() }
            self.mode = .email
            if let threadID {
                await self.email.openThread(threadID, approvalID: approvalID)
            }
        }
    }

    /// A widget's `open` intent, already checked against the feeds it was delivered.
    func open(_ destination: CockpitDestination) {
        switch destination {
        case .session(let id):
            mode = .chat
            Task { await chat.openSharedSession(id) }
        case .emailThread(let id):
            mode = .email
            Task { await email.openThread(id) }
        case .schedule(let id):
            requestedSchedule = id
        case .approval(let id):
            Task { [weak self] in
                guard let self, let api = self.chat.currentAPIClient,
                      let approval = try? await api.getApproval(id)
                else { return }
                self.mode = .chat
                await self.chat.openSharedSession(approval.sessionID)
            }
        }
    }

    /// A Cockpit widget's Discuss in Chat: a new conversation whose composer
    /// names the widget, left for the owner to finish and send.
    public func discussWidget(title: String) {
        chat.newSession()
        chat.composerText = "About my Cockpit widget “\(title)”: "
        mode = .chat
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
    @Published private var inboxItems: [EmailThreadView] = []
    @Published private var archiveHiddenThreads: Set<UUID> = []
    /// Rows the latest refresh added, so the list can show where they landed.
    @Published public private(set) var arrivedThreadIDs: Set<UUID> = []
    @Published public private(set) var thread: EmailThreadView?
    @Published public private(set) var draft: EmailDraftView?
    @Published public private(set) var edits: [UUID: EmailDraftEdit] = [:]
    @Published public private(set) var conflict: EmailDraftView?
    @Published public private(set) var review: ApprovalView?
    @Published public private(set) var reviewDraft: EmailDraftView?
    @Published public private(set) var isLoading = false
    /// A list read is in flight, an in-place refresh included. It holds paging off
    /// without showing the initial loading indicator (ADR-0303).
    @Published public private(set) var isReadingList = false
    @Published public private(set) var isRefreshing = false
    @Published public private(set) var isLoadingThread = false
    @Published public private(set) var isSaving = false
    @Published public private(set) var isPerformingAction = false
    @Published public private(set) var isSelectingReply = false
    @Published public private(set) var isGeneratingDraft = false
    @Published public private(set) var unavailable = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var budgetPauseMessage: String?
    @Published public private(set) var budgetPauseDetails: [String] = []
    @Published public private(set) var budgetRetryAt: Date?
    @Published private var draftActionError: String?
    @Published private var threadReadError: String?
    @Published public private(set) var feedbackMessage: String?
    private let toasts: ToastCenter
    @Published public private(set) var styleExampleMessage: String?
    @Published public private(set) var feedbackID: UUID?
    /// The owner's standing choices on the open thread. Importance and reply need are separate
    /// judgments, so each keeps its own acknowledgment and its own Undo.
    @Published public private(set) var feedbackGiven: [EmailFeedbackKind: EmailFeedbackGiven] = [:]
    /// Teach Veetbot's unapplied input for the open thread. Held here so it outlasts the card,
    /// which Reply hides and the Mac panel's tabs replace; it ends with the selection.
    @Published public var feedbackDraft = EmailFeedbackDraft()
    @Published public private(set) var hasMore = false
    @Published public private(set) var selectedAccountID: String?
    @Published public private(set) var listView = "priority"
    private struct CachedMailList {
        let rows: [EmailThreadView]
        /// The numbers the list was read with, and what they held of the hidden conversations.
        let numbers: CountsRead?
        let cursor: String?
        let seenCursors: Set<String>
        let version: String?
        /// How deep the list was loaded (`loadedDepth`), which can be deeper than `rows` go.
        let depth: Int
    }
    // Only the two unsearched collections, for this connection and account filter.
    private var cachedMailLists: [String: CachedMailList] = [:]
    private var loadedListVersion: String?
    private var threadETag: String?
    private var projectionGeneration = UUID()

    private func invalidateMailSnapshots() {
        cachedMailLists = [:]
        loadedListVersion = nil
        threadETag = nil
        projectionGeneration = UUID()
    }
    /// One answer about the numbers Email shows: the server's sidebar counts and list total, and
    /// what that same answer says of the conversations a pending archive keeps off the list.
    /// The numbers are never stored, kept or brought back without it, and only `pairedNumbers`
    /// makes one.
    private struct CountsRead {
        var counts: EmailCollectionCounts?
        var total: Int?
        /// The rows of the response that carried these numbers, as it sent them: the same moment.
        var rows: [UUID: EmailThreadView]
        /// Hidden conversations those rows left out: each as its own status read found it after
        /// the numbers or, until they are read again, as the copy in hand when it was hidden.
        var others: [UUID: EmailThreadView] = [:]
        /// For each hide that has ended since: the numbers that held the conversation here and
        /// that it has left. They stay taken off until the numbers are read again.
        var settled: [UUID: Set<String>] = [:]

        func held(_ id: UUID) -> EmailThreadView? { rows[id] ?? others[id] }
    }
    @Published private var loadedCounts: CountsRead?

    /// The last confirmed counts, less each conversation a pending archive has hidden,
    /// so the totals match the rows on screen instead of vanishing until Gmail confirms.
    public var collectionCounts: EmailCollectionCounts? {
        guard let read = loadedCounts, let counts = read.counts else { return nil }
        let off = takenOff(read)
        let less = { (count: Int, view: String) in max(0, count - off.filter { $0.contains(view) }.count) }
        return EmailCollectionCounts(
            inbox: less(counts.inbox, EmailCollection.inbox.rawValue),
            needsYou: less(counts.needsYou, EmailCollection.needsYou.rawValue),
            handled: less(counts.handled, EmailCollection.handled.rawValue),
            priority: counts.priority.map { less($0, "priority") },
            suspectedSpam: counts.suspectedSpam.map { less($0, "suspected_spam") })
    }
    var listTotal: Int? {
        guard let read = loadedCounts, let total = read.total else { return nil }
        return max(0, total - takenOff(read).filter { $0.contains(Self.shownList) }.count)
    }

    /// What comes off a read of the numbers, one entry for each conversation so that none is
    /// taken off a number twice: the numbers that read held it in while a pending archive hides
    /// it, together with what is still owed for a hide of it that has ended.
    private func takenOff(_ read: CountsRead) -> [Set<String>] {
        let hidden = archiveHiddenThreads.filter { archiveErrors[$0] == nil }
        return hidden.union(read.settled.keys).map { id in
            (hidden.contains(id) ? holdings(read.held(id)) : []).union(read.settled[id] ?? [])
        }
    }

    private static let countedViews = [
        "priority", "suspected_spam", EmailCollection.inbox.rawValue, EmailCollection.needsYou.rawValue,
        EmailCollection.handled.rawValue,
    ]
    /// Stands for the total of the list on screen among the numbers that hold a conversation.
    private static let shownList = "list"

    /// The numbers that take in a conversation as the lists are now filtered: any of the five
    /// sidebar counts, and `shownList` for the total of the list on screen. None for a
    /// conversation in another account or outside the search.
    private func holdings(_ row: EmailThreadView?) -> Set<String> {
        guard let row, isInFilter(row) else { return [] }
        var held = Set(Self.countedViews.filter { Self.isCounted(row, in: $0) })
        if Self.isInTotal(row, of: queryListView) { held.insert(Self.shownList) }
        return held
    }

    /// Whether the lists as now filtered take in a conversation: it belongs to the chosen
    /// account and matches the search.
    private func isInFilter(_ row: EmailThreadView) -> Bool {
        (selectedAccountID == nil || row.accountID == selectedAccountID)
            && Self.matchesSearch(row, text: searchText)
    }

    /// The server's search: the text, case-folded, somewhere in the subject, senders and summary
    /// joined by spaces. Foundation's folding is the same full Unicode case folding.
    static func matchesSearch(_ row: EmailThreadView, text: String) -> Bool {
        guard !text.isEmpty else { return true }
        let searched = [row.subject, row.senders.joined(separator: " "), row.summary].joined(separator: " ")
        let folded = { (value: String) in Data(value.folding(options: .caseInsensitive, locale: nil).utf8) }
        return folded(searched).range(of: folded(text)) != nil
    }

    /// Whether the server counts a conversation in one of the five sidebar counts: only mail in
    /// the Inbox, by rules that mirror its own, count for count. The two thresholds are its
    /// Important relevance threshold and Inbox relevance floor (ADR-0329). Mail that also has
    /// messages in Spam is left out of Suspected spam only, as the server leaves it.
    private static func isCounted(_ row: EmailThreadView, in view: String) -> Bool {
        guard !row.isArchived else { return false }
        switch view {
        case "priority": return row.priority >= 0.7 && !row.isHandled
        case EmailCollection.inbox.rawValue: return row.priority >= 0.1
        case EmailCollection.needsYou.rawValue: return row.needsReply && !row.isHandled
        case EmailCollection.handled.rawValue: return row.isHandled
        case "suspected_spam": return row.isSuspectedSpam && row.spamCleared != true && !row.isInSpam
        default: return false
        }
    }

    /// Whether the server's total for a list takes in a conversation. Other mail is everything
    /// that is not Important, archived mail included; a search of all mail is all that matches.
    /// Every other list is totalled as it is counted.
    private static func isInTotal(_ row: EmailThreadView, of view: String) -> Bool {
        switch view {
        case "all": return true
        case "other": return !isCounted(row, in: "priority")
        default: return isCounted(row, in: view)
        }
    }

    /// What a list request answered: the rows to show, gathered over as many pages as it took,
    /// and `counted`, the rows of the one response whose numbers are kept.
    private struct ListAnswer {
        let page: EmailThreadPage
        let counted: [EmailThreadView]
    }

    /// Reads each conversation a pending archive hides that the numbers' own rows did not
    /// return. Those numbers may still include it (another collection, another page, a row a new
    /// arrival pushed down), and its absence says nothing either way: only its own status shows
    /// whether it is still in the Inbox and which numbers hold it. The read also resumes
    /// following an archive nobody was. The statuses belong to the list read that asks for them.
    /// Once that read is no longer current (the connection or the visit changed, or another list
    /// read replaced it) none of the rest is sent: each would name a conversation of the old
    /// connection to whatever has taken its place.
    private func hiddenArchiveStatuses(
        missingFrom rows: [EmailThreadView], while isCurrent: @escaping () -> Bool
    ) async -> [EmailThreadView] {
        let returned = Set(rows.map(\.id))
        let missing = archiveHiddenThreads.filter { !returned.contains($0) && archiveErrors[$0] == nil }
        var statuses: [EmailThreadView] = []
        for id in missing.sorted(by: { $0.uuidString < $1.uuidString }) {
            guard isCurrent() else { break }
            if let value = await readArchiveStatus(id, forListRead: isCurrent) { statuses.append(value) }
        }
        return statuses
    }

    /// The only way numbers reach the screen. They come with a list answer, and each conversation
    /// a pending archive hides is checked against that answer: a row in the response that carried
    /// the numbers describes the same moment, and any other conversation is read on its own
    /// afterwards. Those are two requests, and the numbers are not read again when a
    /// conversation changes between them. It is counted as its status says until the next
    /// refresh, and a count may be off by one for that long.
    private func pairedNumbers(_ answer: ListAnswer, isCurrent: @escaping () -> Bool) async -> CountsRead? {
        let statuses = await hiddenArchiveStatuses(missingFrom: answer.counted, while: isCurrent)
        guard isCurrent() else { return nil }
        let byID = { (rows: [EmailThreadView]) in
            Dictionary(rows.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
        }
        return CountsRead(
            counts: answer.page.collectionCounts, total: answer.page.totalCount, rows: byID(answer.counted),
            others: byID(statuses.filter { archiveHiddenThreads.contains($0.id) }))
    }

    /// A conversation has just been hidden. Numbers on screen that already hold it, by the row
    /// that arrived with them or a status read after them, take it off as that says. Otherwise
    /// the copy in hand stands in, and the numbers are read again at once with its status.
    private func noteHidden(_ copy: EmailThreadView) {
        guard loadedCounts != nil, loadedCounts?.held(copy.id) == nil else { return }
        loadedCounts?.others[copy.id] = copy
        readCountsAgain()
    }

    /// Whether Gmail has confirmed the archive that hid a conversation and the list on screen
    /// no longer holds it.
    private func hasLeftTheList(_ row: EmailThreadView) -> Bool {
        row.archiveOperation?.status == "completed" && !Self.staysListed(row, in: queryListView)
    }

    /// The rows of a page as they are shown. Mailbox state learnt since the page was asked for
    /// stays on top of it, and a conversation whose archive Gmail confirmed meanwhile is not put
    /// back in a list that no longer holds it, although the older page still returns it.
    private func shown(_ rows: [EmailThreadView], since versions: [UUID: UUID]) -> [EmailThreadView] {
        rows.compactMap { row in
            let known = preservingArchiveState(row, since: versions)
            return archiveVersions[row.id] != versions[row.id] && hasLeftTheList(known) ? nil : known
        }
    }

    /// Ends a hide, with the conversation as the server now holds it. An archive Gmail confirmed
    /// first leaves the list if the list no longer holds it, so lifting the hide cannot show it
    /// again, whether or not the list can be read afterwards. The numbers on screen keep taken
    /// off whatever they held of the conversation that it has left, until they are read again.
    /// A confirmed archive so stays out of the Inbox and its counts, and comes back in Other
    /// mail and search, which still list it. A failed archive comes back as it was.
    private func liftHide(_ value: EmailThreadView) {
        guard archiveHiddenThreads.remove(value.id) != nil else { return }
        hiddenAccounts[value.id] = nil
        // A kept list's numbers were adjusted for the conversations hidden when it was read.
        cachedMailLists = [:]
        if hasLeftTheList(value) { inboxItems.removeAll { $0.id == value.id } }
        guard let read = loadedCounts else { return }
        loadedCounts?.settled[value.id] = holdings(read.held(value.id)).subtracting(holdings(value))
    }

    /// The account of each conversation a pending archive hides, noted when it was hidden. It
    /// outlasts the rows and numbers a refusal clears, so a second archive of a refused account
    /// is still known to be that account's.
    private var hiddenAccounts: [UUID: String] = [:]

    /// The message of a refusal that is still waiting for the list. From a refusal until a list
    /// read asked after it has answered, nothing has confirmed which accounts can still be read.
    ///
    /// A refused conversation follows one rule, whichever read was refused:
    /// 1. Its account is whatever any part of the model knows of it (`knownAccount`).
    /// 2. Everything of that account is retired at once: its rows, the kept lists and the
    ///    numbers, its archives with their requests, polls and status reads, and the reader if
    ///    it shows one of its conversations (`forgetDeniedAccount`).
    /// 3. An answer for something retired changes nothing and reports nothing.
    /// 4. The list read that answers the refusal takes the list's request in that same step,
    ///    which retires every list read in flight: those were asked before the refusal and are
    ///    never shown. The latest list read asked after the refusal decides, by the way it ends:
    ///    - It answers: nothing more is cleared, the visit goes on, and the refusal's message
    ///      is shown with the list.
    ///    - It is refused, or fails in any way but the next: everything is cleared and the
    ///      refusal's message is shown. If Email is on screen its visit goes on, so Refresh
    ///      still works.
    ///    - It needs the owner to sign in again: that takes its usual route, which clears
    ///      everything, ends the visit and tells the connection.
    ///    - Another list read replaced it: nothing. The newer read decides.
    ///    - It cannot start because there is no connection to ask through: as when it fails.
    ///    - The owner leaves Email first: everything is cleared and the visit ends.
    /// 5. A caller that no longer counts is none of these. A list read asked from a cancelled
    ///    task, or for a visit that has ended, asks nothing, replaces nothing and clears
    ///    nothing, and the read that is out still decides. A refusal is never left waiting on
    ///    such a caller: ending the visit or the connection clears it at once, and while Email
    ///    stays on screen each regular refresh asks the list first for as long as it waits.
    /// It is published because `canLoadMore` depends on it.
    @Published private var refusalAwaitingList: String?

    /// The account of a conversation, from every place the model can know it: the note made
    /// when it was hidden, its row, the reader, the numbers, its archive request, and the
    /// read of it that is still out.
    private func knownAccount(of id: UUID) -> String? {
        if let noted = hiddenAccounts[id] { return noted }
        let copy = inboxItems.first { $0.id == id } ?? (thread?.id == id ? thread : nil)
            ?? loadedCounts?.held(id) ?? archiveRequests[id]?.thread
        return copy?.accountID ?? (threadRead?.id == id ? threadRead?.account : nil)
    }

    /// The server refused to show this conversation, so its account can no longer be read. What
    /// is held from that account goes at once.
    private func forgetDeniedAccount(holding id: UUID) {
        let account = knownAccount(of: id)
        let gone = { (row: EmailThreadView) in row.id == id || row.accountID == account }
        if let selected = selectedThreadID {
            let selectedAccount = knownAccount(of: selected)
            let refused = selected == id || (account != nil && selectedAccount == account)
            // The owner's unsaved words go only with a conversation known to be the refused
            // account's. Any other edit waits with its draft.
            if refused, let draft { edits[draft.id] = nil }
            // The reader closes for that conversation, and also when nothing says whose the
            // open conversation or the refused one is. An answer still on its way was asked
            // for before the refusal; its request is retired with the selection.
            if refused || account == nil || selectedAccount == nil { clearSelection() }
        }
        // Every archive known to be that account's is retired with this one: those hidden,
        // those still waiting to be admitted, and any status read still out for one of them.
        // None of them can come back as another refusal with nothing left to say whose it was.
        let retiring = Set(hiddenAccounts.filter { $0.value == account }.keys)
            .union(archiveRequests.filter { $0.value.thread.accountID == account }.keys).union([id])
        for retired in retiring {
            archiveHiddenThreads.remove(retired)
            hiddenAccounts[retired] = nil
            archiveRequests[retired] = nil
            archiveErrors[retired] = nil
            archiveReadErrors.remove(retired)
            archiveStates[retired] = nil
            archiveVersions[retired] = UUID()
            archiveTasks.removeValue(forKey: retired)?.task.cancel()
        }
        invalidateMailSnapshots()
        EmailConversationCache.shared.removeAll()
        loadedCounts = nil
        inboxItems.removeAll(where: gone)
    }

    /// A list read asked after a refusal has answered, so what it returned may be read.
    private func listAnsweredAfterRefusal() {
        guard let message = refusalAwaitingList else { return }
        refusalAwaitingList = nil
        errorMessage = message
    }

    /// Nothing confirmed what may still be read after a refusal, so everything is cleared, as
    /// after every refusal, and every request still out is retired. Email may still be on
    /// screen: its visit then goes on, so Refresh works and the regular refreshes resume.
    /// Nothing is read at once, and the refusal's message stays until the next read.
    private func clearAfterRefusal(leaving: Bool = false) {
        let message = refusalAwaitingList
        let onScreen = active && !leaving
        resetConnection()
        errorMessage = message
        if onScreen {
            active = true
            startVisit(readingAtOnce: false)
        }
    }

    /// The numbers on screen were read before a conversation was hidden, without its status and
    /// perhaps before the copy of it in hand. Asking for them again reads the two together.
    private func readCountsAgain() {
        guard loadedCounts != nil else { return }
        let foreground = active ? activation : nil
        let query = listRequest
        Task { [weak self] in
            guard let self, self.listRequest == query else { return }
            await self.reload(preserveOrder: true, activation: foreground)
        }
    }
    @Published private(set) var pagingFailed = false
    /// The visible Mac row, independent of the conversation open in the reader.
    var scrollAnchor: UUID?
    let restoreScrollAnchor = PassthroughSubject<UUID, Never>()
    /// Every list on every device loads 100 rows a page and scrolls (ADR-0303, ADR-0326).
    private let pageSize = 100
    /// How deep the list on screen is loaded: the number of rows the server has returned for
    /// it, counted as they arrive. A list read sets it and More mail adds to it. The rows on
    /// screen are not the measure, because rows leave the screen ahead of the read that
    /// follows: a confirmed archive, a refused account, an opened conversation that has left
    /// the list, an exclusion. Every list read that keeps the rows on screen reads at least
    /// this deep, and at least as deep as those rows go, whichever caller asks: the read asked
    /// for the row that left, a regular refresh that replaces it, Try again. Nothing else
    /// holds a depth, and no caller passes one. It belongs to the list on screen: with another
    /// account, collection or search it starts again from nothing, and a list kept for coming
    /// back to keeps its depth with its rows and cursor.
    private var loadedDepth = 0

    /// Do not search later pages for a row already confirmed absent from the current list.
    private var eligibleScrollAnchor: UUID? {
        guard let id = scrollAnchor else { return nil }
        if archiveHiddenThreads.contains(id), archiveErrors[id] == nil { return nil }
        var known = inboxItems.first(where: { $0.id == id })
        if let detail = thread, detail.id == id, detail.revision >= (known?.revision ?? 0) {
            known = detail
        }
        guard let row = known else { return id }
        return Self.staysListed(row, in: queryListView) ? id : nil
    }

    /// Whether a row's known state keeps it in a list (ADR-0231, ADR-0303): Inbox keeps handled
    /// mail until it is archived, Needs you and Important drop it once handled, Handled keeps only
    /// it, and nothing known removes a row from Other mail or search.
    static func staysListed(_ row: EmailThreadView, in view: String) -> Bool {
        let inInbox = !row.isArchived && !row.isInSpam
        switch view {
        case "suspected_spam": return inInbox && row.isSuspectedSpam && row.spamCleared != true
        case "priority": return inInbox && !row.isHandled
        case EmailCollection.inbox.rawValue: return inInbox
        case EmailCollection.needsYou.rawValue: return inInbox && !row.isHandled && row.needsReply
        case EmailCollection.handled.rawValue: return inInbox && row.isHandled
        default: return true
        }
    }

    private var queryListView: String {
        searchText.isEmpty || EmailCollection(rawValue: listView) != nil || listView == "priority" || listView == "suspected_spam" ? listView : "all"
    }
    public var collection: EmailCollection { EmailCollection(rawValue: listView) ?? .inbox }

    public func setCollection(_ value: EmailCollection) {
        setListView(value.rawValue)
    }
    @Published public private(set) var searchText = ""
    @Published public private(set) var selectedThreadID: UUID?
    @Published public private(set) var learning: EmailLearningState?
    @Published public private(set) var revisions: [EmailDraftView] = []
    @Published public private(set) var archiveErrors: [UUID: String] = [:]
    @Published private var attachmentSaves: [EmailAttachmentKey: EmailAttachmentSaveState] = [:]
    /// A press whose outcome is still unknown, so Retry recovers it instead of saving twice.
    private var attachmentRequests: [EmailAttachmentKey: (idempotencyKey: String, operationID: UUID?)] = [:]
    @Published private var archiveSubmitting: Set<UUID> = []
    @Published private var archiveUnsupportedAccounts: Set<String> = []
    @Published private var archiveUnavailableThreads: Set<UUID> = []
    @Published private var spamUnsupportedAccounts: Set<String> = []
    /// A failed Not spam on a flagged conversation, shown beside the flag.
    @Published public private(set) var spamFlagErrors: [UUID: String] = [:]
    @Published private var spamFlagSubmitting: Set<UUID> = []

    public var authenticationFailure: ((Error) -> Void)?
    /// The census is its own surface and its own state; Email mode only opens it.
    public let subscriptions: EmailSubscriptionsViewModel
    private let makeAPIClient: () -> VeetbotAPIClient?
    private let refreshNanoseconds: UInt64
    /// The wait between reads of a draft or send run the owner is watching.
    private let pollNanoseconds: UInt64
    private let now: () -> Date
    private let statusBackoff: @Sendable (UInt64) async throws -> Void
    private var active = false
    private var activation = UUID()
    private var generation = UUID()
    /// The list's request names the one list read whose answer may be shown. What a list
    /// answer brings is published together and only under it: the rows, the numbers
    /// (`loadedCounts`), the cursor (`nextCursor`, `seenCursors`, `hasMore`), the depth
    /// (`loadedDepth`), the version, the kept copy of the list, and the answer to a refusal
    /// that waits for the list (`refusalAwaitingList`).
    /// 1. A list read takes the request as it starts. A refusal takes it for the read it asks
    ///    for, in the same step as the refusal. Taking it retires every list read and every
    ///    page still out: whatever they answer is dropped.
    /// 2. Only the read the request was taken for publishes under it, and only while it is
    ///    still the list's request. A change of account, collection or search, and an opened
    ///    conversation that changed the list, put a request that no read holds in its place.
    /// 3. More mail takes no request. It adds a page to the list the last finished read has
    ///    shown, under that read's request. It starts only when no list read is out and no
    ///    refusal waits for the list: a waiting refusal has taken the request for a read that
    ///    may not have started yet, and nothing else may publish under that. A page is shown
    ///    only if the request has not changed since the page was asked for.
    private var listRequest = UUID()
    private var selectionRequest = UUID()
    /// The conversation being read for the reader, with its account when a row or the reader
    /// already told it. A refusal of that account retires the read before it can answer.
    private var threadRead: (id: UUID, activation: UUID?, account: String?)?
    private var nextCursor: String?
    private var seenCursors: Set<String> = []
    private var refreshTask: Task<Void, Never>?
    private var operationTask: Task<Void, Never>?
    private var autosaveTask: Task<Void, Never>?
    /// The save currently on the wire; a later save or Review & Send waits for it.
    private var inflightSave: Task<Bool, Never>?
    private var lastSavedDraft: EmailDraftView?
    /// A send proposal that outlasted the local wait; the next read of its draft opens the review.
    private var pendingReviewRunID: UUID?
    /// The poll following a one-press send; Undo cancels it.
    private var sendWatchTask: Task<Void, Never>?
    /// Sends the owner took back, so their cancellation is not reported as a failure.
    private var undoneRunIDs: Set<UUID> = []
    private var searchTask: Task<Void, Never>?
    private var refreshKey: String?
    private var refreshFailure: String?
    /// Per-account freshness observed before the recorded failure, absent while never read.
    private var syncedBaseline: [String: Date?] = [:]
    private var saveKeys: [UUID: (EmailDraftEdit, String)] = [:]
    private var sendKeys: [UUID: (Int, String)] = [:]
    private struct ArchiveRequest {
        let thread: EmailThreadView
        let archived: Bool
        let key: String
        /// Through Spam: `archived` reports spam and restoring is Not spam.
        let spam: Bool
        var operationID: UUID?
    }
    private var archiveRequests: [UUID: ArchiveRequest] = [:]
    /// The poll that follows one conversation's pending archive. The token is its own: a poll
    /// that ends removes the entry only while the entry is still that poll's, since a newer
    /// archive of the same conversation may have started a poll under the same key.
    private struct ArchivePoll {
        let token: UUID
        let task: Task<Void, Never>
    }
    private var archiveTasks: [UUID: ArchivePoll] = [:]
    private struct ArchiveMailboxState {
        var inInbox: Bool?
        var operation: EmailArchiveOperation?
        var dismissedRevision: Int?
        var revision: Int
        var inSpam: Bool? = nil
        var isHandled: Bool { dismissedRevision == revision }
    }
    private var archiveVersions: [UUID: UUID] = [:]
    private var archiveStates: [UUID: ArchiveMailboxState] = [:]
    private var archiveReadErrors: Set<UUID> = []

    /// Inject the API factory, refresh cadence, clock used for foreground budget pauses,
    /// and the wait before each refresh-status read.
    public init(
        makeAPIClient: @escaping () -> VeetbotAPIClient?,
        refreshNanoseconds: UInt64 = 60_000_000_000,
        pollNanoseconds: UInt64 = 1_000_000_000,
        now: @escaping () -> Date = Date.init,
        statusBackoff: @escaping @Sendable (UInt64) async throws -> Void = { try await Task.sleep(nanoseconds: $0) },
        toasts: ToastCenter? = nil
    ) {
        self.toasts = toasts ?? .shared
        self.makeAPIClient = makeAPIClient
        self.refreshNanoseconds = refreshNanoseconds
        self.pollNanoseconds = pollNanoseconds
        self.now = now
        self.statusBackoff = statusBackoff
        subscriptions = EmailSubscriptionsViewModel(
            makeAPIClient: makeAPIClient, statusBackoff: statusBackoff)
    }

    /// A surface the owner can reach only when an account advertises support for it.
    public var unsubscribeAvailable: Bool { accounts.contains { $0.unsubscribeSupported == true } }

    /// A thread action needs its own account's support, never another account's.
    public func unsubscribeSupported(for accountID: String) -> Bool {
        accounts.first { $0.id == accountID }?.unsubscribeSupported == true
    }

    /// The account's label and address, or its identifier before accounts load.
    public func accountDescription(for accountID: String) -> String {
        guard let account = accounts.first(where: { $0.id == accountID }) else { return accountID }
        return account.emailAddress.map { "\(account.label) · \($0)" } ?? account.label
    }

    /// Report spam and Not spam need this account's advertised support; Archive is never substituted.
    public func spamSupported(for accountID: String) -> Bool {
        !unavailable && !spamUnsupportedAccounts.contains(accountID)
            && accounts.first { $0.id == accountID }.map { $0.spamSupported == true && $0.writeServerID != nil } == true
    }

    deinit {
        refreshTask?.cancel()
        operationTask?.cancel()
        autosaveTask?.cancel()
        searchTask?.cancel()
        archiveTasks.values.forEach { $0.task.cancel() }
    }

    /// The rows as the list shows them: newest first by each conversation's latest
    /// activity, ties in the server's order. A pending archive stays in its place for
    /// rollback without presenting as archived.
    public var items: [EmailThreadView] {
        inboxItems.enumerated()
            .filter { !archiveHiddenThreads.contains($0.element.id) || archiveErrors[$0.element.id] != nil }
            .sorted { lhs, rhs in
                lhs.element.updatedAt != rhs.element.updatedAt
                    ? lhs.element.updatedAt > rhs.element.updatedAt : lhs.offset < rhs.offset
            }
            .map(\.element)
    }

    /// The row has shown where it landed; a later refresh may mark it again.
    public func acknowledgeArrival(_ id: UUID) {
        arrivedThreadIDs.remove(id)
    }

    public var currentEdit: EmailDraftEdit? { draft.flatMap { edits[$0.id] } }
    /// Keeps an unresolved owner-action failure visible even when a later thread read fails or recovers.
    public var draftError: String? { draftActionError ?? threadReadError }
    /// An owner action that failed, so a refusal can be shown beside the control that
    /// caused it. Reporting it only above the conversation leaves the action looking
    /// inert, because the button that triggered it is far below.
    public var draftActionMessage: String? {
        draftActionError ?? (draft?.isManual == true ? nil : thread?.preparationIssue)
    }
    /// A failed mail read, reported with the conversation it could not refresh.
    public var threadReadMessage: String? { threadReadError }
    /// Says where held edits are while a stale draft waits for the owner's review of the new mail.
    static let staleEditMessage = "Your edits are kept on this device until you confirm you've reviewed the updated thread."
    /// A send the server ended without dispatch, in the owner's terms rather than a closed sheet.
    public static let sendEndedByNewMailMessage =
        "New mail arrived before this reply was sent, so nothing went out. Read it above, then send again."
    static let sendNotMadeMessage =
        "Nothing was sent: Gmail couldn't be checked, or the reply changed. Your draft is kept; send again."
    static let sendFailedMessage = "This reply wasn't sent. Your draft is kept; try again."
    /// Shown when a proposal outlasts the local wait; a later refresh opens the review itself.
    public static let stillPreparingSendMessage =
        "Still preparing to send. The review will open here as soon as it's ready."
    /// Progress on a send proposal the owner is waiting on; not an error, so it is shown quietly.
    @Published public private(set) var sendProgressMessage: String?
    /// True from Review & Send or Send until the proposal or resolution settles; only this locks the editor.
    @Published public private(set) var isPreparingSend = false

    /// The newest message that arrived after the draft was last written: the reason it is stale.
    public var newMailSinceDraft: EmailMessageView? {
        guard let thread, let draft, draft.stale, let messages = thread.messages, !messages.isEmpty else { return nil }
        let later = messages.filter { $0.sentAt > draft.updatedAt }
        return (later.isEmpty ? messages : later).max { $0.sentAt < $1.sentAt }
    }

    /// Names who wrote and quotes the start of their message, so the owner decides from
    /// the mail itself rather than from a revision number.
    public func staleMailMessage(locale: Locale = .current) -> String? {
        guard let message = newMailSinceDraft else { return nil }
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = locale
        formatter.unitsStyle = .full
        let when = formatter.localizedString(for: message.sentAt, relativeTo: now())
        return "\(Self.displayName(message.sender)) wrote \(when): “\(Self.excerpt(message.body))” "
            + "Read it above, then confirm your reply still fits."
    }

    /// "Dana Lee <dana@example.test>" reads as Dana Lee; a bare address stays an address.
    static func displayName(_ sender: String) -> String {
        guard let open = sender.firstIndex(of: "<") else {
            return sender.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        let name = sender[..<open].trimmingCharacters(in: .whitespacesAndNewlines.union(CharacterSet(charactersIn: "\"'")))
        return name.isEmpty ? sender.trimmingCharacters(in: .whitespacesAndNewlines) : name
    }

    /// The first sentence or so of the new text, before any quoted earlier mail.
    static func excerpt(_ body: String, limit: Int = 140) -> String {
        let collapsed = body.split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .first { !$0.isEmpty && !$0.hasPrefix(">") } ?? ""
        guard collapsed.count > limit else { return collapsed }
        return String(collapsed.prefix(limit)).trimmingCharacters(in: .whitespaces) + "…"
    }

    public var canReview: Bool {
        guard let draft, let edit = currentEdit, !edit.body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return false }
        return draft.canEdit && !draft.stale && conflict == nil && !isPerformingAction && !isSelectingReply && !isPreparingSend
    }

    /// Starts foreground refreshes for a new visit or invalidates reads when Email is hidden.
    public func setActive(_ value: Bool) {
        guard active != value else { return }
        // Leaving Email leaves no list read to answer a refusal that is still waiting for one.
        if !value, refusalAwaitingList != nil {
            clearAfterRefusal(leaving: true)
            return
        }
        active = value
        activation = UUID()
        isRefreshing = false
        refreshTask?.cancel()
        operationTask?.cancel()
        archiveTasks.values.forEach { $0.task.cancel() }
        archiveTasks = [:]
        guard value else { return }
        startVisit(readingAtOnce: true)
    }

    /// Runs the refreshes of the visit that has just begun. A visit that goes on after Email
    /// was cleared reads nothing at once: it waits for Refresh or for the next regular refresh.
    private func startVisit(readingAtOnce: Bool) {
        let foreground = activation
        let connection = generation
        refreshTask = Task { [weak self] in
            guard let self else { return }
            if readingAtOnce {
                // Rows already on screen stay and refresh in place; only an empty list loads from nothing.
                await self.reload(preserveOrder: !self.inboxItems.isEmpty, activation: foreground)
            } else {
                do { try await Task.sleep(nanoseconds: self.refreshNanoseconds) } catch { return }
            }
            while self.acceptsRead(connection: connection, activation: foreground) {
                // A refusal still waiting when a regular refresh comes round is asked about
                // first. A refresh reads the list only once the server has admitted it, and
                // the refusal does not wait on that.
                if self.refusalAwaitingList != nil {
                    await self.reload(preserveOrder: true, activation: foreground)
                }
                await self.refresh(activation: foreground)
                do { try await Task.sleep(nanoseconds: self.refreshNanoseconds) }
                catch { return }
            }
        }
    }

    /// Cancels obsolete work and removes all mail, local edits and errors from the old connection.
    public func resetConnection() {
        invalidateMailSnapshots()
        generation = UUID()
        isResettingLearning = false
        activation = UUID()
        selectionRequest = UUID()
        threadRead = nil
        refreshTask?.cancel()
        operationTask?.cancel()
        autosaveTask?.cancel()
        inflightSave = nil
        lastSavedDraft = nil
        pendingReviewRunID = nil
        sendProgressMessage = nil
        isPreparingSend = false
        sendWatchTask?.cancel()
        sendWatchTask = nil
        searchTask?.cancel()
        archiveTasks.values.forEach { $0.task.cancel() }
        archiveTasks = [:]
        archiveRequests = [:]
        archiveErrors = [:]
        attachmentSaves = [:]
        // Kept: signing in again resets this for the same account, and a save the server
        // admitted must not become a second copy. A finished save removes its own entry.
        archiveSubmitting = []
        archiveHiddenThreads = []
        hiddenAccounts = [:]
        refusalAwaitingList = nil
        archiveUnsupportedAccounts = []
        archiveUnavailableThreads = []
        archiveReadErrors = []
        archiveVersions = [:]
        archiveStates = [:]
        spamUnsupportedAccounts = []
        spamFlagErrors = [:]
        spamFlagSubmitting = []
        subscriptions.resetConnection()
        EmailConversationCache.shared.removeAll()
        active = false
        accounts = []
        inboxItems = []
        arrivedThreadIDs = []
        thread = nil
        draft = nil
        edits = [:]
        conflict = nil
        review = nil
        reviewDraft = nil
        selectedThreadID = nil
        selectedAccountID = nil
        resetListQuery()
        listView = "priority"
        searchText = ""
        unavailable = false
        errorMessage = nil
        draftActionError = nil
        threadReadError = nil
        feedbackMessage = nil
        styleExampleMessage = nil
        feedbackID = nil
        feedbackGiven = [:]
        feedbackDraft = EmailFeedbackDraft()
        learning = nil
        revisions = []
        refreshKey = nil
        refreshFailure = nil
        syncedBaseline = [:]
        budgetPauseMessage = nil
        budgetPauseDetails = []
        budgetRetryAt = nil
        saveKeys = [:]
        sendKeys = [:]
        isLoading = false
        isReadingList = false
        isRefreshing = false
        isLoadingThread = false
        isSaving = false
        isPerformingAction = false
        isSelectingReply = false
        isGeneratingDraft = false
    }

    /// Clears thread selection and reloads the chosen account within the current foreground visit.
    public func setAccount(_ id: String?) {
        guard id != selectedAccountID else { return }
        invalidateMailSnapshots()
        inboxItems = []
        selectedAccountID = id
        resetListQuery()
        clearSelection()
        let foreground = active ? activation : nil
        Task { await reload(activation: foreground) }
    }

    /// Reloads the chosen inbox view without allowing results from an obsolete foreground visit.
    public func setListView(_ value: String) {
        guard value != listView else { return }
        listView = value
        resetListQuery()
        let cached = searchText.isEmpty ? cachedMailLists[value] : nil
        inboxItems = cached?.rows ?? []
        // A kept list comes back with its numbers and what they held of the hidden conversations
        // as one value. What a later page learned belongs to that page's numbers.
        loadedCounts = cached?.numbers
        loadedListVersion = cached?.version
        nextCursor = cached?.cursor
        seenCursors = cached?.seenCursors ?? []
        loadedDepth = cached?.depth ?? 0
        hasMore = nextCursor != nil
        arrivedThreadIDs = []
        isLoading = false
        let foreground = active ? activation : nil
        let query = listRequest
        Task {
            guard self.listRequest == query else { return }
            await reload(preserveOrder: cached != nil, activation: foreground)
        }
    }

    /// Debounces search changes and binds the eventual reload to the visit that scheduled it.
    public func setSearchText(_ text: String) {
        invalidateMailSnapshots()
        inboxItems = []
        searchText = text
        resetListQuery()
        searchTask?.cancel()
        let foreground = active ? activation : nil
        searchTask = Task { [weak self] in
            do { try await Task.sleep(nanoseconds: 300_000_000) } catch { return }
            guard let self, !Task.isCancelled else { return }
            await self.reload(activation: foreground)
        }
    }

    /// Invalidates pending page reads and clears metadata belonging to the previous query.
    private func resetListQuery() {
        loadedListVersion = nil
        listRequest = UUID()
        // The abandoned read can no longer clear its own flag.
        isReadingList = false
        loadedCounts = nil
        scrollAnchor = nil
        nextCursor = nil
        loadedDepth = 0
        hasMore = false
        pagingFailed = false
    }

    /// Retains only the two bounded, unsearched mailbox collections.
    private func cacheMailList() {
        guard searchText.isEmpty, listView == "inbox" || listView == "priority" else { return }
        guard inboxItems.count <= 1_000 else {
            cachedMailLists[listView] = nil
            return
        }
        cachedMailLists[listView] = CachedMailList(rows: inboxItems, numbers: loadedCounts,
            cursor: nextCursor, seenCursors: seenCursors, version: loadedListVersion, depth: loadedDepth)
    }

    /// Allows explicit inactive reads while binding visible inbox work to its current foreground visit.
    public func reload(preserveOrder: Bool = false) async {
        await reload(preserveOrder: preserveOrder, activation: active ? activation : nil)
    }

    /// Retains the initiating visit through every asynchronous projection read and error path.
    /// A refusal takes the list's request for the read it asks for, and passes it as `claimed`.
    private func reload(preserveOrder: Bool = false, activation foreground: UUID?, as claimed: UUID? = nil) async {
        let connection = generation
        let projection = projectionGeneration
        let mailboxVersions = archiveVersions
        // A caller that no longer counts (its task was cancelled, or its visit has ended) asks
        // nothing, and says nothing about whether the list can be read. Nothing changes for it,
        // and whatever list read is out still decides for a refusal that is waiting.
        guard acceptsRead(connection: connection, activation: foreground) else { return }
        // The request taken for this read has since been taken over by another list read.
        // That newer read decides, and this one asks nothing.
        if let claimed, listRequest != claimed { return }
        guard let api = makeAPIClient() else {
            // A refusal is waiting for the list, and there is no connection to ask it through.
            // That stops every other list read too, so none is left to answer.
            if refusalAwaitingList != nil { clearAfterRefusal() }
            return
        }
        let requestID = claimed ?? UUID()
        listRequest = requestID
        if !preserveOrder {
            loadedListVersion = nil
            loadedCounts = nil
        }
        let visibleCount = preserveOrder ? max(pageSize, inboxItems.count, loadedDepth) : pageSize
        // An unchanged page says nothing new about a hidden conversation it does not hold.
        let listed = Set(inboxItems.map(\.id))
        let reusable = preserveOrder && archiveHiddenThreads.isSubset(of: listed)
        isLoading = !preserveOrder
        isReadingList = true
        if !preserveOrder {
            inboxItems = []
            nextCursor = nil
            hasMore = false
        }
        errorMessage = refreshFailure
        var listFinished = false
        defer {
            if listRequest == requestID && !listFinished { isLoading = false; isReadingList = false }
        }
        do {
            async let accountPage = api.emailAccounts()
            let answer = try await readInbox(api: api,
                accountID: selectedAccountID, view: queryListView,
                text: searchText, count: visibleCount, requestID: requestID, anchor: preserveOrder ? eligibleScrollAnchor : nil,
                connection: connection, activation: foreground,
                knownVersion: reusable ? loadedListVersion : nil
            )
            guard acceptsRead(connection: connection, activation: foreground), listRequest == requestID else { return }
            unavailable = false
            if let read = answer {
                guard let numbers = await pairedNumbers(read, isCurrent: {
                    self.acceptsRead(connection: connection, activation: foreground) && self.listRequest == requestID
                }) else { return }
                let page = read.page
                let loadedThreads = shown(page.items, since: mailboxVersions)
                loadedCounts = numbers
                loadedDepth = page.items.count
                pagingFailed = false
                seenCursors = []
                nextCursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
                hasMore = nextCursor != nil
                if preserveOrder && !inboxItems.isEmpty {
                    let anchor = scrollAnchor
                    let previousIndex = anchor.flatMap { id in items.firstIndex { $0.id == id } }
                    let oldIDs = Set(inboxItems.map(\.id))
                    let additions = loadedThreads.filter { !oldIDs.contains($0.id) }
                    let current = Dictionary(loadedThreads.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
                    // Keep surviving rows and add arrivals; `items` places them all by recency.
                    inboxItems = inboxItems.compactMap { current[$0.id] } + additions
                    arrivedThreadIDs = Set(additions.map(\.id))
                    if let anchor, let previousIndex, let index = items.firstIndex(where: { $0.id == anchor }),
                       index != previousIndex {
                        restoreScrollAnchor.send(anchor)
                    }
                } else {
                    inboxItems = loadedThreads
                    arrivedThreadIDs = []
                }
                for value in loadedThreads { observeArchive(value) }
                loadedListVersion = projectionGeneration == projection ? page.snapshotVersion : nil
                cacheMailList()
            } else {
                pagingFailed = false
                // A new foreground visit cancelled the old watchers even when
                // the server's mailbox snapshot has not changed.
                for value in inboxItems { observeArchive(value) }
            }
            listAnsweredAfterRefusal()
            isLoading = false
            isReadingList = false
            listFinished = true
            let loadedAccounts = try await accountPage
            guard acceptsRead(connection: connection, activation: foreground), listRequest == requestID else { return }
            accounts = loadedAccounts.items
            recordAccountFreshness(loadedAccounts.items)
        } catch {
            guard acceptsRead(connection: connection, activation: foreground), listRequest == requestID else { return }
            // The list read that was to say what still reads after a refusal has failed.
            // Needing to sign in again takes its usual route, which clears everything too.
            if refusalAwaitingList != nil {
                if case HTTPTransportError.reauthenticationRequired = error { report(error, readAccess: true) }
                else { clearAfterRefusal() }
                return
            }
            if isUnavailable(error) {
                invalidateMailSnapshots()
                resetListQuery()
                isLoading = false
                unavailable = true
                inboxItems = []
                accounts = []
            } else { report(error, readAccess: true) }
        }
    }

    /// How many pages past the visible rows a refresh searches for a displaced visible thread
    /// before keeping its rows without restoring the position (ADR-0303).
    static let anchorSearchPages = 5

    /// Stops paging when the original connection or foreground visit no longer owns the read.
    private func readInbox(api: VeetbotAPIClient, accountID: String?, view: String, text: String, count: Int,
                           requestID: UUID, anchor: UUID?, connection: UUID, activation foreground: UUID?,
                           knownVersion: String?) async throws -> ListAnswer? {
        var rows: [EmailThreadView] = []
        // Each page brings numbers of its own. The last page's are kept, with its rows.
        var counted: [EmailThreadView] = []
        var counts: EmailCollectionCounts?
        var total: Int?
        var ids: Set<UUID> = []
        var cursor: String?
        var cursors: Set<String> = []
        var pages = 0
        var version: String?
        // Pages the visible rows need, then a bounded search for a displaced visible thread.
        let pageLimit = max(1, (count + 99) / 100) + Self.anchorSearchPages
        repeat {
            guard listRequest == requestID, acceptsRead(connection: connection, activation: foreground) else { throw CancellationError() }
            // If a refresh displaced the visible thread, keep paging until we find it or exhaust the list.
            let remaining = count - rows.count
            let limit = remaining > 0 ? min(100, remaining) : 100
            let page = try await api.emailThreads(accountID: accountID, view: view, text: text, limit: limit, cursor: cursor)
            guard listRequest == requestID, acceptsRead(connection: connection, activation: foreground) else { throw CancellationError() }
            if pages == 0 {
                if let knownVersion, page.snapshotVersion == knownVersion,
                   loadedListVersion == knownVersion { return nil }
                version = page.snapshotVersion
            } else if page.snapshotVersion != version { version = nil }
            counts = page.collectionCounts
            total = page.totalCount
            counted = page.items
            rows.append(contentsOf: page.items.filter { ids.insert($0.id).inserted })
            cursor = try nextPageCursor(page.nextCursor, seen: &cursors)
            pages += 1
        } while cursor != nil
            && ((count > 100 && rows.count < count) || (anchor.map { !ids.contains($0) } == true && pages < pageLimit))
        return ListAnswer(page: EmailThreadPage(items: rows, nextCursor: cursor, collectionCounts: counts,
            totalCount: total, snapshotVersion: version), counted: counted)
    }

    /// Whether More mail would ask for a page now: the list has more, no list read or page is
    /// out, and no refusal waits for the list (rule 3 beside `listRequest`). `loadMore` checks
    /// this same value before it asks, so a control that offers a page and the model that
    /// fetches it cannot disagree. Every part of it is published, so a view that reads it is
    /// told when it changes. Two things it cannot say are left to `loadMore`: that the caller's
    /// own task was cancelled, and that there is no connection to ask through.
    public var canLoadMore: Bool {
        hasMore && !isLoading && !isReadingList && refusalAwaitingList == nil
    }

    /// Applies a page only while the requesting connection, list and foreground visit remain current.
    /// More mail takes no request of its own (see `listRequest`): it does nothing unless
    /// `canLoadMore` says it would ask.
    public func loadMore() async {
        let connection = generation
        let mailboxVersions = archiveVersions
        let foreground = active ? activation : nil
        guard acceptsRead(connection: connection, activation: foreground), canLoadMore,
              let cursor = nextCursor, let api = makeAPIClient() else { return }
        isLoading = true
        let requestID = listRequest
        defer { if listRequest == requestID { isLoading = false } }
        do {
            let page = try await api.emailThreads(
                accountID: selectedAccountID, view: queryListView,
                text: searchText, limit: pageSize, cursor: cursor
            )
            guard acceptsRead(connection: connection, activation: foreground), listRequest == requestID else { return }
            guard let numbers = await pairedNumbers(ListAnswer(page: page, counted: page.items), isCurrent: {
                self.acceptsRead(connection: connection, activation: foreground) && self.listRequest == requestID
            }) else { return }
            if loadedListVersion != page.snapshotVersion { loadedListVersion = nil }
            loadedCounts = numbers
            var ids = Set(inboxItems.map(\.id))
            let loaded = shown(page.items, since: mailboxVersions)
            inboxItems.append(contentsOf: loaded.filter { ids.insert($0.id).inserted })
            loadedDepth += page.items.count
            for value in loaded { observeArchive(value) }
            nextCursor = nil
            hasMore = false
            nextCursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
            hasMore = nextCursor != nil
            pagingFailed = false
            cacheMailList()
        } catch {
            guard acceptsRead(connection: connection, activation: foreground), listRequest == requestID else { return }
            pagingFailed = true
            report(error, readAccess: true)
        }
    }

    /// Admits one foreground refresh and ignores results after its connection or visibility expires.
    public func refresh() async {
        await refresh(activation: activation, manual: true)
    }

    /// Keeps admission ownership distinct from a later visit while retaining uncertain admission keys.
    private func refresh(activation foreground: UUID, manual: Bool = false) async {
        let connection = generation
        guard acceptsRead(connection: connection, activation: foreground), !unavailable, !isRefreshing,
              let api = makeAPIClient() else { return }
        if !manual, let budgetRetryAt, budgetRetryAt > now() {
            await reload(preserveOrder: true, activation: foreground)
            return
        }
        isRefreshing = true
        let key = refreshKey ?? UUID().uuidString
        refreshKey = key
        defer { if generation == connection, activation == foreground { isRefreshing = false } }
        do {
            let operation = try await api.refreshEmail(idempotencyKey: key)
            guard acceptsRead(connection: connection, activation: foreground) else { return }
            refreshKey = nil
            budgetPauseMessage = nil
            budgetPauseDetails = []
            budgetRetryAt = nil
            recordRefreshStatus(operation.status)
            await reload(preserveOrder: true, activation: foreground)
            guard acceptsRead(connection: connection, activation: foreground) else { return }
            if !operation.status.isTerminal { watchRefresh(operation.operationID, activation: foreground) }
            if let selectedThreadID { await openThread(selectedThreadID, refreshOnly: true, activation: foreground) }
        } catch {
            guard acceptsRead(connection: connection, activation: foreground) else { return }
            if isUnavailable(error) { unavailable = true }
            else if recordBudgetPause(error) { refreshKey = nil }
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
                    try await self.statusBackoff(delay)
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

    /// Budget refusal is a persistent pause, not a transient refresh error.
    private func recordBudgetPause(_ error: Error) -> Bool {
        guard case HTTPTransportError.api(let failure) = error,
              failure.statusCode == 402, failure.code == .budgetExceeded else { return false }
        let current = now()
        let formatter = ISO8601DateFormatter()
        let raw = failure.details.values["retry_at"]?.stringValue
        var retry = raw.flatMap { formatter.date(from: $0) }
        if retry == nil {
            formatter.formatOptions.insert(.withFractionalSeconds)
            retry = raw.flatMap { formatter.date(from: $0) }
        }
        var utc = Calendar(identifier: .gregorian)
        utc.timeZone = TimeZone(secondsFromGMT: 0)!
        let fallback = utc.startOfDay(for: current).addingTimeInterval(86_400)
        budgetRetryAt = max(retry ?? fallback, current.addingTimeInterval(60))
        setBudgetPausePresentation(failure.details)
        refreshFailure = nil
        errorMessage = nil
        return true
    }

    /// Keep accounting available on demand without putting raw server prose in the sidebar.
    private func setBudgetPausePresentation(_ details: APIErrorDetails) {
        func amount(_ key: String) -> Decimal? {
            guard let raw = details.values[key]?.stringValue,
                  let value = Decimal(string: raw, locale: Locale(identifier: "en_US_POSIX")),
                  !value.isNaN, value >= 0 else { return nil }
            return value
        }
        let currency = NumberFormatter()
        currency.numberStyle = .currency
        currency.currencyCode = "USD"
        currency.maximumFractionDigits = 2
        currency.minimumFractionDigits = 2
        func money(_ value: Decimal) -> String {
            currency.string(from: NSDecimalNumber(decimal: value)) ?? value.description
        }
        let next = amount("next_reservation")
        budgetPauseMessage = "Processing budget is in use."
        budgetPauseDetails = []
        // Prefer the longer-lived restriction when both windows block admission.
        for (window, label, reason) in [
            ("daily", "Today", "Daily processing budget is in use."),
            ("monthly", "Past 30 days", "Monthly processing budget is in use.")
        ] {
            if let spent = amount("\(window)_spent"), let limit = amount("\(window)_limit") {
                var total = "\(label): \(money(spent)) of \(money(limit))"
                if let estimated = amount("\(window)_estimated"), estimated > 0 {
                    total += " (\(money(estimated)) estimated)"
                }
                budgetPauseDetails.append(total)
                if let held = amount("\(window)_reserved"), let next,
                   spent + held + next > limit {
                    budgetPauseMessage = reason
                }
            }
        }
        if let held = amount("daily_reserved") {
            budgetPauseDetails.append("Pending costs: \(money(held))")
        }
        if let next { budgetPauseDetails.append("Next batch: \(money(next))") }
    }

    /// An account synced after a recorded failure proves a later refresh succeeded, even when
    /// this client never read that operation's terminal status: a visit that ends, or an
    /// admission that supersedes the poll, leaves the projection as the only surviving evidence.
    private func recordAccountFreshness(_ loaded: [EmailAccountView]) {
        let freshness = Dictionary(loaded.map { ($0.id, $0.lastSyncedAt) }, uniquingKeysWith: { _, latest in latest })
        defer { syncedBaseline = freshness }
        guard refreshFailure != nil else { return }
        let advanced = loaded.contains { account in
            // An account missing from the baseline was never observed, so it proves nothing.
            guard let synced = account.lastSyncedAt, let baseline = syncedBaseline[account.id] else { return false }
            guard let baseline else { return true }
            return synced > baseline
        }
        guard advanced else { return }
        refreshFailure = nil
        errorMessage = nil
    }

    /// Retains refresh failures across successful cache reads until a later refresh succeeds.
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
        threadETag = nil
        selectionRequest = UUID()
        threadRead = nil
        isLoadingThread = false
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
        feedbackGiven = [:]
        feedbackDraft = EmailFeedbackDraft()
    }

    /// Refreshes a thread without overwriting local edits or clearing unrelated action failures.
    /// An explicit image press, bound to the currently displayed source and connection.
    func loadImage(threadID: UUID, messageID: String, index: Int) async throws -> Data {
        guard thread?.id == threadID, let api = makeAPIClient(),
              let message = thread?.messages?.first(where: { $0.id == messageID }) else {
            throw CancellationError()
        }
        let connection = generation
        let data = try await api.emailImage(threadID: threadID, messageID: messageID, index: index)
        guard generation == connection, thread?.id == threadID,
              thread?.messages?.first(where: { $0.id == messageID }) == message else {
            throw CancellationError()
        }
        return data
    }

    public func openThread(_ id: UUID, approvalID: UUID? = nil, refreshOnly: Bool = false) async {
        await openThread(id, approvalID: approvalID, refreshOnly: refreshOnly, activation: active ? activation : nil)
    }

    /// Validates foreground ownership for both the thread read and an optional separate draft read.
    private func openThread(_ id: UUID, approvalID: UUID? = nil, refreshOnly: Bool = false, activation foreground: UUID?) async {
        let connection = generation
        let mailboxVersions = archiveVersions
        guard acceptsRead(connection: connection, activation: foreground), let api = makeAPIClient() else { return }
        if refreshOnly, approvalID == nil, thread == nil,
           threadRead?.id == id, threadRead?.activation == foreground { return }
        if !refreshOnly {
            clearSelection()
            selectedThreadID = id
        }
        guard selectedThreadID == id else { return }
        let requestID = UUID()
        selectionRequest = requestID
        threadRead = (id, foreground,
            thread?.id == id ? thread?.accountID : inboxItems.first { $0.id == id }?.accountID)
        isLoadingThread = thread == nil
        let projection = projectionGeneration
        defer {
            if selectionRequest == requestID {
                threadRead = nil
                isLoadingThread = false
            }
        }
        do {
            let read = try await api.emailThreadResponse(id, etag: refreshOnly && thread?.id == id ? threadETag : nil)
            guard acceptsRead(connection: connection, activation: foreground), selectionRequest == requestID else { return }
            guard case .content(let response, let etag) = read else {
                threadReadError = nil
                if draft?.isManual == true { await loadRevisions() }
                if let approvalID, draft?.approvalID == approvalID { await loadReview(activation: foreground) }
                else { await settlePendingReview(activation: foreground) }
                return
            }
            let previousMailbox = archiveStates[id]
            let result = preservingArchiveState(response, since: mailboxVersions)
            if let previousMailbox,
               previousMailbox.inInbox != result.inInbox || previousMailbox.inSpam != result.inSpam
                || previousMailbox.dismissedRevision != result.dismissedRevision
                || previousMailbox.isHandled != result.isHandled {
                invalidateMailSnapshots()
                // The list is the same list, read again for what changed: it keeps its depth.
                let depth = loadedDepth
                resetListQuery()
                loadedDepth = depth
                isLoading = false
                if let index = inboxItems.firstIndex(where: { $0.id == id }) {
                    if Self.staysListed(result, in: queryListView) {
                        inboxItems[index] = result
                    } else {
                        inboxItems.remove(at: index)
                    }
                }
                let query = listRequest
                let visit = activation
                Task { [weak self] in
                    guard let self, self.generation == connection, self.activation == visit,
                          self.listRequest == query else { return }
                    await self.reload(preserveOrder: true, activation: foreground)
                }
            }
            thread = result
            threadETag = projectionGeneration == projection && (result.draft != nil || result.draftID == nil) ? etag : nil
            isLoadingThread = false
            observeArchive(result)
            if let returnedDraft = result.draft { mergeDraft(returnedDraft) }
            else if let draftID = result.draftID {
                let value = try await api.emailDraft(draftID)
                guard acceptsRead(connection: connection, activation: foreground), selectionRequest == requestID else { return }
                mergeDraft(value)
            } else { draft = nil }
            threadReadError = nil
            if draft?.isManual == true { await loadRevisions() }
            if let approvalID, draft?.approvalID == approvalID { await loadReview(activation: foreground) }
            else { await settlePendingReview(activation: foreground) }
        } catch {
            guard acceptsRead(connection: connection, activation: foreground), selectionRequest == requestID else { return }
            report(error, draft: true, readAccess: true)
        }
    }

    private func mergeDraft(_ value: EmailDraftView) {
        guard value.threadID == selectedThreadID else { return }
        // A key kept after a lost response is retired once a refresh proves that press
        // landed or that the draft moved on: a new run, a send state, a failure, or a
        // restored draft. An unchanged ready draft keeps it, since admission may still
        // be in doubt.
        if let previous = draft, previous.id == value.id, let cached = sendKeys[value.id],
           cached.0 == value.revision {
            let runChanged = previous.runID != value.runID && (previous.runID != nil || value.runID != nil)
            let restored = previous.status == "ready" && previous.runID == nil
                && value.status == "ready" && value.runID == nil && value.updatedAt > previous.updatedAt
            if runChanged || value.status == "sending" || value.status == "failed" || restored {
                sendKeys.removeValue(forKey: value.id)
            }
        }
        draft = value
        if let local = edits[value.id], local.isDirty {
            if local.base.revision != value.revision { conflict = value }
        } else {
            edits[value.id] = EmailDraftEdit(value)
            conflict = nil
        }
        if reviewDraft?.revision != value.revision || value.stale || value.status != "awaiting_approval" {
            // A review that closes because mail arrived must say so; closing silently
            // leaves the owner wondering what they did.
            if reviewDraft != nil, value.stale { draftActionError = Self.sendEndedByNewMailMessage }
            review = nil
            reviewDraft = nil
        }
    }

    /// Opens the review for a proposal that outlasted its local wait once a later read shows
    /// where it landed, and clears the wait notice whichever way it settled.
    private func settlePendingReview(activation foreground: UUID?) async {
        guard let pending = pendingReviewRunID, let draft else { return }
        // Admission marks the draft awaiting approval before the task has frozen one.
        let stillQueued = draft.runID == pending && draft.approvalID == nil && !draft.stale
            && ["ready", "awaiting_approval"].contains(draft.status)
        if stillQueued { return }
        pendingReviewRunID = nil
        sendProgressMessage = nil
        if draft.runID == pending, draft.approvalID != nil, review == nil {
            await loadReview(activation: foreground)
        }
    }

    /// A send run that completed without sending: say why beside the draft.
    private func reportUnsentSend() {
        guard let draft, !["sent", "sending", "uncertain"].contains(draft.status) else { return }
        draftActionError = draft.stale ? Self.sendEndedByNewMailMessage
            : draft.status == "failed" ? Self.sendFailedMessage : Self.sendNotMadeMessage
    }

    public var draftPreparationMessage: String {
        switch thread?.preparationStage {
        case "finding_context": "Finding context…"
        case "finding_documents": "Finding documents…"
        case "staging_files": "Attaching files…"
        case "ready_for_review": "Ready for review"
        default: "Preparing draft…"
        }
    }

    public func loadDraftAttachment(_ id: UUID) async throws -> LoadedArtifact {
        guard let api = makeAPIClient() else { throw HTTPTransportError.notConfigured }
        // Both requests, and the file handed back, belong to the connection the press was made on.
        let connection = generation
        let metadata = try await api.getArtifact(id)
        guard generation == connection else { throw CancellationError() }
        guard case .content(let data, _) = try await api.getArtifactContent(id) else {
            throw HTTPTransportError.invalidResponse
        }
        guard generation == connection else { throw CancellationError() }
        return LoadedArtifact(metadata: metadata, data: data)
    }

    public func removeDraftAttachment(_ id: UUID) async {
        guard let draft, draft.canEdit, !isPreparingSend, var edit = edits[draft.id] else { return }
        edit.attachmentIDs.removeAll { $0 == id }
        edits[draft.id] = edit
        review = nil
        reviewDraft = nil
        autosaveTask?.cancel()
        _ = await saveDraft(draft.id)
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
        guard let id = id ?? draft?.id else { return false }
        // A save already on the wire settles first, then whatever is still dirty is
        // saved: Review & Send waits here instead of being greyed out by an autosave.
        while let inflight = inflightSave {
            _ = await inflight.value
            if inflightSave == inflight { inflightSave = nil }
        }
        guard let edit = edits[id], let api = makeAPIClient() else { return false }
        guard edit.isDirty else { return true }
        guard conflict?.id != id else { return false }
        // The server refuses edits to a stale draft until the owner confirms the
        // updated thread, so hold them here instead of sending a save it must refuse.
        if draft?.id == id, draft?.stale == true, edit.reviewedSourceRevision == nil {
            draftActionError = Self.staleEditMessage
            return false
        }
        let save = Task { [weak self] in await self?.performSave(id, edit: edit, api: api) ?? false }
        inflightSave = save
        let saved = await save.value
        if inflightSave == save { inflightSave = nil }
        return saved
    }

    private func performSave(_ id: UUID, edit: EmailDraftEdit, api: VeetbotAPIClient) async -> Bool {
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
            lastSavedDraft = saved
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
                let server = try? await api.emailDraft(id), generation == connection, draft?.id == id {
                draft = server
                // Only a newer revision is another device's edit. New mail marks the
                // draft stale at its own revision; calling that a conflict disabled the
                // very confirmation the server is waiting for.
                if server.revision != edit.base.revision { conflict = server }
                if server.stale {
                    // The confirmation records the thread revision on screen, so show
                    // the mail that made the draft stale before asking for it.
                    if selectedThreadID == server.threadID { await openThread(server.threadID, refreshOnly: true) }
                    guard generation == connection else { return false }
                    if conflict == nil {
                        draftActionError = Self.staleEditMessage
                        return false
                    }
                }
                // The conflict already says another device changed the draft; the
                // refused save's own text would only repeat it as a second error.
                if conflict?.id == id { return false }
            }
            // The draft was read again above. A connection that changed meanwhile is not told.
            guard generation == connection else { return false }
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

    /// Validate the chosen feedback scope, persist it, and refresh the current thread projection.
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
            invalidateMailSnapshots()
            // The owner may have opened another thread while this was in flight: the
            // acknowledgment and its Undo belong to the thread that was judged.
            if selectedThreadID == thread.id {
                feedbackID = result.feedbackID
                let message: String
                switch judgment {
                case "needs_reply": message = "This thread needs a reply."
                case "no_reply_needed": message = "This thread needs no reply."
                default:
                    let scope = target == .topic ? (targetValue ?? target.title.lowercased()) : target.title.lowercased()
                    message = "Marked \(scope) as \(judgment.replacingOccurrences(of: "_", with: " "))."
                }
                feedbackMessage = message
                // A later judgment of the same kind replaces its own row only; the other kind stays.
                feedbackGiven[EmailFeedbackKind(judgment: judgment)] = EmailFeedbackGiven(
                    id: result.feedbackID, judgment: judgment, target: target, targetValue: targetValue ?? "",
                    message: message)
            }
            await reload(preserveOrder: true)
            if selectedThreadID == thread.id { await openThread(thread.id, refreshOnly: true) }
        } catch { if generation == connection { report(error, draft: true) } }
    }

    /// Undo one kind of judgment and leave the other standing. Without a kind, undo the latest.
    public func undoFeedback(_ kind: EmailFeedbackKind? = nil) async {
        let kind = kind ?? feedbackGiven.first { $0.value.id == feedbackID }?.key
        guard let id = kind.flatMap({ feedbackGiven[$0]?.id }) ?? feedbackID, let api = makeAPIClient() else { return }
        let connection = generation
        let selection = selectedThreadID
        do {
            _ = try await api.undoEmailFeedback(id, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            if feedbackID == id { feedbackID = nil }
            if selectedThreadID == selection, let kind, feedbackGiven[kind]?.id == id { feedbackGiven[kind] = nil }
            invalidateMailSnapshots()
            feedbackMessage = "Feedback undone."
            await reload(preserveOrder: true)
        } catch { if generation == connection { report(error) } }
    }

    public var canSelectReply: Bool {
        !isSelectingReply && !isPreparingSend && !isGeneratingDraft && conflict == nil
            && (draft == nil || draft?.canEdit == true || draft?.status == "discarded" || draft?.status == "sent")
    }

    public var hasVeetbotDraft: Bool {
        guard let draft, draft.status != "discarded" else { return false }
        return !draft.isManual || revisions.contains { $0.id == draft.id && !$0.isManual }
    }

    /// Reply is owner composition. Switching retains both texts in server revisions.
    @discardableResult
    public func selectReply(manual: Bool) async -> Bool {
        guard let selected = thread, let api = makeAPIClient(), canSelectReply else { return false }
        let connection = generation
        let visit = selectionRequest
        isSelectingReply = true
        var ownsSelection = true
        defer { if ownsSelection && generation == connection { isSelectingReply = false } }
        draftActionError = nil
        if currentEdit?.isDirty == true, !(await saveDraft()) { return false }
        guard generation == connection, selectedThreadID == selected.id, selectionRequest == visit else { return false }
        guard currentEdit?.isDirty != true else {
            draftActionError = "Finish editing before switching replies. Your text is preserved."
            return false
        }
        if let draft, draft.status != "discarded", draft.status != "sent", draft.isManual == manual { return true }
        do {
            let value = try await api.selectEmailReply(
                thread: selected, draft: draft, kind: manual ? "manual" : "veetbot",
                idempotencyKey: UUID().uuidString)
            guard generation == connection, selectedThreadID == selected.id, selectionRequest == visit else { return false }
            mergeDraft(value)
            await loadRevisions()
            return true
        } catch {
            guard generation == connection, selectedThreadID == selected.id, selectionRequest == visit else { return false }
            // Only an explicit Veetbot action may request generation. Reply never falls back to it.
            if !manual, case HTTPTransportError.api(let failure) = error, failure.statusCode == 404 {
                isSelectingReply = false
                ownsSelection = false
                await generateDraft()
                return draft?.isManual == false
            }
            report(error, draft: true)
            return false
        }
    }

    public func generateDraft(instruction: String? = nil) async {
        guard let id = selectedThreadID, let api = makeAPIClient(), !isPerformingAction else { return }
        if currentEdit?.isDirty == true, !(await saveDraft()) { return }
        draftActionError = nil
        isPerformingAction = true
        isGeneratingDraft = true
        let connection = generation
        defer { if generation == connection { isPerformingAction = false; isGeneratingDraft = false } }
        do {
            let result = try await api.generateEmailDraft(threadID: id, instruction: instruction, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            if let value = result.draft { mergeDraft(value) }
            await watchDraftRun(result.runID, threadID: id, connection: connection)
        } catch { if generation == connection { report(error, draft: true) } }
    }

    /// Saves edits and proposes the exact draft for approval, retaining accepted work if Email is hidden.
    public func prepareSend() async {
        guard canReview, let api = makeAPIClient(), let pressed = draft else { return }
        isPreparingSend = true
        sendProgressMessage = nil
        pendingReviewRunID = nil
        let connection = generation
        defer { if generation == connection { isPreparingSend = false } }
        // Typing continues while a save is in flight, so a save can succeed and still
        // leave the edit dirty, holding back the very text the owner asked to send.
        // Settle the newest revision before freezing one, bounding the attempts so
        // continuous typing cannot spin here.
        for _ in 0..<3 {
            autosaveTask?.cancel()
            guard currentEdit?.isDirty == true else { break }
            guard await saveDraft(pressed.id) else { return }
        }
        // The press belongs to the draft it was made on, never to one opened since.
        guard let draft, draft.id == pressed.id, selectedThreadID == draft.threadID else { return }
        guard !draft.stale else {
            draftActionError = "This draft is out of date. Refresh the thread and review the draft again."
            return
        }
        // Never freeze a revision that is not the text on screen; a stalled save is
        // reported instead of leaving the action looking like it did nothing.
        guard currentEdit?.isDirty == false else {
            draftActionError = "Your most recent edits are still saving. Review and send again."
            return
        }
        isPerformingAction = true
        defer { if generation == connection { isPerformingAction = false } }
        let key: String
        if let previous = sendKeys[draft.id], previous.0 == draft.revision { key = previous.1 }
        else { key = UUID().uuidString; sendKeys[draft.id] = (draft.revision, key) }
        do {
            let result = try await api.proposeEmailSend(draft, idempotencyKey: key)
            guard generation == connection else { return }
            if let value = result.draft { mergeDraft(value) }
            await watchDraftRun(result.runID, threadID: draft.threadID, connection: connection, send: true)
            await loadReview(activation: nil)
        } catch { if generation == connection { report(error, draft: true) } }
    }

    public func attachmentSave(_ key: EmailAttachmentKey) -> EmailAttachmentSaveState? {
        attachmentSaves[key]
    }

    /// One press on Save. The file lands on the email's chat, so a saved one needs no
    /// second copy. A press after a known failure is a new request; after a lost answer
    /// it repeats the same one, which the server replays.
    public func saveAttachment(_ key: EmailAttachmentKey, filename: String) async {
        guard let api = makeAPIClient() else { return }
        switch attachmentSaves[key] {
        case .saving, .saved: return
        case .failed, nil: break
        }
        let connection = generation
        attachmentSaves[key] = .saving
        let request = attachmentRequests[key] ?? (UUID().uuidString, nil)
        attachmentRequests[key] = request
        do {
            var operationID = request.operationID
            var status: RunStatus?
            if operationID == nil {
                let operation = try await api.saveEmailAttachment(
                    key, filename: filename, idempotencyKey: request.idempotencyKey)
                guard generation == connection else { return }
                operationID = operation.operationID
                status = operation.status
                attachmentRequests[key]?.operationID = operation.operationID
            }
            guard let operationID else { return }
            // A slice lasts two minutes; past this the file may yet arrive in the chat.
            for _ in 0..<150 where status?.isTerminal != true {
                if status != nil { try await Task.sleep(nanoseconds: pollNanoseconds) }
                guard generation == connection else { return }
                status = try await api.emailOperation(operationID).status
            }
            guard generation == connection else { return }
            guard let status, status.isTerminal else {
                attachmentSaves[key] = .failed(Self.attachmentStillSavingMessage)
                return
            }
            attachmentRequests[key] = nil
            attachmentSaves[key] = status == .completed ? .saved : .failed(Self.attachmentNotSavedMessage)
        } catch {
            guard generation == connection, !(error is CancellationError) else { return }
            if attachmentRequests[key]?.operationID == nil,
               case HTTPTransportError.api(let failure) = error,
               let code = failure.statusCode, (400...499).contains(code), code != 408, code != 429 {
                // The server refused the request: nothing was admitted, so nothing can be replayed.
                attachmentRequests[key] = nil
            }
            attachmentSaves[key] = .failed(error.localizedDescription)
            if case HTTPTransportError.reauthenticationRequired = error { report(error) }
        }
    }

    private static let attachmentNotSavedMessage = "This attachment could not be saved. Try again."
    private static let attachmentStillSavingMessage =
        "This is taking a while. Check the email's chat before trying again."

    /// Follows a draft or send run the owner is waiting on. A send that completes without
    /// sending is reported; one that outlasts the local wait is marked for a later read to open.
    private func watchDraftRun(_ runID: UUID, threadID: UUID, connection: UUID, send: Bool = false) async {
        guard let api = makeAPIClient() else { return }
        // Stop local waiting after a bounded interval; subsequent active refresh
        // recovers the same durable action instead of submitting another one.
        for _ in 0..<60 {
            guard generation == connection, selectedThreadID == threadID, !Task.isCancelled else { return }
            do {
                let run = try await api.getRun(runID)
                await openThread(threadID, refreshOnly: true)
                guard generation == connection, selectedThreadID == threadID, !Task.isCancelled else { return }
                if run.status == .failed {
                    draftActionError = run.failure?.userFacingMessage ?? "The email operation failed. Try again."
                } else if run.status == .cancelled {
                    draftActionError = "The email operation was cancelled."
                } else if send, run.status == .completed {
                    reportUnsentSend()
                }
                if run.status.isTerminal || run.status == .waitingForApproval || run.status == .waitingForUser { return }
                try await Task.sleep(nanoseconds: pollNanoseconds)
            } catch { if generation == connection { report(error, draft: true) }; return }
        }
        guard send, generation == connection, selectedThreadID == threadID else { return }
        pendingReviewRunID = runID
        sendProgressMessage = Self.stillPreparingSendMessage
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
        /// Verifies one frozen argument against the draft.
        ///
        /// The approval view truncates a long string, so comparing it to the full
        /// local value can never succeed. A published digest is then the only exact
        /// evidence, and it is authoritative wherever it exists — it covers the whole
        /// value, not just the prefix the view carries. Without one, a value that no
        /// longer compares equal stays refused rather than presented unverified.
        func textMatches(_ key: String, _ expected: String) -> Bool {
            if let digest = approval.argumentDigests?[key] {
                return digest == Self.sha256Hex(expected)
            }
            return approval.arguments[key]?.stringValue == expected
        }
        func addressesMatch(_ key: String, _ expected: [String]) -> Bool {
            if approval.argumentDigests?[key] != nil { return textMatches(key, expected.joined(separator: ", ")) }
            guard let value = approval.arguments[key], value != .null else { return key != "to" && expected.isEmpty }
            if let text = value.stringValue { return text == expected.joined(separator: ", ") }
            guard let entries = value.arrayValue else { return false }
            let strings = entries.compactMap(\.stringValue)
            return strings.count == entries.count && strings == expected
        }
        let files = draft.attachments ?? []
        let expectedTool = files.isEmpty ? "send_message" : "send_attached_message"
        func attachmentsMatch() -> Bool {
            guard !files.isEmpty else { return approval.arguments["attachments"] == nil }
            guard let encoded = try? JSONEncoder.server.encode(files),
                  let expected = try? JSONDecoder.server.decode([JSONValue].self, from: encoded),
                  let observed = approval.arguments["attachments"]?.arrayValue else { return false }
            func normalized(_ value: JSONValue) -> JSONValue {
                guard var fields = value.objectValue,
                      let id = fields["artifact_id"]?.stringValue else { return value }
                fields["artifact_id"] = .string(id.lowercased())
                // Approval presentation can redact a filename. Identity and
                // content metadata still have to match the retained draft.
                fields.removeValue(forKey: "filename")
                return .object(fields)
            }
            return expected.map(normalized) == observed.map(normalized)
        }
        guard let account = accounts.first(where: { $0.id == draft.accountID }),
            let serverID = account.sendServerID,
            let toolName = draft.sendToolName, toolName == "mcp.\(serverID).\(expectedTool)", approval.toolName == toolName,
            let providerThreadID = draft.providerThreadID,
            approval.arguments["thread_id"]?.stringValue == providerThreadID else { return false }
        return attachmentsMatch() && textMatches("subject", draft.subject) && textMatches("body", draft.body)
            && addressesMatch("to", draft.to) && addressesMatch("cc", draft.cc) && addressesMatch("bcc", draft.bcc)
    }

    private static func sha256Hex(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
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
    /// Marking a thread handled confirms with a toast whose Undo sends the
    /// same reversible command back; the undo itself raises only its own line.
    @discardableResult
    public func setThreadHandled(_ thread: EmailThreadView, handled: Bool, confirms: Bool = true) async -> Bool {
        guard let api = makeAPIClient(), !isPerformingAction else { return false }
        isPerformingAction = true
        let connection = generation
        defer { if generation == connection { isPerformingAction = false } }
        do {
            let result = try await api.dismissEmailThread(thread, dismissed: handled, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return false }
            invalidateMailSnapshots()
            // The command returns a summary. Preserve the open messages and
            // unsaved draft, updating only server-confirmed attention state.
            if self.thread?.id == result.id, self.thread?.revision == result.revision {
                self.thread?.dismissedRevision = result.dismissedRevision
            }
            if let index = inboxItems.firstIndex(where: { $0.id == result.id && $0.revision == result.revision }) {
                inboxItems[index].dismissedRevision = result.dismissedRevision
            }
            await reload(preserveOrder: true)
            if confirms && handled {
                toasts.show("Marked handled.", restored: "Marked not handled.") { [weak self] in
                    await self?.setThreadHandled(result, handled: false, confirms: false) ?? false
                }
            }
            return true
        } catch {
            if generation == connection { report(error, draft: selectedThreadID == thread.id) }
            return false
        }
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
    /// A conversation in Spam returns through Not spam, never through Move to Inbox.
    public func canArchive(_ thread: EmailThreadView) -> Bool {
        guard archiveUnavailableReason(for: thread) == nil, !archiveSubmitting.contains(thread.id),
              !thread.isInSpam else { return false }
        if let operation = thread.archiveOperation {
            return operation.status == "completed" || operation.status == "failed"
        }
        return true
    }

    /// Describes this mailbox action without replacing unrelated refresh, editing or send errors.
    public func archiveMessage(for thread: EmailThreadView) -> String? {
        if let error = archiveErrors[thread.id] { return error }
        if archiveSubmitting.contains(thread.id) { return nil }
        guard let operation = thread.archiveOperation else { return nil }
        switch operation.status {
        case "pending": return nil
        case "failed": return operation.error ?? "Gmail could not complete this action. Try again."
        case "uncertain": return "Outcome not confirmed. Check Gmail; recorded status may update after a later email refresh."
        case "completed": return nil
        default: return "Checking the Gmail action's status…"
        }
    }

    /// Supplies one-action consent and retains the same request after an uncertain admission response.
    /// Archiving the selected thread from its row or its detail advances the reading pane.
    public func setThreadArchived(_ thread: EmailThreadView, archived: Bool) async {
        await changeInbox(thread, archived: archived, spam: false)
    }

    /// Why this conversation cannot be reported or restored; `nil` when it can.
    public func spamUnavailableReason(for thread: EmailThreadView) -> String? {
        guard spamSupported(for: thread.accountID) else {
            return "Reporting spam is unavailable for this account. Update or reconnect the server."
        }
        return thread.inInbox == nil ? "Refresh this thread to check its Gmail Inbox state." : nil
    }

    /// Report spam needs a conversation outside Spam; Not spam needs one inside it.
    public func canReportSpam(_ thread: EmailThreadView, spam: Bool = true) -> Bool {
        guard spamUnavailableReason(for: thread) == nil, !archiveSubmitting.contains(thread.id),
              thread.isInSpam != spam else { return false }
        if let operation = thread.archiveOperation {
            return operation.status == "completed" || operation.status == "failed"
        }
        return true
    }

    /// Archive's gesture with the Spam delta: the row leaves at once and settles from the operation.
    public func setThreadSpam(_ thread: EmailThreadView, spam: Bool) async {
        await changeInbox(thread, archived: spam, spam: true)
    }

    /// The owner's Not spam on a flagged conversation; nothing changes in Gmail.
    public func clearSpamFlag(_ thread: EmailThreadView, cleared: Bool = true) async {
        guard let api = makeAPIClient(), !spamFlagSubmitting.contains(thread.id) else { return }
        guard spamSupported(for: thread.accountID) else {
            spamFlagErrors[thread.id] = spamUnavailableReason(for: thread)
            return
        }
        let connection = generation
        spamFlagSubmitting.insert(thread.id)
        spamFlagErrors[thread.id] = nil
        defer { if generation == connection { spamFlagSubmitting.remove(thread.id) } }
        do {
            let result = try await api.clearEmailThreadSpamFlag(thread.id, cleared: cleared)
            guard generation == connection else { return }
            invalidateMailSnapshots()
            for index in inboxItems.indices where inboxItems[index].id == thread.id {
                inboxItems[index].suspectedSpam = result.suspectedSpam
                inboxItems[index].spamCleared = result.spamCleared
            }
            if self.thread?.id == thread.id {
                self.thread?.suspectedSpam = result.suspectedSpam
                self.thread?.spamCleared = result.spamCleared
            }
            await reload(preserveOrder: true)
        } catch {
            guard generation == connection, !(error is CancellationError) else { return }
            if isUnavailable(error) { spamUnsupportedAccounts.insert(thread.accountID) }
            spamFlagErrors[thread.id] = isUnavailable(error)
                ? spamUnavailableReason(for: thread) : error.localizedDescription
            if case HTTPTransportError.reauthenticationRequired = error { report(error) }
        }
    }

    private func changeInbox(_ thread: EmailThreadView, archived: Bool, spam: Bool) async {
        guard let api = makeAPIClient(), !archiveSubmitting.contains(thread.id) else { return }
        let unavailableReason = spam ? spamUnavailableReason(for: thread) : archiveUnavailableReason(for: thread)
        guard unavailableReason == nil else {
            archiveErrors[thread.id] = unavailableReason
            return
        }
        if archiveRequests[thread.id]?.operationID != nil
            || !(spam ? canReportSpam(thread, spam: archived) : canArchive(thread)) {
            await checkArchiveStatus(thread.id)
            return
        }
        let connection = generation
        let request = archiveRequests[thread.id]
            ?? ArchiveRequest(thread: thread, archived: archived, key: UUID().uuidString, spam: spam)
        archiveRequests[thread.id] = request
        invalidateMailSnapshots()
        archiveErrors[thread.id] = nil
        archiveReadErrors.remove(thread.id)
        archiveVersions[thread.id] = UUID()
        archiveSubmitting.insert(thread.id)
        let visible = items
        // Advancing clears the detail, which may hold newer confirmed mailbox state than its row.
        let detail = self.thread?.id == thread.id ? self.thread : nil
        if request.archived {
            archiveHiddenThreads.insert(thread.id)
            hiddenAccounts[thread.id] = thread.accountID
            noteHidden(inboxItems.first(where: { $0.id == thread.id }) ?? detail ?? thread)
        }
        if request.archived, selectedThreadID == thread.id {
            let next: EmailThreadView?
            if let index = visible.firstIndex(where: { $0.id == thread.id }) {
                next = visible.dropFirst(index + 1).first ?? visible.prefix(index).last
            } else { next = items.first }
            clearSelection()
            if let next {
                selectedThreadID = next.id
                isLoadingThread = true
                let selection = selectionRequest
                let foreground = active ? activation : nil
                Task { [weak self] in
                    guard let self, self.generation == connection,
                          self.selectionRequest == selection else { return }
                    await self.openThread(next.id, refreshOnly: true, activation: foreground)
                }
            }
        }
        defer { if generation == connection { archiveSubmitting.remove(thread.id) } }
        do {
            let operation = request.spam
                ? try await api.reportEmailThreadSpam(request.thread, spam: request.archived, idempotencyKey: request.key)
                : try await api.archiveEmailThread(request.thread, archived: request.archived, idempotencyKey: request.key)
            guard generation == connection else { return }
            // The request was retired meanwhile: its account was refused, or its outcome is
            // already known. Its answer is for nothing that is still followed.
            guard archiveRequests[thread.id]?.key == request.key else { return }
            archiveRequests[thread.id]?.operationID = operation.operationID
            let pending = EmailArchiveOperation(operationID: operation.operationID, runID: operation.runID,
                targetArchived: request.archived, targetSpam: request.spam ? true : nil, status: "pending", error: nil)
            if let index = inboxItems.firstIndex(where: { $0.id == thread.id }) { inboxItems[index].archiveOperation = pending }
            if self.thread?.id == thread.id { self.thread?.archiveOperation = pending }
            let current = self.thread?.id == thread.id ? self.thread : detail ?? inboxItems.first { $0.id == thread.id }
            archiveStates[thread.id] = ArchiveMailboxState(inInbox: current?.inInbox ?? thread.inInbox,
                operation: pending, dismissedRevision: current?.dismissedRevision ?? thread.dismissedRevision,
                revision: current?.revision ?? thread.revision,
                inSpam: current?.inSpam ?? thread.inSpam)
            archiveVersions[thread.id] = UUID()
            await checkArchiveStatus(thread.id)
        } catch {
            guard generation == connection, !(error is CancellationError), !Task.isCancelled else { return }
            guard archiveRequests[thread.id]?.key == request.key else { return }
            if isUnavailable(error), request.spam {
                // A missing spam route means this server or account cannot report; never archive instead.
                spamUnsupportedAccounts.insert(thread.accountID)
                archiveRequests[thread.id] = nil
                archiveErrors[thread.id] = spamUnavailableReason(for: thread)
            } else if isUnavailable(error) {
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
        await readArchiveStatus(threadID)
    }

    /// One status read, applied as the outcome of the pending action when it still answers the
    /// latest request. Returns the conversation as the server holds it whether or not it was
    /// applied: a read a newer gesture overtook still shows where the numbers hold it.
    /// When the read shows that Gmail confirmed the archive, its outcome is applied first, so
    /// what the screen shows never depends on a later read succeeding. The list is then read
    /// again, unless a list read asked for this status: that read shows its own answer, which
    /// nothing replaces. When the server refuses the read, that account's mail goes at once,
    /// and the rest stays only if the list then answers.
    /// A list read that asks passes `forListRead`, which says whether it is still the read on
    /// screen. Once another list read has replaced it, the answer may be older than what that
    /// read has shown, and nothing is changed for it, whether it succeeded or failed.
    @discardableResult
    private func readArchiveStatus(
        _ threadID: UUID, forListRead owner: (() -> Bool)? = nil
    ) async -> EmailThreadView? {
        let connection = generation
        let foreground = active ? activation : nil
        let mailboxVersions = archiveVersions
        guard acceptsRead(connection: connection, activation: foreground), let api = makeAPIClient() else { return nil }
        let isCurrent = { self.acceptsRead(connection: connection, activation: foreground) && owner?() != false }
        do {
            let value = try await api.emailThread(threadID)
            guard isCurrent() else { return nil }
            guard archiveVersions[threadID] == mailboxVersions[threadID] else { return value }
            if archiveReadErrors.remove(threadID) != nil {
                archiveErrors[threadID] = nil
                // Its row is off the list again, so the numbers have to answer for it again.
                if archiveHiddenThreads.contains(threadID) { noteHidden(value) }
            }
            guard acceptsArchiveOperation(value) else { return value }
            mergeArchive(value)
            // The outcome is applied before the list is read. A newer archive of the same
            // conversation that the list then shows is that read's to follow.
            observeArchive(value)
            if value.archiveOperation?.status == "completed", owner == nil {
                // Applying the outcome ended this conversation's poll, which may be the task
                // running this. The list is read in a task of its own, so that it still runs.
                await Task { [weak self] in
                    await self?.reload(preserveOrder: true, activation: foreground)
                }.value
            }
            return value
        } catch {
            guard isCurrent(), archiveVersions[threadID] == mailboxVersions[threadID] else { return nil }
            if case HTTPTransportError.authorizationDenied = error {
                // One refused conversation does not say that all of Email was revoked. Its
                // account's mail goes at once, and the list is asked what still reads.
                forgetDeniedAccount(holding: threadID)
                refusalAwaitingList = error.localizedDescription
                // The list read that answers the refusal takes the list's request here, in the
                // same step as the refusal. Every list read in flight was asked before the
                // refusal and is retired by that. The read starts a moment later, from another
                // task, under the request taken here and no other, so no earlier answer can be
                // shown, or taken for the list's answer, in between.
                let request = UUID()
                listRequest = request
                // The refused conversation's poll was retired with it, and may be the task
                // running this. The list is read in a task of its own, so that it still runs,
                // and whatever poll that read starts is left alone afterwards.
                // The caller waits for that read, as a visit waits for every read it asks: its
                // regular refreshes run one after another. A read the server leaves unanswered
                // ends by the transport's request limit (60 seconds) and is then a failed read.
                // With no network a read waits for it, and a newer read would wait just the same.
                await Task { [weak self] in
                    await self?.reload(preserveOrder: true, activation: foreground, as: request)
                }.value
                return nil
            }
            archiveReadErrors.insert(threadID)
            archiveErrors[threadID] = error.localizedDescription
            if case HTTPTransportError.reauthenticationRequired = error { report(error) }
            return nil
        }
    }

    /// Applies fresh source context and mailbox state while retaining local drafts and independent errors.
    private func mergeArchive(_ value: EmailThreadView) {
        invalidateMailSnapshots()
        archiveVersions[value.id] = UUID()
        archiveStates[value.id] = ArchiveMailboxState(inInbox: value.inInbox, operation: value.archiveOperation,
            dismissedRevision: value.dismissedRevision, revision: value.revision, inSpam: value.inSpam)
        if let index = inboxItems.firstIndex(where: { $0.id == value.id }), value.revision >= inboxItems[index].revision {
            inboxItems[index] = value
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
                preserved.inSpam = state.inSpam
                return preserved
            }
        } else {
            archiveStates[value.id] = ArchiveMailboxState(inInbox: value.inInbox,
                operation: value.archiveOperation, dismissedRevision: value.dismissedRevision,
                revision: value.revision, inSpam: value.inSpam)
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
            && operation.isSpamAction == request.spam
    }

    /// Resumes pending actions learned from another device and clears only confirmed action failures.
    private func observeArchive(_ value: EmailThreadView) {
        guard acceptsArchiveOperation(value), let operation = value.archiveOperation else { return }
        if operation.status == "completed" || operation.status == "failed" || operation.status == "uncertain" {
            liftHide(value)
            archiveRequests[value.id] = nil
            archiveErrors[value.id] = nil
            archiveReadErrors.remove(value.id)
            archiveTasks.removeValue(forKey: value.id)?.task.cancel()
        } else {
            if operation.targetArchived, archiveHiddenThreads.insert(value.id).inserted {
                // An archive begun on another device and first seen here. A kept list's numbers
                // were adjusted for the conversations hidden when it was read.
                cachedMailLists = [:]
                hiddenAccounts[value.id] = value.accountID
                noteHidden(value)
            }
            guard active, archiveTasks[value.id] == nil else { return }
            let connection = generation
            let foreground = activation
            let token = UUID()
            archiveTasks[value.id] = ArchivePoll(token: token, task: Task { [weak self] in
                guard let self else { return }
                // Only its own entry: the outcome it applied may have let a newer archive of
                // this conversation start a poll under the same key.
                defer { if self.archiveTasks[value.id]?.token == token { self.archiveTasks[value.id] = nil } }
                for _ in 0..<60 {
                    do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
                    guard self.acceptsRead(connection: connection, activation: foreground) else { return }
                    await self.checkArchiveStatus(value.id)
                    if self.archiveErrors[value.id] != nil { return }
                }
            })
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

    /// True while a learning reset is on its way, so the sheet can show it.
    @Published public private(set) var isResettingLearning = false

    public func resetLearning(scope: String) async {
        guard !isResettingLearning, let api = makeAPIClient() else { return }
        let connection = generation
        isResettingLearning = true
        defer { if generation == connection { isResettingLearning = false } }
        do {
            let value = try await api.resetEmailLearning(scope: scope, idempotencyKey: UUID().uuidString)
            guard generation == connection else { return }
            invalidateMailSnapshots()
            learning = value
            toasts.show(scope == "preferences" ? "Importance preferences reset."
                : scope == "style" ? "Writing style reset." : "Email learning reset.")
            await reload(preserveOrder: true)
        } catch { if generation == connection { report(error) } }
    }

    public func excludeSelectedThread() async {
        guard let thread, let api = makeAPIClient() else { return }
        let connection = generation
        do {
            try await api.excludeEmailThread(thread, idempotencyKey: UUID().uuidString)
            // The excluded mail leaves the client too, even if the connection moved on.
            EmailConversationCache.shared.remove(threadID: thread.id)
            guard generation == connection else { return }
            invalidateMailSnapshots()
            edits = edits.filter { $0.value.base.threadID != thread.id }
            // The row goes now: a failed reload must not leave the excluded summary in view.
            inboxItems.removeAll { $0.id == thread.id }
            clearSelection()
            await reload(preserveOrder: true)
        } catch { if generation == connection { report(error, draft: true) } }
    }

    /// True while a one-press send is parked in its undo window (ADR-0279).
    public var canUndoSend: Bool {
        guard let draft, draft.status == "sending", let until = draft.sendUndoUntil else { return false }
        return until > now() && !isPerformingAction
    }

    /// Seconds left in the undo window, for the countdown beside Undo.
    public func undoSecondsRemaining(at date: Date? = nil) -> Int {
        guard let until = draft?.sendUndoUntil else { return 0 }
        return max(0, Int((until.timeIntervalSince(date ?? now())).rounded(.up)))
    }

    /// One press sends the exact revision at the thread revision on screen: no proposal, no
    /// sheet. The server parks it for the undo window and this watches it through to Sent.
    public func sendNow() async {
        guard canReview, let api = makeAPIClient(), let pressed = draft else { return }
        var reviewed = pressed
        isPreparingSend = true
        sendProgressMessage = nil
        pendingReviewRunID = nil
        let connection = generation
        defer { if generation == connection { isPreparingSend = false } }
        for _ in 0..<3 {
            autosaveTask?.cancel()
            guard currentEdit?.isDirty == true else { break }
            guard await saveDraft(pressed.id) else { return }
            guard let saved = lastSavedDraft, saved.id == pressed.id,
                  saved.isManual == pressed.isManual else { return }
            reviewed = saved
        }
        // The press belongs to the draft it was made on. If the owner moved to another
        // conversation while that save settled, nothing is sent from here.
        guard generation == connection, let draft, draft == reviewed,
              draft.isManual == pressed.isManual, let thread, thread.id == draft.threadID,
              selectedThreadID == draft.threadID else { return }
        guard !draft.stale else {
            draftActionError = staleMailMessage() ?? Self.sendEndedByNewMailMessage
            return
        }
        guard currentEdit?.isDirty == false else {
            draftActionError = "Your most recent edits are still saving. Press Send again."
            return
        }
        isPerformingAction = true
        defer { if generation == connection { isPerformingAction = false } }
        // The key belongs to this press, not to the revision: it is kept only while
        // the server has not answered, so a lost response can be retried safely, and
        // dropped on any answer, so Undo, a failure or another device's Undo leave the
        // next press a new request rather than a replay of the dead one.
        let key: String
        if let previous = sendKeys[draft.id], previous.0 == draft.revision { key = previous.1 }
        else { key = UUID().uuidString; sendKeys[draft.id] = (draft.revision, key) }
        do {
            let result = try await api.sendEmailDraft(draft, sourceRevision: thread.revision, idempotencyKey: key)
            guard generation == connection else { return }
            sendKeys.removeValue(forKey: draft.id)
            draftActionError = nil
            if let value = result.draft { mergeDraft(value) }
            isPreparingSend = false
            isPerformingAction = false
            await watchSend(result.runID, threadID: draft.threadID, connection: connection)
        } catch {
            guard generation == connection else { return }
            if case HTTPTransportError.api = error { sendKeys.removeValue(forKey: draft.id) }
            report(error, draft: true)
            // A refused press usually means mail arrived: show it beside the draft.
            if case HTTPTransportError.api(let failure) = error, failure.statusCode == 409 {
                await openThread(draft.threadID, refreshOnly: true)
            }
        }
    }

    /// Takes a parked send back; the draft returns to editing with its text intact.
    public func undoSend() async {
        guard let draft, canUndoSend, let api = makeAPIClient() else { return }
        let connection = generation
        // Record the undo before the watcher can read the cancellation as a failure.
        let undoneRunID = draft.runID
        if let undoneRunID { undoneRunIDs.insert(undoneRunID) }
        sendWatchTask?.cancel()
        isPerformingAction = true
        defer { if generation == connection { isPerformingAction = false } }
        do {
            let restored = try await api.undoEmailSend(draft)
            guard generation == connection else { return }
            draftActionError = nil
            mergeDraft(restored)
            sendKeys.removeValue(forKey: restored.id)
        } catch {
            guard generation == connection else { return }
            report(error, draft: true)
            // Undo was refused (usually: already claimed), so the send goes on; watch
            // it again so its outcome is still reported.
            if let undoneRunID {
                undoneRunIDs.remove(undoneRunID)
                Task { [weak self] in
                    await self?.watchSend(undoneRunID, threadID: draft.threadID, connection: connection)
                }
            }
            await openThread(draft.threadID, refreshOnly: true)
        }
    }

    /// Follows a consented send until it settles; an Undo of the owner's own is silent.
    private func watchSend(_ runID: UUID, threadID: UUID, connection: UUID) async {
        guard let api = makeAPIClient() else { return }
        let task = Task { [weak self] in
            for _ in 0..<120 {
                guard let self, self.generation == connection, self.selectedThreadID == threadID,
                      !Task.isCancelled else { return }
                do {
                    let run = try await api.getRun(runID)
                    await self.openThread(threadID, refreshOnly: true)
                    guard self.generation == connection, self.selectedThreadID == threadID, !Task.isCancelled
                    else { return }
                    // Once the draft belongs to a newer send, this watcher is history: it
                    // must neither report this run's outcome against it nor touch its key.
                    guard self.draft?.runID == runID else { return }
                    if run.status == .failed {
                        self.draftActionError = run.failure?.userFacingMessage ?? Self.sendFailedMessage
                    } else if run.status == .cancelled, !self.undoneRunIDs.contains(runID) {
                        self.draftActionError = "This reply wasn't sent. Your draft is kept; try again."
                    } else if run.status == .completed {
                        self.reportUnsentSend()
                    }
                    if run.status.isTerminal { return }
                    try await Task.sleep(nanoseconds: self.pollNanoseconds)
                } catch {
                    if self.generation == connection, !(error is CancellationError) { self.report(error, draft: true) }
                    return
                }
            }
        }
        sendWatchTask = task
        await task.value
    }

    public func resolveSend(_ decision: ApprovalDecision) async {
        guard let review, let frozen = reviewDraft, let draft,
            draft.id == frozen.id, draft.revision == frozen.revision, !draft.stale,
            currentEdit?.isDirty == false, let api = makeAPIClient(), !isPerformingAction else { return }
        isPerformingAction = true
        isPreparingSend = true
        let connection = generation
        defer {
            if generation == connection {
                isPerformingAction = false
                isPreparingSend = false
            }
        }
        do {
            _ = try await api.resolveApproval(review.id, decision: decision)
            guard generation == connection else { return }
            closeReview()
            // A denied send is the owner's own doing; only an approved one that did not go out needs a word.
            await watchDraftRun(review.runID, threadID: draft.threadID, connection: connection, send: decision != .deny)
        } catch {
            guard generation == connection else { return }
            // A lost response or another device may have resolved this action.
            // Read the same approval and draft; never create a new send here.
            let latest = try? await api.getApproval(review.id)
            guard generation == connection else { return }
            if let latest, !latest.status.isPending {
                closeReview()
                await openThread(draft.threadID, refreshOnly: true)
                guard generation == connection else { return }
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
