import SwiftUI

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

/// A folder's row in the sidebar: name, count, and a chevron. Clicking or
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
            HStack(spacing: 8) {
                Label(folder.name, systemImage: isExpanded ? "folder.fill" : "folder")
                    .lineLimit(1)
                Spacer()
                Text("\(count)")
                    .appFont(.caption)
                    .foregroundColor(.secondary)
                Image(systemName: "chevron.right")
                    .appFont(.caption, weight: .semibold)
                    .foregroundColor(.secondary)
                    .rotationEffect(.degrees(isExpanded ? 90 : 0))
            }
            .contentShape(Rectangle())
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

/// One open grouping proposal: the folder it proposes, every conversation it
/// would file on a line of its own, and accept, rename, and decline controls.
struct SuggestedFolderRow: View {
    let proposal: FolderProposalPresentation
    @ObservedObject var model: ChatViewModel
    let onRename: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(proposal.headline)
                .appFont(.headline)
                .fixedSize(horizontal: false, vertical: true)
            FolderProposalMembers(proposal: proposal)
            HStack(spacing: 8) {
                Button("Accept") {
                    Task { await model.acceptFolderProposal(proposal.id) }
                }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("sidebar.proposal.accept.\(proposal.id.uuidString)")
                if proposal.kind == .newFolder {
                    Button("Rename…", action: onRename)
                        .buttonStyle(.bordered)
                        .accessibilityHint("Accepts the folder under a name you choose")
                        .accessibilityIdentifier("sidebar.proposal.rename.\(proposal.id.uuidString)")
                }
                Button("Decline") {
                    Task { await model.declineFolderProposal(proposal.id) }
                }
                .buttonStyle(.bordered)
                .accessibilityIdentifier("sidebar.proposal.decline.\(proposal.id.uuidString)")
            }
            #if os(macOS)
            .controlSize(.small)
            #endif
        }
        .padding(.vertical, 4)
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
            VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(proposal.memberTitles.enumerated()), id: \.offset) { _, title in
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        Image(systemName: "bubble.left")
                            .foregroundColor(.secondary)
                            .accessibilityHidden(true)
                        Text(title)
                            .lineLimit(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .appFont(.subheadline)
                }
                if proposal.unresolvedMemberCount > 0 {
                    Text(
                        proposal.unresolvedMemberCount == 1
                            ? "1 more conversation"
                            : "\(proposal.unresolvedMemberCount) more conversations"
                    )
                    .appFont(.caption)
                    .foregroundColor(.secondary)
                }
            }
        }
    }
}

/// The create, rename, and accept-under-a-name sheet. A refused or duplicate
/// name is shown inline, where it was typed, and the sheet stays open; nothing
/// reaches the global error banner. A text field in an alert needs iOS 16, so
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
        VStack(alignment: .leading, spacing: 12) {
            Text(title)
                .appFont(.headline)
            TextField("Folder name", text: $name)
                .textFieldStyle(.roundedBorder)
                .onSubmit { if canSave { save() } }
                .accessibilityIdentifier("folder.name")
            if let error = model.folderEditorError {
                errorText(error)
            }
            if let proposal = acceptedProposal {
                Text("Files these conversations:")
                    .appFont(.subheadline)
                    .foregroundColor(.secondary)
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
            .padding(.top, 4)
        }
        .padding(20)
        .sheetFrame(minWidth: 320, macMinWidth: 380, idealWidth: 420, minHeight: 0)
    }
    #else
    private var content: some View {
        NavigationView {
            Form {
                Section {
                    TextField("Folder name", text: $name)
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
            .foregroundColor(.red)
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
