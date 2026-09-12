import SwiftUI

public struct EmailModeView: View {
    @ObservedObject var model: EmailViewModel
    let discussInChat: () async -> Void
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

    public var body: some View {
        Group {
            if #available(iOS 16, macOS 13, *) {
                if directSelection {
                    NavigationSplitView { inbox } detail: { detail }
                } else {
                    NavigationStack(path: $navigationPath) {
                        inbox.navigationDestination(for: UUID.self) { id in
                            detail.task { if model.selectedThreadID != id { await model.openThread(id) } }
                        }
                    }
                }
            } else {
                NavigationView {
                    inbox.background {
                        if !directSelection {
                            NavigationLink(isActive: showingThread) { detail } label: { EmptyView() }.hidden()
                        }
                    }
                    detail
                }
            }
        }
        .sheet(isPresented: Binding(get: { model.review != nil }, set: { if !$0 { model.closeReview() } })) {
            EmailSendReview(model: model)
        }
        .sheet(isPresented: $showingLearning) { EmailLearningScreen(model: model) }
        .onAppear {
            if let id = model.selectedThreadID, navigationPath.last != id { navigationPath = [id] }
        }
        .onChange(of: model.selectedThreadID) { id in
            if let id, navigationPath.last != id { navigationPath = [id] }
            else if id == nil { navigationPath = [] }
        }
        .onChange(of: navigationPath) { path in
            if path.isEmpty, !directSelection, model.selectedThreadID != nil { model.clearSelection() }
        }
    }

    private var detail: some View {
        EmailThreadScreen(model: model, discussInChat: discussInChat).id(model.selectedThreadID)
    }

    /// Shows account freshness and independently actionable thread rows without opening mail to handle it.
    private var inbox: some View {
        List {
            Section {
                Picker("Accounts", selection: Binding(get: { model.selectedAccountID }, set: { model.setAccount($0) })) {
                    Text("Work + Personal").tag(String?.none)
                    ForEach(model.accounts) { account in Text(account.label).tag(Optional(account.id)) }
                }
                .accessibilityIdentifier("email.accounts")
                ForEach(model.accounts.filter { model.selectedAccountID == nil || $0.id == model.selectedAccountID }) { account in
                    VStack(alignment: .leading, spacing: 3) {
                        Text(account.label).appFont(.caption)
                        if let update = account.updateMessage {
                            Text(update).foregroundColor(account.hasRefreshFailure ? .orange : .secondary)
                        } else if let date = account.lastSyncedAt {
                            HStack { Text("Last updated"); Text(date, style: .relative) }.foregroundColor(.secondary)
                        } else {
                            Text("Not yet synchronized.").foregroundColor(.secondary)
                        }
                    }.appFont(.caption)
                }
            }

            if model.unavailable {
                Text("This server does not support Email mode yet.").foregroundColor(.secondary)
            } else if let error = model.errorMessage {
                VStack(alignment: .leading) {
                    Text(error).foregroundColor(.red)
                    Button("Retry") { Task { await model.reload(); await model.refresh() } }
                }
            }
            if model.newImportantCount > 0 {
                Button("Show \(model.newImportantCount) new important threads") { model.showNewItems() }
            }
            if model.isLoading && model.items.isEmpty { ProgressView("Loading email…") }
            else if model.items.isEmpty && !model.unavailable && model.errorMessage == nil {
                Text(model.accounts.isEmpty ? "Your connected mail accounts appear here."
                     : model.accounts.contains(where: { $0.status != "ready" })
                     ? "Waiting for mail to be updated and assessed."
                     : "No threads currently meet this view's criteria.")
                    .foregroundColor(.secondary)
            }

            Section(model.listView == "priority" ? "Important" : "Other mail") {
                ForEach(model.items) { thread in
                    HStack(spacing: 8) {
                        threadRow(thread)
                            .accessibilityIdentifier("email.thread.\(thread.id.uuidString)")
                        EmailHandledButton(model: model, thread: thread)
                            .labelStyle(.iconOnly)
                            .accessibilityIdentifier("email.handled.\(thread.id.uuidString)")
                    }
                    .buttonStyle(.plain)
                    .listRowBackground(thread.id == model.selectedThreadID ? AppTheme.turquoise.opacity(0.12) : Color.clear)
                }
                if model.hasMore {
                    Button(model.listView == "priority" ? "More important" : "More mail") { Task { await model.loadMore() } }
                        .disabled(model.isLoading)
                }
            }
            Section {
                Button(model.listView == "priority" ? "Review other mail" : "Back to important") {
                    model.setListView(model.listView == "priority" ? "other" : "priority")
                }
                DisclosureGroup("Historical learning coverage") {
                    ForEach(model.accounts) { account in
                        VStack(alignment: .leading) {
                            Text(account.label).appFont(.headline)
                            Text("\(account.historyProcessed) threads retrieved")
                            Text(account.historyComplete ? "Accessible mail retrieved; analysis continues as needed." : "More history remains. Learning continues while Email is active.")
                                .foregroundColor(.secondary)
                        }.appFont(.caption)
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("Email")
        .searchable(text: Binding(get: { model.searchText }, set: { model.setSearchText($0) }), prompt: "Find mail")
        .toolbar {
            ToolbarItem {
                Button { showingLearning = true } label: { Image(systemName: "slider.horizontal.3") }
                    .accessibilityLabel("Email learning")
                    .accessibilityIdentifier("email.learning")
            }
            ToolbarItem {
                Button { Task { await model.refresh() } } label: {
                    if model.isRefreshing { ProgressView() } else { Image(systemName: "arrow.clockwise") }
                }
                .disabled(model.isRefreshing || model.unavailable)
                .accessibilityLabel("Refresh email")
                .accessibilityIdentifier("email.refresh")
            }
        }
        .accessibilityIdentifier("email.inbox")
    }

    private func accountLabel(_ id: String) -> String { model.accounts.first { $0.id == id }?.label ?? id }

    /// Preserves platform navigation while keeping thread opening separate from the adjacent checkbox.
    @ViewBuilder private func threadRow(_ thread: EmailThreadView) -> some View {
        if directSelection {
            Button { Task { await model.openThread(thread.id) } } label: { threadLabel(thread) }
        } else if #available(iOS 16, macOS 13, *) {
            NavigationLink(value: thread.id) { threadLabel(thread) }
        } else {
            NavigationLink { detail.task { await model.openThread(thread.id) } } label: { threadLabel(thread) }
        }
    }

    /// Gives confirmed handled state precedence over draft and reply-needed labels in the inbox.
    private func threadLabel(_ thread: EmailThreadView) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text(accountLabel(thread.accountID)).appFont(.caption).foregroundColor(AppTheme.turquoise)
                Spacer()
                Text(thread.isHandled ? "Handled" : thread.draftID != nil ? "Draft" : thread.needsReply ? "Needs reply" : "For your attention")
                    .appFont(.caption).foregroundColor(.secondary)
            }
            Text(thread.senders.joined(separator: ", ")).appFont(.headline).lineLimit(1)
            Text(thread.subject).appFont(.subheadline).lineLimit(2)
            Text(thread.summary).appFont(.caption).foregroundColor(.secondary).lineLimit(3)
            if !thread.complete { Text("Partial thread").appFont(.caption).foregroundColor(.orange) }
        }.padding(.vertical, 6).frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
    }
}

private struct EmailHandledButton: View {
    @ObservedObject var model: EmailViewModel
    let thread: EmailThreadView

    /// Toggles server-confirmed attention state with a full hit target and a reversible accessibility label.
    var body: some View {
        Button {
            Task { await model.setThreadHandled(thread, handled: !thread.isHandled) }
        } label: {
            Label(thread.isHandled ? "Handled" : "Mark handled",
                  systemImage: thread.isHandled ? "checkmark.square.fill" : "square")
                .frame(minWidth: 44, minHeight: 44)
                .contentShape(Rectangle())
        }
        .foregroundColor(AppTheme.turquoise)
        .disabled(model.isPerformingAction || model.unavailable)
        .accessibilityLabel(thread.isHandled ? "Mark unhandled" : "Mark handled")
        .accessibilityValue(thread.isHandled ? "Handled" : "Not handled")
        .help(thread.isHandled ? "Return this thread to attention" : "I've dealt with this thread")
    }
}

private struct EmailThreadScreen: View {
    private enum Field: Hashable { case to, cc, bcc, subject, body, topic, feedback, refinement }
    @ObservedObject var model: EmailViewModel
    let discussInChat: () async -> Void
    @State private var target: EmailFeedbackTarget = .thread
    @State private var explanation = ""
    @State private var targetValue = ""
    @State private var refinement = ""
    @State private var showingDiscard = false
    @State private var showingExclusion = false
    @State private var showingRevisions = false
    @FocusState private var focusedField: Field?

    /// Keeps attention controls beside the source conversation, errors and preserved draft editor.
    var body: some View {
        Group {
            if model.isLoadingThread { ProgressView("Opening thread…") }
            else if let thread = model.thread {
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        Text(thread.subject).appFont(.title2)
                        Text(accountDescription(thread.accountID)).appFont(.caption).foregroundColor(.secondary)
                        EmailHandledButton(model: model, thread: thread)
                            .accessibilityIdentifier("email.handled.detail")
                        Text(thread.summary)
                        DisclosureGroup("Why this matters") { Text(thread.reason).frame(maxWidth: .infinity, alignment: .leading) }
                        feedback
                        if let error = model.draftError { Text(error).foregroundColor(.red).accessibilityIdentifier("email.action-error") }
                        ForEach(thread.messages ?? []) { message in
                            VStack(alignment: .leading, spacing: 8) {
                                HStack { Text(message.sender).appFont(.headline); Spacer(); Text(message.sentAt, style: .date).appFont(.caption) }
                                Text("To: \(message.to.joined(separator: ", "))").appFont(.caption).foregroundColor(.secondary)
                                // A Text view never loads remote HTML, images or tracking pixels.
                                Text(message.body).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                                if !message.complete { Text("This message is incomplete. Refresh before relying on its full contents.").foregroundColor(.orange) }
                                ForEach(Array((message.attachments ?? []).enumerated()), id: \.offset) { _, attachment in
                                    Label("\(attachment.filename) — attachment not read", systemImage: "paperclip").appFont(.caption)
                                }
                            }
                            .padding()
                            .background(AppTheme.turquoise.opacity(0.05)).cornerRadius(10)
                        }
                        draftEditor
                        Button("Discuss in Chat") { Task { await discussInChat() } }
                            .accessibilityIdentifier("email.discuss")
                        Button("Exclude this thread from learning", role: .destructive) { showingExclusion = true }
                    }
                    .padding().frame(maxWidth: 900, alignment: .leading).frame(maxWidth: .infinity)
                }
            } else {
                VStack(spacing: 12) {
                    Image(systemName: "envelope").font(.largeTitle).foregroundColor(AppTheme.turquoise)
                    Text("Select an email").appFont(.title2)
                    if let error = model.draftError { Text(error).foregroundColor(.red) }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .accessibilityIdentifier("email.detail")
        .confirmationDialog("Discard this Veetbot draft?", isPresented: $showingDiscard, titleVisibility: .visible) {
            Button("Discard draft", role: .destructive) { Task { await model.discardDraft() } }
            Button("Cancel", role: .cancel) {}
        } message: { Text("This removes the proposal from Veetbot. It does not change Gmail.") }
        .confirmationDialog("Exclude this email thread?", isPresented: $showingExclusion, titleVisibility: .visible) {
            Button("Exclude thread", role: .destructive) { Task { await model.excludeSelectedThread() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Retained email source copies and learning from ‘\(model.thread?.subject ?? "this thread")’ will be removed. Gmail mail and unrelated Chat history remain.")
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

    private var feedback: some View {
        VStack(alignment: .leading, spacing: 10) {
            Picker("Feedback applies to", selection: $target) {
                ForEach(EmailFeedbackTarget.allCases, id: \.self) { value in Text(value.title).tag(value) }
            }.pickerStyle(.menu)
            if target == .person {
                Picker("Person", selection: $targetValue) {
                    Text("Choose a person").tag("")
                    ForEach(model.thread?.senders ?? [], id: \.self) { person in Text(person).tag(person) }
                }
            } else if target == .topic {
                TextField("Content topic", text: $targetValue).focused($focusedField, equals: .topic)
            }
            TextField("Explain what matters (optional)", text: $explanation).focused($focusedField, equals: .feedback)
            HStack {
                Button("Important") { Task { await model.giveFeedback(target: target, judgment: "important", explanation: explanation, targetValue: targetValue) } }
                Button("Less important") { Task { await model.giveFeedback(target: target, judgment: "less_important", explanation: explanation, targetValue: targetValue) } }
            }
            HStack {
                Button("Needs reply") { Task { await model.giveFeedback(target: .thread, judgment: "needs_reply") } }
                Button("No reply needed") { Task { await model.giveFeedback(target: .thread, judgment: "no_reply_needed") } }
            }
            if let message = model.feedbackMessage {
                HStack {
                    Text(message).appFont(.caption)
                    if model.feedbackID != nil { Button("Undo") { Task { await model.undoFeedback() } } }
                }
            }
        }.disabled(model.isPerformingAction)
    }

    @ViewBuilder private var draftEditor: some View {
        if let draft = model.draft, let edit = model.currentEdit {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text("Your draft").appFont(.headline)
                    Spacer()
                    Text(model.isSaving ? "Saving…" : edit.isDirty ? "Unsaved changes" : "Saved").appFont(.caption).foregroundColor(.secondary)
                }
                Text("From: \(accountDescription(draft.accountID))").appFont(.caption)
                if draft.stale {
                    Text("New mail arrived. Review the updated thread before sending.").foregroundColor(.orange)
                    Button("I've reviewed the updated thread") { Task { await model.confirmCurrentSourceReviewed() } }
                        .disabled(model.conflict != nil || model.isSaving)
                }
                if draft.status == "uncertain" {
                    Text("Send status unknown. Check the thread before taking another action. This message will not be resent automatically.").foregroundColor(.orange)
                } else if draft.status == "sent" { Label("Sent", systemImage: "checkmark.circle").foregroundColor(.green) }
                else if draft.isProcessing { ProgressView(draft.status == "sending" ? "Sending approved message…" : "Preparing your draft…") }
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
                Group {
                    TextField("To — comma-separated addresses", text: editBinding(\.to)).focused($focusedField, equals: .to)
                    TextField("Cc — comma-separated addresses", text: editBinding(\.cc)).focused($focusedField, equals: .cc)
                    TextField("Bcc — comma-separated addresses", text: editBinding(\.bcc)).focused($focusedField, equals: .bcc)
                    TextField("Subject", text: editBinding(\.subject)).focused($focusedField, equals: .subject)
                    TextEditor(text: editBinding(\.body)).frame(minHeight: 180)
                        .focused($focusedField, equals: .body)
                        .overlay(RoundedRectangle(cornerRadius: 6).stroke(AppTheme.turquoise.opacity(0.35)))
                        .accessibilityIdentifier("email.draft-body")
                }.disabled(!draft.canEdit || model.isPerformingAction)
                HStack {
                    Button("Save") { Task { await model.saveDraft() } }.disabled(!edit.isDirty || model.isSaving || model.conflict != nil)
                    Button("Review & Send") { Task { await model.prepareSend() } }
                        .disabled(!model.canReview).accessibilityIdentifier("email.review-send")
                    if draft.approvalID != nil && !edit.isDirty {
                        Button("Open approval") { Task { await model.loadReview() } }.disabled(draft.stale)
                    }
                    Spacer()
                    Button("Discard", role: .destructive) { showingDiscard = true }.disabled(!draft.canEdit)
                }
                Button("Draft history") { showingRevisions = true }.accessibilityIdentifier("email.draft-history")
                Menu("Writing style") {
                    Button("Use as writing example") { Task { await model.endorseDraftStyle() } }
                        .accessibilityIdentifier("email.endorse-style")
                }.disabled(model.isPerformingAction || model.isSaving || model.conflict != nil || edit.body.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                if let message = model.styleExampleMessage { Text(message).appFont(.caption).foregroundColor(.secondary) }
                TextField("How should the reply change?", text: $refinement).focused($focusedField, equals: .refinement)
                Button("Refine draft") { Task { await model.generateDraft(instruction: refinement) } }
                    .disabled(refinement.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.isPerformingAction || model.conflict != nil)
            }
        } else {
            Button { Task { await model.generateDraft() } } label: {
                if model.isPerformingAction { ProgressView("Preparing draft…") } else { Text("Draft reply") }
            }.disabled(model.isPerformingAction)
        }
    }

    private func editBinding(_ path: WritableKeyPath<EmailDraftEdit, String>) -> Binding<String> {
        Binding(get: { model.currentEdit?[keyPath: path] ?? "" }, set: { model.changeEdit(path, to: $0) })
    }
    private func accountDescription(_ id: String) -> String {
        guard let account = model.accounts.first(where: { $0.id == id }) else { return id }
        return account.emailAddress.map { "\(account.label) · \($0)" } ?? account.label
    }
}

private struct EmailLearningScreen: View {
    @ObservedObject var model: EmailViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var resetScope: String?
    @State private var isUpdating = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack {
                Text("Email learning").appFont(.title2)
                Spacer()
                Button("Done") { dismiss() }
            }
            Text("Veetbot analyzes received and Sent email, including older correspondence, to learn what matters, your writing style, and useful memories shared with Chat and both accounts. Selected email content is processed by Veetbot's hosted models. This updates your assistant's preferences without fine-tuning a model.")
                .appFont(.caption).foregroundColor(.secondary)
                .accessibilityIdentifier("email.learning-disclosure")
            if let learning = model.learning {
                Text(learning.paused ? "Learning is paused." : "Learning continues while Email is active.")
                Text("\(learning.historyProcessed) threads retrieved · \(learning.styleExamples) style examples")
                    .appFont(.caption).foregroundColor(.secondary)
                Text(learning.historyComplete ? "Accessible mail retrieved; analysis continues as needed." : "Historical retrieval is incomplete.")
                    .appFont(.caption).foregroundColor(.secondary)
                Button(learning.paused ? "Resume learning" : "Pause learning") {
                    isUpdating = true
                    Task { await model.setLearningPaused(!learning.paused); isUpdating = false }
                }.disabled(isUpdating)
                Divider()
                Text("You can correct a person or topic in a thread, undo feedback, or exclude that thread's sources.")
                Text("\(learning.excludedSources) sources excluded").appFont(.caption)
                Button("Reset importance preferences", role: .destructive) { resetScope = "preferences" }
                Button("Reset writing style", role: .destructive) { resetScope = "style" }
                Button("Reset all email learning", role: .destructive) { resetScope = "all" }
            } else { ProgressView("Loading learning settings…") }
            if let error = model.errorMessage {
                Text(error).foregroundColor(.red)
                Button("Retry") { Task { await model.loadLearning() } }
            }
        }
        .padding(24).frame(minWidth: 300, idealWidth: 540)
        .task { await model.loadLearning() }
        .confirmationDialog("Reset email learning?", isPresented: Binding(get: { resetScope != nil }, set: { if !$0 { resetScope = nil } }), titleVisibility: .visible) {
            Button("Reset", role: .destructive) {
                guard let scope = resetScope else { return }
                resetScope = nil
                Task { await model.resetLearning(scope: scope) }
            }
            Button("Cancel", role: .cancel) { resetScope = nil }
        } message: {
            Text(resetScope == "style" ? "Removes the learned email writing style. Source mail, your draft edits and Chat history remain." : resetScope == "preferences" ? "Removes learned sender and content preferences. Source mail, writing style, your draft edits and Chat history remain." : "Removes learned email preferences and writing style. Source mail, your draft edits and Chat history remain.")
        }
    }
}

private struct EmailRevisionsScreen: View {
    @ObservedObject var model: EmailViewModel
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("Draft history").appFont(.title2); Spacer(); Button("Done") { dismiss() } }
            Text("Earlier and conflicting versions remain available. Restoring a version fills your editor; Save creates a new revision.")
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
                                Button("Restore in editor") { model.useRevision(revision); dismiss() }
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
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Review this exact email").appFont(.title2)
            if let draft = model.reviewDraft {
                let account = model.accounts.first { $0.id == draft.accountID }
                Text("From: \(account?.emailAddress ?? account?.label ?? draft.accountID)")
                Text("To: \(draft.to.joined(separator: ", "))")
                if !draft.cc.isEmpty { Text("Cc: \(draft.cc.joined(separator: ", "))") }
                if !draft.bcc.isEmpty { Text("Bcc: \(draft.bcc.joined(separator: ", "))") }
                Text("Subject: \(draft.subject)").appFont(.headline)
                ScrollView { Text(draft.body).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                Text("Send approves this message once. Editing or new source mail requires a new review.")
                    .appFont(.caption).foregroundColor(.secondary)
                if let error = model.draftError { Text(error).foregroundColor(.red) }
                HStack {
                    Button("Back to editing") { model.closeReview() }
                    Button("Deny", role: .destructive) { Task { await model.resolveSend(.deny) } }
                    Spacer()
                    Button("Send") { Task { await model.resolveSend(.approveOnce) } }
                        .disabled(model.isPerformingAction).accessibilityIdentifier("email.approve-send")
                }
            }
        }.padding(24).frame(minWidth: 300, idealWidth: 600, minHeight: 400)
    }
}
