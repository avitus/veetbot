import Foundation
import Testing

@testable import VeetbotCore

/// The Mac right panel's wiring (ADR-0237, MagicPath merry-moon-8243 and
/// clever-village-5022): the root toolbar is its one owner, each mode draws
/// its own panel, and what moved there no longer crowds the transcript or
/// the thread.
@Suite struct InspectorSurfaceStructureTests {
    @Test
    func testTheRootToolbarOwnsOneToggleForTheActiveModesPanel() throws {
        let root = try source(at: "Veetbot/Views/RootView.swift")

        #expect(root.contains("@State private var inspector = InspectorModel()"))
        #expect(root.contains("InspectorToggleButton(inspector: inspector, mode: .chat)"))
        #expect(root.contains("InspectorToggleButton(inspector: inspector, mode: .email)"))
        #expect(root.contains("inspector.windowWidthChanged("))
        #expect(root.contains(".macInspector(inspector, mode: .chat)"))
        #expect(root.contains(".modifier(InspectorRevealEnvironment(inspector: inspector))"))
        #expect(root.contains("runFrames: { model.runFrames($0) }"))
    }

    @Test
    func testThePanelShellIsMacOnlyResizableAndNamedForTheJourneys() throws {
        let panel = try source(at: "Veetbot/Views/InspectorPanel.swift")
        let model = try source(at: "Veetbot/ViewModels/InspectorModel.swift")

        #expect(panel.contains("#if os(macOS)"))
        #expect(model.contains("struct InspectorLayoutStore"))
        #expect(model.contains("\"veetbot.inspector.\\(mode.rawValue).width\""))
        #expect(panel.contains(".accessibilityIdentifier(\"inspector.toggle\")"))
        #expect(panel.contains(".accessibilityIdentifier(\"inspector.hide\")"))
        #expect(panel.contains(".accessibilityIdentifier(\"inspector.resize\")"))
        #expect(panel.contains(".accessibilityIdentifier(\"inspector.tabs\")"))
        #expect(panel.contains(".keyboardShortcut(\"i\", modifiers: [.command, .option])"))
        #expect(panel.contains("NSCursor.resizeLeftRight"))
        #expect(panel.contains(".environment(\\.inspectorReveal, InspectorReveal(inspector))"))
        #expect(panel.contains("AppTheme.paperDeep"))
    }

    @Test
    func testTheChatPanelHoldsTheRunTheNotesTheLedgerAndTheArtifacts() throws {
        let chat = try source(at: "Veetbot/Views/ChatInspectorPanel.swift")

        #expect(chat.contains("InspectorRunSummary.chat("))
        #expect(chat.contains("WorkingStateDetails(state:"))
        #expect(chat.contains("ToolLedgerGroups.make(from: state.activityTimeline)"))
        #expect(chat.contains("ToolLedgerRow("))
        #expect(chat.contains("ConversationArtifacts.collect(from: state.activityTimeline)"))
        #expect(chat.contains("ArtifactViewerView(model: model, artifactID: artifact.id, presentation: .embedded)"))
        #expect(chat.contains(".accessibilityIdentifier(\"inspector.chat.run\")"))
        #expect(chat.contains(".accessibilityIdentifier(\"inspector.ledger.\\(group.id)\")"))
        #expect(chat.contains(".accessibilityIdentifier(\"inspector.artifact.\\(artifact.id.uuidString)\")"))
    }

    @Test
    func testTheTranscriptUsesMessageOnlyRowsOnMac() throws {
        let activity = try source(at: "Veetbot/Views/ActivityViews.swift")
        let chat = try source(at: "Veetbot/Views/ChatView.swift")

        #expect(activity.contains("@Environment(\\.inspectorReveal) private var inspectorReveal"))
        #expect(chat.contains("state.timeline.map(ConversationActivity.message)"))
        #expect(chat.contains("ForEach(Array(transcriptActivities.enumerated())"))
        #expect(activity.contains("struct WorkingStateDetails"))
        // On the Mac the notes live in the panel, not above the transcript.
        #expect(chat.contains("if inspectorReveal == nil, let workingState = state.workingState"))
        #expect(chat.contains("inspectorReveal.artifact(artifactID)"))
        // Approvals and questions still dock above the composer.
        #expect(chat.contains("AttentionDock(model: model, state: state"))
        let dock = try #require(chat.components(separatedBy: "private struct AttentionDock").last)
        #expect(dock.contains(".id(NotificationFocus.approval(approval.id).scrollID)"))
        #expect(chat.contains(".id(approval.status.isPending ? \"approval-waiting-\\(approval.id)\""))
    }

    @Test
    func testTheEmailPanelHoldsTheSenderTheReasonTeachingAttachmentsAndTheDraft() throws {
        let email = try source(at: "Veetbot/Views/EmailInspectorPanel.swift")
        let mode = try source(at: "Veetbot/Views/EmailModeView.swift")

        #expect(email.contains("EmailSenderCard(lookup:"))
        #expect(email.contains("PeopleViewModel(searchText: lookup.text, asOf: lookup.asOf)"))
        #expect(email.contains("EmailPriority(thread.priority)"))
        #expect(email.contains("EmailTeachCard(model: model"))
        #expect(email.contains("EmailDraftSummary.make(draft:"))
        #expect(email.contains("RunActivityReplay()"))
        for identifier in [
            "inspector.email.sender", "inspector.email.priority", "inspector.email.teach",
            "inspector.email.attachments", "inspector.email.draft", "inspector.email.run",
        ] {
            #expect(email.contains(".accessibilityIdentifier(\"\(identifier)\")"), "\(identifier)")
        }
        // The thread hands both to the panel on the Mac.
        #expect(mode.contains("struct EmailTeachCard"))
        #expect(mode.contains("if describesInline {\n                                EmailTeachCard("))
        #expect(mode.contains("inspectorReveal.message(message)"))
        #expect(mode.contains(".macInspector(inspector, mode: .email)"))
    }

    /// The clean reader (MagicPath cozy-evening-6871): on the Mac the thread
    /// keeps the mail itself, and Context holds everything that describes it.
    @Test
    func testTheMacThreadKeepsTheMailAndContextHoldsWhatDescribesIt() throws {
        let email = try source(at: "Veetbot/Views/EmailInspectorPanel.swift")
        let mode = try source(at: "Veetbot/Views/EmailModeView.swift")

        #expect(email.contains("EmailThreadSummary.make(thread: thread, draft: model.draft ?? thread.draft)"))
        #expect(email.contains("EmailSpamControls(model: model, thread: thread)"))
        #expect(email.contains("model.subscriptions.beginUnsubscribe(thread: thread)"))
        #expect(email.contains("model.unsubscribeSupported(for: thread.accountID)"))
        #expect(email.contains("model.archiveUnavailableReason(for: thread)"))
        #expect(email.contains("model.accountDescription(for: thread.accountID)"))
        #expect(email.contains("Toggle(\"Show original\", isOn: $inspector.showsOriginal)"))
        for identifier in [
            "inspector.email.summary", "inspector.email.message", "inspector.email.thread",
            "email.reader.original", "email.unsubscribe.thread",
        ] {
            #expect(email.contains(".accessibilityIdentifier(\"\(identifier)\")"), "\(identifier)")
        }

        // Without a panel (iPhone, iPad) the thread still describes itself.
        #expect(mode.contains("private var describesInline: Bool { inspectorReveal == nil }"))
        #expect(mode.contains("if describesInline {\n                                EmailGistBlock("))
        #expect(mode.contains("if describesInline {\n                    Label(model.accountDescription(for: thread.accountID)"))
        #expect(mode.contains("EmailArchiveStatus(model: model, thread: thread, showsAvailability: describesInline)"))
        #expect(mode.contains("if describesInline {\n                // Bulk mail only"))
        #expect(mode.contains("if describesInline {\n                EmailSpamControls(model: model, thread: thread)"))
        #expect(mode.contains("if describesInline {\n                        Button {\n                            peopleLookup ="))
        // On the Mac the sender's name, one attachment line and the panel's
        // Show original stand in for them.
        #expect(mode.contains("inspectorReveal.attachments()"))
        #expect(mode.contains("EmailReadingView(message: message, showsOriginal: inspectorReveal?.showsOriginal)"))
        #expect(!mode.contains("inspectorReveal.sender("))
    }

    @Test
    func testUIJourneysKeepTheirOwnPanelLayout() throws {
        let fixture = try source(at: "Veetbot/ConversationNavigationUITestFixture.swift")
        #expect(fixture.contains("static func makeInspectorLayoutIfRequested() -> InspectorLayoutStore?"))
        #expect(fixture.contains("\"com.veetbot.apple.ui-tests.inspector\""))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
