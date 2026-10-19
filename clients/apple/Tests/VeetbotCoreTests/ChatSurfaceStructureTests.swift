import Foundation
import Testing

@testable import VeetbotCore

/// The A2 Dock Chat surface: what the owner needs sits above the composer,
/// the transcript says so in one line, and execution detail stays on demand.
@Suite struct ChatSurfaceStructureTests {
    @Test
    func testPendingAttentionDocksAboveTheComposer() throws {
        let source = try source(at: "Veetbot/Views/ChatView.swift")

        // The dock floats over the transcript's end as a measured overlay; a
        // bottom safe-area inset holding the card left the window undrawn.
        #expect(source.contains("ZStack(alignment: .bottom)"))
        #expect(source.contains("onPreferenceChange(DockHeightKey.self)"))
        #expect(!source.contains(".safeAreaInset(edge: .bottom"))
        #expect(source.contains("AttentionDock("))
        #expect(source.contains("WaitingLine(text: \"Waiting for your approval. See below.\")"))
        #expect(source.contains("WaitingLine(text: \"Waitin" + "g for your answer. See below.\")"))
        #expect(source.contains(".accessibilityIdentifier(\"chat.attention-dock\")"))
        #expect(source.contains(".accessibilityIdentifier(\"chat.composer\")"))
        #expect(source.contains(".accessibilityIdentifier(\"chat.attach\")"))
        #expect(source.contains(".accessibilityIdentifier(\"chat.websiteAccess\")"))
    }

    @Test
    func testComposerIsATranslucentCapsuleAndMessagesFollowTheReference() throws {
        let source = try source(at: "Veetbot/Views/ChatView.swift")

        #expect(source.contains("Material"))
        #expect(source.contains("AppTheme.tealWash"))
        #expect(source.contains("AssistantMark("))
        #expect(source.contains("appDisplayFont("))
    }

    @Test
    func testToolRunsCollapseToOneSentenceWithAMonospaceLedger() throws {
        let source = try source(at: "Veetbot/Views/ActivityViews.swift")

        #expect(source.contains("struct WorkStrip"))
        #expect(source.contains("struct ToolLedgerRow"))
        #expect(source.contains(".appCodeFont("))
        #expect(source.contains("\"tool.bundle.\\(bundle.id)\""))
        #expect(source.contains(".accessibilityIdentifier(summaryIdentifier)"))
        #expect(source.contains(".accessibilityIdentifier(\"tool.detail.\\(activity.id)\")"))
        #expect(source.contains("stroke(AppTheme.amberLine)"))
        #expect(source.contains(".accessibilityIdentifier(\"approval.details\")"))
        #expect(source.contains("Approval checkpoint"))
        #expect(source.contains("Clarifying question"))
    }

    @Test
    func testTheSidebarListsWhatNeedsTheOwnerFirst() throws {
        let source = try source(at: "Veetbot/Views/RootView.swift")

        #expect(source.contains("SidebarAttention.rows("))
        #expect(source.contains("\"sidebar.needs-you.\\(row.id)\""))
        // The ten-second attention loop also reads the server's pending approvals.
        #expect(source.contains("await model.refreshSessionsAwaitingApproval()"))
        #expect(source.contains("AppTheme.paperDeep"))
        #expect(source.contains(".accessibilityIdentifier(\"sidebar.new-conversation\")"))
        let folderViews = try self.source(at: "Veetbot/Views/ConversationFolderViews.swift")
        #expect(folderViews.contains(".accessibilityIdentifier(\"sidebar.recent-chats\")"))
        #expect(folderViews.contains("AppTheme.inkSoft"))
    }

    @Test
    func testTheApprovalCardOffersItsThreeDecisionsInTheReferenceOrder() throws {
        let source = try source(at: "Veetbot/Views/BrowserActionApprovalCard.swift")

        #expect(source.contains("Needs your approval"))
        #expect(source.contains(".accessibilityIdentifier(\"approval.allow-once\")"))
        #expect(source.contains(".accessibilityIdentifier(\"approval.allow-for-task\")"))
        #expect(source.contains(".accessibilityIdentifier(\"approval.deny\")"))
        #expect(source.contains(".accessibilityIdentifier(\"approval.task-scope\")"))
        #expect(source.contains(".attentionCard()"))
        #expect(source.contains("ApprovalDecisionButtons"))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
