import Foundation
import Testing

@testable import VeetbotCore

/// The A2 Dock Email surface: the thread list reads like the reference rows,
/// the detail opens with a serif subject and Veetbot's gist behind a teal
/// rule, feedback lives in one Teach Veetbot card, and every control keeps
/// the identifier the journeys drive.
@Suite struct EmailSurfaceStructureTests {
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

        #expect(source.contains(".appDisplayFont(.title2)"))
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
