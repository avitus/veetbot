import SwiftUI

public struct ChatView: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject private var state: RunStateReducer
    @State private var draft = ""
    @State private var artifactSelection: ArtifactSelection?

    public init(model: ChatViewModel) {
        self.model = model
        self.state = model.runState
    }

    public var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if let workingState = state.workingState {
                            WorkingStatePanel(state: workingState)
                        }
                        ForEach(state.activityTimeline) { item in
                            switch item {
                            case .message(let message):
                                TimelineBubble(item: message) { artifactID in
                                    artifactSelection = ArtifactSelection(id: artifactID)
                                }
                            case .tool(let activity):
                                ToolActivityCard(
                                    activity: activity,
                                    approval: state.approvals.first {
                                        $0.id == activity.approvalID
                                    },
                                    resolve: { approval, decision, reason in
                                        Task {
                                            await model.resolveApproval(
                                                approval,
                                                decision: decision,
                                                reason: reason
                                            )
                                        }
                                    },
                                    openArtifact: { artifactID in
                                        artifactSelection = ArtifactSelection(id: artifactID)
                                    }
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
                            ApprovalCard(approval: approval) { decision, reason in
                                Task {
                                    await model.resolveApproval(
                                        approval,
                                        decision: decision,
                                        reason: reason
                                    )
                                }
                            }
                        }
                        if state.runStatus == .running || state.runStatus == .queued {
                            HStack(spacing: 8) {
                                ProgressView()
                                Text(state.reasoningActive ? "Reasoning…" : "Working…")
                                    .foregroundColor(.secondary)
                            }
                            .padding(.vertical, 8)
                        }
                        if let prompt = state.clarifyingQuestion {
                            ClarifyingQuestionCard(prompt: prompt) { answer in
                                await model.answerQuestion(prompt, answer: answer)
                            }
                        }
                        Color.clear.frame(height: 1).id(Self.bottomAnchorID)
                    }
                    .padding()
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: scrollChangeToken) { _ in
                    withAnimation { proxy.scrollTo(Self.bottomAnchorID, anchor: .bottom) }
                }
            }
            Divider()
            composer
        }
        .navigationTitle("Conversation")
        .sheet(item: $artifactSelection) { selection in
            ArtifactViewerView(model: model, artifactID: selection.id)
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.selectedSessionID == nil ? "New conversation" : "Conversation")
                    .appFont(.headline)
                    .accessibilityIdentifier("chat.heading")
                if let status = state.runStatus {
                    Text(status.rawValue.replacingOccurrences(of: "_", with: " ").capitalized)
                        .appFont(.caption)
                        .foregroundColor(.secondary)
                }
            }
            Spacer()
            if state.isRunActive {
                Button(role: .destructive) {
                    Task { await model.cancelActiveRun() }
                } label: {
                    Label("Stop", systemImage: "stop.fill")
                }
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 10)
        .background(AppTheme.brandGradient.opacity(0.42))
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 10) {
            ComposerTextEditor(text: $draft, onSubmit: submitDraft)
                .frame(minHeight: 42, maxHeight: 120)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(AppTheme.turquoise.opacity(0.42))
                )
                .accessibilityLabel("Message")
                .accessibilityIdentifier("chat.composer")
            Button(action: submitDraft) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.title2)
                    .foregroundColor(canSendDraft ? AppTheme.orange : .secondary)
            }
            .buttonStyle(.plain)
            .disabled(!canSendDraft)
            .accessibilityLabel("Send")
        }
        .padding()
        .background(AppTheme.turquoise.opacity(0.055))
    }

    private var canSendDraft: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !model.isSending
            && (!state.isRunActive || state.runStatus == .waitingForUser)
    }

    private static let bottomAnchorID = "conversation-bottom"

    private var scrollChangeToken: String {
        // Follow newly inserted activity, but leave the viewport fixed while an
        // existing assistant message grows so its beginning remains readable.
        "\(state.timeline.count):\(state.tools.count)"
    }

    private func submitDraft() {
        guard canSendDraft else { return }
        let message = draft
        draft = ""
        Task {
            let sent = await model.send(message)
            if !sent, draft.isEmpty {
                draft = message
            }
        }
    }
}

private struct ArtifactSelection: Identifiable {
    let id: UUID
}

private struct TimelineBubble: View {
    let item: TimelineItem
    let openArtifact: (UUID) -> Void

    var body: some View {
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
        .frame(maxWidth: .infinity, alignment: item.role == .user ? .trailing : .leading)
    }

    private func artifactButton(_ label: String, id: UUID) -> some View {
        Button {
            openArtifact(id)
        } label: {
            Label(label, systemImage: "doc")
        }
    }
}
