import SwiftUI

#if os(macOS)
/// Chat's right panel (MagicPath merry-moon-8243): Activity holds what the run
/// is doing, its working notes and every call in the conversation; Artifacts
/// holds every file or image it produced, previewed in place. The transcript
/// keeps one line per run of calls, which reveals its group here.
struct ChatInspectorPanel: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject var inspector: InspectorModel
    @ObservedObject private var state: RunStateReducer
    @State private var flashedGroupID: String?

    init(model: ChatViewModel, inspector: InspectorModel) {
        self.model = model
        self.inspector = inspector
        self.state = model.runState
    }

    var body: some View {
        let groups = ToolLedgerGroups.make(from: state.activityTimeline)
        let artifacts = ConversationArtifacts.collect(from: state.activityTimeline)
        let calls = groups.reduce(0) { $0 + $1.activities.count }
        VStack(spacing: 0) {
            InspectorHeader(
                selection: $inspector.chatTab,
                tabs: [
                    (.activity, calls > 0 ? "Activity  \(calls)" : "Activity"),
                    (.artifacts, artifacts.isEmpty ? "Artifacts" : "Artifacts  \(artifacts.count)"),
                ],
                hide: { inspector.toggle(.chat) }
            )
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: VeetSpace.s4) {
                        switch inspector.chatTab {
                        case .activity: activity(groups: groups, calls: calls)
                        case .artifacts: artifactList(artifacts)
                        }
                    }
                    .padding(.horizontal, VeetSpace.s3)
                    .padding(.top, VeetSpace.s1)
                    .padding(.bottom, VeetSpace.s6)
                }
                .onChange(of: inspector.revealCount) { reveal(in: groups, proxy) }
                .onAppear { reveal(in: groups, proxy) }
            }
        }
    }

    // MARK: - Activity

    @ViewBuilder
    private func activity(groups: [ToolLedgerGroup], calls: Int) -> some View {
        InspectorSection(title: "Now") {
            InspectorRunCard(
                summary: InspectorRunSummary.chat(
                    runStatus: state.runStatus,
                    awaitingApproval: model.awaitingOwnerApproval,
                    activityLabel: RunActivity.label(
                        isSending: model.isSending,
                        runStatus: state.runStatus,
                        awaitingApproval: model.awaitingOwnerApproval,
                        reasoningActive: state.reasoningActive,
                        reasoningTitle: state.reasoningTitle
                    )
                ),
                facts: [("Tool calls", "\(calls)")]
            )
            .accessibilityIdentifier("inspector.chat.run")
        }
        if let workingState = state.workingState {
            InspectorSection(title: "Working notes") {
                WorkingStateDetails(state: workingState)
                    .inspectorCard()
                    .accessibilityElement(children: .contain)
                    .accessibilityIdentifier("inspector.chat.notes")
            }
        }
        InspectorSection(title: "Tool calls", trailing: calls > 0 ? "\(calls)" : nil) {
            if groups.isEmpty {
                Text("No tool calls yet. When Veetbot searches, browses or runs a tool, each call appears here.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(groups) { group in
                VStack(alignment: .leading, spacing: VeetSpace.s1) {
                    Text(group.title)
                        .veetType(.meta, weight: .semibold)
                        .foregroundColor(AppTheme.inkSoft)
                        .lineLimit(2)
                    ForEach(Array(group.activities.enumerated()), id: \.element.id) { offset, activity in
                        ToolLedgerRow(
                            index: group.firstIndex + offset,
                            activity: activity,
                            approval: state.approvals.first {
                                $0.id == activity.approvalID && !$0.status.isPending
                            },
                            openArtifact: { inspector.openArtifact($0) }
                        )
                    }
                }
                .padding(VeetSpace.s2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(
                    RoundedRectangle(cornerRadius: A2Metrics.tableRadius, style: .continuous)
                        .fill(flashedGroupID == group.id ? AppTheme.tealWash : Color.clear)
                )
                .id(group.id)
                .accessibilityElement(children: .contain)
                .accessibilityIdentifier("inspector.ledger.\(group.id)")
            }
        }
    }

    /// Scrolls to the group holding the strip the owner chose and tints it a
    /// moment. The jump is not animated: after an animated scroll macOS 26
    /// hit-tests the old positions (see EmailThreadScreen's reply jump).
    private func reveal(in groups: [ToolLedgerGroup], _ proxy: ScrollViewProxy) {
        guard inspector.chatTab == .activity, let id = inspector.revealedActivityID,
            let group = ToolLedgerGroups.group(containing: id, in: groups)
        else { return }
        DispatchQueue.main.async { proxy.scrollTo(group.id, anchor: .top) }
        flashedGroupID = group.id
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_400_000_000)
            if flashedGroupID == group.id {
                withAnimation(.easeOut(duration: 0.4)) { flashedGroupID = nil }
            }
        }
    }

    // MARK: - Artifacts

    @ViewBuilder
    private func artifactList(_ artifacts: [ConversationArtifact]) -> some View {
        if artifacts.isEmpty {
            InspectorEmptyState(
                symbol: "doc.on.doc", title: "No files yet",
                message: "Files and images Veetbot makes in this chat appear here."
            )
        } else {
            InspectorSection(title: "In this chat", trailing: "\(artifacts.count)") {
                VStack(spacing: VeetSpace.s1 / 2) {
                    ForEach(artifacts) { artifact in
                        artifactRow(artifact, selected: artifact.id == selected(in: artifacts)?.id)
                    }
                }
                if let artifact = selected(in: artifacts) {
                    ArtifactViewerView(model: model, artifactID: artifact.id, presentation: .embedded)
                        .id(artifact.id)
                        .padding(.top, VeetSpace.s2)
                }
            }
        }
    }

    private func selected(in artifacts: [ConversationArtifact]) -> ConversationArtifact? {
        artifacts.first { $0.id == inspector.selectedArtifactID } ?? artifacts.first
    }

    private func artifactRow(_ artifact: ConversationArtifact, selected: Bool) -> some View {
        Button {
            inspector.selectedArtifactID = artifact.id
        } label: {
            HStack(spacing: VeetSpace.s2) {
                Image(systemName: symbol(for: artifact.mediaType))
                    .veetIcon(.small)
                    .foregroundColor(AppTheme.teal)
                    .frame(width: 28, height: 28)
                    .background(RoundedRectangle(cornerRadius: 6, style: .continuous).fill(AppTheme.surface))
                    .overlay(RoundedRectangle(cornerRadius: 6, style: .continuous).stroke(AppTheme.hairline))
                VStack(alignment: .leading, spacing: 0) {
                    Text(artifact.name)
                        .veetType(.ui, weight: .medium)
                        .foregroundColor(AppTheme.ink)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text("\(artifact.source) · \(artifact.mediaType)")
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkFaint)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, VeetSpace.s2)
            .padding(.vertical, VeetSpace.s1)
            .background(
                RoundedRectangle(cornerRadius: A2Metrics.rowRadius, style: .continuous)
                    .fill(selected ? AppTheme.selected : Color.clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("inspector.artifact.\(artifact.id.uuidString)")
    }

    private func symbol(for mediaType: String) -> String {
        if mediaType.hasPrefix("image/") { return "photo" }
        if mediaType == "text/csv" { return "tablecells" }
        if mediaType.hasPrefix("text/") { return "doc.text" }
        return "doc"
    }
}
#endif
