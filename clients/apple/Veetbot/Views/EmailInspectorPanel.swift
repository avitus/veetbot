import SwiftUI

#if os(macOS)
/// Email's right panel (MagicPath clever-village-5022, cozy-evening-6871):
/// Context holds everything that describes the thread, so the thread keeps
/// the mail itself: a spam flag, Veetbot's summary, why the thread is in the
/// priority list, who wrote with that message's envelope and Unsubscribe,
/// Teach Veetbot, the attachments, the draft, and the account with Show
/// original. Activity holds the calls of the run that made the draft.
struct EmailInspectorPanel: View {
    @ObservedObject var model: EmailViewModel
    @ObservedObject var inspector: InspectorModel
    let runFrames: ((UUID) -> AsyncThrowingStream<SSEFrame, Error>)?
    @StateObject private var replay = RunActivityReplay()
    @FocusState private var feedbackFocused: Bool
    @State private var attachmentsFlash = false
    @State private var senderFlash = false
    @State private var flashTask: Task<Void, Never>?

    var body: some View {
        VStack(spacing: 0) {
            InspectorHeader(
                selection: $inspector.emailTab,
                tabs: EmailInspectorTab.allCases.map { ($0, $0.title) },
                hide: { inspector.toggle(.email) }
            )
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: VeetSpace.s4) {
                        if let thread = model.thread, !model.isLoadingThread {
                            switch inspector.emailTab {
                            case .context: context(thread)
                            case .activity: activity
                            }
                        } else {
                            InspectorEmptyState(
                                symbol: "envelope.open", title: "No thread open",
                                message: "Choose a thread to see who wrote it, why it is here and its draft."
                            )
                        }
                    }
                    .padding(.horizontal, VeetSpace.s3)
                    .padding(.top, VeetSpace.s1)
                    .padding(.bottom, VeetSpace.s6)
                }
                // A name or an attachment line in the thread brings its card into view.
                .onChange(of: inspector.pendingEmailReveal, initial: true) {
                    consumeReveal(with: proxy)
                }
                .onChange(of: model.isLoadingThread) { consumeReveal(with: proxy) }
                .onDisappear { flashTask?.cancel() }

            }
        }
        .task(id: draftRunID) {
            guard let draftRunID, let runFrames else { return }
            await replay.load(runID: draftRunID, frames: runFrames)
        }
    }

    private var draftRunID: UUID? { model.draft?.runID ?? model.thread?.draft?.runID }

    private enum Section: Hashable { case sender, attachments }

    private func reveal(_ section: Section, with proxy: ScrollViewProxy) {
        guard inspector.emailTab == .context else { return }
        Task { @MainActor in
            // The tab may have only just switched to Context; scroll once it has laid out.
            await Task.yield()
            withAnimation(.easeInOut(duration: 0.2)) { proxy.scrollTo(section, anchor: .top) }
        }
    }

    /// Handles a queued request on mount as well as requests to an already-open panel.
    private func consumeReveal(with proxy: ScrollViewProxy) {
        guard !model.isLoadingThread, model.thread != nil,
              let request = inspector.consumeEmailReveal() else { return }
        flashTask?.cancel()
        senderFlash = request == .sender
        attachmentsFlash = request == .attachments
        reveal(request == .sender ? .sender : .attachments, with: proxy)
        flashTask = Task { @MainActor in
            do { try await Task.sleep(nanoseconds: 1_200_000_000) }
            catch { return }
            withAnimation(.easeOut(duration: 0.4)) {
                senderFlash = false
                attachmentsFlash = false
            }
        }
    }

    // MARK: - Context

    @ViewBuilder
    private func context(_ thread: EmailThreadView) -> some View {
        // A flag waits on the owner's answer, so it leads.
        EmailSpamControls(model: model, thread: thread)
        let summary = EmailThreadSummary.make(thread: thread, draft: model.draft ?? thread.draft)
        InspectorSection(title: "Veetbot’s summary") {
            summaryCard(summary)
        }
        InspectorSection(title: "Why it’s here") {
            priority(thread, reason: summary.reason)
        }
        if let lookup = senderLookup(thread) {
            InspectorSection(title: "Sender") {
                EmailSenderCard(lookup: lookup, highlighted: senderFlash)
                    .id(lookup.text)
                    .accessibilityElement(children: .contain)
                    .accessibilityIdentifier("inspector.email.sender")
                if let message = chosenMessage(thread) {
                    messageCard(message, in: thread)
                }
            }
            .id(Section.sender)
        }
        // The card's own header names it, so it needs no section label.
        EmailTeachCard(model: model, explanationFocused: $feedbackFocused, inPanel: true)
            .id(thread.id)
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("inspector.email.teach")
        let attachments = (thread.messages ?? []).flatMap { $0.attachments ?? [] }
        InspectorSection(title: "Attachments", trailing: attachments.isEmpty ? nil : "\(attachments.count)") {
            VStack(alignment: .leading, spacing: VeetSpace.s1) {
                if attachments.isEmpty {
                    Text("No attachments.").veetType(.meta).foregroundColor(AppTheme.inkFaint)
                }
                ForEach(Array(attachments.enumerated()), id: \.offset) { _, attachment in
                    attachmentRow(attachment)
                }
                if !attachments.isEmpty {
                    Text("Veetbot does not open attachments; they stay in Gmail.")
                        .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(VeetSpace.s1)
            .background(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .stroke(attachmentsFlash ? AppTheme.teal : .clear)
            )
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("inspector.email.attachments")
        }
        .id(Section.attachments)
        InspectorSection(title: "Draft") {
            draftCard(thread)
        }
        InspectorSection(title: "Thread") {
            threadCard(thread)
        }
    }

    /// The sender the owner chose in the thread, or else the latest one.
    private func senderLookup(_ thread: EmailThreadView) -> PeopleLookup? {
        if let chosen = inspector.sender { return chosen }
        if let latest = thread.messages?.last { return .correspondent(latest.sender, at: latest.sentAt) }
        return thread.senders.first.map { .correspondent($0, at: thread.updatedAt) }
    }

    /// The message whose sender the owner chose, or else the latest one.
    private func chosenMessage(_ thread: EmailThreadView) -> EmailMessageView? {
        let messages = thread.messages ?? []
        return messages.first { $0.id == inspector.senderMessageID } ?? messages.last
    }

    /// Veetbot's gist and the state the row's chip names. A thread Veetbot has
    /// not read yet says so once.
    private func summaryCard(_ summary: EmailThreadSummary) -> some View {
        VStack(alignment: .leading, spacing: VeetSpace.s1) {
            if summary.isPending {
                HStack(spacing: VeetSpace.s2) {
                    ProgressView().controlSize(.mini)
                    Text(EmailThreadSummary.pendingText).veetType(.ui).foregroundColor(AppTheme.inkSoft)
                }
            } else {
                Text(summary.gist).veetType(.ui).foregroundColor(AppTheme.ink)
                    .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
            }
            Text(summary.state).veetType(.meta, weight: .medium).foregroundColor(color(summary.tone))
            if summary.isPending {
                Text("Veetbot has not read this thread yet. Its summary and reason appear here once it has.")
                    .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .inspectorCard()
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("inspector.email.summary")
    }

    /// The chosen message's envelope, which left the thread, and Unsubscribe
    /// for a bulk sender on an account that advertises it.
    private func messageCard(_ message: EmailMessageView, in thread: EmailThreadView) -> some View {
        let messages = thread.messages ?? []
        return VStack(alignment: .leading, spacing: VeetSpace.s2) {
            VStack(alignment: .leading, spacing: VeetSpace.s1) {
                fact("From", message.sender)
                fact("To", message.to.joined(separator: ", "))
                if !message.cc.isEmpty { fact("Cc", message.cc.joined(separator: ", ")) }
                fact("Sent", message.sentAt.formatted(date: .abbreviated, time: .shortened))
            }
            .textSelection(.enabled)
            if messages.count > 1, let index = messages.firstIndex(where: { $0.id == message.id }) {
                Text("Message \(index + 1) of \(messages.count). Choose a sender’s name in the thread to switch.")
                    .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Bulk mail only, on an account that advertises the action, with
            // verified evidence. Everything else offers nothing here.
            if let block = thread.subscription, block.canUnsubscribe,
               model.unsubscribeSupported(for: thread.accountID) {
                Button("Unsubscribe") { model.subscriptions.beginUnsubscribe(thread: thread) }
                    .buttonStyle(SecondaryCapsuleButtonStyle())
                    .accessibilityLabel("Unsubscribe from \(thread.senders.first ?? block.destination)")
                    .accessibilityIdentifier("email.unsubscribe.thread")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .inspectorCard()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("inspector.email.message")
    }

    /// Which account the thread is in, its Gmail state, and the switch that
    /// shows every message's original body.
    private func threadCard(_ thread: EmailThreadView) -> some View {
        let messages = thread.messages ?? []
        return VStack(alignment: .leading, spacing: VeetSpace.s2) {
            VStack(alignment: .leading, spacing: VeetSpace.s1) {
                fact("Account", model.accountDescription(for: thread.accountID))
                fact("Gmail", thread.isInSpam ? "Spam" : thread.isArchived ? "Archived" : thread.inInbox == true ? "In Inbox" : "Not known yet")
                fact("Messages", "\(messages.count)")
            }
            if let reason = model.archiveUnavailableReason(for: thread) {
                Text(reason).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // Only a cleaned message has an original to show.
            if messages.contains(where: { !($0.readerBody ?? "").isEmpty }) {
                HairlineDivider()
                HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Show original").veetType(.ui).foregroundColor(AppTheme.ink)
                        Text("The unchanged messages, without the reading view’s cleanup.")
                            .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer(minLength: 0)
                    // The switch alone is the control, so a click on it is a click on the switch.
                    Toggle("Show original", isOn: $inspector.showsOriginal)
                        .toggleStyle(.switch).controlSize(.small).labelsHidden()
                        .accessibilityIdentifier("email.reader.original")
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .inspectorCard()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("inspector.email.thread")
    }

    private func fact(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
            Text(label).foregroundColor(AppTheme.inkFaint).frame(width: 72, alignment: .leading)
            Text(value).foregroundColor(AppTheme.ink).fixedSize(horizontal: false, vertical: true)
        }
        .veetType(.meta)
    }

    private func priority(_ thread: EmailThreadView, reason: String?) -> some View {
        let level = EmailPriority(thread.priority)
        return VStack(alignment: .leading, spacing: VeetSpace.s2) {
            HStack(alignment: .firstTextBaseline) {
                Text(level.title)
                    .veetType(.ui, weight: .semibold)
                    .foregroundColor(level.isHigh ? AppTheme.amberText : AppTheme.ink)
                Spacer()
                Text("\(level.percent) / 100").veetType(.meta).foregroundColor(AppTheme.inkFaint).monospacedDigit()
            }
            GeometryReader { track in
                Capsule().fill(AppTheme.field)
                    .overlay(alignment: .leading) {
                        Capsule().fill(level.isHigh ? AppTheme.amber : AppTheme.teal)
                            .frame(width: track.size.width * CGFloat(level.percent) / 100)
                    }
            }
            .frame(height: 6)
            .accessibilityHidden(true)
            if let reason {
                Text(reason).veetType(.ui).foregroundColor(AppTheme.ink).fixedSize(horizontal: false, vertical: true)
            }
            let chips = (thread.needsReply ? ["Needs a reply"] : []) + thread.feedbackTopics
            if !chips.isEmpty {
                HStack(spacing: VeetSpace.s1) {
                    ForEach(chips, id: \.self) { chip in
                        Text(chip)
                            .veetType(.meta, weight: chip == "Needs a reply" ? .medium : nil)
                            .foregroundColor(chip == "Needs a reply" ? AppTheme.amberText : AppTheme.inkSoft)
                            .padding(.horizontal, VeetSpace.s2)
                            .padding(.vertical, 2)
                            .background(Capsule().fill(chip == "Needs a reply" ? AppTheme.amberWash : AppTheme.field))
                            .lineLimit(1)
                    }
                }
            }
        }
        .inspectorCard()
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(level.title), \(level.percent) out of 100. \(reason ?? "")")
        .accessibilityIdentifier("inspector.email.priority")
    }

    private func attachmentRow(_ attachment: EmailAttachmentView) -> some View {
        HStack(spacing: VeetSpace.s2) {
            Image(systemName: attachment.mimeType.hasPrefix("image/") ? "photo" : "paperclip")
                .veetIcon(.small)
                .foregroundColor(AppTheme.inkSoft)
                .frame(width: 28, height: 28)
                .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(AppTheme.surface))
                .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous).stroke(AppTheme.hairline))
            VStack(alignment: .leading, spacing: 0) {
                Text(attachment.filename).veetType(.ui).foregroundColor(AppTheme.ink).lineLimit(1).truncationMode(.middle)
                Text(attachmentDetail(attachment)).veetType(.meta).foregroundColor(AppTheme.inkFaint).lineLimit(1)
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func attachmentDetail(_ attachment: EmailAttachmentView) -> String {
        guard let size = attachment.size else { return attachment.mimeType }
        return "\(attachment.mimeType) · \(ByteCountFormatter.string(fromByteCount: Int64(size), countStyle: .file))"
    }

    private func draftCard(_ thread: EmailThreadView) -> some View {
        let draft = model.draft ?? thread.draft
        let summary = EmailDraftSummary.make(draft: draft, needsReply: thread.needsReply)
        return VStack(alignment: .leading, spacing: VeetSpace.s1) {
            Text(summary.title).veetType(.ui, weight: .semibold).foregroundColor(color(summary.tone))
            if let draft {
                Text("Revision \(draft.revision) · to \(draft.to.joined(separator: ", ")) · \(draft.updatedAt.formatted(date: .omitted, time: .shortened))")
                    .veetType(.meta).foregroundColor(AppTheme.inkFaint).lineLimit(2)
                if !draft.body.isEmpty {
                    Text(draft.body).veetType(.ui).foregroundColor(AppTheme.inkSoft).lineLimit(4)
                        .padding(.top, VeetSpace.s1)
                }
                if draft.runID != nil {
                    Button("How it was made") { inspector.emailTab = .activity }
                        .buttonStyle(QuietCapsuleButtonStyle())
                        .padding(.top, VeetSpace.s1)
                }
            } else {
                if let detail = summary.detail {
                    Text(detail).veetType(.meta).foregroundColor(AppTheme.inkFaint).fixedSize(horizontal: false, vertical: true)
                }
                Button {
                    Task { await model.generateDraft() }
                } label: {
                    Label("Draft a reply", systemImage: "square.and.pencil")
                }
                .buttonStyle(SecondaryCapsuleButtonStyle())
                .disabled(model.isPerformingAction)
                .padding(.top, VeetSpace.s1)
            }
        }
        .inspectorCard(border: summary.tone == .attention ? AppTheme.amberLine : AppTheme.hairline)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("inspector.email.draft")
    }

    private func color(_ tone: EmailDraftSummary.Tone) -> Color {
        switch tone {
        case .attention: AppTheme.amberText
        case .draft: AppTheme.tealText
        case .settled: AppTheme.inkSoft
        }
    }

    private func color(_ tone: EmailThreadSummary.Tone) -> Color {
        switch tone {
        case .attention: AppTheme.amberText
        case .draft: AppTheme.tealText
        case .settled: AppTheme.inkSoft
        }
    }

    // MARK: - Activity

    @ViewBuilder
    private var activity: some View {
        if draftRunID == nil {
            InspectorEmptyState(
                symbol: "wrench.and.screwdriver", title: "Nothing has run on this thread",
                message: "When Veetbot drafts a reply, the calls it made appear here."
            )
        } else {
            DraftRunActivity(replay: replay)
        }
    }
}

/// The draft run's status and its calls, numbered as Chat's ledger numbers them.
private struct DraftRunActivity: View {
    @ObservedObject var replay: RunActivityReplay
    @ObservedObject private var state: RunStateReducer

    init(replay: RunActivityReplay) {
        self.replay = replay
        self.state = replay.state
    }

    var body: some View {
        let groups = ToolLedgerGroups.make(from: state.activityTimeline)
        let calls = groups.reduce(0) { $0 + $1.activities.count }
        InspectorSection(title: "Draft run") {
            InspectorRunCard(
                summary: replay.isLoading && state.runStatus == nil
                    ? InspectorRunSummary(title: "Loading…", detail: nil, tone: .idle)
                    : InspectorRunSummary.chat(runStatus: state.runStatus, awaitingApproval: false, activityLabel: nil),
                facts: [("Tool calls", "\(calls)")]
            )
            .accessibilityIdentifier("inspector.email.run")
            if let error = replay.errorMessage {
                Label(error, systemImage: "exclamationmark.circle")
                    .veetType(.meta).foregroundColor(AppTheme.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        InspectorSection(title: "Tool calls", trailing: calls > 0 ? "\(calls)" : nil) {
            if groups.isEmpty, !replay.isLoading {
                Text("This run made no tool calls.").veetType(.meta).foregroundColor(AppTheme.inkFaint)
            }
            ForEach(groups) { group in
                VStack(alignment: .leading, spacing: VeetSpace.s1) {
                    ForEach(Array(group.activities.enumerated()), id: \.element.id) { offset, activity in
                        ToolLedgerRow(index: group.firstIndex + offset, activity: activity, approval: nil, openArtifact: { _ in })
                    }
                }
            }
        }
    }
}

/// The sender's People card, read the way People's own lookup reads a
/// correspondent: a search hint that never confirms or merges an identity.
struct EmailSenderCard: View {
    let lookup: PeopleLookup
    /// Changes when the owner chose this sender in the thread.
    var highlighted = false
    @StateObject private var people: PeopleViewModel
    @State private var showingPeople = false

    init(lookup: PeopleLookup, highlighted: Bool = false) {
        self.lookup = lookup
        self.highlighted = highlighted
        _people = StateObject(wrappedValue: PeopleViewModel(searchText: lookup.text, asOf: lookup.asOf))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            if people.isLoading && people.items.isEmpty {
                ProgressView().controlSize(.small)
            } else if let person = people.items.first {
                found(person)
            } else {
                missing
            }
        }
        .inspectorCard(border: highlighted ? AppTheme.teal : AppTheme.hairline)
        .task { await people.reload() }
        .sheet(isPresented: $showingPeople) { PeopleLookupSheet(lookup: lookup) }
    }

    private func found(_ person: PersonView) -> some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            HStack(spacing: VeetSpace.s3) {
                Text(initials(person.displayName))
                    .veetType(.ui, weight: .semibold)
                    .foregroundColor(AppTheme.tealText)
                    .frame(width: 36, height: 36)
                    .background(Circle().fill(AppTheme.tealWash))
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 0) {
                    Text(person.displayName).veetType(.ui, weight: .semibold).foregroundColor(AppTheme.ink).lineLimit(1)
                    Text(lookup.text).veetType(.meta).foregroundColor(AppTheme.inkSoft).lineLimit(1).truncationMode(.middle)
                }
            }
            if let last = person.lastInteractionAt {
                fact("Last contact", last.formatted(date: .abbreviated, time: .omitted))
            }
            if let owed = person.nextThread {
                fact("You owe", [owed.description, owed.dueLabel].compactMap { $0 }.joined(separator: " · "))
            }
            if people.items.count > 1 {
                Text("\(people.items.count - 1) other people match this sender.")
                    .veetType(.meta).foregroundColor(AppTheme.inkFaint)
            }
            Button {
                showingPeople = true
            } label: {
                Label("Open in People", systemImage: "person.2")
            }
            .buttonStyle(SecondaryCapsuleButtonStyle())
            .accessibilityIdentifier("inspector.email.sender.open")
        }
    }

    private var missing: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            Text(lookup.text).veetType(.ui, weight: .semibold).foregroundColor(AppTheme.ink).lineLimit(1).truncationMode(.middle)
            Text(people.errorMessage ?? "Not in People yet.")
                .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                .fixedSize(horizontal: false, vertical: true)
            Button {
                showingPeople = true
            } label: {
                Label("Find in People", systemImage: "person.2")
            }
            .buttonStyle(SecondaryCapsuleButtonStyle())
            .accessibilityIdentifier("inspector.email.sender.open")
        }
    }

    private func fact(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
            Text(label).foregroundColor(AppTheme.inkFaint).frame(width: 84, alignment: .leading)
            Text(value).foregroundColor(AppTheme.ink).fixedSize(horizontal: false, vertical: true)
        }
        .veetType(.meta)
    }

    private func initials(_ name: String) -> String {
        name.split(separator: " ").prefix(2).compactMap(\.first).map(String.init).joined().uppercased()
    }
}
#endif
