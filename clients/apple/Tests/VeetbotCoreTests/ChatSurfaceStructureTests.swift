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
        // bottom safe-area inset holding the card left the window undrawn. The
        // dock is measured directly: its height as a last-value preference
        // arrived as zero and the composer covered the end of every answer.
        #expect(source.contains("ZStack(alignment: .bottom)"))
        #expect(source.contains("dockHeight = height"))
        #expect(!source.contains("DockHeightKey"))
        #expect(!source.contains(".safeAreaInset(edge: .bottom"))
        #expect(source.contains("AttentionDock("))
        #expect(source.contains("WaitingLine(text: \"Waiting for your approval. See below.\")"))
        #expect(source.contains("WaitingLine(text: \"Waitin" + "g for your answer. See below.\")"))
        #expect(source.contains(".accessibilityIdentifier(\"chat.attention-dock\")"))
        #expect(source.contains(".accessibilityIdentifier(\"chat.composer\")"))
        #expect(source.contains(".accessibilityIdentifier(\"chat.attach\")"))
        #expect(source.contains(".accessibilityIdentifier(\"chat.websiteAccess\")"))
    }

    /// ADR-0280: a sign-in pause is a card with a sign-in button, not a
    /// question, and Website Access stays in the toolbar on every platform.
    @Test
    func testSignInPauseShowsASignInCardAndWebsiteAccessStaysInTheToolbar() throws {
        let chat = try source(at: "Veetbot/Views/ChatView.swift")
        let activity = try source(at: "Veetbot/Views/ActivityViews.swift")

        #expect(chat.contains("if prompt.signIn != nil {"))
        #expect(chat.contains("SignInRequiredCard("))
        #expect(chat.contains(".deviceSignInPresentation(model: model, surface: .chat)"))
        #expect(chat.contains("WaitingLine(text: \"Waiting for you to sign in. See below.\")"))
        #expect(activity.contains("struct SignInRequiredCard"))
        #expect(activity.contains(".accessibilityIdentifier(\"sign-in-card.sign-in\")"))
        let card = try #require(activity.range(of: "struct SignInRequiredCard"))
        let cardEnd = try #require(activity.range(of: "struct WorkingStatePanel"))
        #expect(!activity[card.lowerBound..<cardEnd.lowerBound].contains("TextField"))
        let canSend = try #require(chat.range(of: "var canSendDraft: Bool"))
        #expect(chat[canSend.lowerBound...].prefix(600).contains("acceptsReply()"))
        // The card and the composer change when the wait or the run's deadline
        // passes, with no event to redraw them.
        #expect(chat.contains("state.clarifyingQuestion?.signIn?.expiresAt"))
        #expect(chat.contains("state.clarifyingQuestion?.signIn?.runDeadlineAt"))
        #expect(chat.contains("resumesByItself: prompt.awaitsVerifiedSignIn()"))
        #expect(chat.contains("ranOutOfTime: prompt.runDeadlinePassed()"))
        let cardSource = activity[card.lowerBound..<cardEnd.lowerBound]
        #expect(cardSource.contains("if !resumesByItself && !ranOutOfTime {"))
        #expect(cardSource.contains("This task ran out of time."))

        let toolbar = try #require(chat.range(of: "struct ChatToolbarActions"))
        #expect(chat[toolbar.lowerBound...].contains(".accessibilityIdentifier(\"chat.websiteAccess\")"))
        #expect(!chat.contains("websiteAccessButton"))
        #expect(!chat.contains("showingWebsiteAccess"))
    }

    @Test
    func testComposerIsATranslucentCapsuleAndMessagesFollowTheReference() throws {
        let source = try source(at: "Veetbot/Views/ChatView.swift")

        #expect(source.contains("Material"))
        #expect(source.contains("AppTheme.tealWash"))
        #expect(source.contains("AssistantMark("))
        #expect(source.contains(".veetType(.display)"))
    }

    @Test
    func testToolRunsCollapseToOneSentenceWithAMonospaceLedger() throws {
        let source = try source(at: "Veetbot/Views/ActivityViews.swift")

        #expect(source.contains("struct WorkStrip"))
        #expect(source.contains("struct ToolLedgerRow"))
        // A bundle's ledger names settled outcomes only; a pending approval is the dock's.
        #expect(source.contains("$0.id == activity.approvalID && !$0.status.isPending"))
        #expect(source.contains(".veetMono("))
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
        #expect(folderViews.contains("toggleIdentifier: \"sidebar.recent-chats\""))
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

    /// ADR-0344 and ADR-0342: a job or script card that carries an offer leads with "Allow for
    /// this request", says in the server's words what that allows, and keeps Approve once
    /// beside it. A card without an offer keeps Approve once as its one way to approve.
    @Test
    func testTheGenericApprovalCardLeadsWithAllowForThisRequestOnlyWhenOffered() throws {
        let source = try source(at: "Veetbot/Views/ActivityViews.swift")
        let start = try #require(source.range(of: "struct ApprovalCard: View")).upperBound
        let end = try #require(source.range(of: "Text(\"Details\")", range: start..<source.endIndex)).lowerBound
        let card = String(source[start..<end])
        let buttons = try #require(card.range(of: "ApprovalDecisionButtons {"))
        let secondary = try #require(card.range(of: "} secondary: {", range: buttons.upperBound..<card.endIndex))
        let deny = try #require(card.range(of: "} deny: {", range: secondary.upperBound..<card.endIndex))
        let beforeButtons = String(card[..<buttons.lowerBound])
        let primarySlot = String(card[buttons.upperBound..<secondary.lowerBound])
        let secondarySlot = String(card[secondary.upperBound..<deny.lowerBound])

        // What the offer allows is the server's text, named for the kind of card that carries it.
        #expect(beforeButtons.contains("if let summary = approval.requestOfferSummary {"))
        #expect(beforeButtons.contains("Text(summary)"))
        #expect(beforeButtons.contains("? \"approval.job-request.summary\" : \"approval.script-request.summary\""))
        // With an offer the first decision allows the request; without one it approves once.
        let offered = try #require(primarySlot.range(of: "if approval.requestOfferSummary != nil {"))
        let otherwise = try #require(primarySlot.range(of: "} else {", range: offered.upperBound..<primarySlot.endIndex))
        let withOffer = String(primarySlot[offered.upperBound..<otherwise.lowerBound])
        let withoutOffer = String(primarySlot[otherwise.upperBound...])
        #expect(withOffer.contains("resolve(.approveForRequest, nil)"))
        #expect(withOffer.contains("title: \"Allow for this request\", decision: .approveForRequest"))
        #expect(withOffer.contains(".accessibilityIdentifier(\"approval.allow-for-request\")"))
        #expect(withoutOffer.contains("resolve(.approveOnce, nil)"))
        #expect(withoutOffer.contains("title: \"Approve once\", decision: .approveOnce"))
        #expect(!withoutOffer.contains(".approveForRequest"))
        // Approve once stays beside the offer, and is not shown twice on a card without one.
        #expect(secondarySlot.contains("if approval.requestOfferSummary != nil {"))
        #expect(secondarySlot.contains("resolve(.approveOnce, nil)"))
        #expect(secondarySlot.contains(".accessibilityIdentifier(\"approval.allow-once\")"))
        #expect(!secondarySlot.contains("} else {"))
        #expect(!secondarySlot.contains(".approveForRequest"))
        #expect(ApprovalDecision.approveForRequest.progressLabel(websiteCard: false) == "Approving…")
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
