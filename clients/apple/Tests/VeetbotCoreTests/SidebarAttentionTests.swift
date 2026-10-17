import Foundation
import Testing

@testable import VeetbotCore

/// The sidebar's "Needs you" group: every conversation with an approval the
/// owner has not answered, then every scheduled report the owner has not read,
/// each in the history's order and never listed twice.
@Suite struct SidebarAttentionTests {
    private let approvalSession = UUID()
    private let reportSession = UUID()
    private let quietSession = UUID()
    private let scheduleID = UUID()

    private var history: [SessionHistoryEntry] {
        [
            entry(reportSession, title: "Weekday briefing", scheduleID: scheduleID),
            entry(quietSession, title: "Dentist appointment moved"),
            entry(approvalSession, title: "Tahoe weekend"),
        ]
    }

    @Test
    func testApprovalsComeBeforeReportsInHistoryOrder() {
        let rows = SidebarAttention.rows(
            history: history,
            sessionsAwaitingApproval: [approvalSession],
            hasUnreadReport: { $0.sessionID == reportSession }
        )

        #expect(rows.map(\.kind) == [.approval, .report])
        #expect(rows.map(\.entry.sessionID) == [approvalSession, reportSession])
        #expect(rows[0].label == "Approval waiting")
        #expect(rows[1].label == "New report")
        #expect(rows[0].id == "approval.\(approvalSession.uuidString)")
    }

    @Test
    func testAConversationNeedingBothIsListedOnceAsAnApproval() {
        let rows = SidebarAttention.rows(
            history: history,
            sessionsAwaitingApproval: [reportSession],
            hasUnreadReport: { $0.sessionID == reportSession }
        )

        #expect(rows.count == 1)
        #expect(rows[0].kind == .approval)
    }

    @Test
    func testAnApprovalForAnUnknownSessionIsNotListed() {
        let rows = SidebarAttention.rows(
            history: history,
            sessionsAwaitingApproval: [UUID()],
            hasUnreadReport: { _ in false }
        )

        #expect(rows.isEmpty)
    }

    private func entry(_ sessionID: UUID, title: String, scheduleID: UUID? = nil) -> SessionHistoryEntry {
        SessionHistoryEntry(
            sessionID: sessionID, title: title, agentID: "default",
            createdAt: Date(timeIntervalSince1970: 0), updatedAt: Date(timeIntervalSince1970: 0),
            lastRunID: UUID(), scheduleID: scheduleID
        )
    }
}
