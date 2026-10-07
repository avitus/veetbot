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

public struct ChatView: View {
    @Environment(\.activeClientMode) private var activeMode
    @ObservedObject var model: ChatViewModel
    @ObservedObject private var state: RunStateReducer
    @State private var artifactSelection: ArtifactSelection?
    @State private var textSelection: MessageTextSelection?
    @State private var isDropTargeted = false
    @State private var showingFileImporter = false
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
            || showingFileImporter || model.callResult != nil || model.deviceSignInRequest != nil
        #if os(iOS)
        covered = covered || showingPhotoPicker || model.pendingSmsInvocation != nil
        #endif
        return covered
    }

    private func updateNotificationVisibility() {
        model.notificationTranscriptVisible = activeMode == .chat && !notificationCovered
        if model.notificationTranscriptVisible { Task { await model.synchronizeNotifications() } }
    }

    /// Renders the live conversation and composer while the active mode controls the window title.
    public var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if let workingState = state.workingState {
                            WorkingStatePanel(state: workingState)
                        }
                        ForEach(state.activityTimeline) { item in
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
                                ToolActivityCard(
                                    activity: activity,
                                    approval: state.approvals.first {
                                        $0.id == activity.approvalID
                                    },
                                    activeTaskGrant: model.activeTaskGrant,
                                    approvalInFlight: activity.approvalID.flatMap {
                                        model.approvalsInFlight[$0]
                                    },
                                    resolve: { approval, decision, reason, taskGrant in
                                        Task {
                                            await model.resolveApproval(
                                                approval,
                                                decision: decision,
                                                reason: reason,
                                                taskGrant: taskGrant
                                            )
                                        }
                                    },
                                    openArtifact: { artifactID in
                                        artifactSelection = ArtifactSelection(id: artifactID)
                                    }
                                )
                                .id(
                                    activity.approvalID.map {
                                        NotificationFocus.approval($0).scrollID
                                    } ?? activity.id
                                )
                            case .toolBundle(let bundle):
                                ToolActivityBundleCard(
                                    bundle: bundle,
                                    openArtifact: { artifactID in
                                        artifactSelection = ArtifactSelection(id: artifactID)
                                    }
                                )
                            }
                        }
                        ForEach(
                            state.approvals.filter { approval in
                                !state.tools.contains { $0.approvalID == approval.id }
                            }
                        ) { approval in
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
                            .id(NotificationFocus.approval(approval.id).scrollID)
                        }
                        if let prompt = state.clarifyingQuestion {
                            ClarifyingQuestionCard(prompt: prompt) { answer in
                                await model.answerQuestion(prompt, answer: answer)
                            }
                            .id(NotificationFocus.question(prompt.questionID).scrollID)
                        }
                        if let failure = state.failure {
                            RunFailureCard(failure: failure)
                        }
                        Color.clear.frame(height: 1).id(Self.bottomAnchorID)
                    }
                    .padding(.vertical)
                    .padding(.horizontal, Self.transcriptHorizontalPadding)
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: scrollChangeToken) { _ in
                    scroll(proxy)
                }
                .onChange(of: model.notificationFocus) { _ in
                    scroll(proxy)
                }
                .onAppear {
                    scroll(proxy)
                }
            }
            if let activityLabel {
                HStack(spacing: 8) {
                    ProgressView()
                    Text(activityLabel)
                        .foregroundColor(.secondary)
                        .lineLimit(1)
                    Spacer()
                }
                .padding(.horizontal, Self.transcriptHorizontalPadding)
                .padding(.vertical, 8)
                .accessibilityIdentifier("chat.activity")
            }
            if model.activeTaskGrant != nil {
                TaskGrantBanner(model: model) {
                    Task { await model.stopTaskGrant() }
                }
                .padding(.horizontal, Self.transcriptHorizontalPadding)
                .padding(.vertical, 6)
            }
            Divider()
            composer
        }
        .onAppear { updateNotificationVisibility() }
        .onDisappear { model.notificationTranscriptVisible = false }
        .onChange(of: notificationCovered) { _ in updateNotificationVisibility() }
        .onChange(of: activeMode) { _ in updateNotificationVisibility() }
        // While the conversation is on screen, its task permission is re-read
        // every 30 seconds; a revoke or sweep elsewhere reaches no run stream.
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
            ToolbarItemGroup(placement: .primaryAction) { ChatToolbarActions(model: model) }
        }
        #endif
        .sheet(item: Binding(get: { model.callResult }, set: { if $0 == nil { model.dismissCallResult() } })) { result in
            CallResultSheet(result: result, close: model.dismissCallResult) {
                Task { await model.deleteCallResult() }
            }
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

    #if os(iOS)
    /// The conversation's title and, beneath it, only what the run needs from the owner.
    private var titleBlock: some View {
        VStack(spacing: 1) {
            Text(model.selectedConversationTitle)
                .appFont(.headline)
                .lineLimit(1)
                .accessibilityIdentifier("chat.heading")
            if let statusLabel {
                Text(statusLabel)
                    .appFont(.caption)
                    .foregroundColor(AppTheme.orange)
                    .accessibilityIdentifier("chat.status")
            }
        }
    }
    #endif

    private var statusLabel: String? {
        ConversationStatus.label(runStatus: state.runStatus, awaitingApproval: model.awaitingOwnerApproval)
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if !model.attachments.isEmpty {
                AttachmentChipsView(
                    attachments: model.attachments,
                    remove: { model.removeAttachment($0) },
                    retry: { model.retryAttachment($0) }
                )
            }
            HStack(alignment: .bottom, spacing: 10) {
                attachButton
                messageField
                sendButton
            }
        }
        .padding()
        .background(AppTheme.turquoise.opacity(0.055))
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
                .font(.title3)
                .frame(minWidth: 32, minHeight: 42)
        }
        .disabled(!model.isConfigured)
        .accessibilityLabel("Attach files")
        .accessibilityIdentifier("chat.attach")
        #else
        Button {
            showingFileImporter = true
        } label: {
            Image(systemName: "paperclip")
                .font(.title3)
                .frame(minWidth: 28, minHeight: 42)
        }
        .buttonStyle(.plain)
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
            onDropFiles: { urls in Task { await model.attach(fileURLs: urls) } }
        )
        .frame(minHeight: 42, maxHeight: 120)
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(AppTheme.turquoise.opacity(0.42))
        )
        .accessibilityLabel("Message")
        .accessibilityIdentifier("chat.composer")
        #else
        ComposerTextEditor(
            text: $model.composerText,
            onSubmit: submitDraft,
            onDropItems: { providers in Task { await model.attach(itemProviders: providers) } }
        )
        .frame(minHeight: 42, maxHeight: 120)
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(AppTheme.turquoise.opacity(0.42))
        )
        .accessibilityLabel("Message")
        .accessibilityIdentifier("chat.composer")
        #endif
    }

    private var sendButton: some View {
        Button(action: submitDraft) {
            Image(systemName: "arrow.up.circle.fill")
                .font(.title2)
                .foregroundColor(canSendDraft ? AppTheme.orange : .secondary)
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
                    Image(systemName: "checkmark.shield.fill")
                        .foregroundColor(AppTheme.turquoise)
                    Text(text)
                        .appFont(.caption, weight: .semibold)
                        .lineLimit(2)
                        .accessibilityIdentifier("task-grant.banner.text")
                    Spacer(minLength: 8)
                    Button("Stop", role: .destructive, action: stop)
                        .accessibilityIdentifier("task-grant.banner.stop")
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(AppTheme.turquoise.opacity(0.1))
                .clipShape(RoundedRectangle(cornerRadius: 10))
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
            Label("Conversation failed", systemImage: "exclamationmark.triangle.fill")
                .appFont(.headline)
                .foregroundColor(.red)
            Text(failure.userFacingMessage)
                .appFont(.body)
                .textSelection(.enabled)
            Text(failure.diagnosticSummary)
                .appFont(.caption)
                .foregroundColor(.secondary)
        }
        .padding(12)
        .frame(maxWidth: 680, alignment: .leading)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color.red.opacity(0.08))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 12)
                .stroke(Color.red.opacity(0.35))
        )
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("conversation.failure")
    }
}

private struct TimelineBubble: View {
    let item: TimelineItem
    let openArtifact: (UUID) -> Void
    let selectText: (MessageTextSelection) -> Void

    var body: some View {
        VStack(alignment: item.role == .user ? .trailing : .leading, spacing: 4) {
            bubble
            if item.offersMessageActions {
                MessageActionBar(
                    messageID: item.id,
                    role: item.role,
                    markdown: item.copyableMarkdown,
                    selectText: selectText
                )
            }
        }
        .frame(maxWidth: .infinity, alignment: item.role == .user ? .trailing : .leading)
    }

    private var bubble: some View {
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
        .padding(item.role == .user ? 12 : 0)
        .background {
            if item.role == .user {
                RoundedRectangle(cornerRadius: 14)
                    .fill(AppTheme.turquoise.opacity(0.18))
            }
        }
        .frame(maxWidth: item.role == .user ? 680 : .infinity, alignment: .leading)
    }

    private func artifactButton(_ label: String, id: UUID) -> some View {
        Button {
            openArtifact(id)
        } label: {
            Label(label, systemImage: "doc")
        }
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
