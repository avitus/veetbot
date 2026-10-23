import Foundation
import SwiftUI

/// The Mac right panel's sizes (ADR-0237): its width range, each mode's
/// starting width, and the narrowest window that still has room for it.
enum InspectorLayout {
    static let widthBounds: ClosedRange<CGFloat> = 260...440

    static func defaultWidth(_ mode: ClientMode) -> CGFloat {
        mode == .chat ? 340 : 320
    }

    /// Chat is two columns beside the panel; Email is three (mailboxes,
    /// inbox, thread), so it needs a wider window before a fourth fits.
    static func minimumWindowWidth(_ mode: ClientMode) -> CGFloat {
        mode == .chat ? 1100 : 1300
    }

    static func clamped(_ width: CGFloat) -> CGFloat {
        min(max(width, widthBounds.lowerBound), widthBounds.upperBound)
    }
}

/// The panel width and the open-or-closed choice the owner made in each
/// mode, kept on this Mac like the Email inbox width and never sent to the
/// server.
struct InspectorLayoutStore {
    /// This launch's store: the owner's defaults, or a UI journey's own suite.
    static let app: InspectorLayoutStore = {
        #if DEBUG && !SWIFT_PACKAGE
        if let store = ConversationNavigationUITestFixture.makeInspectorLayoutIfRequested() { return store }
        #endif
        return InspectorLayoutStore()
    }()

    var defaults: UserDefaults = .standard

    static func widthKey(_ mode: ClientMode) -> String { "veetbot.inspector.\(mode.rawValue).width" }
    static func openKey(_ mode: ClientMode) -> String { "veetbot.inspector.\(mode.rawValue).open" }

    func width(for mode: ClientMode) -> CGFloat {
        guard let stored = defaults.object(forKey: Self.widthKey(mode)) as? Double, stored.isFinite, stored > 0 else {
            return InspectorLayout.defaultWidth(mode)
        }
        return InspectorLayout.clamped(CGFloat(stored))
    }

    func remember(width: CGFloat, for mode: ClientMode) {
        guard width.isFinite, width > 0 else { return }
        defaults.set(Double(InspectorLayout.clamped(width)), forKey: Self.widthKey(mode))
    }

    func openChoice(for mode: ClientMode) -> Bool? {
        defaults.object(forKey: Self.openKey(mode)) as? Bool
    }

    func remember(open: Bool, for mode: ClientMode) {
        defaults.set(open, forKey: Self.openKey(mode))
    }
}

enum ChatInspectorTab: String, CaseIterable, Identifiable {
    case activity, artifacts
    var id: String { rawValue }
    var title: String { self == .activity ? "Activity" : "Artifacts" }
}

enum EmailInspectorTab: String, CaseIterable, Identifiable {
    case context, activity
    var id: String { rawValue }
    var title: String { self == .context ? "Context" : "Activity" }
}

enum EmailInspectorReveal: Equatable { case sender, attachments }

/// Which panel is open, at what width and on which tab, and what the
/// transcript or thread last asked it to show. The root owns one; the
/// toolbar toggles the active mode's panel.
@MainActor
final class InspectorModel: ObservableObject {
    @Published private var open: [ClientMode: Bool] = [:] {
        didSet { updateMailboxSidebarFold() }
    }
    @Published private var widths: [ClientMode: CGFloat]
    @Published var chatTab: ChatInspectorTab = .activity
    @Published var emailTab: EmailInspectorTab = .context
    /// The work strip the owner chose, as its conversation activity id.
    @Published private(set) var revealedActivityID: String?
    /// Counts reveals, so choosing the same strip again scrolls to it again.
    @Published private(set) var revealCount = 0
    @Published var selectedArtifactID: UUID?
    /// The correspondent the Context card shows; nil shows the latest sender.
    @Published private(set) var sender: PeopleLookup?
    @Published private(set) var senderRevealCount = 0
    /// The message whose envelope the Context card shows; nil shows the latest.
    @Published private(set) var senderMessageID: String?
    @Published private(set) var attachmentsRevealCount = 0
    @Published private(set) var pendingEmailReveal: EmailInspectorReveal?

    /// Returns the unhandled request once the Context panel can display it.
    func consumeEmailReveal() -> EmailInspectorReveal? {
        defer { pendingEmailReveal = nil }
        return pendingEmailReveal
    }

    /// Show original for every message in the open thread, chosen in Context.
    @Published var showsOriginal = false
    /// Email's panel is open in a window too narrow for four columns, so the
    /// mailbox sidebar folds away and the thread keeps its reading width.
    @Published private(set) var foldsMailboxSidebar = false
    private let store: InspectorLayoutStore
    private var windowWidth: CGFloat?

    init(store: InspectorLayoutStore = .app) {
        self.store = store
        widths = Dictionary(uniqueKeysWithValues: ClientMode.allCases.map { ($0, store.width(for: $0)) })
    }

    func isOpen(_ mode: ClientMode) -> Bool { open[mode] ?? false }

    func width(_ mode: ClientMode) -> CGFloat { widths[mode] ?? InspectorLayout.defaultWidth(mode) }

    /// A drag moves the panel's edge live; only the end of the drag is the
    /// owner choosing a width.
    func setWidth(_ width: CGFloat, for mode: ClientMode, remember: Bool) {
        guard width.isFinite else { return }
        widths[mode] = InspectorLayout.clamped(width)
        if remember { store.remember(width: InspectorLayout.clamped(width), for: mode) }
    }

    /// The toolbar button: the owner's choice, kept for the next launch.
    func toggle(_ mode: ClientMode) {
        let value = !isOpen(mode)
        open[mode] = value
        store.remember(open: value, for: mode)
    }

    /// A window too narrow for the panel starts, or becomes, collapsed;
    /// widening it again restores the owner's choice. A squeeze is never
    /// remembered as a choice.
    func windowWidthChanged(_ width: CGFloat) {
        guard width.isFinite, width > 0 else { return }
        let previous = windowWidth
        windowWidth = width
        for mode in ClientMode.allCases {
            let threshold = InspectorLayout.minimumWindowWidth(mode)
            let fits = width >= threshold
            guard previous == nil || (previous! >= threshold) != fits else { continue }
            open[mode] = fits && (store.openChoice(for: mode) ?? true)
        }
        updateMailboxSidebarFold()
    }

    /// The owner showed the mailbox sidebar in a window too narrow for it and
    /// the panel: they take turns, so the panel folds, and as a squeeze this
    /// is not recorded as a choice.
    func mailboxSidebarShown() {
        guard foldsMailboxSidebar else { return }
        open[.email] = false
    }

    private func updateMailboxSidebarFold() {
        let narrow = windowWidth.map { $0 < InspectorLayout.minimumWindowWidth(.email) } ?? false
        let folds = isOpen(.email) && narrow
        if folds != foldsMailboxSidebar { foldsMailboxSidebar = folds }
    }

    /// A work strip in the transcript: its calls in the Activity tab.
    func revealActivity(_ id: String) {
        open[.chat] = true
        chatTab = .activity
        revealedActivityID = id
        revealCount += 1
    }

    /// A file chip in the transcript or a result: its preview in Artifacts.
    func openArtifact(_ id: UUID) {
        open[.chat] = true
        chatTab = .artifacts
        selectedArtifactID = id
    }

    /// A sender's name in the thread: their People card in Context.
    func showSender(_ lookup: PeopleLookup) {
        open[.email] = true
        emailTab = .context
        sender = lookup
        senderRevealCount += 1
        pendingEmailReveal = .sender
    }

    /// A sender's name in the thread: their card and that message's envelope.
    func showMessage(_ message: EmailMessageView) {
        showSender(.correspondent(message.sender, at: message.sentAt))
        senderMessageID = message.id
    }

    /// A message's attachment line: the thread's attachments in Context.
    func revealAttachments() {
        open[.email] = true
        emailTab = .context
        attachmentsRevealCount += 1
        pendingEmailReveal = .attachments
    }

    /// Each thread opens on its latest message, in the reading view.
    func threadChanged() {
        sender = nil
        senderMessageID = nil
        pendingEmailReveal = nil
        showsOriginal = false
    }
}

/// What the transcript and the thread use to reach the panel. Present only
/// on the Mac, where the panel exists; elsewhere work strips expand in place
/// and files open in the viewer sheet.
struct InspectorReveal: Equatable {
    let activity: (String) -> Void
    let artifact: (UUID) -> Void
    let message: (EmailMessageView) -> Void
    let attachments: () -> Void
    /// The strip whose calls the panel shows, drawn in teal.
    let activeActivityID: String?
    /// The thread shows every message's original body, not the reading view.
    let showsOriginal: Bool

    @MainActor
    init(_ model: InspectorModel) {
        activity = { [weak model] in model?.revealActivity($0) }
        artifact = { [weak model] in model?.openArtifact($0) }
        message = { [weak model] in model?.showMessage($0) }
        attachments = { [weak model] in model?.revealAttachments() }
        activeActivityID = model.isOpen(.chat) && model.chatTab == .activity ? model.revealedActivityID : nil
        showsOriginal = model.showsOriginal
    }

    /// The closures always reach the one model, so only the highlighted strip
    /// and the reading choice distinguish two values.
    static func == (lhs: InspectorReveal, rhs: InspectorReveal) -> Bool {
        lhs.activeActivityID == rhs.activeActivityID && lhs.showsOriginal == rhs.showsOriginal
    }
}

private struct InspectorRevealKey: EnvironmentKey {
    static let defaultValue: InspectorReveal? = nil
}

extension EnvironmentValues {
    var inspectorReveal: InspectorReveal? {
        get { self[InspectorRevealKey.self] }
        set { self[InspectorRevealKey.self] = newValue }
    }
}

// MARK: - The ledger

/// The calls between two of the conversation's messages, under the owner's
/// request that started them.
struct ToolLedgerGroup: Identifiable {
    let id: String
    let title: String
    let activities: [ToolActivity]
    /// The first call's number in the whole conversation's ledger.
    let firstIndex: Int
    /// The conversation activity ids this group was made from: single calls
    /// and bundles, as the transcript's work strips name them.
    let memberIDs: [String]

    /// A strip names a single call or a bundle; either way it is here if it
    /// is one of this group's members or one of its calls.
    func contains(activityID: String) -> Bool {
        memberIDs.contains(activityID) || activities.contains { "tool:\($0.id)" == activityID }
    }
}

enum ToolLedgerGroups {
    static func make(from timeline: [ConversationActivity]) -> [ToolLedgerGroup] {
        var groups: [ToolLedgerGroup] = []
        var request: String?
        var pending: [ToolActivity] = []
        var members: [String] = []
        var next = 1

        func close() {
            guard !pending.isEmpty else { return }
            groups.append(
                ToolLedgerGroup(
                    id: members[0], title: request ?? "Tool calls", activities: pending,
                    firstIndex: next, memberIDs: members
                ))
            next += pending.count
            pending = []
            members = []
        }

        for item in timeline {
            switch item {
            case .message(let message):
                close()
                if message.role == .user { request = title(of: message) ?? request }
            case .tool(let activity):
                pending.append(activity)
                members.append(item.id)
            case .toolBundle(let bundle):
                pending.append(contentsOf: bundle.activities)
                members.append(item.id)
            }
        }
        close()
        return groups
    }

    static func group(containing activityID: String, in groups: [ToolLedgerGroup]) -> ToolLedgerGroup? {
        groups.first { $0.contains(activityID: activityID) }
    }

    /// The request's first line, shortened to a heading.
    private static func title(of message: TimelineItem) -> String? {
        let line = message.text.split(whereSeparator: \.isNewline).first.map(String.init)?
            .trimmingCharacters(in: .whitespaces) ?? ""
        guard !line.isEmpty else { return nil }
        return line.count > 80 ? String(line.prefix(79)) + "…" : line
    }
}

// MARK: - Artifacts

/// A file or image the conversation produced, and where it came from.
struct ConversationArtifact: Identifiable, Equatable {
    let id: UUID
    let name: String
    let mediaType: String
    /// The ledger call that made it ("3. workspace.write_text"), or the
    /// message that carried it.
    let source: String
}

/// There is no list-artifacts call for a session, so the list is read from
/// what the conversation already holds: tool results and messages, in order,
/// each artifact once at the first place it appears.
enum ConversationArtifacts {
    static func collect(from timeline: [ConversationActivity]) -> [ConversationArtifact] {
        var found: [ConversationArtifact] = []
        var seen: Set<UUID> = []
        var index = 0

        func add(_ blocks: [ContentBlock], source: String) {
            for block in blocks {
                let entry: (UUID, String, String)?
                switch block {
                case .text: entry = nil
                case .image(let id, let mediaType, _): entry = (id, "Image · \(mediaType)", mediaType)
                case .file(let id, let mediaType, let filename): entry = (id, filename ?? "File", mediaType)
                }
                guard let (id, name, mediaType) = entry, seen.insert(id).inserted else { continue }
                found.append(ConversationArtifact(id: id, name: name, mediaType: mediaType, source: source))
            }
        }

        func addTool(_ activity: ToolActivity) {
            index += 1
            add(activity.result?.content ?? [], source: "\(index). \(activity.name)")
        }

        for item in timeline {
            switch item {
            case .message(let message):
                add(message.content, source: message.role == .user ? "From you" : "In an answer")
            case .tool(let activity):
                addTool(activity)
            case .toolBundle(let bundle):
                bundle.activities.forEach(addTool)
            }
        }
        return found
    }
}

// MARK: - Summaries

/// The panel's Now card: what the current run is doing, in a few words.
struct InspectorRunSummary: Equatable {
    enum Tone { case idle, active, attention, done, failed }
    let title: String
    let detail: String?
    let tone: Tone

    static func chat(runStatus: RunStatus?, awaitingApproval: Bool, activityLabel: String?) -> InspectorRunSummary {
        switch runStatus {
        case nil:
            return InspectorRunSummary(title: "Nothing running", detail: nil, tone: .idle)
        case .queued, .running:
            return InspectorRunSummary(title: "Running", detail: activityLabel, tone: .active)
        case .waitingForApproval:
            return awaitingApproval
                ? InspectorRunSummary(
                    title: "Waiting for your approval", detail: "The card is above the composer.", tone: .attention)
                : InspectorRunSummary(title: "Running", detail: activityLabel, tone: .active)
        case .waitingForUser:
            return InspectorRunSummary(
                title: "Waiting for your answer", detail: "The question is above the composer.", tone: .attention)
        case .completed:
            return InspectorRunSummary(title: "Finished", detail: nil, tone: .done)
        case .failed:
            return InspectorRunSummary(title: "Failed", detail: nil, tone: .failed)
        case .cancelled:
            return InspectorRunSummary(title: "Stopped", detail: nil, tone: .idle)
        case .unknown(let raw):
            return InspectorRunSummary(
                title: raw.replacingOccurrences(of: "_", with: " ").capitalized, detail: nil, tone: .idle)
        }
    }
}

/// Why a thread is in the priority list, as a level and a meter.
struct EmailPriority: Equatable {
    let percent: Int
    let title: String
    let isHigh: Bool

    init(_ value: Double) {
        let held = value.isFinite ? min(max(value, 0), 1) : 0
        percent = Int((held * 100).rounded())
        isHigh = held >= 0.7
        title = held >= 0.7 ? "High priority" : held >= 0.4 ? "Medium priority" : "Low priority"
    }
}

/// The Draft card: the same words the thread's status line uses.
struct EmailDraftSummary: Equatable {
    enum Tone { case attention, draft, settled }
    let title: String
    let detail: String?
    let tone: Tone

    static func make(draft: EmailDraftView?, needsReply: Bool) -> EmailDraftSummary {
        guard let draft else {
            return EmailDraftSummary(
                title: "No draft",
                detail: needsReply
                    ? "Veetbot has not drafted a reply yet." : "Veetbot did not think this needs a written reply.",
                tone: .settled)
        }
        if draft.stale, draft.canEdit {
            return EmailDraftSummary(
                title: "New mail arrived", detail: "The draft was written before the latest message.", tone: .attention)
        }
        switch draft.status {
        case "ready": return EmailDraftSummary(title: "Draft ready", detail: nil, tone: .draft)
        case "generating": return EmailDraftSummary(title: "Preparing draft", detail: nil, tone: .draft)
        case "awaiting_approval": return EmailDraftSummary(title: "Awaiting approval", detail: nil, tone: .attention)
        case "sending": return EmailDraftSummary(title: "Sending reply", detail: nil, tone: .draft)
        case "sent": return EmailDraftSummary(title: "Reply sent", detail: nil, tone: .settled)
        case "uncertain": return EmailDraftSummary(title: "Check send status", detail: nil, tone: .attention)
        case "failed": return EmailDraftSummary(title: "Draft needs attention", detail: nil, tone: .attention)
        default: return EmailDraftSummary(title: "Draft", detail: nil, tone: .draft)
        }
    }
}

/// What Veetbot made of a thread: its gist and the state the row's chip
/// names. A thread Veetbot has not read yet says so once, rather than as its
/// gist and again as its reason.
struct EmailThreadSummary: Equatable {
    enum Tone { case attention, draft, settled }
    let gist: String
    let isPending: Bool
    let state: String
    let tone: Tone
    /// Why the thread is in the priority list; nil when it would only repeat the gist.
    let reason: String?

    /// The server's gist and reason until the thread has been assessed.
    static let pendingText = "Assessment pending"

    static func make(thread: EmailThreadView, draft: EmailDraftView?) -> EmailThreadSummary {
        let gist = thread.summary.trimmingCharacters(in: .whitespacesAndNewlines)
        let reason = thread.reason.trimmingCharacters(in: .whitespacesAndNewlines)
        let (state, tone) = state(thread: thread, draft: draft)
        return EmailThreadSummary(
            gist: gist, isPending: gist == pendingText, state: state, tone: tone,
            reason: reason.isEmpty || reason == gist ? nil : reason)
    }

    /// An attention state, a draft state, or a settled conversation, in the
    /// words the row's chip and the thread's status line use.
    static func state(thread: EmailThreadView, draft: EmailDraftView?) -> (title: String, tone: Tone) {
        if thread.isInSpam { return ("Reported as spam", .settled) }
        if thread.isArchived { return ("Archived", .settled) }
        if thread.isHandled { return ("Handled", .settled) }
        switch (draft ?? thread.draft)?.status {
        case "ready": return ("Draft ready", .draft)
        case "generating": return ("Preparing draft", .draft)
        case "awaiting_approval": return ("Awaiting approval", .attention)
        case "sending": return ("Sending reply", .draft)
        case "sent": return ("Reply sent", .draft)
        case "uncertain": return ("Check send status", .attention)
        case "failed": return ("Draft needs attention", .attention)
        default:
            return thread.draftID != nil
                ? ("Draft", .draft)
                : thread.needsReply ? ("Needs reply", .attention) : ("For your attention", .attention)
        }
    }
}

// MARK: - A run's calls, replayed

/// Replays one run's events into a reducer of its own, so the Email panel
/// can show the calls that produced a draft without touching Chat's
/// conversation.
@MainActor
final class RunActivityReplay: ObservableObject {
    let state = RunStateReducer()
    @Published private(set) var runID: UUID?
    @Published private(set) var isLoading = false
    @Published private(set) var errorMessage: String?

    func load(runID: UUID, frames: (UUID) -> AsyncThrowingStream<SSEFrame, Error>) async {
        guard self.runID != runID else { return }
        self.runID = runID
        state.reset()
        errorMessage = nil
        isLoading = true
        defer { isLoading = false }
        do {
            for try await frame in frames(runID) {
                guard self.runID == runID else { return }
                state.reduce(frame)
            }
        } catch is CancellationError {
            self.runID = nil
        } catch {
            if self.runID == runID { errorMessage = error.localizedDescription }
        }
    }
}
