import Foundation
import Testing

@testable import VeetbotCore

/// The Mac right panel (ADR-0237): one panel per mode, toggled from the
/// window toolbar, its width kept on this Mac, and in Email collapsed when
/// the window is too narrow for a fourth column.
@MainActor
@Suite struct InspectorPanelTests {
    private func store() -> InspectorLayoutStore {
        let suite = "InspectorPanelTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return InspectorLayoutStore(defaults: defaults)
    }

    // MARK: - Width and choice, kept per mode

    @Test
    func testWidthDefaultsPerModeAndIsKeptWithinItsBounds() {
        let store = store()
        #expect(store.width(for: .chat) == 340)
        #expect(store.width(for: .email) == 320)
        store.remember(width: 400, for: .chat)
        #expect(store.width(for: .chat) == 400)
        #expect(store.width(for: .email) == 320, "Each mode keeps its own width")
        store.remember(width: 90, for: .email)
        #expect(store.width(for: .email) == InspectorLayout.widthBounds.lowerBound)
        store.remember(width: 9_000, for: .email)
        #expect(store.width(for: .email) == InspectorLayout.widthBounds.upperBound)
        store.remember(width: .nan, for: .chat)
        #expect(store.width(for: .chat) == 400, "An unusable width is never remembered")
        #expect(store.defaults.object(forKey: "veetbot.inspector.chat.width") != nil)
    }

    @Test
    func testTheOwnersOpenChoiceIsKeptPerMode() {
        let store = store()
        #expect(store.openChoice(for: .chat) == nil)
        store.remember(open: false, for: .chat)
        #expect(store.openChoice(for: .chat) == false)
        #expect(store.openChoice(for: .email) == nil)
    }

    // MARK: - Visibility

    @Test
    func testAWideWindowOpensBothPanelsAndANarrowOneCollapsesEmailFirst() {
        let wide = InspectorModel(store: store())
        wide.windowWidthChanged(1440)
        #expect(wide.isOpen(.chat))
        #expect(wide.isOpen(.email))

        let medium = InspectorModel(store: store())
        medium.windowWidthChanged(1200)
        #expect(medium.isOpen(.chat), "Chat has two columns and room for the panel")
        #expect(!medium.isOpen(.email), "Email's three columns leave no room for a fourth")

        let narrow = InspectorModel(store: store())
        narrow.windowWidthChanged(1000)
        #expect(!narrow.isOpen(.chat))
        #expect(!narrow.isOpen(.email))
    }

    @Test
    func testTheToolbarToggleOpensANarrowPanelAndIsRemembered() {
        let store = store()
        let model = InspectorModel(store: store)
        model.windowWidthChanged(1000)
        model.toggle(.email)
        #expect(model.isOpen(.email), "The owner can always open the panel")
        #expect(store.openChoice(for: .email) == true)
        model.toggle(.email)
        #expect(!model.isOpen(.email))
        #expect(store.openChoice(for: .email) == false)

        let relaunched = InspectorModel(store: store)
        relaunched.windowWidthChanged(1600)
        #expect(!relaunched.isOpen(.email), "A panel the owner closed stays closed")
        #expect(relaunched.isOpen(.chat))
    }

    @Test
    func testCrossingTheThresholdCollapsesAndRestoresWithoutTouchingTheChoice() {
        let store = store()
        let model = InspectorModel(store: store)
        model.windowWidthChanged(1440)
        #expect(model.isOpen(.email))
        model.windowWidthChanged(1100)
        #expect(!model.isOpen(.email))
        #expect(store.openChoice(for: .email) == nil, "A squeezed window is not the owner's choice")
        model.windowWidthChanged(1250)
        #expect(!model.isOpen(.email), "Still below Email's threshold")
        model.windowWidthChanged(1500)
        #expect(model.isOpen(.email))
        model.windowWidthChanged(1500)
        model.toggle(.email)
        model.windowWidthChanged(1510)
        #expect(!model.isOpen(.email), "A resize that crosses nothing leaves the owner's toggle alone")
    }

    @Test
    func testWidthDragsAreClampedAndRememberedOnlyWhenTheDragEnds() {
        let store = store()
        let model = InspectorModel(store: store)
        model.setWidth(380, for: .chat, remember: false)
        #expect(model.width(.chat) == 380)
        #expect(store.defaults.object(forKey: "veetbot.inspector.chat.width") == nil)
        model.setWidth(1_000, for: .chat, remember: true)
        #expect(model.width(.chat) == 440)
        #expect(store.width(for: .chat) == 440)
    }

    // MARK: - Reaching the panel from the transcript and the thread

    @Test
    func testAWorkStripRevealsItsCallsInTheActivityTab() {
        let model = InspectorModel(store: store())
        model.windowWidthChanged(1000)
        model.chatTab = .artifacts
        model.revealActivity("tool:call-7")
        #expect(model.isOpen(.chat))
        #expect(model.chatTab == .activity)
        #expect(model.revealedActivityID == "tool:call-7")
        let first = model.revealCount
        model.revealActivity("tool:call-7")
        #expect(model.revealCount == first + 1, "Revealing the same run again scrolls to it again")
    }

    @Test
    func testAnArtifactChipOpensItsPreviewInTheArtifactsTab() {
        let model = InspectorModel(store: store())
        model.windowWidthChanged(1000)
        let id = UUID()
        model.openArtifact(id)
        #expect(model.isOpen(.chat))
        #expect(model.chatTab == .artifacts)
        #expect(model.selectedArtifactID == id)
    }

    @Test
    func testASendersNameShowsTheirPeopleCardInTheContextTab() {
        let model = InspectorModel(store: store())
        model.windowWidthChanged(1000)
        model.emailTab = .activity
        let sentAt = Date(timeIntervalSince1970: 1_000)
        model.showSender(.correspondent("Priya <priya@example.test>", at: sentAt))
        #expect(model.isOpen(.email))
        #expect(model.emailTab == .context)
        #expect(model.sender?.text == "priya@example.test")
        model.threadChanged()
        #expect(model.sender == nil, "Another thread shows its own latest sender")
    }

    /// The clean reader (MagicPath cozy-evening-6871): a sender's name opens
    /// their card and that message's envelope, which left the thread.
    @Test
    func testAMessagesSenderShowsTheirCardAndThatMessagesEnvelope() throws {
        let model = InspectorModel(store: store())
        model.windowWidthChanged(1000)
        model.emailTab = .activity
        model.showMessage(try emailMessage(id: "m2", sender: "Priya <priya@example.test>"))
        #expect(model.isOpen(.email))
        #expect(model.emailTab == .context)
        #expect(model.sender?.text == "priya@example.test")
        #expect(model.senderMessageID == "m2")
        #expect(model.senderRevealCount == 1)
        model.threadChanged()
        #expect(model.senderMessageID == nil, "Another thread shows its own latest message")
    }

    @Test
    func testTheAttachmentLineRevealsTheAttachmentsInContext() {
        let model = InspectorModel(store: store())
        model.windowWidthChanged(1000)
        model.emailTab = .activity
        model.revealAttachments()
        #expect(model.isOpen(.email))
        #expect(model.emailTab == .context)
        #expect(model.attachmentsRevealCount == 1)
    }

    /// A reveal survives mounting a closed panel and is consumed once, not replayed on reopen.
    @Test
    func testEmailRevealSurvivesMountAndDoesNotReturnOnReopen() {
        let model = InspectorModel(store: store())
        model.windowWidthChanged(1000)
        model.showSender(.correspondent("Priya <priya@example.test>", at: Date(timeIntervalSince1970: 1000)))
        #expect(model.consumeEmailReveal() == .sender)
        #expect(model.consumeEmailReveal() == nil)
        model.toggle(.email)
        model.toggle(.email)
        #expect(model.consumeEmailReveal() == nil)
        model.revealAttachments()
        #expect(model.consumeEmailReveal() == .attachments)
        #expect(model.consumeEmailReveal() == nil)
        model.showSender(.correspondent("Priya <priya@example.test>", at: Date(timeIntervalSince1970: 1000)))
        model.revealAttachments()
        #expect(model.consumeEmailReveal() == .attachments, "The latest request wins")
        model.revealAttachments()
        model.threadChanged()
        #expect(model.consumeEmailReveal() == nil, "Another thread cannot inherit a reveal")
    }

    /// Show original moved from every message to one switch in Context, which
    /// the thread reads through the reveal.
    @Test
    func testShowOriginalIsOneChoiceForTheWholeThread() {
        let model = InspectorModel(store: store())
        #expect(!model.showsOriginal)
        #expect(!InspectorReveal(model).showsOriginal)
        model.showsOriginal = true
        let reveal = InspectorReveal(model)
        #expect(reveal.showsOriginal)
        #expect(reveal != InspectorReveal(InspectorModel(store: store())), "The thread redraws when the choice changes")
        model.threadChanged()
        #expect(!model.showsOriginal, "Each thread opens in the reading view")
    }

    // MARK: - The ledger: every call in the conversation, grouped by turn

    @Test
    func testTheLedgerGroupsCallsBetweenMessagesUnderTheOwnersRequest() {
        let reducer = RunStateReducer()
        reducer.reduce(userMessageFrame(id: 1, text: "Find a place near Heavenly for Nov 7–9.\nFour adults."))
        reducer.reduce(toolFrame(id: 2, callID: "search", name: "web.search", arguments: ["query": .string("Heavenly lodging")]))
        reducer.reduce(toolFrame(id: 3, callID: "open", name: "browser.open", arguments: ["url": .string("https://lodge.example")]))
        reducer.reduce(assistantMessageFrame(id: 4, text: "Three places fit."))
        reducer.reduce(userMessageFrame(id: 5, text: "Book the lodge."))
        reducer.reduce(toolFrame(id: 6, callID: "act", name: "browser.act"))

        let groups = ToolLedgerGroups.make(from: reducer.activityTimeline)
        #expect(groups.map(\.title) == ["Find a place near Heavenly for Nov 7–9.", "Book the lodge."])
        #expect(groups.map { $0.activities.map(\.callID) } == [["search", "open"], ["act"]])
        #expect(groups.map(\.firstIndex) == [1, 3], "Calls are numbered across the whole conversation")
        #expect(groups[0].contains(activityID: "tool:open"))
        #expect(!groups[0].contains(activityID: "tool:act"))
        #expect(ToolLedgerGroups.group(containing: "tool:act", in: groups)?.id == groups[1].id)
    }

    @Test
    func testABundleRevealsTheGroupHoldingAllItsCalls() {
        let reducer = RunStateReducer()
        reducer.reduce(userMessageFrame(id: 1, text: "Search the web"))
        for index in 1...3 {
            reducer.reduce(toolFrame(id: 1 + index, callID: "w\(index)", name: "web.search", arguments: ["query": .string("q\(index)")]))
        }
        let timeline = reducer.activityTimeline
        guard case .toolBundle(let bundle) = timeline.last else {
            Issue.record("Three successful searches bundle")
            return
        }
        let groups = ToolLedgerGroups.make(from: timeline)
        #expect(groups.count == 1)
        #expect(groups[0].activities.map(\.callID) == ["w1", "w2", "w3"])
        #expect(groups[0].contains(activityID: "tool:\(bundle.id)"))
    }

    @Test
    func testCallsBeforeAnyRequestShareOneGroup() {
        let reducer = RunStateReducer()
        reducer.reduce(toolFrame(id: 1, callID: "brief", name: "web.fetch"))
        #expect(ToolLedgerGroups.make(from: reducer.activityTimeline).map(\.title) == ["Tool calls"])
        #expect(ToolLedgerGroups.make(from: []).isEmpty)
    }

    // MARK: - Artifacts: every file or image the conversation produced

    @Test
    func testArtifactsCollectFromAnswersAndToolResultsOnceEach() {
        let markdown = UUID()
        let shot = UUID()
        let reducer = RunStateReducer()
        reducer.reduce(userMessageFrame(id: 1, text: "Compare the lodges"))
        reducer.reduce(toolFrame(id: 2, callID: "search", name: "web.search"))
        reducer.reduce(toolFrame(id: 3, callID: "open", name: "browser.open", result: [
            .object(["type": .string("image"), "artifact_id": .string(shot.uuidString), "media_type": .string("image/png")]),
        ]))
        reducer.reduce(toolFrame(id: 4, callID: "write", name: "workspace.write_text", result: [
            .object([
                "type": .string("file"), "artifact_id": .string(markdown.uuidString),
                "media_type": .string("text/markdown"), "filename": .string("Tahoe weekend options.md"),
            ]),
        ]))
        reducer.reduce(assistantMessageFrame(id: 5, text: "Here they are.", files: [(markdown, "Tahoe weekend options.md")]))

        let artifacts = ConversationArtifacts.collect(from: reducer.activityTimeline)
        #expect(artifacts.map(\.id) == [shot, markdown])
        #expect(artifacts.map(\.name) == ["Image · image/png", "Tahoe weekend options.md"])
        #expect(artifacts.map(\.source) == ["2. browser.open", "3. workspace.write_text"])
        #expect(artifacts.map(\.mediaType) == ["image/png", "text/markdown"])
    }

    @Test
    func testAnAnswerOnlyFileNamesWhereItCameFrom() {
        let file = UUID()
        let reducer = RunStateReducer()
        reducer.reduce(assistantMessageFrame(id: 1, text: "Attached.", files: [(file, nil)]))
        let artifacts = ConversationArtifacts.collect(from: reducer.activityTimeline)
        #expect(artifacts.map(\.name) == ["File"])
        #expect(artifacts.map(\.source) == ["In an answer"])
    }

    // MARK: - The Now card

    @Test
    func testTheRunCardNamesWhatTheRunIsDoing() {
        #expect(InspectorRunSummary.chat(runStatus: nil, awaitingApproval: false, activityLabel: nil).title == "Nothing running")
        let running = InspectorRunSummary.chat(runStatus: .running, awaitingApproval: false, activityLabel: "Searching the web")
        #expect(running.title == "Running")
        #expect(running.detail == "Searching the web")
        #expect(running.tone == .active)
        let approval = InspectorRunSummary.chat(runStatus: .waitingForApproval, awaitingApproval: true, activityLabel: nil)
        #expect(approval.title == "Waiting for your approval")
        #expect(approval.tone == .attention)
        #expect(InspectorRunSummary.chat(runStatus: .waitingForUser, awaitingApproval: false, activityLabel: nil).title
            == "Waiting for your answer")
        #expect(InspectorRunSummary.chat(runStatus: .completed, awaitingApproval: false, activityLabel: nil).tone == .done)
        #expect(InspectorRunSummary.chat(runStatus: .failed, awaitingApproval: false, activityLabel: nil).tone == .failed)
        #expect(InspectorRunSummary.chat(runStatus: .cancelled, awaitingApproval: false, activityLabel: nil).title == "Stopped")
    }

    // MARK: - Email context

    @Test
    func testPriorityReadsAsALevelAndAPercentage() {
        #expect(EmailPriority(0.86).title == "High priority")
        #expect(EmailPriority(0.86).percent == 86)
        #expect(EmailPriority(0.86).isHigh)
        #expect(EmailPriority(0.52).title == "Medium priority")
        #expect(EmailPriority(0.1).title == "Low priority")
        #expect(EmailPriority(1.7).percent == 100, "An out-of-range value is held within the meter")
        #expect(EmailPriority(-1).percent == 0)
    }

    @Test
    func testTheDraftCardUsesTheThreadStatusWords() throws {
        #expect(EmailDraftSummary.make(draft: nil, needsReply: true).title == "No draft")
        #expect(EmailDraftSummary.make(draft: nil, needsReply: true).detail == "Veetbot has not drafted a reply yet.")
        #expect(EmailDraftSummary.make(draft: nil, needsReply: false).detail
            == "Veetbot did not think this needs a written reply.")
        let ready = try draft(status: "ready", stale: false)
        #expect(EmailDraftSummary.make(draft: ready, needsReply: true).title == "Draft ready")
        #expect(EmailDraftSummary.make(draft: ready, needsReply: true).tone == .draft)
        let stale = try draft(status: "ready", stale: true)
        #expect(EmailDraftSummary.make(draft: stale, needsReply: true).title == "New mail arrived")
        #expect(EmailDraftSummary.make(draft: stale, needsReply: true).tone == .attention)
        #expect(EmailDraftSummary.make(draft: try draft(status: "generating", stale: false), needsReply: true).title
            == "Preparing draft")
        #expect(EmailDraftSummary.make(draft: try draft(status: "sent", stale: false), needsReply: true).title
            == "Reply sent")
        #expect(EmailDraftSummary.make(draft: try draft(status: "uncertain", stale: false), needsReply: true).tone
            == .attention)
    }

    /// Veetbot's gist and state left the thread for Context's summary card.
    @Test
    func testTheSummaryCardUsesTheThreadsGistAndStateWords() throws {
        let thread = try emailThread(summary: "Asks whether you will renew.", reason: "Priya manages your lease.")
        let summary = EmailThreadSummary.make(thread: thread, draft: nil)
        #expect(!summary.isPending)
        #expect(summary.gist == "Asks whether you will renew.")
        #expect(summary.state == "Needs reply")
        #expect(summary.tone == .attention)
        #expect(summary.reason == "Priya manages your lease.")
        let drafted = EmailThreadSummary.make(thread: thread, draft: try draft(status: "ready", stale: false))
        #expect(drafted.state == "Draft ready")
        #expect(drafted.tone == .draft)
        let handled = EmailThreadSummary.make(
            thread: try emailThread(summary: "Filed.", reason: "A bill on autopay.", handled: true), draft: nil)
        #expect(handled.state == "Handled")
        #expect(handled.tone == .settled)
    }

    /// The server's placeholder is both the gist and the reason; the card
    /// says it once.
    @Test
    func testAThreadVeetbotHasNotReadSaysSoOnce() throws {
        let thread = try emailThread(summary: "Assessment pending", reason: "Assessment pending", needsReply: false)
        let summary = EmailThreadSummary.make(thread: thread, draft: nil)
        #expect(summary.isPending)
        #expect(summary.state == "For your attention")
        #expect(summary.reason == nil, "The reason only repeats the gist")
    }

    // MARK: - The draft run's calls

    @Test
    func testTheDraftRunReplaysItsCallsIntoItsOwnLedger() async throws {
        let replay = RunActivityReplay()
        let runID = UUID()
        let frames = [
            toolFrame(id: 1, callID: "read", name: "email.read_thread"),
            toolFrame(id: 2, callID: "draft", name: "email.create_draft"),
            SSEFrame(id: 3, event: "run.completed", data: [:]),
        ]
        await replay.load(runID: runID) { requested in
            #expect(requested == runID)
            return AsyncThrowingStream { continuation in
                for frame in frames { continuation.yield(frame) }
                continuation.finish()
            }
        }
        #expect(replay.runID == runID)
        #expect(replay.state.tools.map(\.callID) == ["read", "draft"])
        #expect(replay.state.runStatus == .completed)
        #expect(replay.errorMessage == nil)

        await replay.load(runID: runID) { _ in
            Issue.record("The same run is not replayed twice")
            return AsyncThrowingStream { $0.finish() }
        }
    }

    @Test
    func testAFailedReplayKeepsItsError() async {
        struct Broken: Error, LocalizedError { var errorDescription: String? { "The run could not be read." } }
        let replay = RunActivityReplay()
        await replay.load(runID: UUID()) { _ in
            AsyncThrowingStream { $0.finish(throwing: Broken()) }
        }
        #expect(replay.errorMessage == "The run could not be read.")
    }

    // MARK: - Fixtures

    private func draft(status: String, stale: Bool) throws -> EmailDraftView {
        let json = """
        {"id": "\(UUID().uuidString)", "thread_id": "\(UUID().uuidString)", "account_id": "a",
         "revision": 2, "source_revision": 1, "to": ["p@example.test"], "cc": [], "bcc": [],
         "subject": "Re: Lease", "body": "Hi Priya", "status": "\(status)", "stale": \(stale),
         "updated_at": "2026-10-21T16:49:00Z"}
        """
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try decoder.decode(EmailDraftView.self, from: Data(json.utf8))
    }

    private func emailMessage(id: String, sender: String) throws -> EmailMessageView {
        let json = """
        {"id": "\(id)", "sender": "\(sender)", "to": ["andy@example.test"], "cc": [], "subject": "Lease",
         "body": "Hi Andy", "sent_at": "2026-10-21T16:48:00Z", "complete": true}
        """
        return try JSONDecoder.server.decode(EmailMessageView.self, from: Data(json.utf8))
    }

    private func emailThread(
        summary: String, reason: String, needsReply: Bool = true, handled: Bool = false
    ) throws -> EmailThreadView {
        let json = """
        {"id": "\(UUID().uuidString)", "account_id": "personal", "subject": "Lease renewal",
         "senders": ["Priya <priya@example.test>"], "updated_at": "2026-10-21T16:48:00Z", "revision": 1,
         "dismissed_revision": \(handled ? "1" : "null"), "summary": "\(summary)", "reason": "\(reason)",
         "needs_reply": \(needsReply), "draft_id": null, "session_id": null, "priority": 0.86,
         "complete": true, "messages": [], "draft": null}
        """
        return try JSONDecoder.server.decode(EmailThreadView.self, from: Data(json.utf8))
    }

    private func userMessageFrame(id: Int, text: String) -> SSEFrame {
        SSEFrame(id: id, event: "user.message.created", data: [
            "content": .array([.object(["kind": .string("text"), "text": .string(text)])]),
        ])
    }

    private func assistantMessageFrame(id: Int, text: String, files: [(UUID, String?)] = []) -> SSEFrame {
        var content: [JSONValue] = [.object(["kind": .string("text"), "text": .string(text)])]
        for (artifactID, filename) in files {
            var block: [String: JSONValue] = [
                "kind": .string("file"), "artifact_id": .string(artifactID.uuidString),
                "media_type": .string("text/markdown"),
            ]
            if let filename { block["filename"] = .string(filename) }
            content.append(.object(block))
        }
        return SSEFrame(id: id, event: "assistant.message.completed", data: [
            "message": .object(["kind": .string("assistant"), "content": .array(content)]),
        ])
    }

    private func toolFrame(
        id: Int, callID: String, name: String,
        arguments: [String: JSONValue] = [:], result: [JSONValue] = []
    ) -> SSEFrame {
        SSEFrame(id: id, event: "tool.call.completed", data: [
            "call_id": .string(callID),
            "name": .string(name),
            "arguments": .object(arguments),
            "result_item": .object([
                "content": .array(result),
                "is_error": .bool(false),
                "trust": .string("internal_tool"),
            ]),
        ])
    }
}
