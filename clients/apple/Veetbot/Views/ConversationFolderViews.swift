import SwiftUI

/// What the folder name sheet is for: a new folder, optionally filing one
/// conversation on creation, or renaming an existing one.
enum FolderEditorRequest: Identifiable {
    case create(moving: UUID?)
    case rename(FolderView)

    var id: String {
        switch self {
        case .create(let moving):
            return "create-\(moving?.uuidString ?? "none")"
        case .rename(let folder):
            return "rename-\(folder.id.uuidString)"
        }
    }
}

/// A folder section's header: name and count, with rename and delete in the
/// context menu (a right-click on Mac, a long press on iPhone and iPad). A
/// disclosure-group label collapses into one accessibility element on macOS,
/// so the header is one combined element carrying the folder identifier.
struct FolderSectionLabel: View {
    let folder: FolderView
    let count: Int
    let onRename: () -> Void
    let onDelete: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Label(folder.name, systemImage: "folder")
                .lineLimit(1)
            Spacer()
            Text("\(count)")
                .appFont(.caption)
                .foregroundColor(.secondary)
                .accessibilityHidden(true)
        }
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine)
        .accessibilityLabel(folder.name)
        .accessibilityIdentifier("sidebar.folder.\(folder.id.uuidString)")
        .contextMenu {
            Button("Rename…", action: onRename)
                .accessibilityIdentifier("sidebar.folder.rename")
            Button("Delete Folder", role: .destructive, action: onDelete)
                .accessibilityIdentifier("sidebar.folder.delete")
        }
    }
}

/// One open grouping proposal with its accept and decline controls, in the
/// same shape as the persona editor's nomination rows.
struct SuggestedFolderRow: View {
    let proposal: FolderProposalPresentation
    @ObservedObject var model: ChatViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(proposal.headline)
                .appFont(.headline)
            if !memberSummary.isEmpty {
                Text(memberSummary)
                    .appFont(.caption)
                    .foregroundColor(.secondary)
                    .lineLimit(2)
            }
            HStack {
                Button("Accept") {
                    Task { await model.acceptFolderProposal(proposal.id) }
                }
                .buttonStyle(.borderedProminent)
                .accessibilityIdentifier("sidebar.proposal.accept.\(proposal.id.uuidString)")
                Button("Decline") {
                    Task { await model.declineFolderProposal(proposal.id) }
                }
                .buttonStyle(.bordered)
                .accessibilityIdentifier("sidebar.proposal.decline.\(proposal.id.uuidString)")
            }
        }
        .padding(.vertical, 2)
        .disabled(model.pendingFolderProposalIDs.contains(proposal.id))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("sidebar.proposal.\(proposal.id.uuidString)")
    }

    private var memberSummary: String {
        var parts = proposal.memberTitles
        if proposal.unresolvedMemberCount > 0 {
            parts.append("+\(proposal.unresolvedMemberCount) more")
        }
        return parts.joined(separator: ", ")
    }
}

/// The create and rename sheet. A refused or duplicate name is shown inline,
/// where it was typed, and the sheet stays open; nothing reaches the global
/// error banner. A text field in an alert needs iOS 16, so this is a sheet.
struct FolderNameSheet: View {
    let request: FolderEditorRequest
    @ObservedObject var model: ChatViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var saving = false

    var body: some View {
        NavigationView {
            Form {
                TextField("Folder name", text: $name)
                    .accessibilityIdentifier("folder.name")
                if let error = model.folderEditorError {
                    Text(error)
                        .foregroundColor(.red)
                        .accessibilityIdentifier("folder.error")
                }
            }
            .navigationTitle(title)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") {
                        model.clearFolderEditorError()
                        dismiss()
                    }
                    .accessibilityIdentifier("folder.cancel")
                }
                ToolbarItem(placement: .primaryAction) {
                    Button("Save") { save() }
                        .disabled(!canSave)
                        .accessibilityIdentifier("folder.save")
                }
            }
        }
        .onAppear {
            model.clearFolderEditorError()
            name = initialName
        }
        #if os(macOS)
        .frame(minWidth: 380, idealWidth: 440, minHeight: 180, idealHeight: 220)
        #endif
    }

    private var initialName: String {
        if case .rename(let folder) = request { return folder.name }
        return ""
    }

    private var title: String {
        if case .rename = request { return "Rename Folder" }
        return "New Folder"
    }

    private var trimmed: String {
        name.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private var canSave: Bool {
        !saving && !trimmed.isEmpty && trimmed != initialName
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
            }
        }
    }
}
