import SwiftUI

/// The Recent section's label, which opens and closes the shortcuts; they
/// stay reachable even while every folder is closed.
struct RecentChatsHeaderRow: View {
    let isExpanded: Bool
    let onToggle: () -> Void

    var body: some View {
        SidebarSectionLabel(
            title: "Recent", isExpanded: isExpanded, onToggle: onToggle,
            toggleLabel: "Recent chats", toggleIdentifier: "sidebar.recent-chats"
        )
    }
}

/// What the folder name sheet is for: a new folder, optionally filing one
/// conversation on creation, renaming an existing one, or accepting a
/// suggested folder under a name the owner may change first.
enum FolderEditorRequest: Identifiable {
    case create(moving: UUID?)
    case rename(FolderView)
    case acceptProposal(FolderProposalPresentation)

    var id: String {
        switch self {
        case .create(let moving):
            return "create-\(moving?.uuidString ?? "none")"
        case .rename(let folder):
            return "rename-\(folder.id.uuidString)"
        case .acceptProposal(let proposal):
            return "accept-\(proposal.id.uuidString)"
        }
    }
}

/// A folder's row in the sidebar: a chevron in the icon column, the name, and
/// the count in the trailing slot. Clicking or
/// tapping it expands or collapses the folder, and its conversations follow as
/// ordinary rows in the same section, so folders sit as close together as
/// conversations do. Expansion is the device's remembered state, never a list
/// control's own, so a proposal or a refresh cannot reopen a closed folder. The
/// context menu (a right-click on Mac, a long press on iPhone and iPad) renames
/// or deletes the folder and switches solo mode.
struct FolderHeaderRow: View {
    let folder: FolderView
    let count: Int
    let isExpanded: Bool
    let solo: Bool
    let onToggle: () -> Void
    let onRename: () -> Void
    let onDelete: () -> Void
    let onSetSolo: (Bool) -> Void

    var body: some View {
        Button(action: onToggle) {
            SidebarRow {
                SidebarChevron(isExpanded: isExpanded)
            } title: {
                Text(folder.name)
            } trailing: { _ in
                Text("\(count)")
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(folder.name)
        .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
        .accessibilityHint(isExpanded ? "Collapses the folder" : "Expands the folder")
        .accessibilityIdentifier("sidebar.folder.\(folder.id.uuidString)")
        .contextMenu {
            Button("Rename…", action: onRename)
                .accessibilityIdentifier("sidebar.folder.rename")
            Button("Delete Folder", role: .destructive, action: onDelete)
                .accessibilityIdentifier("sidebar.folder.delete")
            Divider()
            Toggle("Solo Mode", isOn: Binding(get: { solo }, set: onSetSolo))
                .accessibilityIdentifier("sidebar.folder.solo")
        }
    }
}

/// A schedule's row in the sidebar's Scheduled section: a chevron, its most
/// recent session's title, and its count beside any New report dot. It
/// toggles like a folder row but has no context menu; the group is the
/// scheduler's, not the owner's.
struct ScheduleGroupHeaderRow: View {
    let group: ScheduledConversationGroup
    let isExpanded: Bool
    var hasUnreadReport = false
    let onToggle: () -> Void

    var body: some View {
        Button(action: onToggle) {
            SidebarRow {
                SidebarChevron(isExpanded: isExpanded)
            } title: {
                Text(group.title)
            } trailing: { _ in
                HStack(spacing: VeetSpace.s2) {
                    if hasUnreadReport { NewReportIndicator() }
                    Text("\(group.entries.count)")
                }
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(group.title), \(group.entries.count) scheduled conversations\(hasUnreadReport ? ", New report" : "")")
        .accessibilityValue(isExpanded ? "Expanded" : "Collapsed")
        .accessibilityHint(isExpanded ? "Collapses the schedule" : "Expands the schedule")
        .accessibilityIdentifier("sidebar.schedule.\(group.scheduleID.uuidString)")
    }
}

/// One open grouping proposal: the folder it proposes, every conversation it
/// would file on a line of its own, and accept, rename, and decline controls.
struct SuggestedFolderRow: View {
    let proposal: FolderProposalPresentation
    @ObservedObject var model: ChatViewModel
    let onRename: () -> Void

    @ViewBuilder
    private var answers: some View {
        Button("Accept") {
            Task { await model.acceptFolderProposal(proposal.id) }
        }
        .buttonStyle(PrimaryCapsuleButtonStyle())
        .fixedSize()
        .accessibilityIdentifier("sidebar.proposal.accept.\(proposal.id.uuidString)")
        if proposal.kind == .newFolder {
            Button("Rename…", action: onRename)
                .buttonStyle(SecondaryCapsuleButtonStyle())
                .fixedSize()
                .accessibilityHint("Accepts the folder under a name you choose")
                .accessibilityIdentifier("sidebar.proposal.rename.\(proposal.id.uuidString)")
        }
        Button("Decline") {
            Task { await model.declineFolderProposal(proposal.id) }
        }
        .buttonStyle(QuietCapsuleButtonStyle())
        .fixedSize()
        .accessibilityIdentifier("sidebar.proposal.decline.\(proposal.id.uuidString)")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            Text(proposal.headline)
                .veetType(.ui, weight: .medium)
                .fixedSize(horizontal: false, vertical: true)
            FolderProposalMembers(proposal: proposal)
            // The three answers share a line where the sidebar is wide enough
            // and stack where it is not, so no label is cut short.
            ViewThatFits(in: .horizontal) {
                HStack(spacing: VeetSpace.s2) { answers }
                VStack(alignment: .leading, spacing: VeetSpace.s1) { answers }
            }
        }
        .padding(.vertical, VeetSpace.s1)
        .disabled(model.pendingFolderProposalIDs.contains(proposal.id))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("sidebar.proposal.\(proposal.id.uuidString)")
    }
}

/// The conversations a proposal would file, one per line so each can be read
/// in full on a narrow iPhone sidebar rather than truncated into one list.
struct FolderProposalMembers: View {
    let proposal: FolderProposalPresentation

    var body: some View {
        if !proposal.memberTitles.isEmpty || proposal.unresolvedMemberCount > 0 {
            VStack(alignment: .leading, spacing: VeetSpace.s1) {
                ForEach(Array(proposal.memberTitles.enumerated()), id: \.offset) { _, title in
                    HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
                        Image(systemName: "bubble.left")
                            .foregroundColor(AppTheme.inkSoft)
                            .accessibilityHidden(true)
                        Text(title)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .veetType(.meta)
                }
                if proposal.unresolvedMemberCount > 0 {
                    Text(
                        proposal.unresolvedMemberCount == 1
                            ? "1 more conversation"
                            : "\(proposal.unresolvedMemberCount) more conversations"
                    )
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                }
            }
        }
    }
}

/// The create, rename, and accept-under-a-name sheet. A refused or duplicate
/// name is shown inline, where it was typed, and the sheet stays open; nothing
/// reaches the global error banner. An alert could not keep that inline, so
/// this is a sheet: a form under a navigation bar on iPhone and iPad, and on
/// Mac a compact dialog sized to its field, with Cancel and the default action
/// on the keyboard's Escape and Return.
struct FolderNameSheet: View {
    let request: FolderEditorRequest
    @ObservedObject var model: ChatViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var saving = false

    var body: some View {
        content
            .onAppear {
                model.clearFolderEditorError()
                name = initialName
            }
    }

    #if os(macOS)
    private var content: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s3) {
            Text(title)
                .veetType(.title)
            TextField("Folder name", text: $name, prompt: .a2Prompt("Folder name"))
                .textFieldStyle(.roundedBorder)
                .onSubmit { if canSave { save() } }
                .accessibilityIdentifier("folder.name")
            if let error = model.folderEditorError {
                errorText(error)
            }
            if let proposal = acceptedProposal {
                Text("Files these conversations:")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                FolderProposalMembers(proposal: proposal)
            }
            HStack {
                Spacer()
                Button("Cancel", action: cancel)
                    .keyboardShortcut(.cancelAction)
                    .accessibilityIdentifier("folder.cancel")
                Button(saveTitle, action: save)
                    .keyboardShortcut(.defaultAction)
                    .disabled(!canSave)
                    .accessibilityIdentifier("folder.save")
            }
            .padding(.top, VeetSpace.s1)
        }
        .padding(VeetSpace.s5)
        .sheetFrame(minWidth: 320, macMinWidth: 380, idealWidth: 420, minHeight: 0)
    }
    #else
    private var content: some View {
        NavigationView {
            Form {
                Section {
                    TextField("Folder name", text: $name, prompt: .a2FormPrompt("Folder name"))
                        .accessibilityIdentifier("folder.name")
                    if let error = model.folderEditorError {
                        errorText(error)
                    }
                }
                if let proposal = acceptedProposal {
                    Section("Files these conversations") {
                        FolderProposalMembers(proposal: proposal)
                    }
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel", action: cancel)
                        .accessibilityIdentifier("folder.cancel")
                }
                ToolbarItem(placement: .primaryAction) {
                    Button(saveTitle, action: save)
                        .disabled(!canSave)
                        .accessibilityIdentifier("folder.save")
                }
            }
        }
    }
    #endif

    private func errorText(_ error: String) -> some View {
        Text(error)
            .foregroundColor(AppTheme.red)
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityIdentifier("folder.error")
    }

    private var acceptedProposal: FolderProposalPresentation? {
        if case .acceptProposal(let proposal) = request { return proposal }
        return nil
    }

    private var initialName: String {
        switch request {
        case .create: return ""
        case .rename(let folder): return folder.name
        case .acceptProposal(let proposal): return proposal.proposedName ?? ""
        }
    }

    private var title: String {
        switch request {
        case .create: return "New Folder"
        case .rename: return "Rename Folder"
        case .acceptProposal: return "Accept Folder"
        }
    }

    private var saveTitle: String {
        if case .acceptProposal = request { return "Accept" }
        return "Save"
    }

    private var trimmed: String {
        name.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// A rename must change the name; accepting may keep the proposed one.
    private var canSave: Bool {
        guard !saving, !trimmed.isEmpty else { return false }
        if case .rename(let folder) = request { return trimmed != folder.name }
        return true
    }

    private func cancel() {
        model.clearFolderEditorError()
        dismiss()
    }

    private func save() {
        saving = true
        Task { @MainActor in
            defer { saving = false }
            switch request {
            case .create(let moving):
                if await model.createFolder(named: trimmed, moving: moving) != nil {
                    dismiss()
                }
            case .rename(let folder):
                if await model.renameFolder(folder.id, to: trimmed) {
                    dismiss()
                }
            case .acceptProposal(let proposal):
                if await model.acceptFolderProposal(proposal.id, name: trimmed) {
                    dismiss()
                }
            }
        }
    }
}

/// Quiet shared-state cue; VoiceOver does not have to infer meaning from color.
struct NewReportIndicator: View {
    var body: some View {
        Circle()
            .fill(AppTheme.teal)
            .frame(width: 7, height: 7)
            .accessibilityLabel("New report")
    }
}
