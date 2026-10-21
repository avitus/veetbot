import Foundation
import Testing

@testable import VeetbotCore

/// The A2 Dock Email surface: the thread list reads like the reference rows,
/// the detail opens with a serif subject and Veetbot's gist behind a teal
/// rule, feedback lives in one Teach Veetbot card, and every control keeps
/// the identifier the journeys drive.
@Suite struct EmailSurfaceStructureTests {
    @Test
    func testReferenceNavigationAndDirectActionsArePresent() throws {
        let email = try source(at: "Veetbot/Views/EmailModeView.swift")
        let people = try source(at: "Veetbot/Views/PeopleBrowserView.swift")
        let persona = try source(at: "Veetbot/Views/PersonaEditorView.swift")
        let subscriptions = try source(at: "Veetbot/Views/EmailSubscriptionsView.swift")
        #expect(email.contains("email.collection."))
        #expect(email.contains("email.feedback-topic-chip."))
        #expect(people.contains("people.collection."))
        #expect(persona.contains("persona.section."))
        #expect(subscriptions.contains("email.subscription.inline."))
    }

    @Test
    func testAuxiliarySurfacesExposeTheReferenceReadingAndDisclosureControls() throws {
        let artifact = try source(at: "Veetbot/Views/ArtifactViewerView.swift")
        let signIn = try source(at: "Veetbot/Views/DeviceSignInSheet.swift")
        let call = try source(at: "Veetbot/Views/CallResultSheet.swift")
        #expect(artifact.contains("MarkdownContentView(text: preview.text)"))
        #expect(signIn.contains("device-sign-in.instructions"))
        #expect(call.contains("call.transcript"))
    }

    @Test
    func testTheThreadListUsesTheReferenceRowAndKeepsItsControls() throws {
        let source = try source(at: "Veetbot/Views/EmailModeView.swift")

        #expect(source.contains("struct EmailThreadRow"))
        #expect(source.contains("struct EmailHandledCircle"))
        #expect(source.contains("AppTheme.amberText"))
        #expect(source.contains(".accessibilityIdentifier(\"email.thread.\\(thread.id.uuidString)\")"))
        #expect(source.contains("accessibilityID: \"email.handled.\\(thread.id.uuidString)\""))
        #expect(source.contains(".accessibilityIdentifier(\"email.search\")"))
        #expect(source.contains(".accessibilityIdentifier(\"email.mail-view\")"))
        #expect(source.contains(".accessibilityIdentifier(\"email.accounts\")"))
        #expect(source.contains(".accessibilityIdentifier(\"email.mailbox-status\")"))
        #expect(source.contains(".accessibilityIdentifier(\"email.budget-details\")"))
        #expect(!source.contains("EmailSurface.accent"))
    }

    @Test
    func testTheThreadDetailOpensWithTheSerifSubjectAndVeetbotsGist() throws {
        let source = try source(at: "Veetbot/Views/EmailModeView.swift")

        #expect(source.contains(".veetType(.display)"))
        #expect(source.contains("struct EmailGistBlock"))
        #expect(source.contains("Text(\"Veetbot\")"))
        #expect(source.contains("Teach Veetbot"))
        #expect(source.contains(".accessibilityIdentifier(\"email.feedback\")"))
        #expect(source.contains(".accessibilityIdentifier(\"email.jump-to-reply\")"))
        #expect(source.contains(".accessibilityIdentifier(\"email.discuss\")"))
        #expect(source.contains(".accessibilityIdentifier(\"email.review-send\")"))
        #expect(source.contains(".accessibilityIdentifier(\"email.draft-body\")"))
        #expect(source.contains("PrimaryCapsuleButtonStyle"))
        #expect(!source.contains(".emailCard("))
        #expect(!source.contains("struct EmailAvatar"))
    }

    /// On a compact iPhone the pushed thread keeps its actions beside the back
    /// button in a paper navigation bar, so neither the button nor the bar
    /// covers the subject as the conversation scrolls beneath them.
    @Test
    func testThePushedThreadPutsItsActionsInAPaperNavigationBar() throws {
        let source = try source(at: "Veetbot/Views/EmailModeView.swift")

        #expect(source.contains("actionsInNavigationBar: !directSelection"))
        #expect(source.contains("ToolbarItemGroup(placement: .topBarTrailing)"))
        #expect(source.contains("accessibilityID: \"email.handled.detail\", iconOnly: true)"))
        #expect(source.contains(".toolbarBackground(AppTheme.paper, for: .navigationBar)"))
        #expect(source.contains("if !actionsInNavigationBar {"))
        #expect(source.contains(".accessibilityLabel(\"Thread actions\")"))
    }

    /// The reply composer: a decision the draft waits on reads as one amber
    /// card with its action beside it, the envelope and body share one well
    /// whose shapes never take a click, and the rarer actions sit in a menu.
    @Test
    func testTheReplyComposerPutsEachDecisionBesideItsAction() throws {
        let source = try source(at: "Veetbot/Views/EmailModeView.swift")

        #expect(source.contains("private func draftDecision<Action: View>("))
        #expect(source.contains("draftDecision(\"New mail arrived\""))
        #expect(source.contains("draftDecision(\"Waiting for your approval\""))
        #expect(source.contains("draftDecision(\n                \"Send status unknown\""))
        #expect(source.contains(".attentionCard()\n            .accessibilityIdentifier(\"email.draft-conflict\")"))
        #expect(source.contains("private func draftWell("))
        #expect(source.contains("envelopeRow(\"From\")"))
        #expect(!source.contains("Text(\"From: \\(accountDescription("))
        #expect(source.contains(".stroke(AppTheme.hairlineStrong)\n                .allowsHitTesting(false)"))
        #expect(source.contains(".menuIndicator(.hidden)"))
        for identifier in [
            "email.draft-to", "email.draft-envelope", "email.draft-history", "email.endorse-style",
            "email.action-error", "email.draft-conflict",
        ] {
            #expect(source.contains("\"\(identifier)\")"), "\(identifier)")
        }
        #expect(source.contains(".accessibilityIdentifier(\"email.draft-\\(title.lowercased())\")"))
    }

    @Test
    func testTheSheetsAndTheCensusWearTheSameSystem() throws {
        let mode = try source(at: "Veetbot/Views/EmailModeView.swift")
        let census = try source(at: "Veetbot/Views/EmailSubscriptionsView.swift")

        #expect(mode.contains("struct EmailSheetHeader"))
        #expect(mode.contains(".accessibilityIdentifier(\"email.approve-send\")"))
        #expect(census.contains("AppTheme.tealText"))
        #expect(census.contains(".accessibilityIdentifier(\"email.unsubscribe.confirm\")"))
        #expect(!census.contains("EmailSurface.accent"))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
