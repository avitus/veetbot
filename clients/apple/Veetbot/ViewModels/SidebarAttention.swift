import Foundation

/// The sidebar's "Needs you" group: what the owner has to answer or read,
/// ahead of every other conversation. An approval the owner has not decided
/// comes first; an unread scheduled report second; a conversation with both
/// is listed once, as the approval. Rows keep the history's order.
public enum SidebarAttention {
    public enum Kind: Equatable, Sendable {
        case approval
        case report
    }

    public struct Row: Identifiable, Equatable, Sendable {
        public let kind: Kind
        public let entry: SessionHistoryEntry

        public init(kind: Kind, entry: SessionHistoryEntry) {
            self.kind = kind
            self.entry = entry
        }

        public var id: String {
            "\(kind == .approval ? "approval" : "report").\(entry.sessionID.uuidString)"
        }

        public var label: String {
            switch kind {
            case .approval: return "Approval waiting"
            case .report: return "New report"
            }
        }
    }

    public static func rows(
        history: [SessionHistoryEntry],
        sessionsAwaitingApproval: Set<UUID>,
        hasUnreadReport: (SessionHistoryEntry) -> Bool
    ) -> [Row] {
        let approvals = history
            .filter { sessionsAwaitingApproval.contains($0.sessionID) }
            .map { Row(kind: .approval, entry: $0) }
        let reports = history
            .filter { !sessionsAwaitingApproval.contains($0.sessionID) && hasUnreadReport($0) }
            .map { Row(kind: .report, entry: $0) }
        return approvals + reports
    }
}
