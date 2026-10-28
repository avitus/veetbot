import SwiftUI

#if os(macOS)
    import AppKit
#else
    import UIKit
#endif

/// The text sizes of the A2 Email reference: 13 / 13 / 12 / 11.5 points on the
/// Mac and 16 / 15 / 13.5 / 12.5 on the iPhone, expressed as the nearest
/// Dynamic Type styles so the owner's text size still applies.
enum EmailType {
    #if os(macOS)
    static let sender: VeetType.Role = .ui
    static let subject: VeetType.Role = .ui
    static let gist: VeetType.Role = .meta
    static let note: VeetType.Role = .meta
    static let meta: VeetType.Role = .meta
    static let rowSpacing: CGFloat = 2
    static let handledHit: CGFloat = 28
    #else
    static let sender: VeetType.Role = .ui
    static let subject: VeetType.Role = .ui
    static let gist: VeetType.Role = .meta
    static let note: VeetType.Role = .meta
    static let meta: VeetType.Role = .meta
    static let rowSpacing: CGFloat = 3
    static let handledHit: CGFloat = 44
    #endif
    static let handledDiameter: CGFloat = 18
}

private extension EmailThreadView {
    /// The people line of a row or heading; a conversation with no sender still reads.
    var correspondentsOrUnknown: String { correspondents.isEmpty ? "Unknown sender" : correspondents }
}

#if os(macOS)
/// The inbox width the owner chose by dragging the Mac Email divider, kept on
/// this Mac and never sent to the server. Before any drag, and for an
/// unreadable value, the split view lays the inbox out as it always has.
struct EmailInboxWidthStore {
    static let key = "veetbot.email.inboxWidth"
    static let bounds: ClosedRange<CGFloat> = 260...480
    /// The design's starting width (MagicPath cozy-evening-6871).
    static let idealWidth: CGFloat = 300
    /// This launch's store: the owner's defaults, or a UI journey's own suite.
    static let app: EmailInboxWidthStore = {
        #if DEBUG && !SWIFT_PACKAGE
        if let store = ConversationNavigationUITestFixture.makeEmailInboxWidthIfRequested() { return store }
        #endif
        return EmailInboxWidthStore()
    }()
    var defaults: UserDefaults = .standard
    /// The event AppKit is handling, which tells the owner's drag from a layout pass.
    var currentEvent: () -> NSEvent? = { NSApp?.currentEvent }

    /// The remembered width, held within the column's bounds.
    var width: CGFloat? {
        guard let stored = defaults.object(forKey: Self.key) as? Double, stored.isFinite, stored > 0 else {
            return nil
        }
        return Self.clamped(CGFloat(stored))
    }

    func remember(_ width: CGFloat) {
        guard width.isFinite, width > 0 else { return }
        defaults.set(Double(Self.clamped(width)), forKey: Self.key)
    }

    private static func clamped(_ width: CGFloat) -> CGFloat {
        min(max(width, bounds.lowerBound), bounds.upperBound)
    }
}

/// The mailbox sidebar width the owner chose by dragging its edge, kept on
/// this Mac like the inbox width and never sent to the server. Before any
/// drag it is the design's 260 points (MagicPath cozy-evening-6871).
struct EmailSidebarWidthStore {
    static let key = "veetbot.email.sidebarWidth"
    static let bounds: ClosedRange<CGFloat> = 200...340
    static var defaultWidth: CGFloat { A2Metrics.sidebarWidth }
    /// This launch's store: the owner's defaults, or a UI journey's own suite.
    static let app: EmailSidebarWidthStore = {
        #if DEBUG && !SWIFT_PACKAGE
        if let store = ConversationNavigationUITestFixture.makeEmailSidebarWidthIfRequested() { return store }
        #endif
        return EmailSidebarWidthStore()
    }()
    var defaults: UserDefaults = .standard

    /// The remembered width, held within the column's bounds.
    var width: CGFloat {
        guard let stored = defaults.object(forKey: Self.key) as? Double, stored.isFinite, stored > 0 else {
            return Self.defaultWidth
        }
        return Self.clamped(CGFloat(stored))
    }

    func remember(_ width: CGFloat) {
        guard width.isFinite, width > 0 else { return }
        defaults.set(Double(Self.clamped(width)), forKey: Self.key)
    }

    /// A double-click on the divider returns the sidebar to the design width.
    func forget() {
        defaults.removeObject(forKey: Self.key)
    }

    static func clamped(_ width: CGFloat) -> CGFloat {
        min(max(width, bounds.lowerBound), bounds.upperBound)
    }
}

/// How the Mac Email columns share a window: the sidebar, the inbox and the
/// right panel take the widths the owner gave them, and the reader the rest.
enum EmailColumnLayout {
    /// A drag stops before the reader would fall below this (the design's
    /// `READER_MIN`).
    static let readerMinimum: CGFloat = 420
    /// A window too narrow for every column squeezes the reader to this
    /// before it squeezes the sidebar.
    static let readerSqueezeMinimum: CGFloat = 250

    /// Where a drag of the sidebar's edge to `width` leaves it, `available`
    /// being the room for the sidebar, the inbox and the reader. Narrowing is
    /// always allowed; widening stops where the reader keeps its minimum, and
    /// never pulls the column in from where it already is.
    static func sidebarDrag(to width: CGFloat, from current: CGFloat, available: CGFloat, inbox: CGFloat) -> CGFloat {
        let room = available - inbox - readerMinimum
        return EmailSidebarWidthStore.clamped(min(width, max(current, room)))
    }

    /// The sidebar's and the inbox's hairlines.
    static let hairlines: CGFloat = 2

    /// The width the sidebar shows at: the owner's, unless the window cannot
    /// hold it beside the narrowest inbox and a squeezed reader.
    static func sidebarShown(stored: CGFloat, available: CGFloat) -> CGFloat {
        let room = available - EmailInboxWidthStore.bounds.lowerBound - readerSqueezeMinimum - hairlines
        return EmailSidebarWidthStore.clamped(min(stored, room))
    }
}

/// Sits inside the inbox column of the Mac Email split. `HSplitView` neither
/// saves its divider nor honours an ideal width, so this view moves the divider
/// to the remembered width once the split first lays out, and remembers the
/// width the owner drags it to.
private struct EmailInboxWidthAutosave: NSViewRepresentable {
    let store: EmailInboxWidthStore

    func makeNSView(context: Context) -> EmailInboxWidthAutosaveView {
        let view = EmailInboxWidthAutosaveView()
        view.store = store
        return view
    }

    func updateNSView(_ view: EmailInboxWidthAutosaveView, context: Context) {
        view.store = store
    }
}

private final class EmailInboxWidthAutosaveView: NSView {
    var store = EmailInboxWidthStore()
    private weak var split: NSSplitView?
    private var restoreScheduled = false
    private var restored = false
    private var restoring = false

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        let enclosing = sequence(first: self as NSView, next: \.superview)
            .lazy.compactMap { $0 as? NSSplitView }.first
        guard enclosing !== split else { return }
        let center = NotificationCenter.default
        if let split { center.removeObserver(self, name: NSSplitView.didResizeSubviewsNotification, object: split) }
        split = enclosing
        guard let enclosing else { return }
        center.addObserver(self, selector: #selector(splitResizedSubviews),
                           name: NSSplitView.didResizeSubviewsNotification, object: enclosing)
        scheduleRestore()
    }

    override func setFrameSize(_ newSize: NSSize) {
        super.setFrameSize(newSize)
        scheduleRestore()
    }

    /// AppKit reports every divider move, including the ones a layout pass
    /// makes to keep the reading column at its minimum in a narrow window.
    /// Only a pointer drag in this window, outside a live resize of the window
    /// itself, is the owner choosing a width; a squeezed column leaves the
    /// choice alone. The split view is in live resize during its own divider
    /// drag, so only the window's state tells the two drags apart.
    @objc private func splitResizedSubviews() {
        guard restored, !restoring, let split, let column = inboxColumn,
              let event = store.currentEvent(), event.type == .leftMouseDragged,
              let window = split.window, event.windowNumber == window.windowNumber, !window.inLiveResize
        else { return }
        store.remember(column.frame.width)
    }

    /// The split subview that holds the inbox.
    private var inboxColumn: NSView? {
        split?.subviews.first { isDescendant(of: $0) }
    }

    /// Restores after the layout pass in progress, never from inside it.
    private func scheduleRestore() {
        guard !restored, !restoreScheduled, split != nil else { return }
        restoreScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            restoreScheduled = false
            restoreOnceLaidOut()
        }
    }

    private func restoreOnceLaidOut() {
        guard !restored, let split, split.bounds.width > 0, let column = inboxColumn,
              let index = split.subviews.firstIndex(of: column), index < split.subviews.count - 1
        else { return }
        restored = true
        // Before any drag the inbox opens at the design's width.
        let width = store.width ?? EmailInboxWidthStore.idealWidth
        // Restoring is not a choice: a window too narrow for the remembered
        // width holds the column below it, and the remembered width stays.
        restoring = true
        split.setPosition(column.frame.minX + width, ofDividerAt: index)
        restoring = false
    }
}
#endif

public struct EmailModeView: View {
    @ObservedObject var model: EmailViewModel
    var viewportHeight: CGFloat? = nil
    /// False once the Mac window's sidebar button hides the sidebar.
    var mailboxSidebarVisible = true
    #if os(macOS)
    var inboxWidth = EmailInboxWidthStore.app
    var sidebarStore = EmailSidebarWidthStore.app
    #endif
    let discussInChat: () async -> Void
    var openChat: () -> Void = {}
    /// The mailbox sidebar's mode switch: Cockpit, Chat or Email.
    var openMode: (ClientMode) -> Void = { _ in }
    var newChat: () -> Void = {}
    var openMemory: () -> Void = {}
    var openPeople: () -> Void = {}
    var openSchedules: () -> Void = {}
    var openPersona: () -> Void = {}
    var openSettings: () -> Void = {}
    var chatAttention: [SidebarAttention.Row] = []
    var openAttention: (UUID) -> Void = { _ in }
    /// The Mac right panel and the run stream its Activity tab replays (ADR-0237).
    var inspector: InspectorModel? = nil
    var runFrames: ((UUID) -> AsyncThrowingStream<SSEFrame, Error>)? = nil
    @EnvironmentObject private var appearance: AppearancePreferences
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.activeClientMode) private var activeMode
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showingLearning = false
    @State private var showingSubscriptions = false
    @State private var showingBudgetDetails = false
    @State private var showingOtherMailViews = false
    @State private var navigationPath: [UUID] = []
    #if os(macOS)
    /// The sidebar's live width while its edge is dragged; nil reads the store.
    @State private var draggedSidebarWidth: CGFloat?
    /// The width the four columns share, measured without affecting their layout.
    @State private var columnsWidth: CGFloat = 0
    #endif
    #if os(iOS)
        @Environment(\.horizontalSizeClass) private var sizeClass
    #endif

    private var directSelection: Bool {
        #if os(macOS)
            true
        #else
            sizeClass == .regular
        #endif
    }

    /// Without the mailbox sidebar, its search, accounts and collections sit above the inbox.
    private var showsMailboxSidebar: Bool { directSelection && mailboxSidebarVisible }

    /// Preserves platform navigation and presents learning and exact-send review above the inbox.
    public var body: some View {
        Group {
            if directSelection {
                #if os(macOS)
                macColumns
                #else
                HStack(spacing: 0) {
                    if showsMailboxSidebar { mailboxSidebar.frame(width: 232) }
                    VStack(spacing: 0) {
                        emailHeading
                        HStack(spacing: 0) {
                            inbox.frame(width: 300)
                            HairlineDivider().frame(width: 1)
                            detail.frame(maxWidth: .infinity)
                        }
                    }
                }
                #endif
            } else {
                NavigationStack(path: $navigationPath) {
                    inbox.navigationDestination(for: UUID.self) { id in
                        detail.task { if model.selectedThreadID != id { await model.openThread(id) } }
                    }
                }
            }
        }
        .tint(AppTheme.teal)
        .sheet(isPresented: Binding(get: { model.review != nil }, set: { if !$0 { model.closeReview() } })) {
            EmailSendReview(model: model)
        }
        .sheet(isPresented: $showingLearning) { EmailLearningScreen(model: model) }
        .sheet(isPresented: $showingSubscriptions) {
            EmailSubscriptionsScreen(model: model.subscriptions, accounts: model.accounts)
        }
        // A server that stops advertising support must not leave the surface open.
        .onChange(of: model.unsubscribeAvailable) { _, available in
            if !available { showingSubscriptions = false }
        }
        .onAppear {
            if let id = model.selectedThreadID, navigationPath.last != id { navigationPath = [id] }
        }
        .onChange(of: model.selectedThreadID) { _, id in
            if let id, navigationPath.last != id { navigationPath = [id] } else if id == nil { navigationPath = [] }
            inspector?.threadChanged()
        }
        .onChange(of: navigationPath) { _, path in
            if path.isEmpty, !directSelection, model.selectedThreadID != nil { model.clearSelection() }
        }
    }

    /// On a compact iPhone the thread is pushed, so its actions join the back
    /// button in the navigation bar, as the A2 iPhone reference draws it.
    private var detail: some View {
        EmailThreadScreen(
            model: model, subscriptions: model.subscriptions, discussInChat: discussInChat,
            actionsInNavigationBar: !directSelection
        )
        .id(model.selectedThreadID)
    }

    #if os(macOS)
    /// The thread's right panel beside the inbox and reading columns, when the
    /// root provides one (ADR-0237).
    @ViewBuilder
    private func withPanel<Content: View>(_ content: Content) -> some View {
        if let inspector {
            content.macInspector(inspector, mode: .email) {
                EmailInspectorPanel(model: model, inspector: inspector, runFrames: runFrames)
            }
        } else {
            content
        }
    }

    /// The four Mac columns of the clean reader (MagicPath cozy-evening-6871):
    /// the sidebar, the inbox and the right panel each keep the width the
    /// owner drags them to, and the reader takes the rest. The window's
    /// toolbar carries the title (RootView), so no heading sits above them.
    private var macColumns: some View {
        let panel = inspector.map { $0.isOpen(.email) ? $0.width(.email) + 1 : 0 } ?? 0
        let available = columnsWidth > 0 ? columnsWidth - panel : .infinity
        let inbox = inboxWidth.width ?? EmailInboxWidthStore.idealWidth
        let sidebar = draggedSidebarWidth ?? sidebarStore.width
        let shownSidebar = showsMailboxSidebar ? EmailColumnLayout.sidebarShown(stored: sidebar, available: available) + 1 : 0
        return HStack(spacing: 0) {
            if showsMailboxSidebar {
                mailboxSidebar
                    .frame(width: shownSidebar - 1)
                    .frame(maxHeight: .infinity)
                // The drag starts from the width on screen: a narrow window may
                // squeeze the sidebar below the width the owner stored.
                MacColumnDivider(
                    edge: .trailing, width: shownSidebar - 1, label: "Sidebar width", identifier: "email.sidebar.resize",
                    resize: { width, remember in
                        let next = EmailColumnLayout.sidebarDrag(
                            to: width, from: shownSidebar - 1, available: available, inbox: inbox)
                        if remember {
                            sidebarStore.remember(next)
                            draggedSidebarWidth = nil
                        } else {
                            draggedSidebarWidth = next
                        }
                    },
                    reset: {
                        sidebarStore.forget()
                        draggedSidebarWidth = nil
                    }
                )
                // The hairline's grab area reaches into both neighbours; the
                // inbox split view starts where the hairline ends, and a later
                // sibling is hit-tested first, so without this the half of the
                // grab area over the inbox, and the boundary point itself on a
                // 1x display, belong to the inbox list instead of the hairline.
                .zIndex(1)
            }
            // The toolbar's hairline (the design's scrolled toolbar) also keeps the
            // inbox and the thread from scrolling up beneath the window toolbar.
            withPanel(VStack(spacing: 0) {
                HairlineDivider()
                macSplit
            })
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { columnsWidth = $0 }
        // The panel's edge stops where the reader would fall below its minimum.
        .onChange(of: columnsWidth - shownSidebar - inbox - EmailColumnLayout.readerMinimum - 1, initial: true) {
            _, limit in inspector?.setWidthLimit(columnsWidth > 0 ? limit : nil, for: .email)
        }
    }

    /// Uses a native divider while the shared root owns the Mac window toolbar.
    private var macSplit: some View {
        HSplitView {
            inbox
                .frame(minWidth: EmailInboxWidthStore.bounds.lowerBound, idealWidth: EmailInboxWidthStore.idealWidth,
                       maxWidth: EmailInboxWidthStore.bounds.upperBound)
                .frame(maxHeight: .infinity)
                .background(EmailInboxWidthAutosave(store: inboxWidth))
            detail
                .frame(minWidth: EmailColumnLayout.readerSqueezeMinimum, maxWidth: .infinity)
                .frame(maxHeight: .infinity)
                .layoutPriority(1)
        }
    }
    #endif

    private var visibleAccounts: [EmailAccountView] {
        model.accounts.filter { model.selectedAccountID == nil || $0.id == model.selectedAccountID }
    }

    /// Groups triage controls above independently actionable rows and per-account freshness below them.
    private var inbox: some View {
        VStack(spacing: 0) {
            if !directSelection { emailHeading }
            if !showsMailboxSidebar { inboxHeader.fixedSize(horizontal: false, vertical: true) }
            HairlineDivider()
            List {
                if model.unavailable {
                    EmailEmptyState(
                        symbol: "envelope.badge", title: "Email isn’t available",
                        message: "This server does not support Email mode yet."
                    )
                    .emailHideSeparator()
                } else if let error = model.errorMessage {
                    VStack(alignment: .leading, spacing: 12) {
                        Label("Mail couldn’t be updated", systemImage: "exclamationmark.circle").veetType(.ui, weight: .semibold)
                        Text(error).veetType(.ui).foregroundColor(AppTheme.inkSoft)
                        Button("Try again") {
                            Task {
                                await model.reload()
                                await model.refresh()
                            }
                        }
                        .buttonStyle(SecondaryCapsuleButtonStyle())
                    }.padding(.vertical, 16).emailHideSeparator()
                }
                if !showsMailboxSidebar, let pause = model.budgetPauseMessage {
                    budgetPause(pause)
                        .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
                        .emailHideSeparator()
                }
                if model.isLoading && model.items.isEmpty {
                    ProgressView("Finding your mail…").padding(.vertical, 32)
                        .frame(maxWidth: .infinity).emailHideSeparator()
                } else if model.items.isEmpty && !model.unavailable && model.errorMessage == nil && model.budgetPauseMessage == nil {
                    emptyInbox.emailHideSeparator()
                }
                ForEach(model.items) { thread in
                    HStack(alignment: .top, spacing: 10) {
                        EmailHandledCircle(model: model, thread: thread, accessibilityID: "email.handled.\(thread.id.uuidString)")
                        VStack(alignment: .leading, spacing: EmailType.rowSpacing) {
                            threadRow(thread)
                                .accessibilityIdentifier("email.thread.\(thread.id.uuidString)")
                            // Beside the row's button, not inside its label: AppKit folds a
                            // button's label into one element, and the journeys read this text.
                            EmailArchiveStatus(model: model, thread: thread)
                        }
                    }
                    .buttonStyle(.plain)
                    .modifier(EmailArrivalWash(model: model, threadID: thread.id))
                    // An arrival slides down into its place; a row that leaves fades.
                    .transition(reduceMotion ? .opacity : .asymmetric(
                        insertion: .move(edge: .top).combined(with: .opacity), removal: .opacity))
                    .modifier(EmailRowChrome(selected: thread.id == model.selectedThreadID))
                }
                if model.hasMore {
                    Button(model.listView == "priority" ? "More important" : "More mail") {
                        Task { await model.loadMore() }
                    }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .disabled(model.isLoading).padding(.vertical, 8)
                    .emailHideSeparator()
                }
            }
            .animation(reduceMotion ? nil : .spring(duration: 0.45, bounce: 0.15), value: model.items.map(\.id))
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .frame(minHeight: 0, maxHeight: .infinity)
            if !visibleAccounts.isEmpty {
                HairlineDivider()
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Check a conversation to archive it in Gmail.")
                            .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                        if visibleAccounts.contains(where: { $0.archiveSupported != true }) {
                            Text("Gmail archiving is unavailable for accounts without archive support. Email browsing remains available.")
                                .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                        }
                        ForEach(visibleAccounts) { account in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(account.label).veetType(.ui, weight: .semibold)
                                if let update = account.updateMessage {
                                    Text(update).foregroundColor(account.hasRefreshFailure ? AppTheme.amberText : AppTheme.inkSoft)
                                } else if let date = account.lastSyncedAt {
                                    HStack(spacing: 4) {
                                        Text("Last updated")
                                        Text(date, style: .relative)
                                    }
                                    .foregroundColor(AppTheme.inkSoft)
                                } else {
                                    Text("Not yet synchronized.").foregroundColor(AppTheme.inkSoft)
                                }
                                if let coverage = account.coverageMessage() {
                                    Text(coverage).foregroundColor(AppTheme.inkSoft)
                                }
                            }.veetType(.meta)
                        }
                    }.padding(.top, 10).frame(maxWidth: .infinity, alignment: .leading)
                } label: {
                    Label(
                        mailboxStatus,
                        systemImage: mailboxNeedsAttention ? "exclamationmark.circle" : "arrow.triangle.2.circlepath"
                    )
                    .veetType(.meta).foregroundColor(mailboxNeedsAttention ? AppTheme.amberText : AppTheme.inkSoft)
                }
                .padding(.horizontal, 12).padding(.vertical, 10)
                .accessibilityIdentifier("email.mailbox-status")
            }
        }
        .background(AppTheme.paper)
        .navigationTitle("Email")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            // Remove inactive items themselves so the other mode inherits no empty toolbar slots.
            ToolbarItemGroup {
                if activeMode == .email {
                    // An account that advertises no unsubscribe support offers no
                    // entry, and nothing else is offered in its place.
                    if model.unsubscribeAvailable {
                        Button {
                            showingSubscriptions = true
                        } label: {
                            Image(systemName: "tray.full")
                        }
                        .accessibilityLabel("Subscriptions").help("Subscriptions")
                        .accessibilityIdentifier("email.subscriptions.open")
                    }
                    Button {
                        showingLearning = true
                    } label: {
                        Image(systemName: "slider.horizontal.3")
                    }
                    .accessibilityLabel("Email learning").help("Email learning")
                    .accessibilityIdentifier("email.learning")
                    EmailRefreshButton(model: model)
                }
            }
        }
        #endif
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("email.inbox")
    }

    private var emailHeading: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: 4) {
                Text("Email").veetType(.display)
                Text(threadCount).veetType(.meta).foregroundColor(AppTheme.inkSoft)
            }
            Spacer()
            #if os(iOS)
            if directSelection { EmailRefreshButton(model: model) }
            #endif
            EmailAppearanceButton().buttonStyle(QuietCapsuleButtonStyle())
        }
        .padding(.horizontal, 24).padding(.vertical, 16)
        .background(AppTheme.paper)
    }

    #if os(macOS)
    /// The Mac sidebar in the design's order (MagicPath cozy-evening-6871):
    /// the mode switch and Search mail, then Mail (the collections), then
    /// More (other mail, subscriptions and learning). The account picker,
    /// Chat's pending attention and the shortcuts to the other surfaces keep
    /// their places beneath, in the same quiet rows.
    private var mailboxSidebar: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: VeetSpace.s2) {
                ModeSegment(selected: .email, choose: openMode)
                    .frame(maxWidth: .infinity)
                searchField
            }
            .padding(.horizontal, A2Metrics.sidebarGutter).padding(.top, VeetSpace.s3).padding(.bottom, VeetSpace.s2)
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    SidebarSectionLabel(title: "Mail")
                    ForEach(EmailCollection.allCases, id: \.self) { collection in
                        Button { model.setCollection(collection) } label: {
                            SidebarRow {
                                if collection == .needsYou {
                                    Circle().fill(AppTheme.amber).frame(width: 6, height: 6)
                                } else {
                                    Image(systemName: collectionSymbol(collection))
                                }
                            } title: {
                                Text(collectionTitle(collection))
                                    .fontWeight(model.listView == collection.rawValue ? .medium : .regular)
                            } trailing: { _ in
                                if let count = model.collectionCounts?.count(for: collection) { Text(count, format: .number) }
                            }
                            .padding(.horizontal, VeetSpace.s2)
                            .background(RoundedRectangle(cornerRadius: A2Metrics.rowRadius, style: .continuous)
                                .fill(model.listView == collection.rawValue ? AppTheme.selected : .clear))
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(model.listView == collection.rawValue ? .isSelected : [])
                        .accessibilityIdentifier("email.collection.\(collectionID(collection))")
                    }
                    SidebarSectionLabel(title: "More")
                    if model.unsubscribeAvailable {
                        Button { showingSubscriptions = true } label: {
                            SidebarRow { Image(systemName: "envelope") } title: { Text("Subscriptions") }
                                .padding(.horizontal, VeetSpace.s2)
                        }.buttonStyle(.plain)
                        .accessibilityIdentifier("email.mailbox.subscriptions")
                    }
                    Button { showingLearning = true } label: {
                        SidebarRow { Image(systemName: "slider.horizontal.3") } title: { Text("Email learning") }
                            .padding(.horizontal, VeetSpace.s2)
                    }.buttonStyle(.plain)
                    otherMailDisclosure
                        .padding(.horizontal, VeetSpace.s2).padding(.top, VeetSpace.s2)
                    if !chatAttention.isEmpty {
                        SidebarSectionLabel(title: "Needs you")
                        ForEach(chatAttention) { row in
                            Button { openAttention(row.entry.sessionID) } label: {
                                SidebarRow { AttentionDot(kind: row.kind) } title: {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(row.label)
                                        Text(row.entry.title).veetType(.meta).foregroundColor(AppTheme.inkSoft).lineLimit(1)
                                    }
                                }
                                .padding(.horizontal, VeetSpace.s2)
                            }.buttonStyle(.plain)
                            .accessibilityLabel("\(row.label), \(row.entry.title)")
                            .accessibilityIdentifier("email.chat-attention.\(row.id)")
                        }
                    }
                    SidebarSectionLabel(title: "Accounts")
                    accountsPicker.padding(.horizontal, VeetSpace.s1)
                    if let pause = model.budgetPauseMessage { budgetPause(pause).padding(.top, VeetSpace.s3) }
                }
                .padding(.horizontal, A2Metrics.sidebarGutter).padding(.bottom, VeetSpace.s4)
            }
            HStack {
                Button(action: newChat) { Image(systemName: "square.and.pencil").frame(maxWidth: .infinity, minHeight: 32) }
                    .buttonStyle(.plain).foregroundColor(AppTheme.inkSoft)
                    .accessibilityLabel("New chat").help("New chat")
                    .accessibilityIdentifier("email.new-chat")
                globalButton("Chat", "bubble.left", openChat)
                globalButton("Memory", "brain", openMemory)
                globalButton("People", "person.2", openPeople)
                globalButton("Schedules", "calendar.badge.clock", openSchedules)
                globalButton("Persona", "sparkles", openPersona)
                globalButton("Settings", "gearshape", openSettings)
            }.padding(10)
        }.background(AppTheme.paperDeep)
    }
    #else
    private var mailboxSidebar: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    inboxHeader
                    Button(action: newChat) { Label("New chat", systemImage: "plus").frame(maxWidth: .infinity) }
                        .primaryCapsule().accessibilityIdentifier("email.new-chat")
                    ModeSegment(selected: .email, choose: openMode)
                    if !chatAttention.isEmpty {
                        SidebarSectionLabel(title: "Needs you")
                        ForEach(chatAttention) { row in
                            Button { openAttention(row.entry.sessionID) } label: {
                                SidebarRow { AttentionDot(kind: row.kind) } title: {
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(row.label)
                                        Text(row.entry.title).veetType(.meta).foregroundColor(AppTheme.inkSoft).lineLimit(1)
                                    }
                                }
                            }.buttonStyle(.plain)
                            .accessibilityLabel("\(row.label), \(row.entry.title)")
                            .accessibilityIdentifier("email.chat-attention.\(row.id)")
                        }
                    }
                    SidebarSectionLabel(title: "Mailbox")
                    ForEach(EmailCollection.allCases, id: \.self) { collection in
                        Button { model.setCollection(collection) } label: {
                            SidebarRow { Image(systemName: collectionSymbol(collection)) } title: {
                                Text(collectionTitle(collection))
                            } trailing: { _ in
                                if let count = model.collectionCounts?.count(for: collection) { Text(count, format: .number) }
                            }
                            .padding(.horizontal, 8)
                            .background(RoundedRectangle(cornerRadius: 7).fill(model.listView == collection.rawValue ? AppTheme.selected : .clear))
                        }
                        .buttonStyle(.plain)
                        .accessibilityAddTraits(model.listView == collection.rawValue ? .isSelected : [])
                        .accessibilityIdentifier("email.collection.\(collectionID(collection))")
                    }
                    if model.unsubscribeAvailable {
                        Button { showingSubscriptions = true } label: {
                            SidebarRow { Image(systemName: "tray.full") } title: { Text("Subscriptions") }
                        }.buttonStyle(.plain)
                        #if os(macOS)
                        .accessibilityIdentifier("email.mailbox.subscriptions")
                        #else
                        .accessibilityIdentifier("email.subscriptions.open")
                        #endif
                    }
                    Button { showingLearning = true } label: {
                        SidebarRow { Image(systemName: "slider.horizontal.3") } title: { Text("Email learning") }
                    }.buttonStyle(.plain)
                    #if os(iOS)
                    .accessibilityIdentifier("email.learning")
                    #endif
                    if let pause = model.budgetPauseMessage { budgetPause(pause) }
                }.padding(12)
            }
            HStack {
                globalButton("Chat", "bubble.left", openChat)
                globalButton("Memory", "brain", openMemory)
                globalButton("People", "person.2", openPeople)
                globalButton("Schedules", "calendar.badge.clock", openSchedules)
                globalButton("Persona", "sparkles", openPersona)
                globalButton("Settings", "gearshape", openSettings)
            }.padding(10)
        }.background(AppTheme.paperDeep)
    }

    #endif

    private func globalButton(_ title: String, _ symbol: String, _ action: @escaping () -> Void) -> some View {
        Button(action: action) { Image(systemName: symbol).frame(maxWidth: .infinity, minHeight: 32) }
            .buttonStyle(.plain).foregroundColor(AppTheme.inkSoft)
            .accessibilityLabel(title).help(title)
            .accessibilityIdentifier("email.global.\(title.lowercased())")
    }

    private func collectionID(_ value: EmailCollection) -> String {
        switch value { case .inbox: "inbox"; case .needsYou: "needsYou"; case .handled: "handled" }
    }
    private func collectionTitle(_ value: EmailCollection) -> String {
        switch value { case .inbox: "Inbox"; case .needsYou: "Needs you"; case .handled: "Handled" }
    }
    private func collectionSymbol(_ value: EmailCollection) -> String {
        switch value { case .inbox: "tray"; case .needsYou: "flag"; case .handled: "checkmark" }
    }

    /// A paused budget is one short note on paperDeep, three lines tall with
    /// its accounting folded, so the first row still starts near the top.
    private func budgetPause(_ pause: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Label("Processing paused", systemImage: "pause.circle")
                    .veetType(.meta, weight: .semibold).foregroundColor(AppTheme.amberText)
                Spacer(minLength: 4)
                Button("Check again") { Task { await model.refresh() } }
                    .buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.tealText)
            }
            Text(pause).veetType(.meta).foregroundColor(AppTheme.inkSoft)
            HStack(alignment: .firstTextBaseline) {
                if let retryAt = model.budgetRetryAt {
                    Text("Next check: \(retryAt.formatted(date: .abbreviated, time: .shortened))")
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft).lineLimit(1)
                }
                Spacer(minLength: 4)
                Button {
                    showingBudgetDetails.toggle()
                } label: {
                    Label("Details", systemImage: showingBudgetDetails ? "chevron.down" : "chevron.right")
                }
                .buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.inkSoft)
                .accessibilityValue(showingBudgetDetails ? "Expanded" : "Collapsed")
                .accessibilityIdentifier("email.budget-details")
            }
            if showingBudgetDetails {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(model.budgetPauseDetails, id: \.self) { Text($0) }
                    Text("Cached mail and editing remain available.")
                }.frame(maxWidth: .infinity, alignment: .leading)
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
            }
        }
        .padding(8).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(AppTheme.paperDeep))
    }

    /// Present account and inbox controls above the priority rows.
    private var inboxHeader: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                accountsPicker
                Spacer(minLength: 8)

            }
            searchField
            if !showsMailboxSidebar {
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        ForEach(EmailCollection.allCases, id: \.self) { collection in
                            Button {
                                model.setCollection(collection)
                            } label: {
                                // One label, so the pill style draws one capsule around title and count.
                                HStack(spacing: 6) {
                                    Text(collectionTitle(collection))
                                    if let count = model.collectionCounts?.count(for: collection) { Text(count, format: .number) }
                                }
                            }
                            .buttonStyle(FilterPillButtonStyle(selected: model.listView == collection.rawValue))
                            .accessibilityIdentifier("email.collection.\(collectionID(collection))")
                        }
                    }
                }
            }
            otherMailDisclosure
        }.padding(12)
    }

    private var accountsPicker: some View {
        Picker("Accounts", selection: Binding(get: { model.selectedAccountID }, set: { model.setAccount($0) })) {
            Text("All accounts").tag(String?.none)
            ForEach(model.accounts) { account in Text(account.label).tag(Optional(account.id)) }
        }
        .pickerStyle(.menu).labelsHidden().accessibilityLabel("Accounts")
        .accessibilityIdentifier("email.accounts")
    }

    /// The mail search field; the Mac sidebar calls it Search mail, as the design does.
    private var searchField: some View {
        #if os(macOS)
        let prompt = showsMailboxSidebar ? "Search mail" : "Find mail"
        #else
        let prompt = "Find mail"
        #endif
        return HStack(spacing: 8) {
            Image(systemName: "magnifyingglass").foregroundColor(AppTheme.inkSoft)
            TextField(prompt, text: Binding(get: { model.searchText }, set: { model.setSearchText($0) }), prompt: .a2Prompt(prompt))
                .textFieldStyle(.plain).accessibilityLabel("Find mail")
                .accessibilityIdentifier("email.search")
            if !model.searchText.isEmpty {
                Button {
                    model.setSearchText("")
                } label: {
                    Image(systemName: "xmark.circle.fill")
                }
                .buttonStyle(.plain).foregroundColor(AppTheme.inkSoft).accessibilityLabel("Clear search")
            }
        }
        .veetType(.ui)
        #if os(macOS)
        .padding(.horizontal, VeetSpace.s2).frame(minHeight: A2Metrics.controlHeight)
        .background(RoundedRectangle(cornerRadius: A2Metrics.fieldRadius, style: .continuous).fill(AppTheme.field))
        #else
        .padding(.horizontal, 10).frame(minHeight: A2Metrics.controlHeight + 4)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(AppTheme.field))
        #endif
    }

    private var otherMailDisclosure: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                showingOtherMailViews.toggle()
            } label: {
                HStack {
                    Text("Find other mail")
                    Spacer()
                    Image(systemName: showingOtherMailViews ? "chevron.up" : "chevron.down")
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .veetType(.meta).foregroundColor(AppTheme.inkSoft)
            .accessibilityIdentifier("email.other-mail-views")
            .accessibilityValue(showingOtherMailViews ? "Expanded" : "Collapsed")
            if showingOtherMailViews { mailViewControl }
        }
    }

    /// Important and Other mail: a segmented control on the Mac, filter pills on touch.
    @ViewBuilder private var mailViewControl: some View {
        #if os(macOS)
        Picker("Mail view", selection: Binding(get: { model.listView }, set: { model.setListView($0) })) {
            Text("Important").tag("priority")
            Text("Other mail").tag("other")
        }.pickerStyle(.segmented).labelsHidden().accessibilityLabel("Mail view")
            .accessibilityIdentifier("email.mail-view")
        #else
        HStack(spacing: 8) {
            ForEach([("priority", "Important"), ("other", "Other mail")], id: \.0) { key, title in
                Button(title) { model.setListView(key) }
                    .buttonStyle(FilterPillButtonStyle(selected: model.listView == key))
                    .accessibilityAddTraits(model.listView == key ? .isSelected : [])
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Mail view")
        .accessibilityIdentifier("email.mail-view")
        #endif
    }

    /// Prefer the server's count across pages; older servers describe only the loaded rows.
    private var threadCount: String {
        let counts = EmailCollection(rawValue: model.listView) == nil ? nil : model.collectionCounts
        let count = counts?.count(for: model.collection) ?? model.items.count
        var line = "\(count) \(count == 1 ? "thread" : "threads")"
        let needing = counts?.count(for: .needsYou)
            ?? model.items.filter { $0.needsReply && !$0.isHandled && !$0.isArchived }.count
        if needing > 0 { line += " · \(needing) \(needing == 1 ? "needs" : "need") a reply" }
        return line
    }

    private var mailboxNeedsAttention: Bool {
        visibleAccounts.contains { $0.status != "ready" || $0.lastSyncedAt == nil }
    }

    private var mailboxStatus: String {
        if visibleAccounts.contains(where: { $0.hasRefreshFailure }) { return "An account couldn’t be updated" }
        if !model.isRefreshing && visibleAccounts.contains(where: { $0.status == "unavailable" }) {
            return "Waiting for an email update · results may be incomplete"
        }
        if model.isRefreshing || mailboxNeedsAttention { return "Updating mail · results may be incomplete" }
        return "Mailbox status · updated while you’re here"
    }

    private var emptyInbox: some View {
        let incomplete = model.accounts.contains { $0.status != "ready" || $0.lastSyncedAt == nil }
        return EmailEmptyState(
            symbol: model.searchText.isEmpty ? "tray" : "magnifyingglass",
            title: model.accounts.isEmpty
                ? "Your mail belongs here"
                : incomplete
                    ? "Getting your mail ready"
                    : model.searchText.isEmpty ? "Nothing needs your attention here" : "No matching mail",
            message: model.accounts.isEmpty
                ? "Connected accounts will appear here."
                : incomplete
                    ? "The scan is still incomplete. Your priorities will appear as mail is assessed."
                    : model.searchText.isEmpty
                        ? "Refresh for updates, or explore Other mail." : "Try a different name, subject, or phrase."
        )
    }

    /// Opens the selected thread directly in regular layouts or through compact navigation.
    @ViewBuilder private func threadRow(_ thread: EmailThreadView) -> some View {
        if directSelection {
            Button {
                Task { await model.openThread(thread.id) }
            } label: {
                EmailThreadRow(model: model, thread: thread)
            }
        } else {
            NavigationLink(value: thread.id) { EmailThreadRow(model: model, thread: thread) }
        }
    }
}

/// Row chrome from the reference: on the Mac a rounded row that takes the
/// selected tint and no separator; on touch a full-width row above a hairline.
private struct EmailRowChrome: ViewModifier {
    let selected: Bool

    func body(content: Content) -> some View {
        #if os(macOS)
        content
            .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
            .listRowSeparator(.hidden)
            .listRowBackground(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .fill(selected ? AppTheme.selected : Color.clear)
                    .padding(.horizontal, 6).padding(.vertical, 1))
        #else
        content
            .listRowInsets(EdgeInsets(top: 12, leading: 16, bottom: 12, trailing: 16))
            .listRowSeparatorTint(AppTheme.hairline)
            .listRowBackground(selected ? AppTheme.selected : Color.clear)
        #endif
    }
}

/// One thread as the reference draws it: who (by name) and when (by recency),
/// the subject, Veetbot's gist, and what the conversation needs from the owner
/// as one short chip. A handled or archived conversation reads in soft ink.
private struct EmailThreadRow: View {
    @ObservedObject var model: EmailViewModel
    let thread: EmailThreadView

    private var settled: Bool { thread.isHandled || thread.isArchived || thread.isInSpam }

    var body: some View {
        VStack(alignment: .leading, spacing: EmailType.rowSpacing) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(thread.correspondentsOrUnknown)
                    .veetType(EmailType.sender, weight: settled ? .regular : .semibold)
                    .foregroundColor(settled ? AppTheme.inkSoft : AppTheme.ink)
                    .lineLimit(1)
                Spacer(minLength: 4)
                // The time today, Yesterday, the weekday within the week, then the date.
                TimelineView(.everyMinute) { context in
                    Text(EmailRowDate.label(for: thread.updatedAt, relativeTo: context.date))
                        .veetType(EmailType.meta).foregroundColor(AppTheme.inkFaint).monospacedDigit().fixedSize()
                }
            }
            Text(thread.subject.isEmpty ? "No subject" : thread.subject)
                .veetType(EmailType.subject).foregroundColor(settled ? AppTheme.inkSoft : AppTheme.ink).lineLimit(1)
            // The gist is the line that says what the mail needs; give it two lines.
            Text(thread.summary).veetType(EmailType.gist).foregroundColor(AppTheme.inkSoft).lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .center, spacing: 6) {
                EmailThreadStatus(thread: thread)
                if model.accounts.count > 1 {
                    Spacer(minLength: 4)
                    Text(model.accounts.first { $0.id == thread.accountID }?.label ?? thread.accountID)
                        .veetType(.label, weight: .medium).foregroundColor(AppTheme.inkFaint).lineLimit(1)
                }
            }
            .padding(.top, 1)
            if !thread.complete {
                Label("Partial thread", systemImage: "exclamationmark.circle")
                    .veetType(EmailType.meta).foregroundColor(AppTheme.amberText)
            }
            if thread.isSuspectedSpam {
                Label("Suspected spam", systemImage: "exclamationmark.octagon")
                    .veetType(EmailType.meta).foregroundColor(AppTheme.amberText)
                    .accessibilityIdentifier("email.spam.tag.\(thread.id.uuidString)")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
    }
}

/// The Handled circle of the reference: an empty ring while the conversation
/// sits in the Inbox, a filled teal check once Gmail has archived it. It is the
/// same reversible Gmail label change under the same name, and only a
/// confirmed Inbox state fills it.
private struct EmailHandledCircle: View {
    @ObservedObject var model: EmailViewModel
    let thread: EmailThreadView
    let accessibilityID: String

    var body: some View {
        Button {
            Task { await model.setThreadArchived(thread, archived: !thread.isArchived) }
        } label: {
            ZStack {
                Circle().fill(thread.isArchived ? AppTheme.tealFill : Color.clear)
                Circle().strokeBorder(thread.isArchived ? AppTheme.tealFill : AppTheme.hairlineStrong, lineWidth: 1.5)
                if thread.isArchived {
                    Image(systemName: "checkmark")
                        .veetIcon(.small, weight: .semibold)
                        .foregroundColor(AppTheme.onTeal)
                }
            }
            .frame(width: EmailType.handledDiameter, height: EmailType.handledDiameter)
            .frame(width: EmailType.handledHit, height: EmailType.handledHit)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        // The ring, not its hit area, lines up with the sender's first line.
        .alignmentGuide(.top) { _ in (EmailType.handledHit - EmailType.handledDiameter) / 2 - 1 }
        .disabled(!model.canArchive(thread))
        .accessibilityIdentifier(accessibilityID)
        .accessibilityLabel(thread.isArchived ? "Move to Inbox" : "Archive in Gmail")
        .accessibilityValue(thread.isArchived ? "Archived" : "In Inbox")
        .help(thread.isArchived ? "Move this conversation to its Gmail account's Inbox"
              : "Archive this conversation in its originating Gmail account")
    }
}

/// The detail's archive control: the same Gmail label change as the circle,
/// named in a hairline capsule.
private struct EmailArchiveButton: View {
    @ObservedObject var model: EmailViewModel
    let thread: EmailThreadView
    let accessibilityID: String
    /// The icon alone, for a navigation bar, which draws its own glass behind it.
    var iconOnly = false

    var body: some View {
        if iconOnly {
            button.labelStyle(.iconOnly)
        } else {
            button.buttonStyle(SecondaryCapsuleButtonStyle())
        }
    }

    private var button: some View {
        Button {
            Task { await model.setThreadArchived(thread, archived: !thread.isArchived) }
        } label: {
            Label(thread.isArchived ? "Move to Inbox" : "Archive in Gmail",
                  systemImage: thread.isArchived ? "checkmark.circle.fill" : "archivebox")
        }
        .disabled(!model.canArchive(thread))
        .accessibilityIdentifier(accessibilityID)
        .accessibilityLabel(thread.isArchived ? "Move to Inbox" : "Archive in Gmail")
        .accessibilityValue(thread.isArchived ? "Archived" : "In Inbox")
        .help(thread.isArchived ? "Move this conversation to its Gmail account's Inbox"
              : "Archive this conversation in its originating Gmail account")
    }
}

/// What Gmail said about the last label change, and the way to ask again.
private struct EmailArchiveStatus: View {
    @ObservedObject var model: EmailViewModel
    let thread: EmailThreadView
    var showsAvailability = false

    var body: some View {
        if let message = model.archiveMessage(for: thread) {
            Text(message).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("email.archive.status.\(thread.id.uuidString)")
            if thread.archiveOperation != nil {
                Button(thread.archiveOperation?.status == "uncertain" ? "Check recorded status" : "Check status") {
                    Task { await model.checkArchiveStatus(thread.id) }
                }
                .buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.tealText)
                .labelStyle(.titleOnly)
                .accessibilityIdentifier("email.archive.check.\(thread.id.uuidString)")
            }
        } else if showsAvailability, let reason = model.archiveUnavailableReason(for: thread) {
            Text(reason).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct EmailThreadScreen: View {
    private enum Field: Hashable { case to, cc, bcc, subject, body, refinement }
    @ObservedObject var model: EmailViewModel
    @ObservedObject var subscriptions: EmailSubscriptionsViewModel
    let discussInChat: () async -> Void
    /// Archive and the thread menu sit in the navigation bar beside the back
    /// button instead of on a row of their own above the subject.
    var actionsInNavigationBar = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.activeClientMode) private var activeMode
    @State private var refinement = ""
    @State private var showingDiscard = false
    @State private var showingExclusion = false
    @State private var showingRevisions = false
    @State private var showingEnvelope = false
    @State private var peopleLookup: PeopleLookup?
    @FocusState private var focusedField: Field?
    @FocusState private var feedbackFocused: Bool
    /// On the Mac the sender's card and Teach Veetbot live in the right panel.
    @Environment(\.inspectorReveal) private var inspectorReveal

    /// The Mac's right panel describes the thread (ADR-0237, MagicPath
    /// cozy-evening-6871): Veetbot's gist, the account, who wrote and to whom,
    /// Unsubscribe, the spam flag and Show original. Without a panel the
    /// thread describes itself.
    private var describesInline: Bool { inspectorReveal == nil }

    private var replyScrollAnimation: Animation? {
        #if os(macOS)
        nil
        #else
        reduceMotion ? nil : .easeInOut(duration: 0.2)
        #endif
    }

    /// Keeps reading scrollable while reply and Chat actions remain reachable at the bottom edge.
    var body: some View {
        Group {
            if model.isLoadingThread {
                ProgressView("Opening thread…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let thread = model.thread {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 22) {
                            threadHeader(thread)
                            if describesInline {
                                EmailGistBlock(thread: thread, draft: model.draft)
                            }
                            if let error = model.threadReadMessage {
                                Label(error, systemImage: "exclamationmark.circle").foregroundColor(AppTheme.red)
                                    .accessibilityIdentifier("email.read-error")
                            }
                            conversation(thread)
                            if describesInline {
                                EmailTeachCard(model: model, explanationFocused: $feedbackFocused)
                            }
                            draftEditor.id("reply")
                        }
                        #if os(macOS)
                        .padding(.horizontal, VeetSpace.s6).padding(.top, VeetSpace.s4).padding(.bottom, VeetSpace.s7)
                        #else
                        .padding(.horizontal, 24).padding(.top, 20).padding(.bottom, 28)
                        #endif
                        .frame(maxWidth: 680, alignment: .leading).frame(maxWidth: .infinity)
                    }
                    .accessibilityIdentifier("email.detail")
                    .safeAreaInset(edge: .bottom, spacing: 0) {
                        HStack(spacing: 12) {
                            Button {
                                Task { await discussInChat() }
                            } label: {
                                Label("Discuss in Chat", systemImage: "bubble.left.and.bubble.right")
                            }
                            .buttonStyle(QuietCapsuleButtonStyle())
                            .accessibilityIdentifier("email.discuss")
                            Spacer(minLength: 0)
                            Button {
                                // The Mac jumps: after an animated scrollTo, macOS 26 keeps a
                                // stale transform for accessibility hit-testing, so the controls
                                // it just brought on screen cannot be clicked.
                                withAnimation(replyScrollAnimation) {
                                    proxy.scrollTo("reply", anchor: .top)
                                }
                            } label: {
                                Label(
                                    model.draft == nil ? "Reply" : "Your reply", systemImage: "arrowshape.turn.up.left")
                            }
                            .buttonStyle(PrimaryCapsuleButtonStyle())
                            .accessibilityIdentifier("email.jump-to-reply")
                        }
                        .padding(.horizontal, VeetSpace.s5).padding(.vertical, 10)
                        .background(AppTheme.paper.opacity(0.88).background(.ultraThinMaterial))
                        .overlay(alignment: .top) { HairlineDivider() }
                    }
                }
            } else {
                EmailEmptyState(
                    symbol: "envelope.open", title: "Make room for what matters",
                    message: "Choose a thread to read, reply, or think it through with Veetbot."
                )
                .frame(maxWidth: 400).frame(maxWidth: .infinity, maxHeight: .infinity)
                if let error = model.draftError { Text(error).foregroundColor(AppTheme.red) }
            }
        }
        .background(AppTheme.paper)
        .confirmationDialog("Discard this Veetbot draft?", isPresented: $showingDiscard, titleVisibility: .visible) {
            Button("Discard draft", role: .destructive) { Task { await model.discardDraft() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This removes the proposal from Veetbot. It does not change Gmail.")
        }
        .confirmationDialog("Exclude this email thread?", isPresented: $showingExclusion, titleVisibility: .visible) {
            Button("Exclude thread", role: .destructive) { Task { await model.excludeSelectedThread() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(
                "Retained email source copies and learning from ‘\(model.thread?.subject ?? "this thread")’ will be removed. Gmail mail and unrelated Chat history remain."
            )
        }
        .sheet(isPresented: $showingRevisions) { EmailRevisionsScreen(model: model) }
        // One confirmation state serves both surfaces, so each presents only its own.
        .sheet(isPresented: Binding(
            get: { subscriptions.confirmation?.source == .thread },
            set: { if !$0 { subscriptions.cancelConfirmation() } })
        ) {
            EmailUnsubscribeConfirmationSheet(model: subscriptions)
        }
        #if os(iOS)
        .fullScreenCover(item: $peopleLookup) { lookup in PeopleLookupSheet(lookup: lookup) }
        #else
        .sheet(item: $peopleLookup) { lookup in PeopleLookupSheet(lookup: lookup) }
        #endif
        #if os(iOS)
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") {
                        focusedField = nil
                        feedbackFocused = false
                    }.accessibilityIdentifier("email.keyboard-done")
                }
            }
            .toolbar {
                if actionsInNavigationBar, activeMode == .email, let thread = model.thread {
                    ToolbarItemGroup(placement: .topBarTrailing) {
                        EmailArchiveButton(
                            model: model, thread: thread, accessibilityID: "email.handled.detail", iconOnly: true)
                        threadActionsMenu(thread)
                    }
                }
            }
            // Paper behind the bar: the glass back button never floats over the subject
            // or the conversation as they scroll beneath it.
            .toolbarBackground(AppTheme.paper, for: .navigationBar)
            .toolbarBackgroundVisibility(actionsInNavigationBar ? .visible : .automatic, for: .navigationBar)
        #endif
    }

    /// The subject in the display face, then who wrote and when; the reversible
    /// handling controls sit on the line above them.
    private func threadHeader(_ thread: EmailThreadView) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 8) {
                if describesInline {
                    Label(model.accountDescription(for: thread.accountID), systemImage: "envelope")
                        .veetType(.meta).foregroundColor(AppTheme.inkFaint).lineLimit(1)
                }
                if !actionsInNavigationBar {
                    Spacer(minLength: 8)
                    // The capsule keeps its full name; the account label is what gives way.
                    EmailArchiveButton(model: model, thread: thread, accessibilityID: "email.handled.detail")
                        .fixedSize().layoutPriority(1)
                    threadActionsMenu(thread)
                }
            }
            // What Gmail said about an archive the owner just asked for stays
            // beside the button; why archiving is unavailable is a fact about
            // the account, which the Mac shows in the panel.
            EmailArchiveStatus(model: model, thread: thread, showsAvailability: describesInline)
            VStack(alignment: .leading, spacing: 4) {
                Text(thread.subject.isEmpty ? "No subject" : thread.subject)
                    .veetType(.display)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                Text("\(thread.correspondentsOrUnknown) · \(thread.updatedAt, format: .dateTime.month(.abbreviated).day())")
                    .veetType(EmailType.gist).foregroundColor(AppTheme.inkSoft)
            }
            if describesInline {
                // Bulk mail only, on an account that advertises the action, with
                // verified evidence. Everything else offers nothing here.
                if let block = thread.subscription, block.canUnsubscribe,
                   model.unsubscribeSupported(for: thread.accountID) {
                    HStack(spacing: 8) {
                        Text(thread.senders.first ?? block.destination)
                            .veetType(.meta).foregroundColor(AppTheme.inkSoft).lineLimit(1)
                        Button("Unsubscribe") { subscriptions.beginUnsubscribe(thread: thread) }
                            .buttonStyle(SecondaryCapsuleButtonStyle())
                            .accessibilityLabel("Unsubscribe from \(thread.senders.first ?? block.destination)")
                            .accessibilityIdentifier("email.unsubscribe.thread")
                    }
                }
            }
            if describesInline {
                EmailSpamControls(model: model, thread: thread)
            }
        }
    }

    /// Report spam and exclusion from learning, behind one ellipsis.
    private func threadActionsMenu(_ thread: EmailThreadView) -> some View {
        Menu {
            Button(thread.isHandled ? "Mark not handled" : "Mark handled") {
                Task { await model.setThreadHandled(thread, handled: !thread.isHandled) }
            }
            .disabled(model.isPerformingAction)
            .accessibilityIdentifier("email.attention.toggle")
            Divider()

            if model.canReportSpam(thread), !thread.isSuspectedSpam {
                Button("Report spam") { Task { await model.setThreadSpam(thread, spam: true) } }
                    .accessibilityIdentifier("email.spam.menu.report")
            }
            Button("Exclude this thread from learning", role: .destructive) { showingExclusion = true }
        } label: {
            // The system menu button: a styled Menu hit-tests differently on
            // macOS 26 and the journeys click what sits beside it.
            Image(systemName: "ellipsis").frame(width: 32, height: 32).contentShape(Rectangle())
        }
        // The quiet icon button of the design, built as Draft options is.
        .menuStyle(.button).buttonStyle(.borderless).menuIndicator(.hidden).fixedSize()
        .foregroundColor(AppTheme.inkSoft).tint(AppTheme.inkSoft)
        .accessibilityLabel("Thread actions").help("Thread actions")
        .disabled(model.isPerformingAction)
    }

    /// Each message as prose under its sender's line; a hairline parts them.
    private func conversation(_ thread: EmailThreadView) -> some View {
        let messages = thread.messages ?? []
        return VStack(alignment: .leading, spacing: 18) {
            ForEach(messages) { message in
                VStack(alignment: .leading, spacing: 10) {
                    // A sender too long to share the line puts the date below
                    // it rather than wrapping letter by letter.
                    ViewThatFits(in: .horizontal) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            senderName(message)
                            Spacer(minLength: 4)
                            sentDate(message)
                        }
                        VStack(alignment: .leading, spacing: 2) {
                            senderName(message)
                            sentDate(message)
                        }
                    }
                    if describesInline {
                        Button {
                            peopleLookup = .correspondent(message.sender, at: message.sentAt)
                        } label: {
                            Label("View in People", systemImage: "person.crop.circle")
                        }
                        .buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.tealText)
                        .accessibilityLabel("View people matching \(message.sender)")
                        .accessibilityIdentifier("email.sender.people")
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: 5) {
                                Text("To: \(message.to.joined(separator: ", "))")
                                if !message.cc.isEmpty { Text("Cc: \(message.cc.joined(separator: ", "))") }
                            }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 6)
                        } label: {
                            Text("Message details")
                        }
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    }
                    EmailReadingView(message: message, showsOriginal: inspectorReveal?.showsOriginal)
                    if !message.complete {
                        Label(
                            "This message is incomplete. Refresh before relying on its full contents.",
                            systemImage: "exclamationmark.circle"
                        )
                        .veetType(.ui).foregroundColor(AppTheme.amberText)
                    }
                    attachmentLine(message)
                }
                if message.id != messages.last?.id { HairlineDivider() }
            }
        }
    }

    /// Who wrote, in semibold. On the Mac the name opens their card and this
    /// message's envelope in the panel.
    @ViewBuilder private func senderName(_ message: EmailMessageView) -> some View {
        if let inspectorReveal {
            Button {
                inspectorReveal.message(message)
            } label: {
                Text(message.sender).veetType(EmailType.sender, weight: .semibold).foregroundColor(AppTheme.ink)
            }
            .buttonStyle(.plain)
            #if os(macOS)
            .pointerStyle(.link)
            #endif
            .help("Show who wrote this in the panel")
            .accessibilityHint("Shows who wrote this message in the panel")
            .accessibilityIdentifier("email.sender.people")
        } else {
            Text(message.sender).veetType(EmailType.sender, weight: .semibold).textSelection(.enabled)
        }
    }

    private func sentDate(_ message: EmailMessageView) -> some View {
        Text(message.sentAt, format: .dateTime.month(.abbreviated).day().year().hour().minute())
            .veetType(EmailType.meta).foregroundColor(AppTheme.inkFaint).fixedSize()
    }

    /// Attachment names and the fact they were not read. The Mac lists them in
    /// the panel, so the message keeps one quiet line that opens that list.
    @ViewBuilder private func attachmentLine(_ message: EmailMessageView) -> some View {
        let attachments = message.attachments ?? []
        if let inspectorReveal, !attachments.isEmpty {
            Button {
                inspectorReveal.attachments()
            } label: {
                Label(attachments.count == 1 ? "1 attachment" : "\(attachments.count) attachments",
                      systemImage: "paperclip")
            }
            .buttonStyle(.plain).veetType(.meta).foregroundColor(AppTheme.inkSoft)
            .help("Show the attachments in the panel")
            .accessibilityIdentifier("email.message.attachments")
        } else {
            ForEach(Array(attachments.enumerated()), id: \.offset) { _, attachment in
                Label("\(attachment.filename) · attachment not read", systemImage: "paperclip")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
            }
        }
    }

    /// The reply as one quiet column: a serif heading with the save state, any
    /// decision the draft is waiting on, the envelope and body in a single
    /// well, then Review & Send with the rarer actions folded into a menu.
    @ViewBuilder private var draftEditor: some View {
        if let draft = model.draft, let edit = model.currentEdit {
            VStack(alignment: .leading, spacing: 14) {
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text("Your reply").veetType(.title)
                    Spacer(minLength: 8)
                    Text(model.isSaving ? "Saving…" : edit.isDirty ? "Unsaved changes" : "Drafted by Veetbot · Saved")
                        .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                        .multilineTextAlignment(.trailing)
                }
                draftNotices(draft, edit: edit)
                draftWell(draft, edit: edit)
                draftActions(draft, edit: edit)
                // A refused send belongs next to the control that asked for it; above the
                // conversation it reads as the button having done nothing.
                if let error = model.draftActionMessage {
                    Label(error, systemImage: "exclamationmark.circle").foregroundColor(AppTheme.red)
                        .veetType(.ui).fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("email.action-error")
                }
                if let message = model.styleExampleMessage {
                    Text(message).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                }
                DisclosureGroup {
                    HStack(alignment: .center, spacing: 8) {
                        TextField("How should the reply change?", text: $refinement, prompt: .a2Prompt("How should the reply change?")).focused(
                            $focusedField, equals: .refinement
                        )
                        .textFieldStyle(.plain).veetType(.ui).a2Field()
                        Button("Refine draft") { Task { await model.generateDraft(instruction: refinement) } }
                            .buttonStyle(SecondaryCapsuleButtonStyle())
                            .disabled(
                                refinement.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                    || model.isPerformingAction || model.conflict != nil)
                    }.padding(.top, 8)
                } label: {
                    Label("Refine with Veetbot", systemImage: "sparkles")
                        .veetType(.ui, weight: .medium).foregroundColor(AppTheme.inkSoft)
                }
                .tint(AppTheme.inkSoft)
            }
            .onAppear { showingEnvelope = !edit.cc.isEmpty || !edit.bcc.isEmpty }
            .onChange(of: draft.id) { showingEnvelope = !edit.cc.isEmpty || !edit.bcc.isEmpty }
            .onChange(of: draft.canEdit) { _, canEdit in if !canEdit { focusedField = nil } }
        } else {
            VStack(alignment: .leading, spacing: 10) {
                Text("Your reply").veetType(.title)
                Text("Start with a draft in your own style, then make it yours.").veetType(.ui)
                    .foregroundColor(AppTheme.inkSoft)
                Button {
                    Task { await model.generateDraft() }
                } label: {
                    if model.isPerformingAction {
                        ProgressView("Preparing draft…")
                    } else {
                        Label("Draft reply", systemImage: "square.and.pencil")
                    }
                }.primaryCapsule().disabled(model.isPerformingAction).padding(.top, 4)
                if let error = model.draftActionMessage {
                    Label(error, systemImage: "exclamationmark.circle").foregroundColor(AppTheme.red)
                        .veetType(.ui).accessibilityIdentifier("email.action-error")
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// From, To and the folded Cc, Bcc and subject as hairline-parted rows above
    /// the body, all in one field well like a native mail composer.
    private func draftWell(_ draft: EmailDraftView, edit: EmailDraftEdit) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            envelopeRow("From") {
                Text(model.accountDescription(for: draft.accountID)).veetType(.ui).foregroundColor(AppTheme.inkSoft)
                    .lineLimit(1).truncationMode(.middle)
            }
            HairlineDivider()
            Group {
                envelopeRow("To") {
                    TextField("To", text: editBinding(\.to), prompt: .a2Prompt("To")).textFieldStyle(.plain).veetType(.ui)
                        .focused($focusedField, equals: .to)
                        .accessibilityLabel("To").accessibilityIdentifier("email.draft-to")
                    Button {
                        showingEnvelope.toggle()
                    } label: {
                        HStack(spacing: 4) {
                            Text("Cc, Bcc")
                            Image(systemName: showingEnvelope ? "chevron.up" : "chevron.down")
                                .imageScale(.small)
                        }
                        .padding(.vertical, 4).contentShape(Rectangle())
                    }.buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.tealText)
                        .fixedSize()
                        .accessibilityLabel("Cc, Bcc & subject")
                        .accessibilityIdentifier("email.draft-envelope")
                        .accessibilityValue(showingEnvelope ? "Expanded" : "Collapsed")
                }
                if showingEnvelope {
                    HairlineDivider()
                    envelopeField("Cc", path: \.cc, field: .cc)
                    HairlineDivider()
                    envelopeField("Bcc", path: \.bcc, field: .bcc)
                    HairlineDivider()
                    envelopeField("Subject", path: \.subject, field: .subject)
                }
            }.disabled(!draft.canEdit || model.isPerformingAction)
            HairlineDivider()
            // `.disabled` leaves the AppKit text view editable and focused, so a draft that can
            // no longer change (sending, sent, uncertain) renders as text instead of an editor.
            // Only the editor is disabled: a disabled ancestor would also block selecting the text.
            if draft.canEdit {
                // The text view insets its own text by about five points; the
                // smaller padding lines its first character up with the labels.
                TextEditor(text: editBinding(\.body)).veetType(.ui).lineSpacing(5)
                    .scrollContentBackground(.hidden)
                    .frame(minHeight: 180, idealHeight: 220)
                    .focused($focusedField, equals: .body)
                    .disabled(model.isPerformingAction)
                    .accessibilityLabel("Reply body").accessibilityIdentifier("email.draft-body")
                    .padding(.horizontal, Self.wellInset - 5).padding(.vertical, 8)
            } else {
                // No accessibilityLabel: on a selectable Text, AppKit resolves the label
                // through the text itself and recurses until the stack overflows.
                Text(edit.body).veetType(.ui).lineSpacing(5).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityIdentifier("email.draft-body")
                    .padding(.horizontal, Self.wellInset).padding(.vertical, 12)
            }
        }
        // The well's shapes take no clicks: on a 1x Mac display a stroke over
        // a text view swallows the click meant for it (see `A2FieldModifier`).
        .background(
            RoundedRectangle(cornerRadius: 12, style: .continuous).fill(AppTheme.surface)
                .allowsHitTesting(false)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(AppTheme.hairlineStrong)
                .allowsHitTesting(false)
        )
    }

    /// Review & Send leads; saving, history, the style example and discarding
    /// wait behind one quiet menu at the trailing edge.
    private func draftActions(_ draft: EmailDraftView, edit: EmailDraftEdit) -> some View {
        HStack(spacing: 10) {
            Button("Review & Send") { Task { await model.prepareSend() } }
                .primaryCapsule()
                .disabled(!model.canReview).accessibilityIdentifier("email.review-send")
            Spacer(minLength: 0)
            Menu {
                Button("Save now") { Task { await model.saveDraft() } }
                    .disabled(!edit.isDirty || model.isSaving || model.conflict != nil)
                Button("Draft history") { showingRevisions = true }.accessibilityIdentifier(
                    "email.draft-history")
                Button("Use as writing example") { Task { await model.endorseDraftStyle() } }
                    .accessibilityIdentifier("email.endorse-style")
                    .disabled(
                        model.isPerformingAction || model.isSaving || model.conflict != nil
                            || edit.body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                Divider()
                Button("Discard draft", role: .destructive) { showingDiscard = true }.disabled(!draft.canEdit)
            } label: {
                Label("Draft options", systemImage: "ellipsis.circle")
                    .labelStyle(.iconOnly).imageScale(.large)
                    .frame(minWidth: A2Metrics.controlHeight, minHeight: A2Metrics.controlHeight)
                    .contentShape(Rectangle())
            }
            .menuStyle(.button).buttonStyle(.borderless).menuIndicator(.hidden).fixedSize()
            .foregroundColor(AppTheme.inkSoft).tint(AppTheme.inkSoft)
            .help("Draft options")
        }
    }

    /// Distinguishes draft progress, confirmed sending, uncertainty and recoverable editing conflicts.
    /// Each notice that waits on the owner is one amber-ruled card: what happened, then the
    /// action that answers it beside the sentence.
    @ViewBuilder private func draftNotices(_ draft: EmailDraftView, edit: EmailDraftEdit) -> some View {
        if draft.stale {
            draftDecision("New mail arrived", message: "Review the updated thread above before sending.") {
                Button("I've reviewed the updated thread") { Task { await model.confirmCurrentSourceReviewed() } }
                    .primaryCapsule()
                    .disabled(model.conflict != nil || model.isSaving)
            }
        }
        if draft.status == "uncertain" {
            draftDecision(
                "Send status unknown",
                message:
                    "Check the thread before taking another action. This message will not be resent automatically."
            ) { EmptyView() }
        } else if draft.status == "sent" {
            Label("Sent", systemImage: "checkmark.circle.fill").veetType(.ui, weight: .medium)
                .foregroundColor(AppTheme.tealText)
        } else if draft.isProcessing {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(draft.status == "sending" ? "Sending approved message…" : "Preparing your draft…")
                    .veetType(.ui).foregroundColor(AppTheme.inkSoft)
            }
            .accessibilityElement(children: .combine)
        }
        if draft.approvalID != nil && !edit.isDirty && draft.status == "awaiting_approval" {
            draftDecision("Waiting for your approval", message: "Open the send request to approve or decline it.") {
                Button("Open pending approval") { Task { await model.loadReview() } }
                    .buttonStyle(SecondaryCapsuleButtonStyle()).disabled(draft.stale)
            }
        }
        if let conflict = model.conflict {
            VStack(alignment: .leading, spacing: 10) {
                AttentionCardLabel(title: "Edited on another device")
                Text("This draft changed on another device. Your edits are preserved.")
                    .veetType(.ui).foregroundColor(AppTheme.ink).fixedSize(horizontal: false, vertical: true)
                DisclosureGroup {
                    A2Quote { Text(conflict.body).veetType(.ui).textSelection(.enabled) }.padding(.top, 6)
                } label: {
                    Text("Read the server version").veetType(.ui, weight: .medium)
                        .foregroundColor(AppTheme.inkSoft)
                }
                .tint(AppTheme.inkSoft)
                HStack(spacing: 8) {
                    Button("Save my version") { Task { await model.keepLocalDraft() } }
                        .primaryCapsule()
                    Button("Use server version") { model.useServerDraft() }
                        .buttonStyle(SecondaryCapsuleButtonStyle())
                }
            }
            .attentionCard()
            .accessibilityIdentifier("email.draft-conflict")
        }
    }

    /// One decision the draft waits on: an amber eyebrow and a sentence, with
    /// its action beside them when the width allows and beneath them when not.
    private func draftDecision<Action: View>(
        _ title: String, message: String, @ViewBuilder action: () -> Action
    ) -> some View {
        let action = action()
        let words = VStack(alignment: .leading, spacing: 4) {
            AttentionCardLabel(title: title)
            Text(message).veetType(.ui).foregroundColor(AppTheme.ink)
                .fixedSize(horizontal: false, vertical: true)
        }
        return ViewThatFits(in: .horizontal) {
            HStack(alignment: .center, spacing: 16) {
                words.frame(minWidth: 220, alignment: .leading)
                Spacer(minLength: 0)
                action
            }
            VStack(alignment: .leading, spacing: 10) {
                words
                action
            }
        }
        .attentionCard()
    }

    /// The leading inset every row of the reply well shares.
    private static let wellInset: CGFloat = 14

    /// One row of the reply envelope: a faint label in a fixed column, then its value.
    private func envelopeRow<Value: View>(_ title: String, @ViewBuilder value: () -> Value) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(title).veetType(.ui).foregroundColor(AppTheme.inkFaint)
                .frame(width: 58, alignment: .leading)
            value()
        }
        .padding(.horizontal, Self.wellInset)
        .frame(minHeight: A2Metrics.rowHeight + 8)
    }

    /// Connects an envelope field to the versioned edit buffer and keyboard focus.
    private func envelopeField(_ title: String, path: WritableKeyPath<EmailDraftEdit, String>, field: Field)
        -> some View
    {
        envelopeRow(title) {
            TextField(title, text: editBinding(path), prompt: .a2Prompt(title)).textFieldStyle(.plain).veetType(.ui)
                .focused($focusedField, equals: field)
                .accessibilityLabel(title).accessibilityIdentifier("email.draft-\(title.lowercased())")
        }
    }

    /// Routes local text changes through the view model's autosave and conflict-preservation path.
    private func editBinding(_ path: WritableKeyPath<EmailDraftEdit, String>) -> Binding<String> {
        Binding(get: { model.currentEdit?[keyPath: path] ?? "" }, set: { model.changeEdit(path, to: $0) })
    }
}

/// Veetbot's reading of the thread behind a teal rule: the gist, then what the
/// conversation needs and why it reached the owner's attention.
/// Teach Veetbot: one card that opens into scoped importance feedback and
/// reports what was applied, with Undo. Below the conversation on touch; in
/// the Mac right panel's Context tab (ADR-0237).
struct EmailTeachCard: View {
    @ObservedObject var model: EmailViewModel
    var explanationFocused: FocusState<Bool>.Binding
    /// On the panel's paperDeep the card is a surface card behind a hairline.
    var inPanel = false
    @State private var target: EmailFeedbackTarget = .thread
    @State private var explanation = ""
    @State private var targetValue = ""
    @State private var showingFeedback = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                showingFeedback.toggle()
            } label: {
                HStack {
                    Text("Teach Veetbot").veetType(.meta, weight: .semibold)
                    Spacer()
                    Image(systemName: showingFeedback ? "chevron.up" : "chevron.down").veetType(.meta)
                }.foregroundColor(AppTheme.inkSoft).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(showingFeedback ? "Expanded" : "Collapsed")
            .accessibilityIdentifier("email.feedback")
            if showingFeedback {
                feedback
            } else {
                Text("Say whether mail like this deserves your attention: this thread, a person, or a kind of content.")
                    .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let message = model.feedbackMessage {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "checkmark").veetType(.meta, weight: .semibold)
                    Text(message).veetType(.meta)
                    if model.feedbackID != nil {
                        Button("Undo") { Task { await model.undoFeedback() } }
                            .buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.tealText)
                    }
                }.foregroundColor(AppTheme.inkSoft)
            }
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(inPanel ? AppTheme.surface : AppTheme.paperDeep))
        .overlay {
            if inPanel { RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(AppTheme.hairline) }
        }
    }

    /// Offer explicit importance feedback with supported person and topic targets.
    private var feedback: some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker("Apply to", selection: $target) {
                ForEach(EmailFeedbackTarget.allCases, id: \.self) { value in Text(value.title).tag(value) }
            }.pickerStyle(.menu).accessibilityIdentifier("email.feedback-target")
                .onChange(of: target) { targetValue = "" }
            if target == .person {
                Picker("Person", selection: $targetValue) {
                    Text("Choose a person").tag("")
                    ForEach(model.thread?.senders ?? [], id: \.self) { person in Text(person).tag(person) }
                }
                .accessibilityIdentifier("email.feedback-person")
            } else if target == .topic {
                let topics = model.thread?.feedbackTopics ?? []
                ScrollView(.horizontal) {
                    HStack(spacing: 8) {
                        ForEach(topics, id: \.self) { topic in
                            Button(topic) { targetValue = targetValue == topic ? "" : topic }
                                .buttonStyle(FilterPillButtonStyle(selected: targetValue == topic))
                                .accessibilityAddTraits(targetValue == topic ? .isSelected : [])
                                .accessibilityIdentifier("email.feedback-topic-chip.\(topic)")
                        }
                    }
                }
                .accessibilityIdentifier("email.feedback-topic")
                .onChange(of: topics) { _, values in
                    if !values.contains(targetValue) { targetValue = "" }
                }
                if topics.isEmpty {
                    Text("No content topics are available yet. You can apply feedback to This thread.")
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                }
            }
            TextField("Explain what matters (optional)", text: $explanation, prompt: .a2Prompt("Explain what matters (optional)")).focused(explanationFocused)
                .textFieldStyle(.plain).a2Field()
            HStack(spacing: 8) {
                Button("Important") {
                    Task {
                        await model.giveFeedback(
                            target: target, judgment: "important", explanation: explanation, targetValue: targetValue)
                    }
                }
                .accessibilityIdentifier("email.feedback-important")
                Button("Less important") {
                    Task {
                        await model.giveFeedback(
                            target: target, judgment: "less_important", explanation: explanation,
                            targetValue: targetValue)
                    }
                }
                .accessibilityIdentifier("email.feedback-less-important")
            }.buttonStyle(SecondaryCapsuleButtonStyle())
                .disabled(target == .person && targetValue.isEmpty)
                .disabled(target == .topic && !(model.thread?.feedbackTopics ?? []).contains(targetValue))
            HairlineDivider()
            Text("Does this thread need a reply?").veetType(.meta).foregroundColor(AppTheme.inkSoft)
            HStack(spacing: 8) {
                Button("Needs reply") { Task { await model.giveFeedback(target: .thread, judgment: "needs_reply") } }
                Button("No reply needed") {
                    Task { await model.giveFeedback(target: .thread, judgment: "no_reply_needed") }
                }
            }.buttonStyle(QuietCapsuleButtonStyle())
        }.veetType(.ui).disabled(model.isPerformingAction).padding(.top, 4)
    }

}

private struct EmailGistBlock: View {
    let thread: EmailThreadView
    var draft: EmailDraftView? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Veetbot").veetType(.meta, weight: .semibold).foregroundColor(AppTheme.tealText)
            Text(thread.summary).veetType(.ui).fixedSize(horizontal: false, vertical: true)
            EmailThreadStatus(thread: thread, draft: draft, showsReason: true)
        }
        .padding(.leading, 12)
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 1).fill(AppTheme.teal).frame(width: 2)
        }
    }
}

/// Native text only: links open on a gesture; remote images and HTML never load.
private struct EmailReadingView: View {
    let message: EmailMessageView
    /// The whole thread's choice from the Mac panel; nil gives this message
    /// its own Reading view and Show original switch.
    var showsOriginal: Bool? = nil
    @State private var showingOriginal = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let reader = message.readerBody, !reader.isEmpty {
                if showsOriginal == nil {
                    HStack {
                        Text(showingOriginal ? "Original message" : "Reading view")
                            .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                        Spacer()
                        Button(showingOriginal ? "Reading view" : "Show original") {
                            showingOriginal.toggle()
                        }
                        .buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.tealText)
                        .accessibilityIdentifier("email.reader.original")
                    }
                }
                if showsOriginal ?? showingOriginal {
                    original
                } else {
                    MarkdownContentView(text: reader)
                        .veetType(.ui).lineSpacing(5)
                        .environment(\.openURL, OpenURLAction { url in
                            guard ["https", "http"].contains(url.scheme?.lowercased() ?? ""),
                                  url.host != nil else { return .discarded }
                            return .systemAction
                        })
                        .accessibilityIdentifier("email.reader.content")
                }
            } else {
                original
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onChange(of: message.id) { showingOriginal = false }
    }

    private var original: some View {
        Text(message.body).veetType(.ui).lineSpacing(5).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

extension View {
    /// Hides separators for inbox notices.
    func emailHideSeparator() -> some View {
        listRowSeparator(.hidden)
    }
}

/// A flag explains itself and offers the owner's two answers; a report offers
/// its undo (ADR-0165). Below the subject on touch; atop Context on the Mac.
struct EmailSpamControls: View {
    @ObservedObject var model: EmailViewModel
    let thread: EmailThreadView

    var body: some View {
        if thread.isInSpam {
            HStack(spacing: 8) {
                Text("Reported as spam. Gmail empties Spam after thirty days.")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                if model.spamSupported(for: thread.accountID) {
                    Button("Not spam") { Task { await model.setThreadSpam(thread, spam: false) } }
                        .buttonStyle(SecondaryCapsuleButtonStyle())
                        .disabled(!model.canReportSpam(thread, spam: false))
                        .accessibilityIdentifier("email.spam.restore")
                }
            }
        } else if thread.isSuspectedSpam {
            VStack(alignment: .leading, spacing: 8) {
                Label("Suspected spam", systemImage: "exclamationmark.octagon")
                    .veetType(.meta, weight: .semibold).foregroundColor(AppTheme.amberText)
                    .accessibilityIdentifier("email.spam.tag.detail")
                Text("Veetbot thinks this may be spam, so it is not in your priority list. Nothing has changed in Gmail.")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                if model.spamSupported(for: thread.accountID) {
                    HStack(spacing: 8) {
                        Button("Report spam") { Task { await model.setThreadSpam(thread, spam: true) } }
                            .buttonStyle(SecondaryCapsuleButtonStyle())
                            .disabled(!model.canReportSpam(thread))
                            .accessibilityIdentifier("email.spam.report")
                        Button("Not spam") { Task { await model.clearSpamFlag(thread) } }
                            .buttonStyle(QuietCapsuleButtonStyle())
                            .accessibilityIdentifier("email.spam.clear")
                    }
                }
                if let error = model.spamFlagErrors[thread.id] {
                    Text(error).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(10).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(AppTheme.amberWash))
        }
    }
}

/// One line under the gist, as the reference writes it: an attention state
/// carries its reason in amber; a draft state reads in teal; a settled
/// conversation says so in soft ink. The detail always gives the reason.
private struct EmailThreadStatus: View {
    let thread: EmailThreadView
    var draft: EmailDraftView? = nil
    var showsReason = false

    typealias Tone = EmailThreadSummary.Tone

    private var state: (title: String, tone: Tone) { EmailThreadSummary.state(thread: thread, draft: draft) }

    private func color(for tone: Tone) -> Color {
        switch tone {
        case .attention: AppTheme.amberText
        case .draft: AppTheme.tealText
        case .settled: AppTheme.inkSoft
        }
    }

    /// Summarizes server-confirmed attention and draft state in one line.
    var body: some View {
        let (title, tone) = state
        let reason = thread.reason.trimmingCharacters(in: .whitespacesAndNewlines)
        if !showsReason {
            // In a row the state is a chip; the reason is read in the conversation.
            EmailStateChip(title: title, tone: tone)
        } else if tone == .attention, !reason.isEmpty {
            Text("\(title) · \(reason)")
                .veetType(EmailType.note, weight: .medium).foregroundColor(color(for: tone))
                .lineLimit(showsReason ? nil : 1)
                .fixedSize(horizontal: false, vertical: showsReason)
        } else {
            Text(title).veetType(EmailType.note, weight: .medium).foregroundColor(color(for: tone))
            if showsReason, !reason.isEmpty {
                Text(reason).veetType(EmailType.note).foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// A conversation's state as one short chip: an amber wash for attention, teal
/// for a draft, and bare soft ink once the conversation is settled.
private struct EmailStateChip: View {
    let title: String
    let tone: EmailThreadStatus.Tone

    var body: some View {
        Text(title)
            .veetType(EmailType.note, weight: .medium)
            .foregroundColor(foreground)
            .padding(.horizontal, tone == .settled ? 0 : 7)
            .padding(.vertical, tone == .settled ? 0 : 2)
            .background(Capsule().fill(wash))
    }

    private var foreground: Color {
        switch tone {
        case .attention: AppTheme.amberText
        case .draft: AppTheme.tealText
        case .settled: AppTheme.inkSoft
        }
    }

    private var wash: Color {
        switch tone {
        case .attention: AppTheme.amberWash
        case .draft: AppTheme.tealWash
        case .settled: .clear
        }
    }
}

/// A teal wash behind a row the last refresh brought in, fading once the eye
/// has had a moment to find it. Reduce Motion skips the wash.
private struct EmailArrivalWash: ViewModifier {
    @ObservedObject var model: EmailViewModel
    let threadID: UUID
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var washed = false

    func body(content: Content) -> some View {
        content
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(AppTheme.tealWash)
                    .padding(.horizontal, -8).padding(.vertical, -5)
                    .opacity(washed ? 1 : 0)
                    .accessibilityHidden(true)
            )
            .task(id: threadID) {
                guard model.arrivedThreadIDs.contains(threadID) else { return }
                model.acknowledgeArrival(threadID)
                guard !reduceMotion else { return }
                washed = true
                try? await Task.sleep(nanoseconds: 900_000_000)
                withAnimation(.easeOut(duration: 1.4)) { washed = false }
            }
    }
}

struct EmailEmptyState: View {
    let symbol: String
    let title: String
    let message: String
    /// Explains empty, unavailable or unselected mail states without implying a completed scan.
    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: symbol).veetIcon(.emptyState).foregroundColor(AppTheme.teal)
                .frame(width: 60, height: 60).background(Circle().fill(AppTheme.tealWash))
            Text(title).veetType(.title).multilineTextAlignment(.center)
            Text(message).veetType(.ui).foregroundColor(AppTheme.inkSoft).multilineTextAlignment(.center)
        }.padding(.horizontal, 20).padding(.vertical, 36).frame(maxWidth: .infinity)
    }
}

/// Observes refresh state directly even when the window toolbar is owned by the app coordinator's view.
/// Light or dark, from the Email heading on touch and the window toolbar on the Mac.
struct EmailAppearanceButton: View {
    @EnvironmentObject private var appearance: AppearancePreferences
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        Button { appearance.theme = colorScheme == .dark ? .light : .dark } label: {
            Image(systemName: colorScheme == .dark ? "sun.max" : "moon")
        }
        .accessibilityLabel(colorScheme == .dark ? "Switch to light appearance" : "Switch to dark appearance")
        .help(colorScheme == .dark ? "Light appearance" : "Dark appearance")
        .accessibilityIdentifier("email.appearance")
    }
}

#if os(macOS)
/// The Mac window's title while Email is showing (MagicPath
/// cozy-evening-6871): the collection in semibold, then how many
/// conversations need the owner in faint ink.
struct EmailToolbarTitle: View {
    @ObservedObject var model: EmailViewModel

    var body: some View {
        Text("\(Text(title).fontWeight(.semibold))\(Text(" · \(detail)").foregroundColor(AppTheme.inkFaint))")
            .veetType(.ui)
            .lineLimit(1)
            .accessibilityIdentifier("email.toolbar-title")
    }

    private var title: String {
        if model.listView == "other" { return "Other mail" }
        switch model.collection {
        case .inbox: return "Inbox"
        case .needsYou: return "Needs you"
        case .handled: return "Handled"
        }
    }

    /// Prefers the server's count across pages; older servers describe only the loaded rows.
    private var detail: String {
        let needing = model.collectionCounts?.count(for: .needsYou)
            ?? model.items.filter { $0.needsReply && !$0.isHandled && !$0.isArchived }.count
        if needing > 0 { return "\(needing) need\(needing == 1 ? "s" : "") you" }
        let count = model.collectionCounts?.count(for: model.collection) ?? model.items.count
        return "\(count) \(count == 1 ? "thread" : "threads")"
    }
}
#endif

struct EmailRefreshButton: View {
    @ObservedObject var model: EmailViewModel

    /// Present the manual refresh action and its loading state.
    var body: some View {
        Button {
            Task { await model.refresh() }
        } label: {
            if model.isRefreshing { ProgressView() } else { Image(systemName: "arrow.clockwise") }
        }
        .disabled(model.isRefreshing || model.unavailable)
        .accessibilityLabel("Refresh email").help("Refresh email")
        .accessibilityIdentifier("email.refresh")
    }
}

/// The head of an Email sheet: the title in the display face and the one way out.
struct EmailSheetHeader: View {
    let title: String
    var dismissTitle = "Done"
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).veetType(.display)
            Spacer()
            Button(dismissTitle, action: dismiss).buttonStyle(QuietCapsuleButtonStyle())
        }
    }
}

struct EmailLearningScreen: View {
    @ObservedObject var model: EmailViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var resetScope: String?
    @State private var isUpdating = false

    /// Exposes learning consent, pause and reset controls together with historical retrieval progress.
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            EmailSheetHeader(title: "Email learning") { dismiss() }
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text(
                        "Veetbot automatically analyzes received and Sent email from the latest 90 days to learn what matters, your writing style, and useful memories shared with Chat and both accounts. Selected email content is processed by Veetbot's hosted models. This updates your assistant's preferences without fine-tuning a model."
                    )
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    .accessibilityIdentifier("email.learning-disclosure")
                    if let learning = model.learning {
                        Text(learning.paused ? "Learning is paused." : "Learning continues while Email is active.")
                        Text(
                            "\(learning.historyProcessed) \(learning.historyProcessed == 1 ? "thread" : "threads") retrieved · \(learning.styleExamples) \(learning.styleExamples == 1 ? "style example" : "style examples")"
                        )
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                        Text(
                            learning.historyComplete
                                ? "The 90-day window has been retrieved; analysis continues as needed."
                                : "Retrieval of the latest 90 days is incomplete. Older history is paused."
                        )
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                        Button(learning.paused ? "Resume learning" : "Pause learning") {
                            isUpdating = true
                            Task {
                                await model.setLearningPaused(!learning.paused)
                                isUpdating = false
                            }
                        }.buttonStyle(SecondaryCapsuleButtonStyle()).disabled(isUpdating)
                        HairlineDivider()
                        Text(
                            "You can correct a person or topic in a thread, undo feedback, or exclude that thread's sources."
                        )
                        Text("\(learning.excludedSources) \(learning.excludedSources == 1 ? "source" : "sources") excluded")
                            .veetType(.meta)
                        VStack(alignment: .leading, spacing: 4) {
                            Button("Reset importance preferences", role: .destructive) { resetScope = "preferences" }
                            Button("Reset writing style", role: .destructive) { resetScope = "style" }
                            Button("Reset all email learning", role: .destructive) { resetScope = "all" }
                            if model.isResettingLearning {
                                ProgressView("Resetting…").controlSize(.small)
                            }
                        }.buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red)).disabled(model.isResettingLearning)
                        DisclosureGroup("History by account") {
                            VStack(alignment: .leading, spacing: 16) {
                                ForEach(model.accounts) { account in
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text(account.label).veetType(.ui, weight: .semibold)
                                        Text("\(account.historyProcessed) \(account.historyProcessed == 1 ? "thread" : "threads") retrieved")
                                            .veetType(.ui)
                                        Text(
                                            account.historyComplete
                                                ? "The 90-day window has been retrieved; analysis continues as needed."
                                                : "More of the 90-day window remains. Older history is paused."
                                        )
                                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                                    }
                                }
                            }.padding(.top, 10).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    } else {
                        ProgressView("Loading learning settings…")
                    }
                    if let error = model.errorMessage {
                        Text(error).foregroundColor(AppTheme.red)
                        Button("Retry") { Task { await model.loadLearning() } }
                            .buttonStyle(SecondaryCapsuleButtonStyle())
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(24).sheetFrame(minWidth: 300, macMinWidth: 480, idealWidth: 540, minHeight: 400, idealHeight: 620)
        .background(AppTheme.paper)
        .task { await model.loadLearning() }
        .confirmationDialog(
            "Reset email learning?",
            isPresented: Binding(get: { resetScope != nil }, set: { if !$0 { resetScope = nil } }),
            titleVisibility: .visible
        ) {
            Button("Reset", role: .destructive) {
                guard let scope = resetScope else { return }
                resetScope = nil
                Task { await model.resetLearning(scope: scope) }
            }
            Button("Cancel", role: .cancel) { resetScope = nil }
        } message: {
            Text(
                resetScope == "style"
                    ? "Removes the learned email writing style. Source mail, your draft edits and Chat history remain."
                    : resetScope == "preferences"
                        ? "Removes learned sender and content preferences. Source mail, writing style, your draft edits and Chat history remain."
                        : "Removes learned email preferences and writing style. Source mail, your draft edits and Chat history remain."
            )
        }
    }
}

struct EmailRevisionsScreen: View {
    @ObservedObject var model: EmailViewModel
    @Environment(\.dismiss) private var dismiss
    /// Lists saved draft revisions and restores selected wording into the current edit buffer.
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            EmailSheetHeader(title: "Draft history") { dismiss() }
            Text(
                "Earlier and conflicting versions remain available. Restoring a version fills your editor; Save creates a new revision."
            )
            .veetType(.meta).foregroundColor(AppTheme.inkSoft)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    ForEach(Array(model.revisions.enumerated()), id: \.offset) { _, revision in
                        DisclosureGroup("Revision \(revision.revision) · \(revision.updatedAt.formatted())") {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("To: \(revision.to.joined(separator: ", "))")
                                if !revision.cc.isEmpty { Text("Cc: \(revision.cc.joined(separator: ", "))") }
                                if !revision.bcc.isEmpty { Text("Bcc: \(revision.bcc.joined(separator: ", "))") }
                                Text(revision.subject).veetType(.ui, weight: .semibold)
                                Text(revision.body).textSelection(.enabled)
                                Button("Restore in editor") {
                                    model.useRevision(revision)
                                    dismiss()
                                }
                                .buttonStyle(SecondaryCapsuleButtonStyle())
                                .disabled(model.draft?.canEdit != true || model.conflict != nil)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }
            if let error = model.draftError { Text(error).foregroundColor(AppTheme.red) }
        }.padding(24).sheetFrame(minWidth: 300, macMinWidth: 520, idealWidth: 600, minHeight: 400)
            .background(AppTheme.paper)
            .task { await model.loadRevisions() }
    }
}

struct EmailSendReview: View {
    @ObservedObject var model: EmailViewModel
    /// Shows the frozen envelope and body before the owner explicitly approves or denies this send.
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            EmailSheetHeader(title: "Review & Send", dismissTitle: "Back to editing") { model.closeReview() }
                .padding(24)
            HairlineDivider()
            if let draft = model.reviewDraft {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        let account = model.accounts.first { $0.id == draft.accountID }
                        VStack(alignment: .leading, spacing: 6) {
                            Text("From: \(account?.emailAddress ?? account?.label ?? draft.accountID)")
                            Text("To: \(draft.to.joined(separator: ", "))")
                            if !draft.cc.isEmpty { Text("Cc: \(draft.cc.joined(separator: ", "))") }
                            if !draft.bcc.isEmpty { Text("Bcc: \(draft.bcc.joined(separator: ", "))") }
                        }.veetType(.ui).foregroundColor(AppTheme.inkSoft).textSelection(.enabled)
                        HairlineDivider()
                        Text(draft.subject).veetType(.ui, weight: .semibold)
                        Text(draft.body).veetType(.ui).lineSpacing(5).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
                }
                HairlineDivider()
                VStack(alignment: .leading, spacing: 12) {
                    Text("Send approves this exact message once.")
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    if let error = model.draftError { Text(error).veetType(.ui).foregroundColor(AppTheme.red) }
                    HStack(spacing: 12) {
                        Button("Deny this send", role: .destructive) { Task { await model.resolveSend(.deny) } }
                            .buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red)).disabled(model.isPerformingAction)
                        Spacer(minLength: 0)
                        Button("Send") { Task { await model.resolveSend(.approveOnce) } }
                            .primaryCapsule()
                            .disabled(model.isPerformingAction).accessibilityIdentifier("email.approve-send")
                    }
                }.padding(24)
            }
        }.sheetFrame(minWidth: 300, macMinWidth: 520, idealWidth: 600, minHeight: 400, idealHeight: 660)
            .background(AppTheme.paper)
    }
}
