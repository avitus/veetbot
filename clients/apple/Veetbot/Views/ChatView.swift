import SwiftUI

public struct ChatView: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject private var state: RunStateReducer
    @State private var draft = ""
    @State private var selectedArtifactID: UUID?
    @State private var showingArtifact = false

    public init(model: ChatViewModel) {
        self.model = model
        self.state = model.runState
    }

    public var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if let workingState = state.workingState {
                        WorkingStatePanel(state: workingState)
                    }
                    ForEach(state.timeline) { item in
                        TimelineBubble(item: item) { artifactID in
                            selectedArtifactID = artifactID
                            showingArtifact = true
                        }
                    }
                    ForEach(state.tools) { activity in
                        ToolActivityCard(
                            activity: activity,
                            approval: state.approvals.first { $0.id == activity.approvalID },
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
                                selectedArtifactID = artifactID
                                showingArtifact = true
                            }
                        )
                    }
                    ForEach(state.approvals.filter { approval in
                        !state.tools.contains { $0.approvalID == approval.id }
                    }) { approval in
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
                            Task { _ = await model.answerQuestion(prompt, answer: answer) }
                        }
                    }
                }
                .padding()
                .frame(maxWidth: 900)
                .frame(maxWidth: .infinity)
            }
            Divider()
            composer
        }
        .navigationTitle("Conversation")
        .sheet(isPresented: $showingArtifact) {
            if let selectedArtifactID {
                ArtifactViewerView(model: model, artifactID: selectedArtifactID)
            }
        }
    }

    private var header: some View {
        HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(model.selectedSessionID == nil ? "New conversation" : "Conversation")
                    .font(.headline)
                if let status = state.runStatus {
                    Text(status.rawValue.replacingOccurrences(of: "_", with: " ").capitalized)
                        .font(.caption)
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
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 10) {
            ComposerTextEditor(text: $draft, onSubmit: submitDraft)
                .frame(minHeight: 42, maxHeight: 120)
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(Color.secondary.opacity(0.3))
                )
                .accessibilityLabel("Message")
            Button(action: submitDraft) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.title2)
            }
            .buttonStyle(.plain)
            .disabled(!canSendDraft)
            .accessibilityLabel("Send")
        }
        .padding()
        .background(Color.secondary.opacity(0.04))
    }

    private var canSendDraft: Bool {
        !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && !model.isSending
            && (!state.isRunActive || state.runStatus == .waitingForUser)
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

private struct TimelineBubble: View {
    let item: TimelineItem
    let openArtifact: (UUID) -> Void

    var body: some View {
        HStack {
            if item.role == .user { Spacer(minLength: 0) }
            VStack(alignment: .leading, spacing: 8) {
                ForEach(Array(item.content.enumerated()), id: \.offset) { _, block in
                    switch block {
                    case let .text(text):
                        Text(text).textSelection(.enabled)
                    case let .image(artifactID, mediaType, _):
                        artifactButton("Image · \(mediaType)", id: artifactID)
                    case let .file(artifactID, _, filename):
                        artifactButton(filename ?? "File", id: artifactID)
                    }
                }
                if item.isStreaming {
                    ProgressView().controlSize(.small)
                }
            }
            .padding(12)
            .background(
                item.role == .user
                    ? Color.accentColor.opacity(0.16)
                    : Color.secondary.opacity(0.10)
            )
            .clipShape(RoundedRectangle(cornerRadius: 14))
            .frame(maxWidth: 680, alignment: .leading)
            if item.role == .assistant { Spacer(minLength: 0) }
        }
    }

    private func artifactButton(_ label: String, id: UUID) -> some View {
        Button { openArtifact(id) } label: {
            Label(label, systemImage: "doc")
        }
    }
}
