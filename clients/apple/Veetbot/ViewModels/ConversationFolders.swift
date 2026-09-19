import Foundation

/// One folder section of the sidebar: the folder and the cached conversations filed in it.
public struct ConversationFolderSection: Identifiable, Equatable, Sendable {
    public let folder: FolderView
    public let entries: [SessionHistoryEntry]

    public var id: UUID { folder.id }

    public init(folder: FolderView, entries: [SessionHistoryEntry]) {
        self.folder = folder
        self.entries = entries
    }
}

/// The sidebar's grouping of the cached history, derived on every render so it
/// can never drift from the history it is computed from. Folders sort by their
/// name, case-insensitively; entries keep the history's activity order; an
/// entry whose folder the server no longer lists renders as unfiled until the
/// next reconciliation; and an unavailable folder surface flattens everything.
public struct GroupedConversationHistory: Equatable, Sendable {
    public let uncategorized: [SessionHistoryEntry]
    public let folders: [ConversationFolderSection]

    public init(uncategorized: [SessionHistoryEntry], folders: [ConversationFolderSection]) {
        self.uncategorized = uncategorized
        self.folders = folders
    }

    public static func make(
        history: [SessionHistoryEntry],
        folders: [FolderView],
        available: Bool
    ) -> GroupedConversationHistory {
        guard available else {
            return GroupedConversationHistory(uncategorized: history, folders: [])
        }
        let known = Set(folders.map(\.id))
        var filed: [UUID: [SessionHistoryEntry]] = [:]
        var uncategorized: [SessionHistoryEntry] = []
        for entry in history {
            if let folderID = entry.folderID, known.contains(folderID) {
                filed[folderID, default: []].append(entry)
            } else {
                uncategorized.append(entry)
            }
        }
        let sections = folders.sorted(by: folderOrder).map { folder in
            ConversationFolderSection(folder: folder, entries: filed[folder.id] ?? [])
        }
        return GroupedConversationHistory(uncategorized: uncategorized, folders: sections)
    }

    /// Case-insensitive name order with the identifier as the tie-break.
    public static func folderOrder(_ left: FolderView, _ right: FolderView) -> Bool {
        switch left.name.localizedCaseInsensitiveCompare(right.name) {
        case .orderedAscending: return true
        case .orderedDescending: return false
        case .orderedSame: return left.id.uuidString < right.id.uuidString
        }
    }
}

/// A proposal as the sidebar shows it: a headline and the member titles it can
/// resolve from the cached history. A kind this build does not know is hidden.
/// A new-folder proposal also carries the name the owner may change before
/// accepting.
public struct FolderProposalPresentation: Identifiable, Equatable, Sendable {
    public let id: UUID
    public let kind: FolderProposalKind
    public let headline: String
    public let memberTitles: [String]
    public let unresolvedMemberCount: Int
    public let proposedName: String?

    public init(
        id: UUID,
        kind: FolderProposalKind,
        headline: String,
        memberTitles: [String],
        unresolvedMemberCount: Int,
        proposedName: String? = nil
    ) {
        self.id = id
        self.kind = kind
        self.headline = headline
        self.memberTitles = memberTitles
        self.unresolvedMemberCount = unresolvedMemberCount
        self.proposedName = proposedName
    }

    public static func make(
        proposals: [FolderProposalView],
        folders: [FolderView],
        history: [SessionHistoryEntry]
    ) -> [FolderProposalPresentation] {
        let folderNames = Dictionary(
            folders.map { ($0.id, $0.name) }, uniquingKeysWith: { first, _ in first }
        )
        let titles = Dictionary(
            history.map { ($0.sessionID, $0.title) }, uniquingKeysWith: { first, _ in first }
        )
        return proposals.compactMap { proposal in
            guard let kind = proposal.kindValue else { return nil }
            let headline: String
            switch kind {
            case .newFolder:
                headline = "New folder “\(proposal.proposedName ?? "Untitled")”"
            case .addToFolder:
                if let target = proposal.targetFolderID, let name = folderNames[target] {
                    headline = "Add to “\(name)”"
                } else {
                    headline = "Add to a folder"
                }
            }
            let resolved = proposal.memberSessionIDs.compactMap { titles[$0] }
            return FolderProposalPresentation(
                id: proposal.id,
                kind: kind,
                headline: headline,
                memberTitles: resolved,
                unresolvedMemberCount: proposal.memberSessionIDs.count - resolved.count,
                proposedName: kind == .newFolder ? proposal.proposedName : nil
            )
        }
    }
}

/// Which folders this device shows expanded. In solo mode, the default, at most
/// one folder is open and expanding one collapses the rest, so a folder the
/// owner has not opened stays closed. Without solo, every folder is open until
/// the owner closes it. Only the owner's own toggles change this: a folder or
/// proposal arriving from the server never does.
public struct FolderExpansionState: Equatable, Codable, Sendable {
    public private(set) var solo: Bool
    /// In solo mode, the one open folder, if any.
    private var soloExpandedID: UUID?
    /// Without solo, the folders the owner has closed.
    private var collapsedIDs: Set<UUID>

    public init(solo: Bool = true) {
        self.solo = solo
        soloExpandedID = nil
        collapsedIDs = []
    }

    public func isExpanded(_ folderID: UUID) -> Bool {
        solo ? soloExpandedID == folderID : !collapsedIDs.contains(folderID)
    }

    public mutating func setExpanded(_ expanded: Bool, folder folderID: UUID) {
        if solo {
            if expanded {
                soloExpandedID = folderID
            } else if soloExpandedID == folderID {
                soloExpandedID = nil
            }
        } else if expanded {
            collapsedIDs.remove(folderID)
        } else {
            collapsedIDs.insert(folderID)
        }
    }

    /// Switches mode, keeping what is on screen as far as the new rule allows:
    /// turning solo on keeps the first open folder in `order` open, and turning
    /// it off keeps the open folder open and every other listed folder closed.
    public mutating func setSolo(_ enabled: Bool, order: [UUID]) {
        guard enabled != solo else { return }
        if enabled {
            soloExpandedID = order.first { !collapsedIDs.contains($0) }
            collapsedIDs = []
        } else {
            collapsedIDs = Set(order.filter { $0 != soloExpandedID })
            soloExpandedID = nil
        }
        solo = enabled
    }

    /// Forgets folders the server no longer lists, so the remembered state
    /// stays bounded by the folders that exist.
    public mutating func retain(only folderIDs: Set<UUID>) {
        if let open = soloExpandedID, !folderIDs.contains(open) {
            soloExpandedID = nil
        }
        collapsedIDs.formIntersection(folderIDs)
    }
}

/// The sidebar's folder expansion, remembered on this device and never sent to
/// the server. An unreadable stored value falls back to the default.
@MainActor
public final class FolderSidebarPreferences: ObservableObject {
    static let expansionKey = "veetbot.folders.expansion"

    @Published public var expansion: FolderExpansionState {
        didSet {
            guard expansion != oldValue,
                let data = try? JSONEncoder().encode(expansion)
            else { return }
            defaults.set(data, forKey: Self.expansionKey)
        }
    }

    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        expansion = defaults.data(forKey: Self.expansionKey)
            .flatMap { try? JSONDecoder().decode(FolderExpansionState.self, from: $0) }
            ?? FolderExpansionState()
    }
}
