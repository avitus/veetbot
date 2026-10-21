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

/// Where the transcript goes when its content grows, as an answer does while
/// it streams. The composer floats over the end of the transcript, so while
/// the owner reads at the end the view follows it and the newest line stays
/// above the composer; once the growing answer is taller than the view, it
/// stops with the answer's beginning at the top so the beginning stays readable.
enum TranscriptFollow: Equatable {
    case stay
    case end
    case latestTop

    /// - Parameters:
    ///   - latestMessageHeight: The height of the newest row when it is a message.
    ///   - endClearance: The space kept free below the last row for the dock.
    static func step(
        following: Bool, contentGrew: Bool, latestMessageHeight: CGFloat?,
        visibleHeight: CGFloat, endClearance: CGFloat
    ) -> TranscriptFollow {
        guard following, contentGrew else { return .stay }
        return end(latestMessageHeight: latestMessageHeight, visibleHeight: visibleHeight, endClearance: endClearance)
    }

    /// Where the end of the transcript is: its last row, unless that row is
    /// a message taller than the view, whose beginning is then the end to
    /// show. Opening a conversation and new rows go there as growth does.
    /// A view not yet laid out has no height to compare with.
    static func end(
        latestMessageHeight: CGFloat?, visibleHeight: CGFloat, endClearance: CGFloat
    ) -> TranscriptFollow {
        guard visibleHeight > 0, let latestMessageHeight,
            latestMessageHeight + endClearance > visibleHeight
        else {
            return .end
        }
        return .latestTop
    }

    /// Whether the view shows the end of its content, give or take a few points.
    static func isAtEnd(
        contentHeight: CGFloat, offset: CGFloat, containerHeight: CGFloat, bottomInset: CGFloat
    ) -> Bool {
        offset + containerHeight >= contentHeight + bottomInset - 4
    }
}

/// A message row's laid-out height, with the row it was measured on.
private struct MeasuredMessage: Equatable {
    var id: String
    var height: CGFloat
}

/// The parts of the transcript's scroll geometry that following its end reads.
private struct TranscriptScrollMetrics: Equatable {
    var contentHeight: CGFloat
    var offset: CGFloat
    var containerHeight: CGFloat
    var topInset: CGFloat
    var bottomInset: CGFloat

    init(_ geometry: ScrollGeometry) {
        contentHeight = geometry.contentSize.height
        offset = geometry.contentOffset.y
        containerHeight = geometry.containerSize.height
        topInset = geometry.contentInsets.top
        bottomInset = geometry.contentInsets.bottom
    }

    var visibleHeight: CGFloat { containerHeight - topInset - bottomInset }

    var isAtEnd: Bool {
        TranscriptFollow.isAtEnd(
            contentHeight: contentHeight, offset: offset,
            containerHeight: containerHeight, bottomInset: bottomInset
        )
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
    /// Whether growth keeps the end in view: true after the view scrolls to
    /// the end, false once the owner scrolls away from it.
    @State private var followsTranscriptEnd = true
    /// The answer whose beginning the view stopped at: it stays at the top
    /// while rows above it change height, until the owner scrolls.
    @State private var heldAnswerTop: String?
    /// The newest message's laid-out height, with the row it was measured
    /// on: a row just added has none until it is laid out.
    @State private var latestMessage: MeasuredMessage?
    /// The transcript's height between its insets.
    @State private var visibleHeight: CGFloat = 0
    /// The display title's height and whether it has scrolled out of view;
    /// only then does the toolbar carry the title.
    @State private var headingHeight: CGFloat = 0
    @State private var headingScrolledAway = false
    /// The conversation and row count the last scroll saw: a conversation
    /// opens at its latest turn without animating there.
    @State private var lastScroll: (session: UUID?, rows: Int) = (nil, 0)
    #if os(iOS)
    @State private var showingPhotoPicker = false
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

    /// `onChange` callers pass the changed mode from the action's `newValue`, so
    /// the result never depends on which render's environment the closure read.
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
                            header
                                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { headingHeight = $0 }
                            transcript
                                .transcriptBody()
                                .padding(.top, VeetSpace.s2)
                                .background(
                                    GeometryReader { geometry in
                                        Color.clear
                                            .onAppear { transcriptProbe.contentHeight = geometry.size.height }
                                            .onChange(of: geometry.size.height) { _, height in transcriptProbe.contentHeight = height }
                                    }
                                )
                        }
                        .padding(.horizontal, A2Metrics.transcriptSidePadding)
                        .frame(maxWidth: A2Metrics.transcriptMaxWidth)
                        .frame(maxWidth: .infinity)
                        Color.clear
                            .frame(height: max(dockHeight + VeetSpace.s2, 1))
                            .id(Self.bottomAnchorID)
                    }
                    .onScrollGeometryChange(for: Bool.self) { geometry in
                        geometry.contentOffset.y + geometry.contentInsets.top > max(headingHeight, VeetSpace.s6)
                    } action: { _, away in
                        headingScrolledAway = away
                    }
                    .background(
                        GeometryReader { geometry in
                            Color.clear
                                .onAppear { transcriptProbe.viewportHeight = geometry.size.height }
                                .onChange(of: geometry.size.height) { _, height in transcriptProbe.viewportHeight = height }
                        }
                    )
                    .onScrollGeometryChange(for: TranscriptScrollMetrics.self) { geometry in
                        TranscriptScrollMetrics(geometry)
                    } action: { old, new in
                        visibleHeight = new.visibleHeight
                        followGrowth(from: old, to: new, proxy)
                    }
                    .onScrollPhaseChange { _, phase in
                        if phase == .interacting {
                            followsTranscriptEnd = false
                            heldAnswerTop = nil
                        }
                    }
                    // Read directly: a preference with a last-value reducer
                    // arrived as zero and left the composer over the end.
                    bottomDock(maxCardHeight: pane.size.height * 0.55)
                        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height in
                            dockHeight = height
                        }
                }
            }
            .onChange(of: scrollChangeToken) {
                scroll(proxy)
            }
            // A dock that just grew would otherwise cover the end of the
            // transcript; the clearance below the anchor has to be laid out first.
            // One that shrank, as it does when a run ends, uncovers it already.
            .onChange(of: dockHeight) { old, new in
                guard new > old, followsTranscriptEnd else { return }
                DispatchQueue.main.async { scroll(proxy) }
            }
            .onChange(of: model.notificationFocus) {
                scroll(proxy)
            }
            // A message that arrives whole is measured after the scroll that
            // followed it in; once it proves taller than the view, its
            // beginning is the end to show.
            .onChange(of: latestMessage) {
                guard followsTranscriptEnd, endOfTranscript == .latestTop, let id = latestMessage?.id else { return }
                holdAnswerTop(id, proxy)
            }
            .onChange(of: model.selectedSessionID) {
                headingScrolledAway = false
                DispatchQueue.main.async { scroll(proxy) }
            }
            .onAppear {
                scroll(proxy)
            }
        }
        .background(AppTheme.paper.ignoresSafeArea())
        .onAppear { updateNotificationVisibility() }
        .onDisappear { model.notificationTranscriptVisible = false }
        .onChange(of: notificationCovered) { _, covered in updateNotificationVisibility(covered: covered) }
        .onChange(of: activeMode) { _, mode in updateNotificationVisibility(mode: mode) }
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
        #if os(macOS)
        // The window's title is the toolbar's: empty while the display title
        // is on screen, the conversation's once it has scrolled away (the
        // system draws it in the ui role at 600). The window's one toolbar
        // owner places People and Stop (RootView).
        .navigationTitle(
            activeMode == .chat ? (headingScrolledAway ? model.selectedConversationTitle : "") : "Email"
        )
        #else
        .navigationTitle(activeMode == .chat ? model.selectedConversationTitle : "Email")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) { toolbarTitle }
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
        .onChange(of: model.connectionGeneration) { model.isPeopleLookupPresented = false }
    }

    // MARK: - Header

    /// The conversation's title in the display role, with what the run needs
    /// from the owner beneath it, or else where the conversation is filed and
    /// when it last moved. It scrolls with the transcript.
    private var header: some View {
        HStack(alignment: .top, spacing: VeetSpace.s3) {
            VStack(alignment: .leading, spacing: VeetSpace.s1) {
                Text(model.selectedConversationTitle)
                    .veetType(.display)
                    .foregroundColor(AppTheme.ink)
                    .lineLimit(2)
                    .accessibilityAddTraits(.isHeader)
                    .accessibilityIdentifier("chat.heading")
                if let statusLabel {
                    Text(statusLabel)
                        .veetType(.meta)
                        .foregroundColor(AppTheme.amberText)
                        .accessibilityIdentifier("chat.status")
                } else if let entry = selectedEntry {
                    ConversationMetaLine(prefix: metaPrefix(for: entry), updatedAt: entry.updatedAt)
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkFaint)
                }
            }
            Spacer(minLength: VeetSpace.s2)
            #if os(macOS)
            websiteAccessButton
            #endif
        }
        .padding(.top, VeetSpace.s5)
        .padding(.bottom, VeetSpace.s4)
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

    #if os(iOS)
    /// The navigation bar's title: the ui role at 600, only once the display
    /// title has scrolled away.
    private var toolbarTitle: some View {
        Text(model.selectedConversationTitle)
            .veetType(.ui, weight: .semibold)
            .foregroundColor(AppTheme.ink)
            .lineLimit(1)
            .opacity(headingScrolledAway ? 1 : 0)
            .accessibilityHidden(!headingScrolledAway)
            .accessibilityIdentifier("chat.toolbar-title")
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

    /// Laid out eagerly. A lazy stack, opened on a report after another
    /// conversation, could settle at an estimated height of a few hundred
    /// points with both of its rows held and draw neither, leaving a blank
    /// pane too short to scroll. A conversation is tens of rows, so measuring
    /// each one is cheap and the transcript always draws what it holds.
    private var transcript: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let workingState = state.workingState {
                WorkingStatePanel(state: workingState)
                    .padding(.bottom, VeetSpace.s4)
            }
            ForEach(Array(state.activityTimeline.enumerated()), id: \.element.id) { index, item in
                row(for: item)
                    .padding(.top, index == 0 ? 0 : spacingBefore(item))
                    .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { height in
                        if case .message = item, item.id == state.activityTimeline.last?.id {
                            latestMessage = MeasuredMessage(id: item.id, height: height)
                        }
                    }
            }
            ForEach(detachedApprovals) { approval in
                Group {
                    if approval.status.isPending {
                        WaitingLine(text: "Waiting for your approval. See below.")
                    } else {
                        ApprovalCardView(approval: approval) { _, _, _ in }
                    }
                }
                .padding(.top, VeetSpace.s3)
                .id(NotificationFocus.approval(approval.id).scrollID)
            }
            if let prompt = state.clarifyingQuestion {
                WaitingLine(text: "Waiting for your answer. See below.")
                    .padding(.top, VeetSpace.s3)
                    .id(NotificationFocus.question(prompt.questionID).scrollID)
            }
            if let failure = state.failure {
                RunFailureCard(failure: failure)
                    .padding(.top, VeetSpace.s4)
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
        case .message: return VeetSpace.s5
        case .tool, .toolBundle: return VeetSpace.s3
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
            .id(item.id)
        case .tool(let activity):
            VStack(alignment: .leading, spacing: VeetSpace.s3) {
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
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            AttentionDock(model: model, state: state, maxHeight: maxCardHeight)
            if let activityLabel {
                HStack(spacing: VeetSpace.s2) {
                    ProgressView().controlSize(.small)
                    Text(activityLabel)
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkSoft)
                        .lineLimit(1)
                    Spacer()
                }
                .padding(.horizontal, VeetSpace.s2)
                .accessibilityIdentifier("chat.activity")
            }
            if model.activeTaskGrant != nil {
                TaskGrantBanner(model: model) {
                    Task { await model.stopTaskGrant() }
                }
            }
            composer
        }
        .padding(.horizontal, A2Metrics.transcriptSidePadding)
        .padding(.top, VeetSpace.s3)
        .padding(.bottom, VeetSpace.s3)
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
            .padding(.top, -VeetSpace.s5)
            .allowsHitTesting(false)
        )
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s1) {
            if !model.attachments.isEmpty {
                AttachmentChipsView(
                    attachments: model.attachments,
                    remove: { model.removeAttachment($0) },
                    retry: { model.retryAttachment($0) }
                )
                .padding(.horizontal, VeetSpace.s2)
                .padding(.top, VeetSpace.s1)
            }
            HStack(alignment: .bottom, spacing: VeetSpace.s1) {
                attachButton
                messageField
                sendButton
                    .padding(.bottom, VeetSpace.s1)
            }
        }
        .padding(.horizontal, VeetSpace.s2)
        .padding(.vertical, VeetSpace.s1)
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
                .veetIcon(.regular)
                .frame(width: 36, height: 36)
                .foregroundColor(AppTheme.inkSoft)
                .contentShape(Rectangle())
        }
        .padding(.bottom, VeetSpace.s1)
        .disabled(!model.isConfigured)
        .accessibilityLabel("Attach files")
        .accessibilityIdentifier("chat.attach")
        #else
        Button {
            showingFileImporter = true
        } label: {
            Image(systemName: "paperclip")
                .veetIcon(.regular)
        }
        .buttonStyle(QuietIconButtonStyle())
        .padding(.bottom, VeetSpace.s1)
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
                .veetIcon(.regular, weight: .semibold)
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

    /// Follows the conversation to its latest turn, or to a notification's
    /// row. A conversation that has just opened (or just loaded its rows)
    /// lands there at once; only new activity in a conversation already on
    /// screen animates.
    private func scroll(_ proxy: ScrollViewProxy) {
        let rows = state.activityTimeline.count
        let animated = lastScroll.session == model.selectedSessionID && lastScroll.rows > 0
        lastScroll = (model.selectedSessionID, rows)
        let target = ConversationScrollTarget.resolve(model.notificationFocus)
        let answerTop = target == .bottom && endOfTranscript == .latestTop ? latestMessage?.id : nil
        followsTranscriptEnd = target == .bottom && answerTop == nil
        heldAnswerTop = answerTop
        let move = {
            switch target {
            case .notification:
                proxy.scrollTo(target.scrollID, anchor: .center)
            case .bottom:
                if let answerTop {
                    proxy.scrollTo(answerTop, anchor: .top)
                } else {
                    proxy.scrollTo(target.scrollID, anchor: .bottom)
                }
            }
        }
        // An answer's beginning is held in place, not animated to: rows above
        // it can change height again before an animation would end.
        if animated && answerTop == nil { withAnimation { move() } } else { move() }
    }

    /// The newest row's height when it is a message already laid out.
    private var measuredLatestMessageHeight: CGFloat? {
        guard case .message = state.activityTimeline.last,
            let latestMessage, latestMessage.id == state.activityTimeline.last?.id
        else { return nil }
        return latestMessage.height
    }

    private var endOfTranscript: TranscriptFollow {
        TranscriptFollow.end(
            latestMessageHeight: measuredLatestMessageHeight,
            visibleHeight: visibleHeight,
            endClearance: max(dockHeight + VeetSpace.s2, 1)
        )
    }

    /// Keeps a growing answer's newest line above the composer while the
    /// owner reads at the end; an owner who scrolls back to the end resumes it.
    private func followGrowth(
        from old: TranscriptScrollMetrics, to new: TranscriptScrollMetrics, _ proxy: ScrollViewProxy
    ) {
        if let held = heldAnswerTop, abs(new.contentHeight - old.contentHeight) > 0.5 {
            // Rows above the answer changed height, as the header's status
            // line does when the run ends; keep the answer's beginning in place.
            if held == state.activityTimeline.last?.id {
                DispatchQueue.main.async { proxy.scrollTo(held, anchor: .top) }
                return
            }
            heldAnswerTop = nil
        }
        let contentGrew = new.contentHeight > old.contentHeight + 0.5
        guard contentGrew else {
            if new.isAtEnd { followsTranscriptEnd = true }
            return
        }
        let latest = state.activityTimeline.last
        switch TranscriptFollow.step(
            following: followsTranscriptEnd,
            contentGrew: contentGrew,
            latestMessageHeight: measuredLatestMessageHeight,
            visibleHeight: new.visibleHeight,
            endClearance: max(dockHeight + VeetSpace.s2, 1)
        ) {
        case .stay:
            break
        case .end:
            // As for a grown dock, the new end has to be laid out first.
            DispatchQueue.main.async { proxy.scrollTo(Self.bottomAnchorID, anchor: .bottom) }
        case .latestTop:
            if let latest { holdAnswerTop(latest.id, proxy) }
        }
    }

    /// Stops following the end at an answer taller than the view, with its
    /// beginning at the top; the new row has to be laid out first.
    private func holdAnswerTop(_ id: String, _ proxy: ScrollViewProxy) {
        followsTranscriptEnd = false
        heldAnswerTop = id
        DispatchQueue.main.async { proxy.scrollTo(id, anchor: .top) }
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

/// The card the run waits on, docked above the composer until the owner
/// answers it: every pending approval in order, or else the clarifying
/// question. Nothing shows while nothing is waiting.
private struct AttentionDock: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject var state: RunStateReducer
    /// The most of the pane the cards may take before they scroll.
    let maxHeight: CGFloat
    @State private var contentHeight: CGFloat = 0

    var body: some View {
        let pending = state.approvals.filter { $0.status.isPending }
        if !pending.isEmpty || state.clarifyingQuestion != nil {
            // The dock scrolls like the transcript: it hugs the cards up to the
            // pane's share and scrolls past it, and a card laid out outside a
            // scroll container left the whole window undrawn on macOS 26. The
            // cards report their height; a fixed-size scroll view would keep
            // its full content height past the cap and overlap the composer.
            ScrollView {
            VStack(alignment: .leading, spacing: VeetSpace.s2) {
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
            .background(
                GeometryReader { proxy in
                    Color.clear.preference(key: DockContentHeightKey.self, value: proxy.size.height)
                }
            )
            }
            .onPreferenceChange(DockContentHeightKey.self) { height in
                DispatchQueue.main.async { contentHeight = height }
            }
            .frame(height: contentHeight > 0 ? min(contentHeight, max(maxHeight, 120)) : nil)
            .fixedSize(horizontal: false, vertical: contentHeight == 0)
        }
    }
}

/// The height of the dock's cards, so the dock can hug them below the cap.
private struct DockContentHeightKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) {
        value = max(value, nextValue())
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
                HStack(spacing: VeetSpace.s2) {
                    Image(systemName: "checkmark.shield")
                        .foregroundColor(AppTheme.teal)
                    Text(text)
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkSoft)
                        .lineLimit(2)
                        .accessibilityIdentifier("task-grant.banner.text")
                    Spacer(minLength: VeetSpace.s2)
                    Button("Stop", role: .destructive, action: stop)
                        .buttonStyle(QuietCapsuleButtonStyle())
                        .accessibilityIdentifier("task-grant.banner.stop")
                }
                .padding(.horizontal, VeetSpace.s3)
                .padding(.vertical, VeetSpace.s1)
                .background(
                    RoundedRectangle(cornerRadius: A2Metrics.tableRadius, style: .continuous).fill(AppTheme.tealWash)
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
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            Label("Conversation failed", systemImage: "exclamationmark.triangle")
                .veetType(.label)
                .foregroundColor(AppTheme.red)
            Text(failure.userFacingMessage)
                .veetType(.body)
                .textSelection(.enabled)
            Text(failure.diagnosticSummary)
                .textSelection(.enabled)
                .veetMono()
                .foregroundColor(AppTheme.inkSoft)
        }
        .padding(.leading, VeetSpace.s3)
        .overlay(alignment: .leading) {
            Rectangle().fill(AppTheme.red).frame(width: 2)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("conversation.failure")
    }
}

/// One message: the owner's in a teal-wash capsule at the trailing edge, the
/// assistant's on bare paper behind a small teal mark. Copy and Select Text
/// appear on hover on the Mac and iPad (a tap shows them on an iPad without a
/// pointer); the iPhone keeps them visible, since a long-press menu would take
/// the gesture text selection uses (ADR-0122).
private struct TimelineBubble: View {
    let item: TimelineItem
    let openArtifact: (UUID) -> Void
    let selectText: (MessageTextSelection) -> Void
    @Environment(\.appTextSize) private var textSize
    @State private var hovering = false
    @State private var hoverEnd: Task<Void, Never>?
    @State private var revealed = false

    var body: some View {
        bubble
            .onHover { inside in
                hoverEnd?.cancel()
                guard !inside else {
                    hovering = true
                    return
                }
                // The bar lingers a moment after the pointer leaves. On macOS
                // 26 a bar that changed while its Select Text click presented
                // the sheet (which takes the pointer) left the window undrawn,
                // so it changes only once the presentation has settled.
                hoverEnd = Task { @MainActor in
                    try? await Task.sleep(nanoseconds: 1_200_000_000)
                    if !Task.isCancelled { hovering = false }
                }
            }
            #if os(iOS)
            .simultaneousGesture(
                TapGesture().onEnded { if VeetType.Platform.current == .iPad { revealed.toggle() } }
            )
            #endif
            .accessibilityActions {
                if item.offersMessageActions {
                    Button("Copy message") {
                        SystemClipboard.copy(MarkdownRendition(markdown: item.copyableMarkdown, textSize: textSize))
                    }
                    Button("Select text") {
                        selectText(
                            MessageTextSelection(
                                id: item.id,
                                rendition: MarkdownRendition(markdown: item.copyableMarkdown, textSize: textSize)
                            )
                        )
                    }
                }
            }
    }

    @ViewBuilder
    private var bubble: some View {
        if item.role == .user {
            HStack(alignment: .bottom, spacing: 0) {
                Spacer(minLength: VeetSpace.s7)
                VStack(alignment: .trailing, spacing: VeetSpace.s1) {
                    content
                        .padding(.horizontal, VeetSpace.s4)
                        .padding(.vertical, VeetSpace.s2)
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
            HStack(alignment: .top, spacing: VeetSpace.s3) {
                AssistantMark()
                    .padding(.top, VeetSpace.s2)
                VStack(alignment: .leading, spacing: VeetSpace.s2) {
                    content
                    actions
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var showsMessageActions: Bool {
        switch VeetType.Platform.current {
        case .mac: return hovering
        case .iPad: return hovering || revealed
        case .iPhone: return true
        }
    }

    @ViewBuilder
    private var actions: some View {
        if item.offersMessageActions {
            // The bar keeps its place, so showing it never moves the
            // transcript; hidden, it takes no clicks or taps.
            MessageActionBar(
                messageID: item.id,
                role: item.role,
                markdown: item.copyableMarkdown,
                selectText: selectText
            )
            .opacity(showsMessageActions ? 1 : 0)
            .allowsHitTesting(showsMessageActions)
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s3) {
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
