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
    static let sender: Font.TextStyle = .body
    static let subject: Font.TextStyle = .body
    static let gist: Font.TextStyle = .callout
    static let note: Font.TextStyle = .subheadline
    static let meta: Font.TextStyle = .caption
    static let rowSpacing: CGFloat = 2
    static let handledHit: CGFloat = 28
    #else
    static let sender: Font.TextStyle = .callout
    static let subject: Font.TextStyle = .subheadline
    static let gist: Font.TextStyle = .footnote
    static let note: Font.TextStyle = .caption
    static let meta: Font.TextStyle = .caption
    static let rowSpacing: CGFloat = 3
    static let handledHit: CGFloat = 44
    #endif
    static let handledDiameter: CGFloat = 18
}

public struct EmailModeView: View {
    @ObservedObject var model: EmailViewModel
    var viewportHeight: CGFloat? = nil
    let discussInChat: () async -> Void
    @Environment(\.activeClientMode) private var activeMode
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var showingLearning = false
    @State private var showingSubscriptions = false
    @State private var showingBudgetDetails = false
    @State private var navigationPath: [UUID] = []
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

    /// Preserves platform navigation and presents learning and exact-send review above the inbox.
    public var body: some View {
        Group {
            #if os(macOS)
            macSplit
            #else
            if directSelection {
                NavigationSplitView {
                    inbox
                        .frame(height: macColumnHeight)
                        .navigationSplitViewColumnWidth(min: 280, ideal: 340, max: 420)
                } detail: {
                    detail.frame(height: macColumnHeight)
                }
            } else {
                NavigationStack(path: $navigationPath) {
                    inbox.navigationDestination(for: UUID.self) { id in
                        detail.task { if model.selectedThreadID != id { await model.openThread(id) } }
                    }
                }
            }
            #endif
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
        .onChange(of: model.unsubscribeAvailable) { available in
            if !available { showingSubscriptions = false }
        }
        .onAppear {
            if let id = model.selectedThreadID, navigationPath.last != id { navigationPath = [id] }
        }
        .onChange(of: model.selectedThreadID) { id in
            if let id, navigationPath.last != id { navigationPath = [id] } else if id == nil { navigationPath = [] }
        }
        .onChange(of: navigationPath) { path in
            if path.isEmpty, !directSelection, model.selectedThreadID != nil { model.clearSelection() }
        }
    }

    private var detail: some View {
        EmailThreadScreen(model: model, subscriptions: model.subscriptions, discussInChat: discussInChat)
            .id(model.selectedThreadID)
    }

    #if os(macOS)
    /// Uses a native divider while the shared root owns the Mac window toolbar.
    private var macSplit: some View {
        HSplitView {
            inbox
                .frame(minWidth: 280, idealWidth: 340, maxWidth: 560)
                .frame(height: macColumnHeight)
            detail
                .frame(minWidth: 360, maxWidth: .infinity)
                .frame(height: macColumnHeight)
                .layoutPriority(1)
        }
    }
    #endif

    /// Bounds AppKit split-view columns without imposing a fixed height on compact iOS navigation.
    private var macColumnHeight: CGFloat? {
        #if os(macOS)
        viewportHeight
        #else
        nil
        #endif
    }

    private var visibleAccounts: [EmailAccountView] {
        model.accounts.filter { model.selectedAccountID == nil || $0.id == model.selectedAccountID }
    }

    /// Groups triage controls above independently actionable rows and per-account freshness below them.
    private var inbox: some View {
        VStack(spacing: 0) {
            inboxHeader
                .fixedSize(horizontal: false, vertical: true)
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
                        Label("Mail couldn’t be updated", systemImage: "exclamationmark.circle").appFont(.headline)
                        Text(error).appFont(.callout).foregroundColor(AppTheme.inkSoft)
                        Button("Try again") {
                            Task {
                                await model.reload()
                                await model.refresh()
                            }
                        }
                        .buttonStyle(SecondaryCapsuleButtonStyle())
                    }.padding(.vertical, 16).emailHideSeparator()
                }
                if let pause = model.budgetPauseMessage {
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
                    .transition(.opacity)
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
            .animation(reduceMotion ? nil : .easeInOut(duration: 0.2), value: model.items.map(\.id))
            .listStyle(.plain)
            .scrollContentBackground(.hidden)
            .frame(minHeight: 0, maxHeight: .infinity)
            if !visibleAccounts.isEmpty {
                HairlineDivider()
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 12) {
                        Text("Check a conversation to archive it in Gmail.")
                            .appFont(.caption).foregroundColor(AppTheme.inkSoft)
                        if visibleAccounts.contains(where: { $0.archiveSupported != true }) {
                            Text("Gmail archiving is unavailable for accounts without archive support. Email browsing remains available.")
                                .appFont(.caption).foregroundColor(AppTheme.inkSoft)
                        }
                        ForEach(visibleAccounts) { account in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(account.label).appFont(.callout, weight: .semibold)
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
                            }.appFont(.caption)
                        }
                    }.padding(.top, 10).frame(maxWidth: .infinity, alignment: .leading)
                } label: {
                    Label(
                        mailboxStatus,
                        systemImage: mailboxNeedsAttention ? "exclamationmark.circle" : "arrow.triangle.2.circlepath"
                    )
                    .appFont(.caption).foregroundColor(mailboxNeedsAttention ? AppTheme.amberText : AppTheme.inkSoft)
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

    /// A paused budget is one short note on paperDeep, three lines tall with
    /// its accounting folded, so the first row still starts near the top.
    private func budgetPause(_ pause: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(alignment: .firstTextBaseline) {
                Label("Processing paused", systemImage: "pause.circle")
                    .appFont(.caption, weight: .semibold).foregroundColor(AppTheme.amberText)
                Spacer(minLength: 4)
                Button("Check again") { Task { await model.refresh() } }
                    .buttonStyle(.plain).appFont(.caption, weight: .medium).foregroundColor(AppTheme.tealText)
            }
            Text(pause).appFont(.caption).foregroundColor(AppTheme.inkSoft)
            HStack(alignment: .firstTextBaseline) {
                if let retryAt = model.budgetRetryAt {
                    Text("Next check: \(retryAt.formatted(date: .abbreviated, time: .shortened))")
                        .appFont(.caption).foregroundColor(AppTheme.inkSoft).lineLimit(1)
                }
                Spacer(minLength: 4)
                Button {
                    showingBudgetDetails.toggle()
                } label: {
                    Label("Details", systemImage: showingBudgetDetails ? "chevron.down" : "chevron.right")
                }
                .buttonStyle(.plain).appFont(.caption, weight: .medium).foregroundColor(AppTheme.inkSoft)
                .accessibilityValue(showingBudgetDetails ? "Expanded" : "Collapsed")
                .accessibilityIdentifier("email.budget-details")
            }
            if showingBudgetDetails {
                VStack(alignment: .leading, spacing: 3) {
                    ForEach(model.budgetPauseDetails, id: \.self) { Text($0) }
                    Text("Cached mail and editing remain available.")
                }.frame(maxWidth: .infinity, alignment: .leading)
                    .appFont(.caption).foregroundColor(AppTheme.inkSoft)
            }
        }
        .padding(8).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(AppTheme.paperDeep))
    }

    /// Present account and inbox controls above the priority rows.
    private var inboxHeader: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Picker("Accounts", selection: Binding(get: { model.selectedAccountID }, set: { model.setAccount($0) }))
                {
                    Text("All accounts").tag(String?.none)
                    ForEach(model.accounts) { account in Text(account.label).tag(Optional(account.id)) }
                }
                .pickerStyle(.menu).labelsHidden().accessibilityLabel("Accounts")
                .accessibilityIdentifier("email.accounts")
                Spacer(minLength: 8)
                Text(threadCount)
                    .appFont(.caption).foregroundColor(AppTheme.inkSoft).fixedSize()
            }
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundColor(AppTheme.inkSoft)
                TextField("Find mail", text: Binding(get: { model.searchText }, set: { model.setSearchText($0) }))
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
            .appFont(.callout)
            .padding(.horizontal, 10).frame(minHeight: A2Metrics.controlHeight + 4)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(AppTheme.field))
            mailViewControl
        }.padding(12)
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

    /// "5 threads · 2 need a reply", counted over the loaded rows as the reference's header line.
    private var threadCount: String {
        let count = model.items.count
        var line = "\(count) \(count == 1 ? "thread" : "threads")"
        let needing = model.items.filter { $0.needsReply && !$0.isHandled && !$0.isArchived }.count
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

/// One thread as the reference draws it: who and when, the subject, Veetbot's
/// gist, and what the conversation needs from the owner. A handled or
/// archived conversation reads in soft ink.
private struct EmailThreadRow: View {
    @ObservedObject var model: EmailViewModel
    let thread: EmailThreadView

    private var settled: Bool { thread.isHandled || thread.isArchived || thread.isInSpam }

    var body: some View {
        VStack(alignment: .leading, spacing: EmailType.rowSpacing) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(thread.senders.joined(separator: ", "))
                    .appFont(EmailType.sender, weight: settled ? .regular : .semibold)
                    .foregroundColor(settled ? AppTheme.inkSoft : AppTheme.ink)
                    .lineLimit(1)
                Spacer(minLength: 4)
                Text(thread.updatedAt, format: .dateTime.month(.abbreviated).day())
                    .appFont(EmailType.meta).foregroundColor(AppTheme.inkFaint).fixedSize()
            }
            Text(thread.subject.isEmpty ? "No subject" : thread.subject)
                .appFont(EmailType.subject).foregroundColor(settled ? AppTheme.inkSoft : AppTheme.ink).lineLimit(1)
            Text(thread.summary).appFont(EmailType.gist).foregroundColor(AppTheme.inkSoft).lineLimit(1)
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                EmailThreadStatus(thread: thread)
                if model.accounts.count > 1 {
                    Spacer(minLength: 4)
                    Text(model.accounts.first { $0.id == thread.accountID }?.label ?? thread.accountID)
                        .appFont(EmailType.meta).foregroundColor(AppTheme.inkFaint).lineLimit(1)
                }
            }
            if !thread.complete {
                Label("Partial thread", systemImage: "exclamationmark.circle")
                    .appFont(EmailType.meta).foregroundColor(AppTheme.amberText)
            }
            if thread.isSuspectedSpam {
                Label("Suspected spam", systemImage: "exclamationmark.octagon")
                    .appFont(EmailType.meta).foregroundColor(AppTheme.amberText)
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
                        .font(.system(size: 10, weight: .bold))
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

    var body: some View {
        Button {
            Task { await model.setThreadArchived(thread, archived: !thread.isArchived) }
        } label: {
            Label(thread.isArchived ? "Move to Inbox" : "Archive in Gmail",
                  systemImage: thread.isArchived ? "checkmark.circle.fill" : "archivebox")
        }
        .buttonStyle(SecondaryCapsuleButtonStyle())
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
            Text(message).appFont(.caption).foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("email.archive.status.\(thread.id.uuidString)")
            if thread.archiveOperation != nil {
                Button(thread.archiveOperation?.status == "uncertain" ? "Check recorded status" : "Check status") {
                    Task { await model.checkArchiveStatus(thread.id) }
                }
                .buttonStyle(.plain).appFont(.caption, weight: .medium).foregroundColor(AppTheme.tealText)
                .labelStyle(.titleOnly)
                .accessibilityIdentifier("email.archive.check.\(thread.id.uuidString)")
            }
        } else if showsAvailability, let reason = model.archiveUnavailableReason(for: thread) {
            Text(reason).appFont(.caption).foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

private struct EmailThreadScreen: View {
    private enum Field: Hashable { case to, cc, bcc, subject, body, feedback, refinement }
    @ObservedObject var model: EmailViewModel
    @ObservedObject var subscriptions: EmailSubscriptionsViewModel
    let discussInChat: () async -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var target: EmailFeedbackTarget = .thread
    @State private var explanation = ""
    @State private var targetValue = ""
    @State private var refinement = ""
    @State private var showingDiscard = false
    @State private var showingExclusion = false
    @State private var showingRevisions = false
    @State private var showingFeedback = false
    @State private var showingEnvelope = false
    @State private var peopleLookup: PeopleLookup?
    @FocusState private var focusedField: Field?

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
                            EmailGistBlock(thread: thread, draft: model.draft)
                            if let error = model.threadReadMessage {
                                Label(error, systemImage: "exclamationmark.circle").foregroundColor(AppTheme.red)
                                    .accessibilityIdentifier("email.read-error")
                            }
                            conversation(thread)
                            teachCard
                            draftEditor.id("reply")
                        }
                        .padding(.horizontal, 24).padding(.top, 20).padding(.bottom, 28)
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
                                withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.2)) {
                                    proxy.scrollTo("reply", anchor: .top)
                                }
                            } label: {
                                Label(
                                    model.draft == nil ? "Reply" : "Your reply", systemImage: "arrowshape.turn.up.left")
                            }
                            .buttonStyle(PrimaryCapsuleButtonStyle())
                            .accessibilityIdentifier("email.jump-to-reply")
                        }
                        .padding(.horizontal, 20).padding(.vertical, 10)
                        .background(.regularMaterial)
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
                    Button("Done") { focusedField = nil }.accessibilityIdentifier("email.keyboard-done")
                }
            }
        #endif
    }

    /// The subject in the display face, then who wrote and when; the reversible
    /// handling controls sit on the line above them.
    private func threadHeader(_ thread: EmailThreadView) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 8) {
                Label(accountDescription(thread.accountID), systemImage: "envelope")
                    .appFont(.caption).foregroundColor(AppTheme.inkFaint).lineLimit(1)
                Spacer(minLength: 8)
                // The capsule keeps its full name; the account label is what gives way.
                EmailArchiveButton(model: model, thread: thread, accessibilityID: "email.handled.detail")
                    .fixedSize().layoutPriority(1)
                Menu {
                    if model.canReportSpam(thread), !thread.isSuspectedSpam {
                        Button("Report spam") { Task { await model.setThreadSpam(thread, spam: true) } }
                            .accessibilityIdentifier("email.spam.menu.report")
                    }
                    Button("Exclude this thread from learning", role: .destructive) { showingExclusion = true }
                } label: {
                    Image(systemName: "ellipsis")
                }
                .menuStyle(.button)
                .buttonStyle(QuietIconButtonStyle())
                .menuIndicator(.hidden)
                .accessibilityLabel("Thread actions").help("Thread actions")
                .disabled(model.isPerformingAction)
            }
            EmailArchiveStatus(model: model, thread: thread, showsAvailability: true)
            VStack(alignment: .leading, spacing: 4) {
                Text(thread.subject.isEmpty ? "No subject" : thread.subject)
                    .appDisplayFont(.title2)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                Text("\(thread.senders.joined(separator: ", ")) · \(thread.updatedAt, format: .dateTime.month(.abbreviated).day())")
                    .appFont(EmailType.gist).foregroundColor(AppTheme.inkSoft)
            }
            // Bulk mail only, on an account that advertises the action, with
            // verified evidence. Everything else offers nothing here.
            if let block = thread.subscription, block.canUnsubscribe,
               model.unsubscribeSupported(for: thread.accountID) {
                HStack(spacing: 8) {
                    Text(thread.senders.first ?? block.destination)
                        .appFont(.caption).foregroundColor(AppTheme.inkSoft).lineLimit(1)
                    Button("Unsubscribe") { subscriptions.beginUnsubscribe(thread: thread) }
                        .buttonStyle(SecondaryCapsuleButtonStyle())
                        .accessibilityLabel("Unsubscribe from \(thread.senders.first ?? block.destination)")
                        .accessibilityIdentifier("email.unsubscribe.thread")
                }
            }
            EmailSpamControls(model: model, thread: thread)
        }
    }

    /// Each message as prose under its sender's line; a hairline parts them.
    private func conversation(_ thread: EmailThreadView) -> some View {
        let messages = thread.messages ?? []
        return VStack(alignment: .leading, spacing: 18) {
            ForEach(messages) { message in
                VStack(alignment: .leading, spacing: 10) {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(message.sender).appFont(EmailType.sender, weight: .semibold).textSelection(.enabled)
                        Spacer(minLength: 4)
                        Text(message.sentAt, format: .dateTime.month(.abbreviated).day().year().hour().minute())
                            .appFont(EmailType.meta).foregroundColor(AppTheme.inkFaint).fixedSize()
                    }
                    Button {
                        peopleLookup = .correspondent(message.sender, at: message.sentAt)
                    } label: {
                        Label("View in People", systemImage: "person.crop.circle")
                    }
                    .buttonStyle(.plain).appFont(.caption, weight: .medium).foregroundColor(AppTheme.tealText)
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
                    .appFont(.caption).foregroundColor(AppTheme.inkSoft)
                    EmailReadingView(message: message)
                    if !message.complete {
                        Label(
                            "This message is incomplete. Refresh before relying on its full contents.",
                            systemImage: "exclamationmark.circle"
                        )
                        .appFont(.callout).foregroundColor(AppTheme.amberText)
                    }
                    ForEach(Array((message.attachments ?? []).enumerated()), id: \.offset) { _, attachment in
                        Label("\(attachment.filename) · attachment not read", systemImage: "paperclip")
                            .appFont(.caption).foregroundColor(AppTheme.inkSoft)
                    }
                }
                if message.id != messages.last?.id { HairlineDivider() }
            }
        }
    }

    /// Teach Veetbot: one paperDeep card that opens into scoped importance
    /// feedback and reports what was applied, with Undo.
    private var teachCard: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                showingFeedback.toggle()
            } label: {
                HStack {
                    Text("Teach Veetbot").appFont(.caption, weight: .semibold)
                    Spacer()
                    Image(systemName: showingFeedback ? "chevron.up" : "chevron.down").appFont(.caption)
                }.foregroundColor(AppTheme.inkSoft).contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(showingFeedback ? "Expanded" : "Collapsed")
            .accessibilityIdentifier("email.feedback")
            if showingFeedback {
                feedback
            } else {
                Text("Say whether mail like this deserves your attention: this thread, a person, or a kind of content.")
                    .appFont(.caption).foregroundColor(AppTheme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let message = model.feedbackMessage {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: "checkmark").appFont(.caption, weight: .semibold)
                    Text(message).appFont(.caption)
                    if model.feedbackID != nil {
                        Button("Undo") { Task { await model.undoFeedback() } }
                            .buttonStyle(.plain).appFont(.caption, weight: .medium).foregroundColor(AppTheme.tealText)
                    }
                }.foregroundColor(AppTheme.inkSoft)
            }
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(AppTheme.paperDeep))
    }

    /// Offer explicit importance feedback with supported person and topic targets.
    private var feedback: some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker("Apply to", selection: $target) {
                ForEach(EmailFeedbackTarget.allCases, id: \.self) { value in Text(value.title).tag(value) }
            }.pickerStyle(.menu).accessibilityIdentifier("email.feedback-target")
                .onChange(of: target) { _ in targetValue = "" }
            if target == .person {
                Picker("Person", selection: $targetValue) {
                    Text("Choose a person").tag("")
                    ForEach(model.thread?.senders ?? [], id: \.self) { person in Text(person).tag(person) }
                }
                .accessibilityIdentifier("email.feedback-person")
            } else if target == .topic {
                let topics = model.thread?.feedbackTopics ?? []
                Picker("Content topic", selection: $targetValue) {
                    Text("Choose a topic").tag("")
                    ForEach(topics, id: \.self) { topic in Text(topic).tag(topic) }
                }
                .accessibilityIdentifier("email.feedback-topic")
                .onChange(of: topics) { values in
                    if !values.contains(targetValue) { targetValue = "" }
                }
                if topics.isEmpty {
                    Text("No content topics are available yet. You can apply feedback to This thread.")
                        .appFont(.caption).foregroundColor(AppTheme.inkSoft)
                }
            }
            TextField("Explain what matters (optional)", text: $explanation).focused($focusedField, equals: .feedback)
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
            Text("Does this thread need a reply?").appFont(.caption).foregroundColor(AppTheme.inkSoft)
            HStack(spacing: 8) {
                Button("Needs reply") { Task { await model.giveFeedback(target: .thread, judgment: "needs_reply") } }
                Button("No reply needed") {
                    Task { await model.giveFeedback(target: .thread, judgment: "no_reply_needed") }
                }
            }.buttonStyle(QuietCapsuleButtonStyle())
        }.appFont(.callout).disabled(model.isPerformingAction).padding(.top, 4)
    }

    @ViewBuilder private var draftEditor: some View {
        if let draft = model.draft, let edit = model.currentEdit {
            VStack(alignment: .leading, spacing: 12) {
                HStack(alignment: .firstTextBaseline) {
                    Text("Your reply, drafted by Veetbot").appFont(.caption, weight: .semibold)
                        .foregroundColor(AppTheme.inkSoft)
                    Spacer()
                    Text(model.isSaving ? "Saving…" : edit.isDirty ? "Unsaved changes" : "Saved")
                        .appFont(.caption).foregroundColor(AppTheme.inkFaint)
                }
                Text("From: \(accountDescription(draft.accountID))").appFont(.caption).foregroundColor(AppTheme.inkFaint)
                draftNotices(draft)
                VStack(alignment: .leading, spacing: 10) {
                    Group {
                        envelopeField("To", path: \.to, field: .to)
                        Button {
                            showingEnvelope.toggle()
                        } label: {
                            HStack(spacing: 6) {
                                Text("Cc, Bcc & subject")
                                Image(systemName: showingEnvelope ? "chevron.up" : "chevron.down")
                            }
                        }.buttonStyle(.plain).appFont(.caption, weight: .medium).foregroundColor(AppTheme.tealText)
                            .accessibilityIdentifier("email.draft-envelope")
                            .accessibilityValue(showingEnvelope ? "Expanded" : "Collapsed")
                        if showingEnvelope {
                            envelopeField("Cc", path: \.cc, field: .cc)
                            envelopeField("Bcc", path: \.bcc, field: .bcc)
                            envelopeField("Subject", path: \.subject, field: .subject)
                        }
                    }.disabled(!draft.canEdit || model.isPerformingAction)
                    HairlineDivider()
                    // `.disabled` leaves the AppKit text view editable and focused, so a draft that can
                    // no longer change (sending, sent, uncertain) renders as text instead of an editor.
                    // Only the editor is disabled: a disabled ancestor would also block selecting the text.
                    if draft.canEdit {
                        TextEditor(text: editBinding(\.body)).appFont(.body).lineSpacing(5)
                            .scrollContentBackground(.hidden)
                            .frame(minHeight: 180, idealHeight: 220)
                            .focused($focusedField, equals: .body)
                            .disabled(model.isPerformingAction)
                            .accessibilityLabel("Reply body").accessibilityIdentifier("email.draft-body")
                    } else {
                        // No accessibilityLabel: on a selectable Text, AppKit resolves the label
                        // through the text itself and recurses until the stack overflows.
                        Text(edit.body).appFont(.body).lineSpacing(5).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .accessibilityIdentifier("email.draft-body")
                    }
                }
                .padding(12)
                .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(AppTheme.surface))
                .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(AppTheme.hairlineStrong))
                HStack(spacing: 10) {
                    Button("Review & Send") { Task { await model.prepareSend() } }
                        .buttonStyle(PrimaryCapsuleButtonStyle())
                        .disabled(!model.canReview).accessibilityIdentifier("email.review-send")
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
                    }
                    .menuStyle(.button)
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .menuIndicator(.hidden)
                    Spacer(minLength: 0)
                }
                // A refused send belongs next to the control that asked for it; above the
                // conversation it reads as the button having done nothing.
                if let error = model.draftActionMessage {
                    Label(error, systemImage: "exclamationmark.circle").foregroundColor(AppTheme.red)
                        .appFont(.callout).accessibilityIdentifier("email.action-error")
                }
                if draft.approvalID != nil && !edit.isDirty && draft.status == "awaiting_approval" {
                    Button("Open pending approval") { Task { await model.loadReview() } }
                        .buttonStyle(SecondaryCapsuleButtonStyle()).disabled(draft.stale)
                }
                if let message = model.styleExampleMessage {
                    Text(message).appFont(.caption).foregroundColor(AppTheme.inkSoft)
                }
                DisclosureGroup("Refine with Veetbot") {
                    VStack(alignment: .leading, spacing: 10) {
                        TextField("How should the reply change?", text: $refinement).focused(
                            $focusedField, equals: .refinement
                        )
                        .textFieldStyle(.plain).a2Field()
                        Button("Refine draft") { Task { await model.generateDraft(instruction: refinement) } }
                            .buttonStyle(SecondaryCapsuleButtonStyle())
                            .disabled(
                                refinement.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                    || model.isPerformingAction || model.conflict != nil)
                    }.padding(.top, 10)
                }.appFont(.callout).foregroundColor(AppTheme.inkSoft)
            }
            .onAppear { showingEnvelope = !edit.cc.isEmpty || !edit.bcc.isEmpty }
            .onChange(of: draft.id) { _ in showingEnvelope = !edit.cc.isEmpty || !edit.bcc.isEmpty }
            .onChange(of: draft.canEdit) { canEdit in if !canEdit { focusedField = nil } }
        } else {
            VStack(alignment: .leading, spacing: 10) {
                Text("Your reply").appFont(.caption, weight: .semibold).foregroundColor(AppTheme.inkSoft)
                Text("Start with a draft in your own style, then make it yours.").appFont(.callout)
                    .foregroundColor(AppTheme.inkSoft)
                Button {
                    Task { await model.generateDraft() }
                } label: {
                    if model.isPerformingAction {
                        ProgressView("Preparing draft…")
                    } else {
                        Label("Draft reply", systemImage: "square.and.pencil")
                    }
                }.buttonStyle(PrimaryCapsuleButtonStyle()).disabled(model.isPerformingAction)
                if let error = model.draftActionMessage {
                    Label(error, systemImage: "exclamationmark.circle").foregroundColor(AppTheme.red)
                        .appFont(.callout).accessibilityIdentifier("email.action-error")
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Distinguishes draft progress, confirmed sending, uncertainty and recoverable editing conflicts.
    @ViewBuilder private func draftNotices(_ draft: EmailDraftView) -> some View {
        if draft.stale {
            Text("New mail arrived. Review the updated thread before sending.").foregroundColor(AppTheme.amberText)
            Button("I've reviewed the updated thread") { Task { await model.confirmCurrentSourceReviewed() } }
                .buttonStyle(SecondaryCapsuleButtonStyle())
                .disabled(model.conflict != nil || model.isSaving)
        }
        if draft.status == "uncertain" {
            Text(
                "Send status unknown. Check the thread before taking another action. This message will not be resent automatically."
            ).foregroundColor(AppTheme.amberText)
        } else if draft.status == "sent" {
            Label("Sent", systemImage: "checkmark.circle.fill").foregroundColor(AppTheme.tealText)
        } else if draft.isProcessing {
            ProgressView(draft.status == "sending" ? "Sending approved message…" : "Preparing your draft…")
        }
        if let conflict = model.conflict {
            VStack(alignment: .leading, spacing: 8) {
                Text("This draft changed on another device. Your edits are preserved.").foregroundColor(AppTheme.amberText)
                DisclosureGroup("Read the server version") { Text(conflict.body).textSelection(.enabled) }
                HStack(spacing: 8) {
                    Button("Use server version") { model.useServerDraft() }
                    Button("Save my version") { Task { await model.keepLocalDraft() } }
                }.buttonStyle(SecondaryCapsuleButtonStyle())
            }.accessibilityIdentifier("email.draft-conflict")
        }
    }

    /// Connects an envelope field to the versioned edit buffer and keyboard focus.
    private func envelopeField(_ title: String, path: WritableKeyPath<EmailDraftEdit, String>, field: Field)
        -> some View
    {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(title).appFont(.callout).foregroundColor(AppTheme.inkSoft).frame(width: 54, alignment: .leading)
            TextField(title, text: editBinding(path)).textFieldStyle(.plain).appFont(.callout)
                .focused($focusedField, equals: field)
                .accessibilityLabel(title).accessibilityIdentifier("email.draft-\(title.lowercased())")
        }
    }

    /// Routes local text changes through the view model's autosave and conflict-preservation path.
    private func editBinding(_ path: WritableKeyPath<EmailDraftEdit, String>) -> Binding<String> {
        Binding(get: { model.currentEdit?[keyPath: path] ?? "" }, set: { model.changeEdit(path, to: $0) })
    }
    /// Identifies the source account by its label and address, falling back to the server identifier.
    private func accountDescription(_ id: String) -> String {
        guard let account = model.accounts.first(where: { $0.id == id }) else { return id }
        return account.emailAddress.map { "\(account.label) · \($0)" } ?? account.label
    }
}

/// Veetbot's reading of the thread behind a teal rule: the gist, then what the
/// conversation needs and why it reached the owner's attention.
private struct EmailGistBlock: View {
    let thread: EmailThreadView
    var draft: EmailDraftView? = nil

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Veetbot").appFont(.caption, weight: .semibold).foregroundColor(AppTheme.tealText)
            Text(thread.summary).appFont(.body).fixedSize(horizontal: false, vertical: true)
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
    @State private var showingOriginal = false

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if let reader = message.readerBody, !reader.isEmpty {
                HStack {
                    Text(showingOriginal ? "Original message" : "Reading view")
                        .appFont(.caption).foregroundColor(AppTheme.inkFaint)
                    Spacer()
                    Button(showingOriginal ? "Reading view" : "Show original") {
                        showingOriginal.toggle()
                    }
                    .buttonStyle(.plain).appFont(.caption, weight: .medium).foregroundColor(AppTheme.tealText)
                    .accessibilityIdentifier("email.reader.original")
                }
                if showingOriginal {
                    original
                } else {
                    MarkdownContentView(text: reader)
                        .appFont(.body).lineSpacing(5)
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
        .onChange(of: message.id) { _ in showingOriginal = false }
    }

    private var original: some View {
        Text(message.body).appFont(.body).lineSpacing(5).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

extension View {
    /// Hides separators for inbox notices.
    func emailHideSeparator() -> some View {
        listRowSeparator(.hidden)
    }
}

/// A flag explains itself and offers the owner's two answers; a report offers its undo (ADR-0165).
private struct EmailSpamControls: View {
    @ObservedObject var model: EmailViewModel
    let thread: EmailThreadView

    var body: some View {
        if thread.isInSpam {
            HStack(spacing: 8) {
                Text("Reported as spam. Gmail empties Spam after thirty days.")
                    .appFont(.caption).foregroundColor(AppTheme.inkSoft)
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
                    .appFont(.caption, weight: .semibold).foregroundColor(AppTheme.amberText)
                    .accessibilityIdentifier("email.spam.tag.detail")
                Text("Veetbot thinks this may be spam, so it is not in your priority list. Nothing has changed in Gmail.")
                    .appFont(.caption).foregroundColor(AppTheme.inkSoft)
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
                    Text(error).appFont(.caption).foregroundColor(AppTheme.inkSoft)
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

    private enum Tone { case attention, draft, settled }

    private var state: (title: String, tone: Tone) {
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
        if tone == .attention, !reason.isEmpty {
            Text("\(title) · \(reason)")
                .appFont(EmailType.note, weight: .medium).foregroundColor(color(for: tone))
                .lineLimit(showsReason ? nil : 1)
                .fixedSize(horizontal: false, vertical: showsReason)
        } else {
            Text(title).appFont(EmailType.note, weight: .medium).foregroundColor(color(for: tone))
            if showsReason, !reason.isEmpty {
                Text(reason).appFont(EmailType.note).foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
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
            Image(systemName: symbol).font(.system(size: 26, weight: .light)).foregroundColor(AppTheme.teal)
                .frame(width: 60, height: 60).background(Circle().fill(AppTheme.tealWash))
            Text(title).appDisplayFont(.title3).multilineTextAlignment(.center)
            Text(message).appFont(.callout).foregroundColor(AppTheme.inkSoft).multilineTextAlignment(.center)
        }.padding(.horizontal, 20).padding(.vertical, 36).frame(maxWidth: .infinity)
    }
}

/// Observes refresh state directly even when the window toolbar is owned by the app coordinator's view.
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
            Text(title).appDisplayFont(.title2)
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
                    .appFont(.caption).foregroundColor(AppTheme.inkSoft)
                    .accessibilityIdentifier("email.learning-disclosure")
                    if let learning = model.learning {
                        Text(learning.paused ? "Learning is paused." : "Learning continues while Email is active.")
                        Text(
                            "\(learning.historyProcessed) \(learning.historyProcessed == 1 ? "thread" : "threads") retrieved · \(learning.styleExamples) \(learning.styleExamples == 1 ? "style example" : "style examples")"
                        )
                        .appFont(.caption).foregroundColor(AppTheme.inkSoft)
                        Text(
                            learning.historyComplete
                                ? "The 90-day window has been retrieved; analysis continues as needed."
                                : "Retrieval of the latest 90 days is incomplete. Older history is paused."
                        )
                        .appFont(.caption).foregroundColor(AppTheme.inkSoft)
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
                            .appFont(.caption)
                        VStack(alignment: .leading, spacing: 4) {
                            Button("Reset importance preferences", role: .destructive) { resetScope = "preferences" }
                            Button("Reset writing style", role: .destructive) { resetScope = "style" }
                            Button("Reset all email learning", role: .destructive) { resetScope = "all" }
                        }.buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red))
                        DisclosureGroup("History by account") {
                            VStack(alignment: .leading, spacing: 16) {
                                ForEach(model.accounts) { account in
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text(account.label).appFont(.headline)
                                        Text("\(account.historyProcessed) \(account.historyProcessed == 1 ? "thread" : "threads") retrieved")
                                            .appFont(.callout)
                                        Text(
                                            account.historyComplete
                                                ? "The 90-day window has been retrieved; analysis continues as needed."
                                                : "More of the 90-day window remains. Older history is paused."
                                        )
                                        .appFont(.caption).foregroundColor(AppTheme.inkSoft)
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
            .appFont(.caption).foregroundColor(AppTheme.inkSoft)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    ForEach(Array(model.revisions.enumerated()), id: \.offset) { _, revision in
                        DisclosureGroup("Revision \(revision.revision) · \(revision.updatedAt.formatted())") {
                            VStack(alignment: .leading, spacing: 8) {
                                Text("To: \(revision.to.joined(separator: ", "))")
                                if !revision.cc.isEmpty { Text("Cc: \(revision.cc.joined(separator: ", "))") }
                                if !revision.bcc.isEmpty { Text("Bcc: \(revision.bcc.joined(separator: ", "))") }
                                Text(revision.subject).appFont(.headline)
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
                        }.appFont(.callout).foregroundColor(AppTheme.inkSoft).textSelection(.enabled)
                        HairlineDivider()
                        Text(draft.subject).appFont(.headline)
                        Text(draft.body).appFont(.body).lineSpacing(5).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
                }
                HairlineDivider()
                VStack(alignment: .leading, spacing: 12) {
                    Text("Send approves this exact message once.")
                        .appFont(.caption).foregroundColor(AppTheme.inkSoft)
                    if let error = model.draftError { Text(error).appFont(.callout).foregroundColor(AppTheme.red) }
                    HStack(spacing: 12) {
                        Button("Deny this send", role: .destructive) { Task { await model.resolveSend(.deny) } }
                            .buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red)).disabled(model.isPerformingAction)
                        Spacer(minLength: 0)
                        Button("Send") { Task { await model.resolveSend(.approveOnce) } }
                            .buttonStyle(PrimaryCapsuleButtonStyle())
                            .disabled(model.isPerformingAction).accessibilityIdentifier("email.approve-send")
                    }
                }.padding(24)
            }
        }.sheetFrame(minWidth: 300, macMinWidth: 520, idealWidth: 600, minHeight: 400, idealHeight: 660)
            .background(AppTheme.paper)
    }
}
