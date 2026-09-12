import SwiftUI

#if os(macOS)
    import AppKit
#else
    import UIKit
#endif

public struct EmailModeView: View {
    @ObservedObject var model: EmailViewModel
    var viewportHeight: CGFloat? = nil
    let discussInChat: () async -> Void
    @Environment(\.activeClientMode) private var activeMode
    @State private var showingLearning = false
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

    private var showingThread: Binding<Bool> {
        Binding(get: { model.selectedThreadID != nil }, set: { if !$0 { model.clearSelection() } })
    }

    /// Preserves platform navigation and presents learning and exact-send review above the inbox.
    public var body: some View {
        Group {
            if #available(iOS 16, macOS 13, *) {
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
            } else {
                NavigationView {
                    inbox.frame(height: macColumnHeight).background {
                        if !directSelection {
                            NavigationLink(isActive: showingThread) {
                                detail
                            } label: {
                                EmptyView()
                            }.hidden()
                        }
                    }
                    detail.frame(height: macColumnHeight)
                }
            }
        }
        .tint(EmailSurface.accent)
        .sheet(isPresented: Binding(get: { model.review != nil }, set: { if !$0 { model.closeReview() } })) {
            EmailSendReview(model: model)
        }
        .sheet(isPresented: $showingLearning) { EmailLearningScreen(model: model) }
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
        EmailThreadScreen(model: model, discussInChat: discussInChat).id(model.selectedThreadID)
    }

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
            Divider()
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
                        Text(error).appFont(.callout).foregroundColor(.secondary)
                        Button("Try again") {
                            Task {
                                await model.reload()
                                await model.refresh()
                            }
                        }
                        .buttonStyle(.bordered)
                    }.padding(.vertical, 16).emailHideSeparator()
                }
                if model.newImportantCount > 0 {
                    Button {
                        model.showNewItems()
                    } label: {
                        Label(
                            "Show \(model.newImportantCount) new important \(model.newImportantCount == 1 ? "thread" : "threads")",
                            systemImage: "arrow.up.circle.fill"
                        )
                        .appFont(.callout, weight: .semibold)
                    }.emailHideSeparator()
                }
                if model.isLoading && model.items.isEmpty {
                    ProgressView("Finding your mail…").padding(.vertical, 32)
                        .frame(maxWidth: .infinity).emailHideSeparator()
                } else if model.items.isEmpty && !model.unavailable && model.errorMessage == nil {
                    emptyInbox.emailHideSeparator()
                }
                if !model.items.isEmpty {
                    Text("Check a conversation to archive it in Gmail.")
                        .appFont(.caption).foregroundColor(.secondary).emailHideSeparator()
                    if model.accounts.contains(where: { $0.archiveSupported != true }) {
                        Text("Gmail archiving is unavailable for accounts without archive support. Email browsing remains available.")
                            .appFont(.caption).foregroundColor(.secondary).emailHideSeparator()
                    }
                }
                ForEach(model.items) { thread in
                    HStack(spacing: 8) {
                        threadRow(thread)
                            .accessibilityIdentifier("email.thread.\(thread.id.uuidString)")
                        EmailArchiveButton(model: model, thread: thread, accessibilityID: "email.handled.\(thread.id.uuidString)")
                            .labelStyle(.iconOnly)
                    }
                    .buttonStyle(.plain)
                    .listRowInsets(EdgeInsets(top: 8, leading: 16, bottom: 8, trailing: 16))
                    .listRowBackground(
                        thread.id == model.selectedThreadID ? EmailSurface.accent.opacity(0.10) : Color.clear)
                }
                if model.hasMore {
                    Button(model.listView == "priority" ? "More important" : "More mail") {
                        Task { await model.loadMore() }
                    }
                    .disabled(model.isLoading).padding(.vertical, 12)
                }
            }
            .listStyle(.plain)
            .frame(minHeight: 0, maxHeight: .infinity)
            if !visibleAccounts.isEmpty {
                Divider()
                DisclosureGroup {
                    VStack(alignment: .leading, spacing: 12) {
                        ForEach(visibleAccounts) { account in
                            VStack(alignment: .leading, spacing: 4) {
                                Text(account.label).appFont(.callout, weight: .semibold)
                                if let update = account.updateMessage {
                                    Text(update).foregroundColor(account.hasRefreshFailure ? .orange : .secondary)
                                } else if let date = account.lastSyncedAt {
                                    HStack(spacing: 4) {
                                        Text("Last updated")
                                        Text(date, style: .relative)
                                    }
                                    .foregroundColor(.secondary)
                                } else {
                                    Text("Not yet synchronized.").foregroundColor(.secondary)
                                }
                            }.appFont(.caption)
                        }
                    }.padding(.top, 10).frame(maxWidth: .infinity, alignment: .leading)
                } label: {
                    Label(
                        mailboxStatus,
                        systemImage: mailboxNeedsAttention ? "exclamationmark.circle" : "arrow.triangle.2.circlepath"
                    )
                    .appFont(.caption).foregroundColor(mailboxNeedsAttention ? .orange : .secondary)
                }
                .padding(16)
                .accessibilityIdentifier("email.mailbox-status")
            }
        }
        .background(EmailSurface.canvas)
        .navigationTitle("Email")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar {
            ToolbarItem {
                if activeMode == .email {
                    Button {
                        showingLearning = true
                    } label: {
                        Image(systemName: "slider.horizontal.3")
                    }
                    .accessibilityLabel("Email learning").help("Email learning")
                    .accessibilityIdentifier("email.learning")

                }
            }
            ToolbarItem {
                if activeMode == .email {
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

        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("email.inbox")
    }

    private var inboxHeader: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 5) {
                    Text(model.listView == "priority" ? "Your priorities" : "Other mail")
                        .appFont(.title2, weight: .bold)
                    Text(
                        model.listView == "priority"
                            ? "What needs your attention, across your accounts." : "Find what deserves a closer look."
                    )
                    .appFont(.caption).foregroundColor(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundColor(.secondary)
                TextField("Find mail", text: Binding(get: { model.searchText }, set: { model.setSearchText($0) }))
                    .textFieldStyle(.plain).accessibilityLabel("Find mail")
                    .accessibilityIdentifier("email.search")
                if !model.searchText.isEmpty {
                    Button {
                        model.setSearchText("")
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                    }
                    .buttonStyle(.plain).foregroundColor(.secondary).accessibilityLabel("Clear search")
                }
            }.appFont(.callout).padding(10).background(Color.primary.opacity(0.045))
                .clipShape(RoundedRectangle(cornerRadius: 9, style: .continuous))
            Picker("Mail view", selection: Binding(get: { model.listView }, set: { model.setListView($0) })) {
                Text("Important").tag("priority")
                Text("Other mail").tag("other")
            }.pickerStyle(.segmented).labelsHidden().accessibilityLabel("Mail view")
                .accessibilityIdentifier("email.mail-view")
            HStack {
                Picker("Accounts", selection: Binding(get: { model.selectedAccountID }, set: { model.setAccount($0) }))
                {
                    Text("All accounts").tag(String?.none)
                    ForEach(model.accounts) { account in Text(account.label).tag(Optional(account.id)) }
                }
                .pickerStyle(.menu).labelsHidden().accessibilityLabel("Accounts")
                .accessibilityIdentifier("email.accounts")
                Spacer(minLength: 8)
                Text("\(model.items.count) \(model.items.count == 1 ? "thread" : "threads")")
                    .appFont(.caption).foregroundColor(.secondary)
            }
        }.padding(20)
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
                threadLabel(thread)
            }
        } else if #available(iOS 16, macOS 13, *) {
            NavigationLink(value: thread.id) { threadLabel(thread) }
        } else {
            NavigationLink {
                detail.task { await model.openThread(thread.id) }
            } label: {
                threadLabel(thread)
            }
        }
    }

    /// Separates sender, account, subject and attention state so a thread can be scanned before opening.
    private func threadLabel(_ thread: EmailThreadView) -> some View {
        VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 10) {
                EmailAvatar(sender: thread.senders.first ?? "?")
                VStack(alignment: .leading, spacing: 3) {
                    Text(thread.senders.joined(separator: ", ")).appFont(.callout, weight: .semibold).lineLimit(1)
                    Text(model.accounts.first { $0.id == thread.accountID }?.label ?? thread.accountID)
                        .appFont(.caption).foregroundColor(.secondary)
                }
                Spacer(minLength: 4)
                Text(thread.updatedAt, format: .dateTime.month(.abbreviated).day())
                    .appFont(.caption).foregroundColor(.secondary)
            }
            Text(thread.subject.isEmpty ? "No subject" : thread.subject).appFont(.headline).lineLimit(2)
            Text(thread.summary).appFont(.callout).foregroundColor(.secondary).lineLimit(2)
            EmailThreadStatus(thread: thread)
            if !thread.complete {
                Label("Partial thread", systemImage: "exclamationmark.circle").appFont(.caption).foregroundColor(
                    .orange)
            }
        }
        .padding(.vertical, 10).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
    }
}

private struct EmailArchiveButton: View {
    @ObservedObject var model: EmailViewModel
    let thread: EmailThreadView
    let accessibilityID: String
    var showsAvailability = false

    /// The labelled gesture authorizes one Gmail label change; only confirmed Inbox state checks the box.
    var body: some View {
        VStack(alignment: .trailing, spacing: 5) {
            Button {
                Task { await model.setThreadArchived(thread, archived: !thread.isArchived) }
            } label: {
                Label(thread.isArchived ? "Move to Inbox" : "Archive in Gmail",
                      systemImage: thread.isArchived ? "checkmark.square.fill" : "square")
                    .frame(minWidth: 44, minHeight: 44)
                    .contentShape(Rectangle())
            }
            .foregroundColor(EmailSurface.accent)
            .disabled(!model.canArchive(thread))
            .accessibilityIdentifier(accessibilityID)
            .accessibilityLabel(thread.isArchived ? "Move to Inbox" : "Archive in Gmail")
            .accessibilityValue(thread.isArchived ? "Archived" : "In Inbox")
            .help(thread.isArchived ? "Move this conversation to its Gmail account's Inbox"
                  : "Archive this conversation in its originating Gmail account")
            if let message = model.archiveMessage(for: thread) {
                Text(message).appFont(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("email.archive.status.\(thread.id.uuidString)")
                if thread.archiveOperation != nil {
                    Button(thread.archiveOperation?.status == "uncertain" ? "Check recorded status" : "Check status") {
                        Task { await model.checkArchiveStatus(thread.id) }
                    }
                        .appFont(.caption).labelStyle(.titleOnly)
                        .accessibilityIdentifier("email.archive.check.\(thread.id.uuidString)")
                }
            } else if showsAvailability, let reason = model.archiveUnavailableReason(for: thread) {
                Text(reason).appFont(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

private struct EmailThreadScreen: View {
    private enum Field: Hashable { case to, cc, bcc, subject, body, topic, feedback, refinement }
    @ObservedObject var model: EmailViewModel
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
    @FocusState private var focusedField: Field?

    /// Keeps reading scrollable while reply and Chat actions remain reachable at the bottom edge.
    var body: some View {
        Group {
            if model.isLoadingThread {
                ProgressView("Opening thread…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let thread = model.thread {
                ScrollViewReader { proxy in
                    ScrollView {
                        VStack(alignment: .leading, spacing: 24) {
                            threadHeader(thread)
                            attentionSummary(thread)
                            if let error = model.draftError {
                                Label(error, systemImage: "exclamationmark.circle").foregroundColor(.red)
                                    .accessibilityIdentifier("email.action-error")
                            }
                            conversation(thread)
                            draftEditor.id("reply")
                        }
                        .padding(24).frame(maxWidth: 800, alignment: .leading).frame(maxWidth: .infinity)
                    }
                    .accessibilityIdentifier("email.detail")
                    .safeAreaInset(edge: .bottom, spacing: 0) {
                        HStack(spacing: 12) {
                            Button {
                                Task { await discussInChat() }
                            } label: {
                                Label("Discuss in Chat", systemImage: "bubble.left.and.bubble.right")
                            }.accessibilityIdentifier("email.discuss")
                            Spacer(minLength: 0)
                            Button {
                                withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.2)) {
                                    proxy.scrollTo("reply", anchor: .top)
                                }
                            } label: {
                                Label(
                                    model.draft == nil ? "Reply" : "Your reply", systemImage: "arrowshape.turn.up.left")
                            }
                            .buttonStyle(.borderedProminent).tint(AppTheme.turquoise)
                            .accessibilityIdentifier("email.jump-to-reply")
                        }
                        .appFont(.callout, weight: .semibold)
                        .padding(.horizontal, 20).padding(.vertical, 12)
                        .background(.regularMaterial)
                        .overlay(alignment: .top) { Divider() }
                    }
                }
            } else {
                EmailEmptyState(
                    symbol: "envelope.open", title: "Make room for what matters",
                    message: "Choose a thread to read, reply, or think it through with Veetbot."
                )
                .frame(maxWidth: 400).frame(maxWidth: .infinity, maxHeight: .infinity)
                if let error = model.draftError { Text(error).foregroundColor(.red) }
            }
        }
        .background(EmailSurface.canvas)
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
        #if os(iOS)
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    Button("Done") { focusedField = nil }.accessibilityIdentifier("email.keyboard-done")
                }
            }
        #endif
    }

    /// Shows source identity and confirmed attention state alongside reversible handling controls.
    private func threadHeader(_ thread: EmailThreadView) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                EmailThreadStatus(thread: thread, draft: model.draft)
                Spacer()
                EmailArchiveButton(model: model, thread: thread, accessibilityID: "email.handled.detail", showsAvailability: true)
                    .buttonStyle(.plain)
                Menu {
                    Button("Exclude this thread from learning", role: .destructive) { showingExclusion = true }
                } label: {
                    Image(systemName: "ellipsis").frame(width: 32, height: 32).contentShape(Rectangle())
                }
                .accessibilityLabel("Thread actions").help("Thread actions")
                .disabled(model.isPerformingAction)
            }
            Text(thread.subject.isEmpty ? "No subject" : thread.subject).appFont(.title, weight: .bold)
                .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            Label(accountDescription(thread.accountID), systemImage: "envelope")
                .appFont(.caption).foregroundColor(.secondary)
        }
    }

    /// Presents the summary first and expands explanations and scoped feedback only when requested.
    private func attentionSummary(_ thread: EmailThreadView) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("At a glance", systemImage: "text.alignleft").appFont(.caption, weight: .semibold)
                .foregroundColor(EmailSurface.accent)
            Text(thread.summary).appFont(.body).fixedSize(horizontal: false, vertical: true)
            DisclosureGroup("Why this matters") {
                Text(thread.reason).appFont(.callout).foregroundColor(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading).padding(.top, 6)
            }.appFont(.callout)
            Divider()
            Button {
                showingFeedback.toggle()
            } label: {
                HStack {
                    Label("Improve priorities", systemImage: "slider.horizontal.3")
                    Spacer()
                    Image(systemName: showingFeedback ? "chevron.up" : "chevron.down")
                }.contentShape(Rectangle())
            }
            .buttonStyle(.plain).appFont(.callout).foregroundColor(.secondary)
            .accessibilityValue(showingFeedback ? "Expanded" : "Collapsed")
            .accessibilityIdentifier("email.feedback")
            if showingFeedback { feedback }
            if let message = model.feedbackMessage {
                HStack {
                    Text(message).appFont(.caption)
                    if model.feedbackID != nil { Button("Undo") { Task { await model.undoFeedback() } } }
                }.foregroundColor(EmailSurface.accent)
            }
        }.padding(18).emailCard()
    }

    /// Renders source messages as selectable plain text with recipient and attachment metadata.
    private func conversation(_ thread: EmailThreadView) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Conversation").appFont(.headline)
            ForEach(thread.messages ?? []) { message in
                VStack(alignment: .leading, spacing: 16) {
                    HStack(alignment: .top, spacing: 12) {
                        EmailAvatar(sender: message.sender)
                        VStack(alignment: .leading, spacing: 5) {
                            Text(message.sender).appFont(.callout, weight: .semibold).textSelection(.enabled)
                            Text(message.sentAt, format: .dateTime.month(.abbreviated).day().year().hour().minute())
                                .appFont(.caption).foregroundColor(.secondary)
                        }
                        Spacer(minLength: 0)
                    }
                    DisclosureGroup {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("To: \(message.to.joined(separator: ", "))")
                            if !message.cc.isEmpty { Text("Cc: \(message.cc.joined(separator: ", "))") }
                        }.textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading).padding(.top, 6)
                    } label: {
                        Text("Message details")
                    }
                    .appFont(.caption).foregroundColor(.secondary)
                    Divider()
                    // Plain text never loads external HTML, remote images or tracking pixels.
                    Text(message.body).appFont(.body).lineSpacing(5).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if !message.complete {
                        Label(
                            "This message is incomplete. Refresh before relying on its full contents.",
                            systemImage: "exclamationmark.circle"
                        )
                        .appFont(.callout).foregroundColor(.orange)
                    }
                    ForEach(Array((message.attachments ?? []).enumerated()), id: \.offset) { _, attachment in
                        Label("\(attachment.filename) · attachment not read", systemImage: "paperclip")
                            .appFont(.caption).foregroundColor(.secondary)
                    }
                }.padding(20).emailCard()
            }
        }
    }

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
                TextField("Content topic", text: $targetValue).focused($focusedField, equals: .topic)
            }
            TextField("Explain what matters (optional)", text: $explanation).focused($focusedField, equals: .feedback)
                .textFieldStyle(.roundedBorder)
            HStack {
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
            }.buttonStyle(.bordered)
                .disabled(target == .person && targetValue.isEmpty)
            Divider()
            Text("Does this thread need a reply?").appFont(.caption).foregroundColor(.secondary)
            HStack {
                Button("Needs reply") { Task { await model.giveFeedback(target: .thread, judgment: "needs_reply") } }
                Button("No reply needed") {
                    Task { await model.giveFeedback(target: .thread, judgment: "no_reply_needed") }
                }
            }.buttonStyle(.bordered)
        }.appFont(.callout).disabled(model.isPerformingAction).padding(.top, 4)
    }

    @ViewBuilder private var draftEditor: some View {
        if let draft = model.draft, let edit = model.currentEdit {
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Label("Your reply", systemImage: "square.and.pencil").appFont(.headline)
                    Spacer()
                    Text(model.isSaving ? "Saving…" : edit.isDirty ? "Unsaved changes" : "Saved")
                        .appFont(.caption).foregroundColor(.secondary)
                }
                Text("From: \(accountDescription(draft.accountID))").appFont(.caption).foregroundColor(.secondary)
                draftNotices(draft)
                VStack(alignment: .leading, spacing: 12) {
                    envelopeField("To", path: \.to, field: .to)
                    Button {
                        showingEnvelope.toggle()
                    } label: {
                        HStack(spacing: 6) {
                            Text("Cc, Bcc & subject")
                            Image(systemName: showingEnvelope ? "chevron.up" : "chevron.down")
                        }
                    }.buttonStyle(.plain).appFont(.caption).foregroundColor(EmailSurface.accent)
                        .accessibilityIdentifier("email.draft-envelope")
                        .accessibilityValue(showingEnvelope ? "Expanded" : "Collapsed")
                    if showingEnvelope {
                        envelopeField("Cc", path: \.cc, field: .cc)
                        envelopeField("Bcc", path: \.bcc, field: .bcc)
                        envelopeField("Subject", path: \.subject, field: .subject)
                    }
                    Divider()
                    TextEditor(text: editBinding(\.body)).appFont(.body).lineSpacing(5).frame(
                        minHeight: 180, idealHeight: 220
                    )
                    .focused($focusedField, equals: .body)
                    .accessibilityLabel("Reply body").accessibilityIdentifier("email.draft-body")
                }.disabled(!draft.canEdit || model.isPerformingAction)
                Divider()
                HStack(spacing: 12) {
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
                    .appFont(.callout)
                    Spacer(minLength: 0)
                    Button("Review & Send") { Task { await model.prepareSend() } }
                        .buttonStyle(.borderedProminent).tint(AppTheme.turquoise).controlSize(.large)
                        .disabled(!model.canReview).accessibilityIdentifier("email.review-send")
                }
                if draft.approvalID != nil && !edit.isDirty && draft.status == "awaiting_approval" {
                    Button("Open pending approval") { Task { await model.loadReview() } }.disabled(draft.stale)
                }
                if let message = model.styleExampleMessage {
                    Text(message).appFont(.caption).foregroundColor(.secondary)
                }
                DisclosureGroup("Refine with Veetbot") {
                    VStack(alignment: .leading, spacing: 10) {
                        TextField("How should the reply change?", text: $refinement).focused(
                            $focusedField, equals: .refinement
                        )
                        .textFieldStyle(.roundedBorder)
                        Button("Refine draft") { Task { await model.generateDraft(instruction: refinement) } }
                            .buttonStyle(.bordered)
                            .disabled(
                                refinement.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                                    || model.isPerformingAction || model.conflict != nil)
                    }.padding(.top, 10)
                }.appFont(.callout).foregroundColor(.secondary)
            }.padding(20).emailCard(accent: true)
                .onAppear { showingEnvelope = !edit.cc.isEmpty || !edit.bcc.isEmpty }
                .onChange(of: draft.id) { _ in showingEnvelope = !edit.cc.isEmpty || !edit.bcc.isEmpty }
        } else {
            VStack(alignment: .leading, spacing: 14) {
                Label("Your reply", systemImage: "square.and.pencil").appFont(.headline)
                Text("Start with a draft in your own style, then make it yours.").appFont(.callout).foregroundColor(
                    .secondary)
                Button {
                    Task { await model.generateDraft() }
                } label: {
                    if model.isPerformingAction {
                        ProgressView("Preparing draft…")
                    } else {
                        Label("Draft reply", systemImage: "square.and.pencil")
                    }
                }.buttonStyle(.borderedProminent).tint(AppTheme.turquoise).disabled(model.isPerformingAction)
            }.frame(maxWidth: .infinity, alignment: .leading).padding(20).emailCard(accent: true)
        }
    }

    /// Distinguishes draft progress, confirmed sending, uncertainty and recoverable editing conflicts.
    @ViewBuilder private func draftNotices(_ draft: EmailDraftView) -> some View {
        if draft.stale {
            Text("New mail arrived. Review the updated thread before sending.").foregroundColor(.orange)
            Button("I've reviewed the updated thread") { Task { await model.confirmCurrentSourceReviewed() } }
                .disabled(model.conflict != nil || model.isSaving)
        }
        if draft.status == "uncertain" {
            Text(
                "Send status unknown. Check the thread before taking another action. This message will not be resent automatically."
            ).foregroundColor(.orange)
        } else if draft.status == "sent" {
            Label("Sent", systemImage: "checkmark.circle.fill").foregroundColor(EmailSurface.accent)
        } else if draft.isProcessing {
            ProgressView(draft.status == "sending" ? "Sending approved message…" : "Preparing your draft…")
        }
        if let conflict = model.conflict {
            VStack(alignment: .leading, spacing: 8) {
                Text("This draft changed on another device. Your edits are preserved.").foregroundColor(.orange)
                DisclosureGroup("Read the server version") { Text(conflict.body).textSelection(.enabled) }
                HStack {
                    Button("Use server version") { model.useServerDraft() }
                    Button("Save my version") { Task { await model.keepLocalDraft() } }
                }
            }.accessibilityIdentifier("email.draft-conflict")
        }
    }

    /// Connects an envelope field to the versioned edit buffer and keyboard focus.
    private func envelopeField(_ title: String, path: WritableKeyPath<EmailDraftEdit, String>, field: Field)
        -> some View
    {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(title).appFont(.callout).foregroundColor(.secondary).frame(width: 54, alignment: .leading)
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

private enum EmailSurface {
    static let accent: Color = {
        #if os(macOS)
            Color(
                nsColor: NSColor(name: nil) { appearance in
                    appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                        ? NSColor(srgbRed: 0.38, green: 0.82, blue: 0.77, alpha: 1)
                        : NSColor(srgbRed: 0, green: 112.0 / 255, blue: 109.0 / 255, alpha: 1)
                })
        #else
            Color(
                uiColor: UIColor { traits in
                    traits.userInterfaceStyle == .dark
                        ? UIColor(red: 0.38, green: 0.82, blue: 0.77, alpha: 1)
                        : UIColor(red: 0, green: 112.0 / 255, blue: 109.0 / 255, alpha: 1)
                })
        #endif
    }()

    static var canvas: Color {
        #if os(macOS)
            Color(nsColor: .windowBackgroundColor)
        #else
            Color(uiColor: .systemGroupedBackground)
        #endif
    }
    static var card: Color {
        #if os(macOS)
            Color(nsColor: .controlBackgroundColor)
        #else
            Color(uiColor: .secondarySystemGroupedBackground)
        #endif
    }
}

extension View {
    /// Hides separators for inbox notices only on platforms that support the list modifier.
    @ViewBuilder fileprivate func emailHideSeparator() -> some View {
        if #available(iOS 15, macOS 13, *) { listRowSeparator(.hidden) } else { self }
    }

    /// Gives reading sections an adaptive surface and optionally highlights the reply composer.
    fileprivate func emailCard(accent: Bool = false) -> some View {
        background(EmailSurface.card)
            .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: 16, style: .continuous)
                    .stroke(accent ? EmailSurface.accent.opacity(0.4) : Color.primary.opacity(0.07), lineWidth: 1))
    }
}

private struct EmailAvatar: View {
    let sender: String
    /// Adds a decorative sender initial without duplicating its accessible name.
    var body: some View {
        Text(String(sender.trimmingCharacters(in: .whitespacesAndNewlines).prefix(1)).uppercased())
            .appFont(.callout, weight: .semibold).foregroundColor(EmailSurface.accent)
            .frame(width: 34, height: 34).background(EmailSurface.accent.opacity(0.10))
            .clipShape(Circle()).accessibilityHidden(true)
    }
}

private struct EmailThreadStatus: View {
    let thread: EmailThreadView
    var draft: EmailDraftView? = nil

    private var title: String {
        if thread.isArchived { return "Archived" }
        if thread.isHandled { return "Handled" }
        switch (draft ?? thread.draft)?.status {
        case "ready": return "Draft ready"
        case "generating": return "Preparing draft"
        case "awaiting_approval": return "Awaiting approval"
        case "sending": return "Sending reply"
        case "sent": return "Reply sent"
        case "uncertain": return "Check send status"
        case "failed": return "Draft needs attention"
        default: return thread.draftID != nil ? "Draft" : thread.needsReply ? "Needs reply" : "For your attention"
        }
    }

    /// Summarizes server-confirmed attention and draft state in a compact badge.
    var body: some View {
        Label(
            title,
            systemImage: thread.isArchived || thread.isHandled ? "checkmark" : thread.draftID != nil
                ? "square.and.pencil" : thread.needsReply ? "arrowshape.turn.up.left" : "bookmark"
        )
        .appFont(.caption, weight: .medium)
        .foregroundColor(thread.draftID != nil ? EmailSurface.accent : .secondary)
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(thread.draftID != nil ? EmailSurface.accent.opacity(0.08) : Color.primary.opacity(0.04))
        .clipShape(Capsule())
    }
}

private struct EmailEmptyState: View {
    let symbol: String
    let title: String
    let message: String
    /// Explains empty, unavailable or unselected mail states without implying a completed scan.
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: symbol).font(.system(size: 32, weight: .light)).foregroundColor(EmailSurface.accent)
                .frame(width: 72, height: 72).background(EmailSurface.accent.opacity(0.07)).clipShape(Circle())
            Text(title).appFont(.title3, weight: .semibold).multilineTextAlignment(.center)
            Text(message).appFont(.callout).foregroundColor(.secondary).multilineTextAlignment(.center)
        }.padding(.horizontal, 20).padding(.vertical, 40).frame(maxWidth: .infinity)
    }
}

private struct EmailLearningScreen: View {
    @ObservedObject var model: EmailViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var resetScope: String?
    @State private var isUpdating = false

    /// Exposes learning consent, pause and reset controls together with historical retrieval progress.
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text("Email learning").appFont(.title2)
                Spacer()
                Button("Done") { dismiss() }
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    Text(
                        "Veetbot analyzes received and Sent email, including older correspondence, to learn what matters, your writing style, and useful memories shared with Chat and both accounts. Selected email content is processed by Veetbot's hosted models. This updates your assistant's preferences without fine-tuning a model."
                    )
                    .appFont(.caption).foregroundColor(.secondary)
                    .accessibilityIdentifier("email.learning-disclosure")
                    if let learning = model.learning {
                        Text(learning.paused ? "Learning is paused." : "Learning continues while Email is active.")
                        Text(
                            "\(learning.historyProcessed) \(learning.historyProcessed == 1 ? "thread" : "threads") retrieved · \(learning.styleExamples) \(learning.styleExamples == 1 ? "style example" : "style examples")"
                        )
                        .appFont(.caption).foregroundColor(.secondary)
                        Text(
                            learning.historyComplete
                                ? "Accessible mail retrieved; analysis continues as needed."
                                : "Historical retrieval is incomplete."
                        )
                        .appFont(.caption).foregroundColor(.secondary)
                        Button(learning.paused ? "Resume learning" : "Pause learning") {
                            isUpdating = true
                            Task {
                                await model.setLearningPaused(!learning.paused)
                                isUpdating = false
                            }
                        }.disabled(isUpdating)
                        Divider()
                        Text(
                            "You can correct a person or topic in a thread, undo feedback, or exclude that thread's sources."
                        )
                        Text("\(learning.excludedSources) \(learning.excludedSources == 1 ? "source" : "sources") excluded")
                            .appFont(.caption)
                        Button("Reset importance preferences", role: .destructive) { resetScope = "preferences" }
                        Button("Reset writing style", role: .destructive) { resetScope = "style" }
                        Button("Reset all email learning", role: .destructive) { resetScope = "all" }
                        DisclosureGroup("History by account") {
                            VStack(alignment: .leading, spacing: 16) {
                                ForEach(model.accounts) { account in
                                    VStack(alignment: .leading, spacing: 5) {
                                        Text(account.label).appFont(.headline)
                                        Text("\(account.historyProcessed) \(account.historyProcessed == 1 ? "thread" : "threads") retrieved")
                                            .appFont(.callout)
                                        Text(
                                            account.historyComplete
                                                ? "Accessible mail retrieved; analysis continues as needed."
                                                : "More history remains. Learning continues while Email is active."
                                        )
                                        .appFont(.caption).foregroundColor(.secondary)
                                    }
                                }
                            }.padding(.top, 10).frame(maxWidth: .infinity, alignment: .leading)
                        }
                    } else {
                        ProgressView("Loading learning settings…")
                    }
                    if let error = model.errorMessage {
                        Text(error).foregroundColor(.red)
                        Button("Retry") { Task { await model.loadLearning() } }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(24).frame(minWidth: 300, idealWidth: 540, minHeight: 400, idealHeight: 620)
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

private struct EmailRevisionsScreen: View {
    @ObservedObject var model: EmailViewModel
    @Environment(\.dismiss) private var dismiss
    /// Lists saved draft revisions and restores selected wording into the current edit buffer.
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Text("Draft history").appFont(.title2)
                Spacer()
                Button("Done") { dismiss() }
            }
            Text(
                "Earlier and conflicting versions remain available. Restoring a version fills your editor; Save creates a new revision."
            )
            .appFont(.caption).foregroundColor(.secondary)
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
                                .disabled(model.draft?.canEdit != true || model.conflict != nil)
                            }.frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }
                }
            }
            if let error = model.draftError { Text(error).foregroundColor(.red) }
        }.padding(24).frame(minWidth: 300, idealWidth: 600, minHeight: 400)
            .task { await model.loadRevisions() }
    }
}

private struct EmailSendReview: View {
    @ObservedObject var model: EmailViewModel
    /// Shows the frozen envelope and body before the owner explicitly approves or denies this send.
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "paperplane").foregroundColor(EmailSurface.accent)
                Text("Review your email").appFont(.title2, weight: .bold)
            }.padding(24)
            Divider()
            if let draft = model.reviewDraft {
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        let account = model.accounts.first { $0.id == draft.accountID }
                        VStack(alignment: .leading, spacing: 8) {
                            Text("From: \(account?.emailAddress ?? account?.label ?? draft.accountID)")
                            Text("To: \(draft.to.joined(separator: ", "))")
                            if !draft.cc.isEmpty { Text("Cc: \(draft.cc.joined(separator: ", "))") }
                            if !draft.bcc.isEmpty { Text("Bcc: \(draft.bcc.joined(separator: ", "))") }
                        }.appFont(.callout).foregroundColor(.secondary).textSelection(.enabled)
                        Divider()
                        Text(draft.subject).appFont(.headline)
                        Text(draft.body).appFont(.body).lineSpacing(5).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
                VStack(alignment: .leading, spacing: 14) {
                    Text("Send approves this exact message once.")
                        .appFont(.caption).foregroundColor(.secondary)
                    if let error = model.draftError { Text(error).appFont(.callout).foregroundColor(.red) }
                    HStack(spacing: 12) {
                        Button("Back to editing") { model.closeReview() }
                        Spacer(minLength: 0)
                        Button("Send") { Task { await model.resolveSend(.approveOnce) } }
                            .buttonStyle(.borderedProminent).tint(AppTheme.turquoise).controlSize(.large)
                            .disabled(model.isPerformingAction).accessibilityIdentifier("email.approve-send")
                    }
                    Button("Deny this send", role: .destructive) { Task { await model.resolveSend(.deny) } }
                        .appFont(.caption).disabled(model.isPerformingAction)
                }.padding(24)
            }
        }.frame(minWidth: 300, idealWidth: 600, minHeight: 400, idealHeight: 660)
            .background(EmailSurface.card)
    }
}
