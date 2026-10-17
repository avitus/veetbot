import SwiftUI
import UniformTypeIdentifiers

#if os(iOS)
import UIKit
#endif

enum ConversationScrollTarget: Equatable {
    case bottom
    case notification(NotificationFocus)

    static func resolve(_ focus: NotificationFocus?) -> ConversationScrollTarget {
        focus.map(Self.notification) ?? .bottom
    }

    var scrollID: String {
        switch self {
        case .bottom: return "conversation-bottom"
        case .notification(let focus): return focus.scrollID
        }
    }
}

private struct ChatListOverlayPresentedKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    /// Whether the compact chat list is sliding over the conversation, which
    /// covers it for notification attention as a sheet would.
    var chatListOverlayPresented: Bool {
        get { self[ChatListOverlayPresentedKey.self] }
        set { self[ChatListOverlayPresentedKey.self] = newValue }
    }
}

/// The conversation in the A2 Dock design: answers on bare paper behind a
/// teal mark, the owner's messages in teal-wash capsules, each tool run one
/// sentence that opens into a ledger, and whatever needs the owner docked
/// above a translucent composer, with one amber line in the transcript saying
/// so.
public struct ChatView: View {
    @Environment(\.activeClientMode) private var activeMode
    @Environment(\.chatListOverlayPresented) private var chatListOverlayPresented
    @ObservedObject var model: ChatViewModel
    @ObservedObject private var state: RunStateReducer
    @State private var artifactSelection: ArtifactSelection?
    @State private var textSelection: MessageTextSelection?
    @State private var isDropTargeted = false
    @State private var showingFileImporter = false
    @State private var showingWebsiteAccess = false
    @State private var transcriptProbe = TranscriptProbe()
    /// What the draft needs; the field hugs one line and grows to a cap.
    @State private var composerContentHeight: CGFloat = 0
    /// The dock's laid-out height, kept free at the end of the transcript.
    @State private var dockHeight: CGFloat = 0
    #if os(iOS)
    @State private var showingPhotoPicker = false
    #endif

    #if os(macOS)
    private static let transcriptHorizontalPadding: CGFloat = 36
    #else
    private static let transcriptHorizontalPadding: CGFloat = 16
    #endif

    public init(model: ChatViewModel) {
        self.model = model
        self.state = model.runState
    }

    private var notificationCovered: Bool {
        var covered = artifactSelection != nil || textSelection != nil || model.isPeopleLookupPresented
            || showingWebsiteAccess || showingFileImporter || model.callResult != nil || model.deviceSignInRequest != nil
            || chatListOverlayPresented
        #if os(iOS)
        covered = covered || showingPhotoPicker || model.pendingSmsInvocation != nil
        #endif
        return covered
    }

    /// An `onChange` action is the closure built for the previous body, so the
    /// environment it reads still holds the old mode: callers pass the new value.
    private func updateNotificationVisibility(mode: ClientMode? = nil, covered: Bool? = nil) {
        model.notificationTranscriptVisible = (mode ?? activeMode) == .chat && !(covered ?? notificationCovered)
        if model.notificationTranscriptVisible { Task { await model.synchronizeNotifications() } }
    }

    /// Renders the live conversation and composer while the active mode controls the window title.
    public var body: some View {
        ScrollViewReader { proxy in
            // The dock and composer float over the end of the transcript, which
            // scrolls up from beneath them: the transcript keeps the dock's
            // measured height clear at its end, and the scroll anchor sits
            // below that clearance. (A bottom safe-area inset holding the
            // approval card left the whole window undrawn on macOS 26.)
            GeometryReader { pane in
                ZStack(alignment: .bottom) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 0) {
                            #if os(macOS)
                            header
                            #endif
                            transcript
                                .padding(.top, 8)
                                .background(
                                    GeometryReader { geometry in
                                        Color.clear
                                            .onAppear { transcriptProbe.contentHeight = geometry.size.height }
                                            .onChange(of: geometry.size.height) { transcriptProbe.contentHeight = $0 }
                                    }
                                )
                        }
                        .padding(.horizontal, Self.transcriptHorizontalPadding)
                        .frame(maxWidth: A2Metrics.transcriptMaxWidth)
                        .frame(maxWidth: .infinity)
                        Color.clear
                            .frame(height: max(dockHeight + 8, 1))
                            .id(Self.bottomAnchorID)
                    }
                    .background(
                        GeometryReader { geometry in
                            Color.clear
                                .onAppear { transcriptProbe.viewportHeight = geometry.size.height }
                                .onChange(of: geometry.size.height) { transcriptProbe.viewportHeight = $0 }
                        }
                    )
                    bottomDock(maxCardHeight: pane.size.height * 0.55)
                        .background(
                            GeometryReader { geometry in
                                Color.clear.preference(key: DockHeightKey.self, value: geometry.size.height)
                            }
                        )
                }
            }
            .onPreferenceChange(DockHeightKey.self) { dockHeight = $0 }
            .onChange(of: scrollChangeToken) { _ in
                scroll(proxy)
            }
            // A dock that just grew would otherwise cover the end of the
            // transcript; the clearance below the anchor has to be laid out first.
            .onChange(of: dockHeight) { _ in
                DispatchQueue.main.async { scroll(proxy) }
            }
            .onChange(of: model.notificationFocus) { _ in
                scroll(proxy)
            }
            .onAppear {
                scroll(proxy)
            }
        }
        .background(AppTheme.paper.ignoresSafeArea())
        .onAppear { updateNotificationVisibility() }
        .onDisappear { model.notificationTranscriptVisible = false }
        .onChange(of: notificationCovered) { covered in updateNotificationVisibility(covered: covered) }
        .onChange(of: activeMode) { mode in updateNotificationVisibility(mode: mode) }
        // While the conversation is on screen, its task permission is re-read
        // every 30 seconds; a revoke or sweep elsewhere reaches no run stream.
        // One diagnostic line per opened conversation once its load settles:
        // rows in the reducer against the height the transcript laid out.
        .task(id: "transcript-\(model.selectedSessionID?.uuidString ?? "new")") {
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            guard !Task.isCancelled, let sessionID = model.selectedSessionID else { return }
            let rows = state.activityTimeline.count
            let content = Int(transcriptProbe.contentHeight)
            let viewport = Int(transcriptProbe.viewportHeight)
            ConversationDiagnostics.logger.notice(
                "settled session=\(sessionID.uuidString, privacy: .public) rows=\(rows) content=\(content) viewport=\(viewport)"
            )
        }
        .task(id: model.selectedSessionID) {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                guard !Task.isCancelled else { return }
                await model.reconcileTaskGrant()
            }
        }
        .onDrop(
            of: [.item],
            delegate: AttachmentDropDelegate(
                isTargeted: $isDropTargeted,
                isEnabled: model.isConfigured,
                attach: { providers in Task { await model.attach(itemProviders: providers) } }
            )
        )
        .overlay {
            if isDropTargeted { AttachmentDropHighlight() }
        }
        .fileImporter(
            isPresented: $showingFileImporter,
            allowedContentTypes: [.item],
            allowsMultipleSelection: true
        ) { result in
            switch result {
            case .success(let urls):
                Task { await model.attach(fileURLs: urls) }
            case .failure(let error):
                model.reportConnectionError(error)
            }
        }
        #if os(iOS)
        .sheet(isPresented: $showingPhotoPicker) {
            PhotoLibraryPicker { providers in
                Task { await model.attach(itemProviders: providers) }
            }
        }
        #endif
        .navigationTitle(activeMode == .chat ? model.selectedConversationTitle : "Email")
        #if os(macOS)
        // The window's one toolbar owner places People and Stop (RootView).
        .navigationSubtitle(activeMode == .chat ? statusLabel ?? "" : "")
        #else
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) { titleBlock }
            ToolbarItemGroup(placement: .primaryAction) {
                websiteAccessButton
                ChatToolbarActions(model: model)
            }
        }
        #endif
        .sheet(item: Binding(get: { model.callResult }, set: { if $0 == nil { model.dismissCallResult() } })) { result in
            CallResultSheet(result: result, close: model.dismissCallResult) {
                Task { await model.deleteCallResult() }
            }
        }
        .sheet(isPresented: $showingWebsiteAccess) {
            ConnectionSettingsView(model: model, embedded: false)
        }
        .sheet(item: $artifactSelection) { selection in
            ArtifactViewerView(model: model, artifactID: selection.id)
        }
        // One sheet for every message, held here so it survives the lazy
        // stack unloading the row that opened it.
        .sheet(item: $textSelection) { selection in
            MessageTextSheet(selection: selection)
        }
        #if os(iOS)
        .fullScreenCover(isPresented: $model.isPeopleLookupPresented) {
            PeopleLookupSheet(lookup: PeopleLookup(), sessionID: model.selectedSessionID)
        }
        #else
        .sheet(isPresented: $model.isPeopleLookupPresented) {
            PeopleLookupSheet(lookup: PeopleLookup(), sessionID: model.selectedSessionID)
        }
        #endif
        .onChange(of: model.connectionGeneration) { _ in model.isPeopleLookupPresented = false }
    }

    // MARK: - Header

    #if os(macOS)
    /// The conversation's title in the display face, with what the run needs
    /// from the owner beneath it, or else where the conversation is filed and
    /// when it last moved.
    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.selectedConversationTitle)
                    .appDisplayFont(.title2)
                    .lineLimit(1)
                    .accessibilityIdentifier("chat.heading")
                if let statusLabel {
                    Text(statusLabel)
                        .appFont(.caption)
                        .foregroundColor(AppTheme.amberText)
                        .accessibilityIdentifier("chat.status")
                } else if let entry = selectedEntry {
                    ConversationMetaLine(prefix: metaPrefix(for: entry), updatedAt: entry.updatedAt)
                        .appFont(.caption)
                        .foregroundColor(AppTheme.inkSoft)
                }
            }
            Spacer(minLength: 8)
            websiteAccessButton
        }
        .padding(.top, 12)
        .padding(.bottom, 8)
    }

    private var selectedEntry: SessionHistoryEntry? {
        guard let selectedSessionID = model.selectedSessionID else { return nil }
        return model.history.first { $0.sessionID == selectedSessionID }
    }

    private func metaPrefix(for entry: SessionHistoryEntry) -> String {
        if entry.scheduleID != nil { return "Scheduled" }
        if let folderID = entry.folderID, let folder = model.folders.first(where: { $0.id == folderID }) {
            return folder.name
        }
        return "Chat"
    }
    #endif

    #if os(iOS)
    /// The conversation's title and, beneath it, only what the run needs from the owner.
    private var titleBlock: some View {
        VStack(spacing: 1) {
            Text(model.selectedConversationTitle)
                .appDisplayFont(.headline)
                .lineLimit(1)
                .accessibilityIdentifier("chat.heading")
            if let statusLabel {
                Text(statusLabel)
                    .appFont(.caption)
                    .foregroundColor(AppTheme.amberText)
                    .accessibilityIdentifier("chat.status")
            }
        }
    }
    #endif

    private var websiteAccessButton: some View {
        Button { showingWebsiteAccess = true } label: {
            Image(systemName: "globe")
        }
        .buttonStyle(QuietIconButtonStyle())
        .help("Website Access")
        .accessibilityLabel("Website Access")
        .accessibilityIdentifier("chat.websiteAccess")
    }

    private var statusLabel: String? {
        ConversationStatus.label(runStatus: state.runStatus, awaitingApproval: model.awaitingOwnerApproval)
    }

    // MARK: - Transcript

    private var transcript: some View {
        LazyVStack(alignment: .leading, spacing: 0) {
            if let workingState = state.workingState {
                WorkingStatePanel(state: workingState)
                    .padding(.bottom, 16)
            }
            ForEach(Array(state.activityTimeline.enumerated()), id: \.element.id) { index, item in
                row(for: item)
                    .padding(.top, index == 0 ? 0 : spacingBefore(item))
            }
            ForEach(detachedApprovals) { approval in
                Group {
                    if approval.status.isPending {
                        WaitingLine(text: "Waiting for your approval. See below.")
                    } else {
                        ApprovalCardView(approval: approval) { _, _, _ in }
                    }
                }
                .padding(.top, 12)
                .id(NotificationFocus.approval(approval.id).scrollID)
            }
            if let prompt = state.clarifyingQuestion {
                WaitingLine(text: "Waiting for your answer. See below.")
                    .padding(.top, 12)
                    .id(NotificationFocus.question(prompt.questionID).scrollID)
            }
            if let failure = state.failure {
                RunFailureCard(failure: failure)
                    .padding(.top, 16)
            }
        }
    }

    /// Approvals no tool call carries, such as one restored ahead of its call.
    private var detachedApprovals: [ApprovalView] {
        state.approvals.filter { approval in
            !state.tools.contains { $0.approvalID == approval.id }
        }
    }

    private func spacingBefore(_ item: ConversationActivity) -> CGFloat {
        switch item {
        case .message: return 24
        case .tool, .toolBundle: return 12
        }
    }

    @ViewBuilder
    private func row(for item: ConversationActivity) -> some View {
        switch item {
        case .message(let message):
            TimelineBubble(
                item: message,
                openArtifact: { artifactID in
                    artifactSelection = ArtifactSelection(id: artifactID)
                },
                selectText: { textSelection = $0 }
            )
        case .tool(let activity):
            VStack(alignment: .leading, spacing: 12) {
                WorkStrip(
                    content: .single(activity),
                    approvals: state.approvals,
                    openArtifact: { artifactID in
                        artifactSelection = ArtifactSelection(id: artifactID)
                    }
                )
                if let approval = state.approvals.first(where: { $0.id == activity.approvalID }),
                    approval.status.isPending
                {
                    WaitingLine(text: "Waiting for your approval. See below.")
                }
            }
            .id(
                activity.approvalID.map {
                    NotificationFocus.approval($0).scrollID
                } ?? activity.id
            )
        case .toolBundle(let bundle):
            WorkStrip(
                content: .bundle(bundle),
                approvals: state.approvals,
                openArtifact: { artifactID in
                    artifactSelection = ArtifactSelection(id: artifactID)
                }
            )
            .id(scrollID(for: bundle, fallback: item.id))
        }
    }

    // MARK: - Dock and composer

    /// What sits over the end of the transcript: the card the run waits on,
    /// the activity line, the task permission, and the composer.
    private func bottomDock(maxCardHeight: CGFloat) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            AttentionDock(model: model, state: state, maxHeight: maxCardHeight)
            if let activityLabel {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text(activityLabel)
                        .appFont(.callout)
                        .foregroundColor(AppTheme.inkSoft)
                        .lineLimit(1)
                    Spacer()
                }
                .padding(.horizontal, 8)
                .accessibilityIdentifier("chat.activity")
            }
            if model.activeTaskGrant != nil {
                TaskGrantBanner(model: model) {
                    Task { await model.stopTaskGrant() }
                }
            }
            composer
        }
        .padding(.horizontal, Self.transcriptHorizontalPadding)
        .padding(.top, 12)
        .padding(.bottom, 12)
        .frame(maxWidth: A2Metrics.transcriptMaxWidth)
        .frame(maxWidth: .infinity)
        .background(
            LinearGradient(
                stops: [
                    .init(color: AppTheme.paper.opacity(0), location: 0),
                    .init(color: AppTheme.paper, location: 0.45),
                ],
                startPoint: .top,
                endPoint: .bottom
            )
            .padding(.top, -24)
            .allowsHitTesting(false)
        )
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 4) {
            if !model.attachments.isEmpty {
                AttachmentChipsView(
                    attachments: model.attachments,
                    remove: { model.removeAttachment($0) },
                    retry: { model.retryAttachment($0) }
                )
                .padding(.horizontal, 6)
                .padding(.top, 4)
            }
            HStack(alignment: .bottom, spacing: 4) {
                attachButton
                messageField
                sendButton
                    .padding(.bottom, 4)
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(
            .regularMaterial,
            in: RoundedRectangle(cornerRadius: A2Metrics.composerRadius, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: A2Metrics.composerRadius, style: .continuous)
                .stroke(AppTheme.hairline)
        )
        .shadow(color: AppTheme.ink.opacity(0.10), radius: 18, x: 0, y: 8)
    }

    @ViewBuilder
    private var attachButton: some View {
        #if os(iOS)
        Menu {
            Button {
                showingFileImporter = true
            } label: {
                Label("Choose Files…", systemImage: "folder")
            }
            Button {
                showingPhotoPicker = true
            } label: {
                Label("Photo Library…", systemImage: "photo.on.rectangle")
            }
        } label: {
            Image(systemName: "paperclip")
                .font(.system(size: 17))
                .frame(width: 36, height: 36)
                .foregroundColor(AppTheme.inkSoft)
                .contentShape(Rectangle())
        }
        .padding(.bottom, 3)
        .disabled(!model.isConfigured)
        .accessibilityLabel("Attach files")
        .accessibilityIdentifier("chat.attach")
        #else
        Button {
            showingFileImporter = true
        } label: {
            Image(systemName: "paperclip")
                .font(.system(size: 15))
        }
        .buttonStyle(QuietIconButtonStyle())
        .padding(.bottom, 4)
        .disabled(!model.isConfigured)
        .help("Attach files")
        .accessibilityLabel("Attach files")
        .accessibilityIdentifier("chat.attach")
        #endif
    }

    @ViewBuilder
    private var messageField: some View {
        #if os(macOS)
        ComposerTextEditor(
            text: $model.composerText,
            onSubmit: submitDraft,
            onDropFiles: { urls in Task { await model.attach(fileURLs: urls) } },
            onContentHeightChange: { composerContentHeight = $0 }
        )
        .frame(height: composerHeight)
        .accessibilityLabel("Message")
        .accessibilityIdentifier("chat.composer")
        #else
        ComposerTextEditor(
            text: $model.composerText,
            onSubmit: submitDraft,
            onDropItems: { providers in Task { await model.attach(itemProviders: providers) } },
            onContentHeightChange: { composerContentHeight = $0 }
        )
        .frame(height: composerHeight)
        .accessibilityLabel("Message")
        .accessibilityIdentifier("chat.composer")
        #endif
    }

    #if os(macOS)
    private static let composerLineHeight: CGFloat = 36
    #else
    private static let composerLineHeight: CGFloat = 42
    #endif

    /// One line at rest, up to about six lines, then the draft scrolls inside.
    private var composerHeight: CGFloat {
        min(max(composerContentHeight, Self.composerLineHeight), 140)
    }

    private var sendButton: some View {
        Button(action: submitDraft) {
            Image(systemName: "arrow.up")
                .font(.system(size: 14, weight: .semibold))
                .foregroundColor(canSendDraft ? AppTheme.onTeal : AppTheme.inkFaint)
                .frame(width: A2Metrics.controlHeight, height: A2Metrics.controlHeight)
                .background(Circle().fill(canSendDraft ? AppTheme.tealFill : AppTheme.field))
                .contentShape(Circle())
        }
        .buttonStyle(.plain)
        .disabled(!canSendDraft)
        .accessibilityLabel("Send")
    }

    private var activityLabel: String? {
        RunActivity.label(
            isSending: model.isSending,
            runStatus: state.runStatus,
            awaitingApproval: model.awaitingOwnerApproval,
            reasoningActive: state.reasoningActive,
            reasoningTitle: state.reasoningTitle
        )
    }

    var canSendDraft: Bool {
        let hasText = !model.composerText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let answering = state.runStatus == .waitingForUser
        // Attachments alone make a message, but an answer to a question is text.
        return (hasText || (!model.attachments.isEmpty && !answering))
            && (answering || model.attachmentsReady)
            && !model.isSending
            && (!state.isRunActive || answering)
            && model.isConfigured
    }

    private static let bottomAnchorID = ConversationScrollTarget.bottom.scrollID

    private var scrollChangeToken: String {
        // Follow newly inserted activity, but leave the viewport fixed while an
        // existing assistant message grows so its beginning remains readable.
        "\(state.timeline.count):\(state.tools.count):\(state.approvals.count):\(state.clarifyingQuestion?.id.uuidString ?? "none"):\(state.failure?.occurredAt.timeIntervalSince1970 ?? 0)"
    }

    /// A notification for an approval a bundle now holds scrolls to the bundle.
    private func scrollID(for bundle: ToolActivityBundle, fallback: String) -> String {
        guard case .approval(let approvalID) = model.notificationFocus,
            bundle.activities.contains(where: { $0.approvalID == approvalID })
        else { return fallback }
        return NotificationFocus.approval(approvalID).scrollID
    }

    private func scroll(_ proxy: ScrollViewProxy) {
        withAnimation {
            let target = ConversationScrollTarget.resolve(model.notificationFocus)
            switch target {
            case .notification:
                proxy.scrollTo(target.scrollID, anchor: .center)
            case .bottom:
                proxy.scrollTo(target.scrollID, anchor: .bottom)
            }
        }
    }

    private func submitDraft() {
        guard canSendDraft else { return }
        let message = model.composerText
        model.composerText = ""
#if os(iOS)
        UIApplication.shared.sendAction(
            #selector(UIResponder.resignFirstResponder),
            to: nil,
            from: nil,
            for: nil
        )
#endif
        Task {
            let sent = await model.send(message)
            if !sent && model.composerText.isEmpty {
                model.composerText = message
            }
        }
    }
}

private struct DockHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

/// The card the run waits on, docked above the composer until the owner
/// answers it: every pending approval in order, or else the clarifying
/// question. Nothing shows while nothing is waiting.
private struct AttentionDock: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject var state: RunStateReducer
    /// The most of the pane the cards may take before they scroll.
    let maxHeight: CGFloat

    var body: some View {
        let pending = state.approvals.filter { $0.status.isPending }
        if !pending.isEmpty || state.clarifyingQuestion != nil {
            // The dock scrolls like the transcript: a card taller than the
            // window's share stays reachable, and a card laid out outside a
            // scroll container left the whole window undrawn on macOS 26.
            ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(pending) { approval in
                    ApprovalCardView(
                        approval: approval, activeTaskGrant: model.activeTaskGrant,
                        inFlight: model.approvalsInFlight[approval.id]
                    ) { decision, reason, taskGrant in
                        Task {
                            await model.resolveApproval(
                                approval,
                                decision: decision,
                                reason: reason,
                                taskGrant: taskGrant
                            )
                        }
                    }
                    .shadow(color: AppTheme.ink.opacity(0.12), radius: 20, x: 0, y: 10)
                }
                if pending.isEmpty, let prompt = state.clarifyingQuestion {
                    ClarifyingQuestionCard(prompt: prompt) { answer in
                        await model.answerQuestion(prompt, answer: answer)
                    }
                    .shadow(color: AppTheme.ink.opacity(0.12), radius: 20, x: 0, y: 10)
                }
            }
            .accessibilityElement(children: .contain)
            .accessibilityIdentifier("chat.attention-dock")
            }
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxHeight: max(maxHeight, 120))
        }
    }
}

/// Where a conversation is filed and when it last moved: "Home · Updated 2 min ago".
private struct ConversationMetaLine: View {
    let prefix: String
    let updatedAt: Date

    var body: some View {
        TimelineView(ConversationAgeSchedule(updatedAt: updatedAt)) { context in
            Text(
                "\(prefix) · Updated \(ConversationAgeFormatter.string(since: updatedAt, relativeTo: context.date))"
            )
        }
    }
}

/// The open conversation's task permission, above the composer: where it
/// allows Veetbot to act, how many actions it has used and how long is left,
/// and Stop, which needs no confirmation (ADR-0129).
private struct TaskGrantBanner: View {
    @ObservedObject var model: ChatViewModel
    let stop: () -> Void

    var body: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            if let text = model.taskGrantBannerText(now: context.date) {
                HStack(spacing: 10) {
                    Image(systemName: "checkmark.shield")
                        .foregroundColor(AppTheme.teal)
                    Text(text)
                        .appFont(.caption, weight: .medium)
                        .foregroundColor(AppTheme.inkSoft)
                        .lineLimit(2)
                        .accessibilityIdentifier("task-grant.banner.text")
                    Spacer(minLength: 8)
                    Button("Stop", role: .destructive, action: stop)
                        .buttonStyle(QuietCapsuleButtonStyle())
                        .accessibilityIdentifier("task-grant.banner.stop")
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(
                    RoundedRectangle(cornerRadius: 10, style: .continuous).fill(AppTheme.tealWash)
                )
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("task-grant.banner")
            }
        }
    }
}

private struct ArtifactSelection: Identifiable {
    let id: UUID
}

private struct RunFailureCard: View {
    let failure: RunFailureView

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Conversation failed", systemImage: "exclamationmark.triangle")
                .appFont(.caption, weight: .semibold)
                .foregroundColor(AppTheme.red)
            Text(failure.userFacingMessage)
                .appFont(.body)
                .textSelection(.enabled)
            Text(failure.diagnosticSummary)
                .textSelection(.enabled)
                .appCodeFont()
                .foregroundColor(AppTheme.inkSoft)
        }
        .padding(.leading, 14)
        .overlay(alignment: .leading) {
            Rectangle().fill(AppTheme.red).frame(width: 2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("conversation.failure")
    }
}

/// One message: the owner's in a teal-wash capsule at the trailing edge, the
/// assistant's on bare paper behind a small teal mark.
private struct TimelineBubble: View {
    let item: TimelineItem
    let openArtifact: (UUID) -> Void
    let selectText: (MessageTextSelection) -> Void

    var body: some View {
        if item.role == .user {
            HStack(alignment: .bottom, spacing: 0) {
                Spacer(minLength: 48)
                VStack(alignment: .trailing, spacing: 4) {
                    content
                        .padding(.horizontal, 16)
                        .padding(.vertical, 10)
                        .background(
                            RoundedRectangle(cornerRadius: 18, style: .continuous)
                                .fill(AppTheme.tealWash)
                        )
                    actions
                }
                .frame(maxWidth: 560, alignment: .trailing)
            }
            .frame(maxWidth: .infinity, alignment: .trailing)
        } else {
            HStack(alignment: .top, spacing: 12) {
                AssistantMark()
                    .padding(.top, 8)
                VStack(alignment: .leading, spacing: 6) {
                    content
                    actions
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder
    private var actions: some View {
        if item.offersMessageActions {
            MessageActionBar(
                messageID: item.id,
                role: item.role,
                markdown: item.copyableMarkdown,
                selectText: selectText
            )
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(item.content.enumerated()), id: \.offset) { _, block in
                switch block {
                case .text(let text):
                    MarkdownContentView(text: text)
                case .image(let artifactID, let mediaType, _):
                    artifactButton("Image · \(mediaType)", id: artifactID)
                case .file(let artifactID, _, let filename):
                    artifactButton(filename ?? "File", id: artifactID)
                }
            }
            if item.isStreaming {
                ProgressView().controlSize(.small)
            }
        }
    }

    /// A file the reply carries, as a chip that opens it.
    private func artifactButton(_ label: String, id: UUID) -> some View {
        Button {
            openArtifact(id)
        } label: {
            Label {
                Text(label).lineLimit(1)
            } icon: {
                Image(systemName: "doc.text").foregroundColor(AppTheme.teal)
            }
        }
        .buttonStyle(SecondaryCapsuleButtonStyle())
    }
}

/// The open conversation's People and Stop controls. On the Mac the window's one
/// toolbar owner places them; on iOS the conversation's navigation bar does.
struct ChatToolbarActions: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject private var state: RunStateReducer

    init(model: ChatViewModel) {
        self.model = model
        self.state = model.runState
    }

    var body: some View {
        Button { model.isPeopleLookupPresented = true } label: {
            Label("People", systemImage: "person.2")
        }
        .help("People")
        .accessibilityIdentifier("chat.people")
        if state.isRunActive {
            Button(role: .destructive) {
                Task { await model.cancelActiveRun() }
            } label: {
                Label("Stop", systemImage: "stop.circle")
            }
            .help("Stop")
            .accessibilityIdentifier("chat.stop")
        }
    }
}

/// The transcript's last laid-out heights, held outside view state so that
/// recording them never re-renders the conversation.
@MainActor
private final class TranscriptProbe {
    var contentHeight: CGFloat = 0
    var viewportHeight: CGFloat = 0
}
