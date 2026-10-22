import XCTest
#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

final class ConversationNavigationUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        app = XCUIApplication()
        #if os(macOS)
        // XCTest ends the app without quitting it, so AppKit would restore the
        // window list the previous launch saved. Once that list is empty, the
        // app restores no window and never opens one. Launch arguments are read
        // as -key value pairs: keep this pair ahead of the bare fixture flags,
        // or a flag takes the key as its value and AppKit opens YES as a
        // document instead of a window.
        app.launchArguments = ["-ApplePersistenceIgnoreState", "YES"]
        #endif
        app.launchArguments.append("--ui-testing-conversation-navigation")
        // Each case adds its fixture options and then launches once; launching
        // here would make those cases pay for a second launch.
    }

    override func tearDown() {
        app?.terminate()
        super.tearDown()
    }

    func testA2CallResultDisclosesTranscriptAndStagesANewCall() {
        app.launchArguments.append("--ui-testing-call-result")
        app.launch()
        let again = app.buttons["call.again"]
        XCTAssertTrue(again.waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertTrue(app.staticTexts["call.duration"].exists)
        let transcript = app.staticTexts.matching(NSPredicate(
            format: "label CONTAINS %@ OR value CONTAINS %@",
            "Assistant: Which afternoons", "Assistant: Which afternoons"
        )).firstMatch
        XCTAssertFalse(transcript.exists)
        let disclosure = app.descendants(matching: .any)["call.transcript"].firstMatch
        activate(disclosure)
        XCTAssertTrue(transcript.waitForExistence(timeout: 5))
        XCTAssertTrue(again.isHittable)
        attachEmailScreenshot("A2 Call result and transcript")
        activate(again)
        assertCallFollowUpIsEditable()
        XCTAssertFalse(again.exists)
    }

    func testA2CallConcernStagesEditableChat() {
        app.launchArguments.append("--ui-testing-call-result")
        app.launch()
        let concern = app.buttons["call.something-wrong"]
        XCTAssertTrue(concern.waitForExistence(timeout: 10))
        activate(concern)
        assertCallFollowUpIsEditable()
        XCTAssertFalse(concern.exists)
    }

    private func assertCallFollowUpIsEditable() {
        let composer = app.descendants(matching: .any)["chat.composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        #if os(macOS)
        let text = composer.textViews.firstMatch
        #else
        let text = composer
        #endif
        let staged = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "value CONTAINS[c] %@", "00000000-0000-0000-0000-000000000d01"), object: text)
        XCTAssertEqual(XCTWaiter.wait(for: [staged], timeout: 5), .completed, app.debugDescription)
    }

    func testA2MarkdownArtifactRendersAReadableDocument() {
        app.launchArguments += ["--ui-testing-artifact", "--ui-testing-markdown-artifact"]
        app.launch()
        let row = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        #if os(macOS)
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.25)).click()
        #else
        openSidebarRow(row)
        #endif
        let file = app.buttons["appointment.md"]
        XCTAssertTrue(file.waitForExistence(timeout: 10))
        activate(file)
        let heading = app.staticTexts["Appointment notes"]
        XCTAssertTrue(heading.waitForExistence(timeout: 5), app.debugDescription)
        XCTAssertFalse(app.staticTexts["# Appointment notes"].exists)
        XCTAssertTrue(app.buttons["Download"].isEnabled)
        attachEmailScreenshot("A2 rendered Markdown artifact")
        #if os(macOS)
        activate(app.buttons["inspector.hide"])
        XCTAssertTrue(app.descendants(matching: .any)["inspector.chat"].waitForNonExistence(timeout: 5))
        #else
        activate(app.buttons["Close"])
        #endif
        XCTAssertTrue(file.waitForExistence(timeout: 5))
    }

    /// Changing the visible section must not discard the in-progress persona edit.
    func testA2PersonaSectionsPreserveUnsavedVoice() {
        app.launch()
        openA2Destination("sidebar.persona")
        let voice = app.descendants(matching: .any)["persona.voice"]
        XCTAssertTrue(voice.waitForExistence(timeout: 10), app.debugDescription)
        activate(voice)
        voice.typeText(" Keep explanations concrete.")
        choosePersonaSection("What Veetbot assumes")
        XCTAssertTrue(app.buttons["persona.add-entry"].waitForExistence(timeout: 5))
        XCTAssertFalse(voice.exists)
        choosePersonaSection("Voice")
        XCTAssertTrue(voice.waitForExistence(timeout: 5))
        XCTAssertTrue((voice.value as? String ?? "").contains("Keep explanations concrete."))
        attachEmailScreenshot("A2 Persona voice and sections")
    }

    func testPersonaEmptyPagesExplainMissingData() {
        app.launchEnvironment["VEETBOT_UI_TEST_EMPTY_PERSONA_BOUNDARIES"] = "1"
        app.launch()
        #if os(iOS)
        // Navigation helpers must recover the foreground before querying hit targets.
        XCUIDevice.shared.press(.home)
        XCTAssertTrue(app.wait(for: .runningBackground, timeout: 10))
        #endif
        openA2Destination("sidebar.persona")
        XCTAssertTrue(app.descendants(matching: .any)["persona.voice"].waitForExistence(timeout: 10))
        choosePersonaSection("Nominations")
        XCTAssertTrue(app.staticTexts["Veetbot has no new assumptions to suggest right now."].waitForExistence(timeout: 5))
        choosePersonaSection("Boundaries")
        XCTAssertTrue(app.staticTexts["This server does not list any boundaries."].waitForExistence(timeout: 5))
    }

    func testA2WideMemoryKeepsCollectionsBesideRecords() throws {
        #if os(iOS)
        try XCTSkipIf(UIDevice.current.userInterfaceIdiom != .pad, "Compact Memory uses push navigation")
        #endif
        app.launch()
        openA2Destination("sidebar.memory")
        let collection = app.buttons["memory.group.all"]
        let record = app.descendants(matching: .any)["memory.row.00000000-0000-0000-0000-000000000321"]
        XCTAssertTrue(record.waitForExistence(timeout: 10))
        XCTAssertTrue(collection.waitForExistence(timeout: 5))
        #if os(macOS)
        // AppKit combines the link's text into its button's accessibility label.
        let recordContent = record
        #else
        // iPad exposes that grouping element with a zero frame; its visible
        // text supplies the actual column geometry.
        let recordContent = texts("The user prefers dark mode.").firstMatch
        #endif
        XCTAssertTrue(recordContent.waitForExistence(timeout: 5))
        waitForSettledFrame(of: recordContent, stableFor: 3)
        XCTAssertTrue(recordContent.isHittable)
        XCTAssertLessThan(collection.frame.maxX, recordContent.frame.minX)
        activate(record)
        XCTAssertTrue(app.descendants(matching: .any)["memory.detail"].waitForExistence(timeout: 5))
        XCTAssertTrue(collection.isHittable)
        attachEmailScreenshot("A2 Memory columns")
    }

    func testA2SchedulesKeepListBesideSelectedSchedule() throws {
        #if os(iOS)
        try XCTSkipIf(UIDevice.current.userInterfaceIdiom != .pad, "Compact Schedules uses push navigation")
        #endif
        app.launch()
        openA2Destination("sidebar.schedules")
        let row = app.descendants(matching: .any)["schedule.row.00000000-0000-0000-0000-000000000654"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        activate(row)
        let detail = app.descendants(matching: .any)["schedule.detail"]
        XCTAssertTrue(detail.waitForExistence(timeout: 5))
        let instruction = texts("Full instruction from the schedule point read.").firstMatch
        XCTAssertTrue(instruction.waitForExistence(timeout: 5))
        waitForSettledFrame(of: instruction, stableFor: 3)
        XCTAssertTrue(instruction.isHittable)
        XCTAssertTrue(row.isHittable)
        XCTAssertLessThanOrEqual(row.frame.maxX, instruction.frame.minX)
        XCTAssertTrue(app.buttons["schedule.new"].isHittable)
        attachEmailScreenshot("A2 Schedules list and detail")
    }

    private func openA2Destination(_ identifier: String) {
        #if os(macOS)
        let button = app.buttons[identifier]
        XCTAssertTrue(button.waitForExistence(timeout: 10))
        activate(button)
        #else
        openSidebarDestination(identifier: identifier)
        #endif
    }

    private func choosePersonaSection(_ title: String) {
        let sidebar = app.buttons["persona.section.\(title)"]
        if sidebar.exists { activate(sidebar); return }
        let picker = app.buttons["persona.section-picker"]
        XCTAssertTrue(picker.waitForExistence(timeout: 5))
        activate(picker)
        activate(app.buttons[title].firstMatch)
    }

    #if os(macOS)
    func testA2EmailHasMailboxBesideTheInbox() {
        app.launch()
        activate(app.buttons["mode.email"])
        let mailbox = app.buttons["email.collection.inbox"]
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        XCTAssertTrue(mailbox.waitForExistence(timeout: 5))
        XCTAssertLessThan(mailbox.frame.maxX, row.frame.minX)
        XCTAssertTrue(app.buttons["email.collection.needsYou"].isHittable)
        XCTAssertTrue(app.buttons["email.collection.handled"].isHittable)
        activate(row)
        let message = texts("Please review the agenda before Friday.").firstMatch
        XCTAssertTrue(message.waitForExistence(timeout: 5))
        XCTAssertTrue(mailbox.isHittable)
        XCTAssertTrue(row.isHittable)
        XCTAssertLessThan(row.frame.maxX, message.frame.minX)
        attachEmailScreenshot("A2 Email mailbox and inbox")
    }

    /// The window's sidebar button hides Email's mailbox sidebar as it does
    /// Chat's; the mode switch, search and collections move above the inbox.
    func testEmailSidebarHidesFromTheWindowToolbar() {
        app.launch()
        activate(app.buttons["mode.email"])
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        let mailboxSettings = app.buttons["email.global.settings"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        XCTAssertTrue(mailboxSettings.waitForExistence(timeout: 5))
        let besideSidebar = row.frame.minX

        activate(sidebarToggle())
        waitForDisappearance(of: mailboxSettings)
        XCTAssertLessThan(row.frame.minX, besideSidebar - 150, "The inbox must take the sidebar's place")
        XCTAssertTrue(app.buttons["mode.chat"].isHittable, "Hiding the sidebar must keep the mode switch")
        XCTAssertTrue(app.textFields["email.search"].isHittable, "Search must move above the inbox")
        XCTAssertTrue(app.buttons["email.collection.needsYou"].isHittable, "Collections must move above the inbox")
        attachEmailScreenshot("Email with the sidebar hidden")

        activate(sidebarToggle())
        XCTAssertTrue(mailboxSettings.waitForExistence(timeout: 5))
        XCTAssertTrue(waitForFrame(of: row, timeout: 5) { $0.minX >= besideSidebar - 1 },
                      "Showing the sidebar must put the inbox back beside it")
    }

    /// The window toolbar's own sidebar button, which the split view adds.
    private func sidebarToggle() -> XCUIElement {
        let toggle = app.toolbars.buttons.matching(NSPredicate(
            format: "NOT (identifier BEGINSWITH 'sidebar.') AND (identifier CONTAINS[c] 'sidebar' OR label CONTAINS[c] 'sidebar')"
        )).firstMatch
        XCTAssertTrue(toggle.waitForExistence(timeout: 5), app.toolbars.firstMatch.debugDescription)
        return toggle
    }

    func testA2HandledCollectionDoesNotArchiveMail() {
        app.launch()
        openEmailMode()
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        activate(row)
        let actions = app.descendants(matching: .any)["Thread actions"].firstMatch
        XCTAssertTrue(actions.waitForExistence(timeout: 5))
        activate(actions)
        activate(app.menuItems["Mark handled"])
        activate(app.buttons["email.collection.needsYou"])
        XCTAssertTrue(row.waitForNonExistence(timeout: 5))
        activate(app.buttons["email.collection.handled"])
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        activate(app.buttons["email.collection.inbox"])
        XCTAssertTrue(row.waitForExistence(timeout: 5), "Handled mail remains in Inbox until separately archived")
        activate(actions)
        activate(app.menuItems["Mark not handled"])
        activate(app.buttons["email.collection.needsYou"])
        XCTAssertTrue(row.waitForExistence(timeout: 5))
    }

    func testA2PeopleHasDirectoryNavigation() {
        app.launch()
        let historical = app.descendants(matching: .any)["sidebar.recent.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(historical.waitForExistence(timeout: 10))
        activate(historical)
        activate(app.buttons["chat.people"])
        let everyone = app.buttons["people.collection.Everyone"]
        XCTAssertTrue(everyone.waitForExistence(timeout: 5))
        activate(everyone)
        let person = app.descendants(matching: .any)["people.row.00000000-0000-0000-0000-000000000777"]
        XCTAssertTrue(person.waitForExistence(timeout: 10))
        XCTAssertLessThan(everyone.frame.maxX, person.frame.minX)
        activate(app.buttons["people.collection.Possible duplicates"])
        XCTAssertFalse(person.exists)
        attachEmailScreenshot("A2 People directory")
    }

    func testA2InlineUnsubscribeKeepsTheConfirmation() {
        addSubscriptionsFixture()
        app.launch()
        openEmailMode()
        openSubscriptions()
        let action = app.buttons["email.subscription.inline.\(Self.newsSubscriptionID)"]
        XCTAssertTrue(action.waitForExistence(timeout: 5))
        activate(action)
        checkConfirmation(title: "Unsubscribe from this sender?",
                          naming: [("Daily Brief", "One-click request to daily.example.test")])
        XCTAssertEqual(subscriptionStateCount("Unsubscribed"), 0)
        activate(app.buttons["email.unsubscribe.confirm"])
        waitForSubscriptionState("Unsubscribed", count: 1)
    }

    /// A loaded SVG used to rebuild the viewer's implicit navigation columns,
    /// raising an AppKit exception before the owner could save the file.
    func testSVGArtifactCanBeDownloadedOnMac() throws {
        app.launchArguments.append("--ui-testing-artifact")
        app.launch()
        let row = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.25)).click()
        let file = app.buttons["garden.svg"]
        XCTAssertTrue(file.waitForExistence(timeout: 5))
        file.click()
        let download = app.buttons["Download"]
        XCTAssertTrue(download.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["image/svg+xml"].waitForExistence(timeout: 5))
        XCTAssertTrue(download.isEnabled)
        download.click()
        let cancel = app.sheets.buttons["CancelButton"]
        XCTAssertTrue(cancel.waitForExistence(timeout: 5))
        cancel.click()
        XCTAssertTrue(download.waitForExistence(timeout: 5))
        XCTAssertTrue(download.isEnabled)

        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        download.click()
        let save = app.sheets.buttons["OKButton"]
        XCTAssertTrue(save.waitForExistence(timeout: 5))
        app.typeKey("g", modifierFlags: [.command, .shift])
        let goToFolder = app.sheets.textFields["PathTextField"]
        XCTAssertTrue(goToFolder.waitForExistence(timeout: 5))
        goToFolder.typeKey("a", modifierFlags: .command)
        goToFolder.typeText(directory.path)
        goToFolder.typeKey(.return, modifierFlags: [])
        save.click()
        let destination = directory.appendingPathComponent("garden.svg")
        let written = XCTNSPredicateExpectation(
            predicate: NSPredicate { _, _ in FileManager.default.fileExists(atPath: destination.path) }, object: nil
        )
        XCTAssertEqual(XCTWaiter.wait(for: [written], timeout: 5), .completed)
        let expected = #"<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="green"/></svg>"#
        XCTAssertEqual(try Data(contentsOf: destination), Data(expected.utf8))
        // The preview lives in the right panel (ADR-0237); hiding the panel
        // leaves the file under its answer.
        app.buttons["inspector.hide"].click()
        XCTAssertFalse(app.descendants(matching: .any)["inspector.artifact.preview"].exists)
        XCTAssertTrue(file.waitForExistence(timeout: 5))
    }

    // MARK: - The right panel (ADR-0237)

    /// Opens the Email panel when the window started too narrow for it.
    private func showEmailPanel() {
        let panel = app.descendants(matching: .any)["inspector.email"]
        if !panel.exists { app.buttons["inspector.toggle"].click() }
        XCTAssertTrue(panel.waitForExistence(timeout: 5))
    }

    /// A wide window opens Chat's panel with the run's status; the toolbar
    /// hides and shows it, and calls stay exclusively in that panel.
    func testChatPanelHoldsTheRunAndRevealsAWorkStripsCalls() {
        app.launchArguments.append("--ui-testing-mixed-tools")
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1400,860"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        app.launch()
        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 5))
        XCTAssertTrue(waitForFrame(of: window, timeout: 5) {
            abs($0.width - 1400) <= 3 && $0.minX >= 0
        })
        let panel = app.descendants(matching: .any)["inspector.chat"]
        XCTAssertTrue(panel.waitForExistence(timeout: 5), "A wide window opens the panel")
        XCTAssertTrue(app.descendants(matching: .any)["inspector.chat.run"].exists)
        let toggle = app.buttons["inspector.toggle"]
        let input = app.descendants(matching: .any)["chat.composer"]
        XCTAssertTrue(input.waitForExistence(timeout: 5))
        waitForSettledFrame(of: input)
        let withPanel = input.frame
        let panelWidth = panel.frame.width
        XCTAssertEqual(toggle.value as? String, "Shown")
        toggle.click()
        XCTAssertFalse(panel.waitForExistence(timeout: 1))
        XCTAssertEqual(toggle.value as? String, "Hidden")
        XCTAssertTrue(waitForFrame(of: input, timeout: 5) {
            abs($0.width - withPanel.width - panelWidth) <= 4
                && abs($0.minX - withPanel.minX) <= 2
        }, "Hiding the panel must widen Chat instead of adding empty center-panel margins")
        toggle.click()
        XCTAssertTrue(panel.waitForExistence(timeout: 5))
        XCTAssertTrue(waitForFrame(of: input, timeout: 5) {
            abs($0.width - withPanel.width) <= 2
        })

        let row = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        // On a shared desktop another app can come forward and take the click
        // (seen 2026-10-21: the row click landed in another app's window). The
        // app is brought forward and the row must be hittable before the one click.
        app.activate()
        let hittable = XCTNSPredicateExpectation(predicate: NSPredicate(format: "isHittable == true"), object: row)
        XCTAssertEqual(XCTWaiter.wait(for: [hittable], timeout: 5), .completed, "The row must be clickable")
        openSidebarRow(row)
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].waitForExistence(timeout: 5))
        let composer = app.descendants(matching: .any)["chat.composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.click()
        composer.typeText("Show tool summary")
        app.buttons["Send"].click()
        XCTAssertFalse(app.buttons["tool.bundle.gmail-1"].exists)
        XCTAssertTrue(app.descendants(matching: .any)["inspector.ledger.tool:gmail-1"].waitForExistence(timeout: 10))
        let detail = app.descendants(matching: .any)["tool.detail.gmail-1"].firstMatch
        XCTAssertTrue(detail.waitForExistence(timeout: 5))
        XCTAssertTrue(panel.frame.contains(detail.frame), "Calls appear only in the right panel")
        attachEmailScreenshot("Chat right panel")
    }

    /// The width the owner drags the panel to is kept for the next launch.
    func testPanelWidthPersistsAcrossApplicationRestart() {
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1400,860"
        app.launch()
        let panel = app.descendants(matching: .any)["inspector.chat"]
        XCTAssertTrue(panel.waitForExistence(timeout: 10))
        let before = panel.frame.width
        let handle = app.descendants(matching: .any)["inspector.resize"]
        XCTAssertTrue(handle.waitForExistence(timeout: 5))
        let start = handle.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.2, thenDragTo: start.withOffset(CGVector(dx: -60, dy: 0)))
        XCTAssertTrue(waitForFrame(of: panel, timeout: 5) { abs($0.width - (before + 60)) <= 4 })
        let dragged = panel.frame.width
        app.terminate()
        app.launchEnvironment["VEETBOT_UI_TEST_KEEP_INSPECTOR"] = "1"
        app.launch()
        let relaunched = app.descendants(matching: .any)["inspector.chat"]
        XCTAssertTrue(relaunched.waitForExistence(timeout: 10))
        XCTAssertTrue(waitForFrame(of: relaunched, timeout: 5) { abs($0.width - dragged) <= 2 })
    }

    /// Email's panel holds the thread's context, Teach Veetbot leaves the
    /// thread for it, the sender's link reveals their card, and a window too
    /// narrow for a fourth column starts with the panel folded away.
    func testEmailPanelShowsTheThreadsContextAndFoldsInANarrowWindow() {
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1400,860"
        app.launch()
        openEmailAfterWindowResize(width: 1400, height: 860)
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        activate(row)
        XCTAssertTrue(app.staticTexts["Please review the agenda before Friday."].waitForExistence(timeout: 5))
        let panel = app.descendants(matching: .any)["inspector.email"]
        XCTAssertTrue(panel.waitForExistence(timeout: 5))
        for identifier in [
            "inspector.email.summary", "inspector.email.priority", "inspector.email.sender", "inspector.email.message",
            "inspector.email.teach", "inspector.email.draft", "inspector.email.thread",
        ] {
            XCTAssertTrue(app.descendants(matching: .any)[identifier].waitForExistence(timeout: 5), identifier)
        }
        // The thread keeps the mail; what describes it moved to the panel.
        XCTAssertFalse(app.buttons["View in People"].exists, "The sender's name opens the panel instead")
        XCTAssertFalse(app.descendants(matching: .any)["Message details"].exists, "The envelope is in the panel")
        let feedback = app.buttons["email.feedback"]
        XCTAssertTrue(feedback.exists)
        XCTAssertTrue(panel.frame.contains(feedback.frame), "Teach Veetbot sits in the panel, not the thread")
        app.buttons["email.sender.people"].firstMatch.click()
        XCTAssertTrue(app.descendants(matching: .any)["inspector.email.sender"].waitForExistence(timeout: 5))
        attachEmailScreenshot("Email right panel")

        app.terminate()
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,760"
        app.launch()
        openEmailAfterWindowResize(width: 1100, height: 760)
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        activate(row)
        XCTAssertTrue(app.staticTexts["Please review the agenda before Friday."].waitForExistence(timeout: 5))
        XCTAssertFalse(panel.waitForExistence(timeout: 2), "Three columns leave no room for the panel")
        showEmailPanel()
        XCTAssertTrue(app.descendants(matching: .any)["inspector.email.priority"].waitForExistence(timeout: 5))
    }

    /// The fixture resizes the window after launch. A click computed before
    /// that resize can land in the title bar instead of the Email control.
    private func openEmailAfterWindowResize(width: CGFloat, height: CGFloat) {
        app.activate()
        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 10))
        XCTAssertTrue(waitForFrame(of: window, timeout: 10) {
            abs($0.width - width) <= 1 && abs($0.height - height) <= 1
        })
        let email = window.buttons["mode.email"]
        let ready = XCTNSPredicateExpectation(predicate: NSPredicate(format: "hittable == true"), object: email)
        XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 10), .completed)
        waitForSettledFrame(of: email, stableFor: 3)
        email.click()
    }
    #endif

    /// Twenty mixed calls occupy one compact row; each original result remains expandable.
    func testMixedToolSummaryKeepsAnswerVisibleAndExpandsDetails() {
        app.launchArguments.append("--ui-testing-mixed-tools")
        #if os(macOS)
        // Exercise discovery after the original one-shot resize deadline.
        app.launchArguments.append("--ui-testing-delayed-window")
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "900,650"
        #endif
        app.launch()
        #if os(macOS)
        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 5))
        XCTAssertTrue(waitForFrame(of: window, timeout: 5) { $0.minX >= 0 && $0.width == 1000 })
        #endif
        let row = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        #if os(macOS)
        // The plain sidebar button's vertical midpoint is between its two text lines.
        row.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.25)).click()
        #else
        row.tap()
        #endif
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].waitForExistence(timeout: 5))
        let composer = app.descendants(matching: .any)["chat.composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        #if os(macOS)
        composer.click()
        #else
        composer.tap()
        #endif
        composer.typeText("Show tool summary")
        #if os(macOS)
        app.buttons["Send"].click()
        #else
        app.buttons["Send"].tap()
        #endif
        let summary = app.buttons["tool.bundle.gmail-1"]
        let answer = app.staticTexts["Your answer is visible below the tool summary."].firstMatch
        XCTAssertTrue(answer.waitForExistence(timeout: 10))
        #if os(macOS)
        checkAnswerInsideTranscript(answer, above: composer)
        XCTAssertFalse(summary.exists)
        app.buttons["inspector.toggle"].click()
        #else
        XCTAssertTrue(summary.waitForExistence(timeout: 10))
        // The bundle appears with its first call and grows as the stream's
        // twenty calls are reduced one frame at a time (seen in the Tart VM on
        // 2026-10-22: "4 tool calls · Completed"), so wait for the whole run.
        let complete = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "label CONTAINS '20 tool calls'"), object: summary)
        XCTAssertEqual(XCTWaiter.wait(for: [complete], timeout: 10), .completed, summary.label)
        XCTAssertTrue(summary.label.contains("20 tool calls"))
        XCTAssertTrue(summary.label.contains("6 Failed"))
        XCTAssertEqual(summary.value as? String, "Collapsed")
        XCTAssertLessThan(summary.frame.height, 80)
        checkAnswerStaysVisible(answer, below: summary)
        summary.tap()
        #endif
        let detail = app.descendants(matching: .any)["tool.detail.gmail-1"].firstMatch
        XCTAssertTrue(detail.waitForExistence(timeout: 5))
        #if os(macOS)
        detail.click()
        #else
        detail.tap()
        #endif
        XCTAssertTrue(app.staticTexts["Example result 1"].waitForExistence(timeout: 5))
        #if os(macOS)
        app.buttons["inspector.hide"].click()
        XCTAssertFalse(summary.exists)
        #else
        summary.tap()
        XCTAssertEqual(summary.value as? String, "Collapsed")
        #endif
        XCTAssertFalse(app.staticTexts["Example result 1"].exists)
        #if os(macOS)
        checkAnswerInsideTranscript(answer, above: composer)
        #else
        checkAnswerStaysVisible(answer, below: summary)
        #endif
    }

    /// The owner reads a short scheduled report, then opens the weekday
    /// briefing, which is about as tall as their window. Each visit must draw
    /// its report rather than a blank, unscrollable transcript.
    func testBriefingAboutAWindowTallIsDrawnAfterAShortReport() {
        app.launchArguments += ["--ui-testing-unread-report", "--ui-testing-briefing-replay"]
        for key in ["VEETBOT_UI_TEST_BRIEFING_STORIES", "VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] {
            if let value = ProcessInfo.processInfo.environment[key] { app.launchEnvironment[key] = value }
        }
        app.launch()
        let group = app.descendants(matching: .any)[
            "sidebar.schedule.00000000-0000-0000-0000-000000000654"
        ]
        XCTAssertTrue(group.waitForExistence(timeout: 10))
        let today = app.descendants(matching: .any)[
            "sidebar.session.00000000-0000-0000-0000-0000000005A1"
        ]
        let yesterday = app.descendants(matching: .any)[
            "sidebar.session.00000000-0000-0000-0000-0000000005A3"
        ]
        for _ in 0..<3 where !yesterday.waitForExistence(timeout: 2) {
            if group.value as? String != "Expanded" { openSidebarRow(group) }
        }
        XCTAssertTrue(today.waitForExistence(timeout: 5))
        for visit in 1...3 {
            for (row, day) in [(yesterday, "Yesterday's"), (today, "Today's")] {
                openSidebarRow(row)
                let closingText = "\(day) briefing closing line."
                let closing = app.staticTexts[closingText].firstMatch
                // The replay streams for about a second; judge the settled transcript.
                sleep(3)
                let shot = XCTAttachment(screenshot: app.windows.firstMatch.screenshot())
                shot.name = "\(day)-\(visit)"
                shot.lifetime = .keepAlways
                add(shot)
                XCTAssertTrue(closing.exists, "\(day) visit \(visit)\n\(app.debugDescription)")
                // A briefing taller than the window opens with its beginning
                // at the top, below the bars, not scrolled past it.
                let heading = staticTextFrame("\(day) briefing")
                let top = transcriptTop()
                XCTAssertNotNil(heading, "\(day) visit \(visit)\n\(app.debugDescription)")
                XCTAssertGreaterThanOrEqual(
                    heading?.minY ?? -.infinity, top - 1, "\(day) visit \(visit): heading \(String(describing: heading)) top \(top)"
                )
                // A drawn transcript scrolls on to its last line.
                // A Mac keeps static text in its value, iOS in its label.
                let transcript = app.scrollViews.containing(
                    NSPredicate(format: "label == %@ OR value == %@", closingText, closingText)
                ).firstMatch
                #if os(macOS)
                let window = app.windows.firstMatch
                for _ in 0..<6 where !window.frame.contains(closing.frame) {
                    transcript.scroll(byDeltaX: 0, deltaY: -250)
                }
                XCTAssertTrue(
                    window.frame.contains(closing.frame),
                    "\(day) visit \(visit)\n\(app.debugDescription)"
                )
                #else
                for _ in 0..<6 where !closing.isHittable { transcript.swipeUp() }
                XCTAssertTrue(closing.isHittable, "\(day) visit \(visit)\n\(app.debugDescription)")
                #endif
            }
        }
    }

    private func openSidebarRow(_ row: XCUIElement) {
        #if os(macOS)
        // The plain sidebar button's vertical midpoint is between its two text lines.
        row.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.25)).click()
        #else
        // On an iPhone the first navigation-bar button is People, not the
        // chat list's toggle, so the list comes back through its own toggle.
        revealSidebarIfNeeded(for: row)
        row.tap()
        #endif
    }

    /// The composer floats over the end of the transcript, so an answer that
    /// grows while it streams has to keep its end clear of it.
    func testStreamedAnswerEndsAboveTheComposer() {
        app.launchArguments.append("--ui-testing-streamed-answer")
        app.launch()
        let composer = sendStreamedAnswerRequest()
        let lastLine = streamedText("The answer's last line.")
        XCTAssertTrue(lastLine.waitForExistence(timeout: 10))
        let firstLine = streamedParagraph(1)
        XCTAssertTrue(firstLine.waitForExistence(timeout: 5))
        waitForStreamedAnswerToSettle()
        attachEmailScreenshot("Streamed answer")
        // The answer's own actions end the transcript, beneath its last line;
        // they show on hover on the Mac and after a tap on an iPad.
        revealMessageActions(lastLine)
        XCTAssertTrue(
            app.buttons.matching(NSPredicate(format: "label == %@", "Copy message")).firstMatch
                .waitForExistence(timeout: 5)
        )
        let answerActions = app.buttons
            .matching(NSPredicate(format: "label == %@", "Copy message"))
            .allElementsBoundByIndex
            .map(\.frame)
            .max { $0.maxY < $1.maxY } ?? .null
        let geometry = "last line \(lastLine.frame) actions \(answerActions) first line \(firstLine.frame) composer \(composer.frame)"
        XCTAssertGreaterThan(answerActions.minY, lastLine.frame.maxY, geometry)
        XCTAssertLessThanOrEqual(answerActions.maxY, composer.frame.minY, geometry)
        XCTAssertGreaterThan(firstLine.frame.minY, app.windows.firstMatch.frame.minY, geometry)
    }

    /// An answer taller than the window keeps its beginning in view while it
    /// streams and after the run ends, so it can be read from the top.
    func testLongStreamedAnswerKeepsItsBeginningInView() {
        app.launchArguments += ["--ui-testing-streamed-answer", "--ui-testing-long-answer"]
        app.launch()
        let composer = sendStreamedAnswerRequest()
        XCTAssertTrue(streamedParagraph(2).waitForExistence(timeout: 10))
        waitForStreamedAnswerToSettle()
        attachEmailScreenshot("Long streamed answer")
        let firstLine = streamedParagraph(1)
        XCTAssertTrue(firstLine.exists)
        let top = transcriptTop()
        let geometry = "first line \(firstLine.frame) composer \(composer.frame) transcript top \(top)"
        XCTAssertGreaterThanOrEqual(firstLine.frame.minY, top - 1, geometry)
        XCTAssertLessThan(firstLine.frame.maxY, composer.frame.minY, geometry)
    }

    #if os(iOS)
    /// The paper runs under the home indicator: the strip below the composer
    /// is the transcript's paper, not the window's white.
    func testPaperReachesTheBottomEdgeBelowTheComposer() {
        app.launchArguments.append("--ui-testing-streamed-answer")
        app.launch()
        _ = sendStreamedAnswerRequest()
        let lastLine = streamedText("The answer's last line.")
        XCTAssertTrue(lastLine.waitForExistence(timeout: 10))
        waitForStreamedAnswerToSettle()
        let window = app.windows.firstMatch.frame
        let screenshot = XCUIScreen.main.screenshot().image
        // Both points sit in the conversation's column (beside the iPad's
        // sidebar): paper just past the short last line, clear of the
        // composer's shadow, and the window's bottom edge below it.
        let column = lastLine.frame.maxX + 24
        let paper = pixel(in: screenshot, at: CGPoint(x: column, y: lastLine.frame.midY))
        let bottomEdge = pixel(in: screenshot, at: CGPoint(x: column, y: window.maxY - 2))
        let attachment = XCTAttachment(screenshot: XCUIScreen.main.screenshot())
        attachment.name = "Bottom edge"
        attachment.lifetime = .keepAlways
        add(attachment)
        let difference = zip(paper, bottomEdge).map { abs($0 - $1) }.max() ?? 255
        XCTAssertLessThanOrEqual(difference, 3, "paper \(paper), bottom edge \(bottomEdge)")
    }

    /// The red, green and blue values of one point of a screenshot.
    private func pixel(in image: UIImage, at point: CGPoint) -> [Int] {
        guard let cgImage = image.cgImage else { return [] }
        let x = Int(point.x * image.scale)
        let y = Int(point.y * image.scale)
        var data = [UInt8](repeating: 0, count: 4)
        data.withUnsafeMutableBytes { buffer in
            guard let context = CGContext(
                data: buffer.baseAddress, width: 1, height: 1, bitsPerComponent: 8, bytesPerRow: 4,
                space: CGColorSpaceCreateDeviceRGB(),
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            ) else { return }
            // Core Graphics counts rows from the bottom.
            context.draw(
                cgImage,
                in: CGRect(
                    x: -x, y: -(cgImage.height - 1 - y), width: cgImage.width, height: cgImage.height
                )
            )
        }
        return data.prefix(3).map(Int.init)
    }
    #endif

    /// Opens the fixture conversation and sends the message the streamed answer replies to.
    private func sendStreamedAnswerRequest() -> XCUIElement {
        let row = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        #if os(macOS)
        row.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.25)).click()
        #else
        row.tap()
        #endif
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].waitForExistence(timeout: 5))
        let composer = app.descendants(matching: .any)["chat.composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        #if os(macOS)
        composer.click()
        #else
        composer.tap()
        #endif
        composer.typeText("Stream an answer")
        #if os(macOS)
        app.buttons["Send"].click()
        #else
        app.buttons["Send"].tap()
        #endif
        return composer
    }

    private func streamedParagraph(_ number: Int) -> XCUIElement {
        streamedText("Streamed paragraph \(number) of the answer.")
    }

    /// Mac static text carries its words in `value`, iOS in `label`.
    private func streamedText(_ text: String) -> XCUIElement {
        app.staticTexts
            .matching(NSPredicate(format: "label CONTAINS %@ OR value CONTAINS %@", text, text))
            .firstMatch
    }

    /// The frame of the static text that reads exactly `text`, from one
    /// snapshot of the app: a query by label does not find a Markdown
    /// heading on the Mac, though the snapshot holds it.
    private func staticTextFrame(_ text: String) -> CGRect? {
        guard let root = try? app.snapshot() else { return nil }
        var pending = [root]
        while let node = pending.popLast() {
            if node.elementType == .staticText, node.label == text || node.value as? String == text {
                return node.frame
            }
            pending += node.children
        }
        return nil
    }

    /// The top of the transcript's readable area: below the navigation bar
    /// it scrolls under on iOS, and the top of its own frame on the Mac.
    private func transcriptTop() -> CGFloat {
        let transcript = app.scrollViews.containing(
            NSPredicate(format: "identifier == %@", "chat.heading")
        ).firstMatch
        var top = transcript.exists ? transcript.frame.minY : app.windows.firstMatch.frame.minY
        #if os(iOS)
        let bar = app.navigationBars.allElementsBoundByIndex
            .map(\.frame)
            .filter { $0.minX < transcript.frame.midX && $0.maxX > transcript.frame.midX }
            .map(\.maxY)
            .max()
        if let bar { top = max(top, bar) }
        #endif
        return top
    }

    /// Waits for the run to end and any scroll to come to rest before frames are read.
    private func waitForStreamedAnswerToSettle() {
        let completed = NSPredicate(format: "exists == false")
        expectation(for: completed, evaluatedWith: app.descendants(matching: .any)["chat.activity"])
        waitForExpectations(timeout: 20)
        Thread.sleep(forTimeInterval: 1)
    }

    #if os(macOS)
    /// Checks visible transcript geometry after streaming or an inspector resize.
    private func checkAnswerInsideTranscript(_ answer: XCUIElement, above composer: XCUIElement) {
        XCTAssertTrue(waitForFrame(of: answer, timeout: 5) { frame in
            frame.width > 0 && frame.height > 0
                && self.app.windows.firstMatch.frame.contains(frame)
                && frame.minY >= self.transcriptTop()
                && frame.maxY <= composer.frame.minY
        }, "Answer must remain above the composer and inside the visible transcript: \(answer.frame)")
    }
    #endif

    /// Keeps the answer on screen under the summary row.
    ///
    /// macOS 27 reports SwiftUI's message text as disabled in the accessibility
    /// tree, so hittability no longer follows from visibility there.
    private func checkAnswerStaysVisible(_ answer: XCUIElement, below summary: XCUIElement) {
        #if os(macOS)
        XCTAssertTrue(
            app.windows.firstMatch.frame.contains(answer.frame),
            app.debugDescription
        )
        XCTAssertGreaterThan(answer.frame.minY, summary.frame.maxY)
        #else
        XCTAssertTrue(answer.isHittable)
        #endif
    }

    /// The fixture holds a Gmail operation pending for four thread reads and the
    /// client polls once a second, so an outcome takes three seconds on an idle
    /// host and longer beside a second simulator. Five seconds was too near that.
    private static let archiveOutcomeTimeout: TimeInterval = 20

    /// Legacy views live in the Find other mail disclosure on the A2 surface.
    private func selectLegacyMailView(_ title: String) {
        #if os(macOS)
        let choice = app.radioButtons[title]
        #else
        let choice = app.buttons[title]
        #endif
        if !choice.exists {
            let disclosure = app.buttons["email.other-mail-views"]
            XCTAssertTrue(disclosure.waitForExistence(timeout: 5))
            activate(disclosure)
        }
        XCTAssertTrue(choice.waitForExistence(timeout: 5))
        activate(choice)
    }

    /// Archiving the only thread clears detail; reopen it from Other mail to restore its Inbox state.
    private func checkHandledActionInDetail() {
        let action = app.buttons["email.handled.detail"]
        XCTAssertTrue(action.waitForExistence(timeout: 5))
        XCTAssertEqual(action.label, "Archive in Gmail")
        #if os(macOS)
        action.click()
        #else
        action.tap()
        #endif
        XCTAssertFalse(action.exists, "Archiving the last visible email must clear its detail")
        selectLegacyMailView("Other mail")
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        #if os(macOS)
        row.click()
        #else
        row.tap()
        #endif
        let handled = NSPredicate(format: "label == %@ AND enabled == true", "Move to Inbox")
        expectation(for: handled, evaluatedWith: action)
        waitForExpectations(timeout: Self.archiveOutcomeTimeout)
        #if os(macOS)
        action.click()
        #else
        action.tap()
        #endif
        let unhandled = NSPredicate(format: "label == %@ AND enabled == true", "Archive in Gmail")
        expectation(for: unhandled, evaluatedWith: action)
        waitForExpectations(timeout: Self.archiveOutcomeTimeout)
    }

    /// The detail checkbox replaces the archived message with the next conversation's actual content.
    func testEmailDetailArchiveDisplaysNextConversation() {
        app.launchArguments.append("--ui-testing-email-archive-next")
        app.launch()
        let mode = app.buttons["mode.email"]
        XCTAssertTrue(mode.waitForExistence(timeout: 10))
        #if os(macOS)
        mode.click()
        #else
        mode.tap()
        #endif
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        #if os(macOS)
        row.click()
        #else
        row.tap()
        #endif
        let action = app.buttons["email.handled.detail"]
        XCTAssertTrue(action.waitForExistence(timeout: 5))
        #if os(macOS)
        action.click()
        #else
        action.tap()
        #endif
        XCTAssertTrue(app.staticTexts["Here is the next email to read."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["Please review the agenda before Friday."].exists)
    }

    /// Checking off the open conversation's inbox row also moves the reading pane to the next conversation.
    func testEmailRowArchiveOfOpenConversationDisplaysNext() throws {
        #if os(iOS)
        try XCTSkipIf(UIDevice.current.userInterfaceIdiom == .phone,
                      "Compact navigation covers the inbox while a conversation is open")
        #endif
        app.launchArguments.append("--ui-testing-email-archive-next")
        app.launch()
        let mode = app.buttons["mode.email"]
        XCTAssertTrue(mode.waitForExistence(timeout: 10))
        #if os(macOS)
        mode.click()
        #else
        mode.tap()
        #endif
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        #if os(macOS)
        row.click()
        #else
        row.tap()
        #endif
        XCTAssertTrue(app.staticTexts["Please review the agenda before Friday."].waitForExistence(timeout: 5))
        let check = app.buttons["email.handled.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(check.waitForExistence(timeout: 5))
        #if os(macOS)
        check.click()
        #else
        check.tap()
        #endif
        XCTAssertTrue(app.staticTexts["Here is the next email to read."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["Please review the agenda before Friday."].exists)
    }

    /// Removes a row immediately without progress messages, then finds its confirmed state in Other mail.
    func testEmailCanBeCheckedOffFromInboxWithoutOpeningThread() {
        app.launch()
        let mode = app.buttons["mode.email"]
        XCTAssertTrue(mode.waitForExistence(timeout: 10))
        #if os(macOS)
        mode.click()
        #else
        mode.tap()
        #endif
        let check = app.buttons["email.handled.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(check.waitForExistence(timeout: 10))
        #if os(macOS)
        check.click()
        #else
        check.tap()
        #endif
        let progress = app.staticTexts["email.archive.status.00000000-0000-0000-0000-000000000801"]
        // A predicate expectation waits one second before its first snapshot, racing a one-second deadline.
        XCTAssertFalse(check.exists, "Archiving must remove the row as soon as the tap finishes")
        XCTAssertFalse(progress.exists)
        XCTAssertFalse(app.buttons["email.handled.detail"].exists)
        selectLegacyMailView("Other mail")
        XCTAssertTrue(check.waitForExistence(timeout: Self.archiveOutcomeTimeout))
        XCTAssertEqual(check.label, "Move to Inbox")
    }

    /// A failed Gmail operation restores the optimistically removed row with an explicit retryable outcome.
    func testEmailArchiveFailurePreservesInboxRow() {
        app.launchArguments.append("--ui-testing-email-archive-failure")
        app.launch()
        let mode = app.buttons["mode.email"]
        XCTAssertTrue(mode.waitForExistence(timeout: 10))
        #if os(macOS)
        mode.click()
        #else
        mode.tap()
        #endif
        let action = app.buttons["email.handled.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(action.waitForExistence(timeout: 10))
        #if os(macOS)
        action.click()
        #else
        action.tap()
        #endif
        XCTAssertFalse(action.exists, "The row must disappear before the asynchronous failure arrives")
        let status = app.staticTexts["email.archive.status.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(status.waitForExistence(timeout: 5))
        #if os(macOS)
        expectation(for: NSPredicate(format: "value CONTAINS %@", "could not complete"), evaluatedWith: status)
        #else
        expectation(for: NSPredicate(format: "label CONTAINS %@", "could not complete"), evaluatedWith: status)
        #endif
        waitForExpectations(timeout: Self.archiveOutcomeTimeout)
        XCTAssertTrue(action.exists)
        XCTAssertTrue(action.isEnabled)
        XCTAssertEqual(action.label, "Archive in Gmail")
    }

    /// The cleaned reading view keeps an accessible, reversible route to source text.
    func testEmailReadingViewCanRevealOriginal() {
        app.launchArguments.append("--ui-testing-email-reader")
        app.launch()
        let mode = app.buttons["mode.email"]
        XCTAssertTrue(mode.waitForExistence(timeout: 10))
        activate(mode)
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        activate(row)
        #if os(macOS)
        // On the Mac, Show original is one switch for the thread, in the panel.
        showEmailPanel()
        let toggle = firstElement("email.reader.original")
        #else
        let toggle = app.buttons["email.reader.original"]
        #endif
        XCTAssertTrue(toggle.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Research and discovery"].exists)
        attachEmailScreenshot("Clean email reading view")
        // macOS carries a text's string in its value, iOS in its label.
        let original = app.staticTexts.containing(
            NSPredicate(format: "label CONTAINS %@ OR value CONTAINS %@", "Unsubscribe", "Unsubscribe")).firstMatch
        activate(toggle)
        XCTAssertTrue(original.waitForExistence(timeout: 5))
        attachEmailScreenshot("Original email preserved")
        activate(toggle)
        XCTAssertTrue(app.staticTexts["Research and discovery"].waitForExistence(timeout: 5))
        XCTAssertFalse(original.exists)
    }

    /// Verifies the reading and reply flow with normal platform appearance.
    func testEmailReadingKeepsFeedbackOptionalAndReplyReachable() {
        app.launch()
        checkEmailReadingFlow()
    }

    /// Exercises the same native controls with a deterministic dark appearance.
    func testEmailReadingInDarkAppearance() {
        app.launchEnvironment["VEETBOT_UI_TEST_COLOR_SCHEME"] = "dark"
        app.launch()
        checkEmailReadingFlow()
    }

    /// Checks inbox discovery, optional feedback, direct reply access and toolbar isolation.
    private func checkEmailReadingFlow() {
        let emailMode = app.buttons["mode.email"]
        XCTAssertTrue(emailMode.waitForExistence(timeout: 10))
        #if os(macOS)
        XCTAssertTrue(app.buttons["sidebar.settings"].isHittable, "Chat settings must be visible before switching modes")
        #endif
        activate(emailMode)
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        XCTAssertTrue(app.textFields["email.search"].exists)
        XCTAssertFalse(app.buttons["sidebar.settings"].isHittable, "The hidden Chat toolbar must not leak into Email")
        XCTAssertTrue(app.buttons["email.learning"].isHittable)
        XCTAssertTrue(app.buttons["email.refresh"].isHittable)
        #if os(macOS)
        activate(app.buttons["email.learning"])
        XCTAssertTrue(app.staticTexts["Email learning"].waitForExistence(timeout: 5))
        activate(app.buttons["Done"])
        #endif
        attachEmailScreenshot("Priority inbox")
        activate(row)
        XCTAssertTrue(app.staticTexts["Please review the agenda before Friday."].waitForExistence(timeout: 5))
        attachEmailScreenshot("Reading a thread")

        let reply = app.buttons["email.jump-to-reply"]
        XCTAssertTrue(reply.waitForExistence(timeout: 5), "A reply must be reachable without scrolling through the conversation")
        XCTAssertTrue(reply.isHittable)
        XCTAssertFalse(app.textFields["Explain what matters (optional)"].isHittable,
                       "Feedback must not compete with reading the message")
        activate(reply)
        let editor = app.textViews["email.draft-body"]
        XCTAssertTrue(editor.waitForExistence(timeout: 5))
        XCTAssertTrue(editor.isHittable)
        XCTAssertTrue(app.buttons["email.review-send"].isHittable)
        attachEmailScreenshot("Reply composer")

        let envelope = app.buttons["email.draft-envelope"]
        XCTAssertTrue(envelope.exists)
        activate(envelope)
        XCTAssertTrue(app.textFields["email.draft-cc"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.textFields["email.draft-bcc"].exists)
        XCTAssertTrue(app.textFields["email.draft-subject"].exists)
        activate(app.buttons["mode.chat"])
        XCTAssertFalse(app.textFields["email.search"].isHittable, "Mail search belongs to Email mode")
        #if os(macOS)
        XCTAssertTrue(app.buttons["sidebar.settings"].isHittable,
                      "Chat must restore its toolbar after leaving Email's split view")
        for identifier in ["sidebar.memory", "sidebar.persona", "sidebar.schedules"] {
            XCTAssertTrue(app.buttons[identifier].isHittable, "Global Chat actions must fit in the window toolbar")
        }
        XCTAssertFalse(app.buttons["email.learning"].isHittable)
        XCTAssertFalse(app.buttons["email.refresh"].isHittable)
        #endif
    }

    /// Activates a control using the platform's native input action.
    // MARK: - Conversation folders (Milestone 29)

    private static let folderID = "00000000-0000-0000-0000-000000000F01"
    private static let proposedFolderID = "00000000-0000-0000-0000-000000000F02"
    private static let workFolderID = "00000000-0000-0000-0000-000000000F03"
    private static let proposalID = "00000000-0000-0000-0000-000000000E01"
    private static let followUpProposalID = "00000000-0000-0000-0000-000000000E02"
    private static let firstSessionID = "00000000-0000-0000-0000-000000000123"
    private static let secondSessionID = "00000000-0000-0000-0000-000000000456"
    private static let planningSessionID = "00000000-0000-0000-0000-0000000004A1"
    private static let proposedTitles = [
        "Historical chat",
        "Flights to Lisbon in October",
        "Alfama hotel shortlist",
        "Day trip to Sintra and Cascais",
    ]

    /// Every folder journey adds the fixture argument and launches once. The
    /// folder fixture's sidebar is taller than the default Mac window.
    private func addFolderFixture() {
        app.launchArguments.append("--ui-testing-folders")
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
    }

    private func element(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any)[identifier]
    }

    private func folderHeader(_ id: String) -> XCUIElement {
        element("sidebar.folder.\(id)")
    }

    private func sessionRow(_ id: String) -> XCUIElement {
        element("sidebar.session.\(id)")
    }

    /// The phone's list lazily creates folder rows below the five shortcuts.
    /// Close Recent chats before exercising the existing folder-only journeys.
    private func collapseRecentChats() {
        let header = app.buttons["sidebar.recent-chats"]
        XCTAssertTrue(header.waitForExistence(timeout: 10))
        if header.value as? String == "Expanded" { activate(header) }
        XCTAssertTrue(waitForFolder(header, expanded: false))
    }

    /// A conversation's move menu (design system revision 2): on the Mac it
    /// replaces the row's trailing slot while the pointer is over the row; on
    /// touch it is Move to Folder in the row's long-press menu.
    private func openMoveMenu(for row: XCUIElement, sessionID: String) {
        XCTAssertTrue(reveal(row))
        let move = element("sidebar.session.move.\(sessionID)")
        #if os(macOS)
        row.hover()
        XCTAssertTrue(move.waitForExistence(timeout: 5), app.debugDescription)
        move.click()
        #else
        row.press(forDuration: 1.2)
        let submenu = move.waitForExistence(timeout: 3) ? move : app.buttons["Move to Folder"]
        XCTAssertTrue(submenu.waitForExistence(timeout: 5), app.debugDescription)
        submenu.tap()
        #endif
    }

    /// A menu item by its identifier. A SwiftUI context menu on iOS drops
    /// its items' identifiers, so there the item is the unidentified button
    /// with its label (a folder row of the same name carries an identifier).
    private func menuItem(_ identifier: String, label: String) -> XCUIElement {
        let byIdentifier = element(identifier)
        #if os(iOS)
        if !byIdentifier.waitForExistence(timeout: 2) {
            return app.buttons.matching(NSPredicate(format: "label == %@ AND identifier == ''", label)).firstMatch
        }
        #endif
        return byIdentifier
    }

    private func waitForDisappearance(of element: XCUIElement, timeout: TimeInterval = 5) {
        let gone = expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: element)
        wait(for: [gone], timeout: timeout)
    }

    /// Waits until a folder header reports the expected name and state.
    private func waitForFolder(
        _ header: XCUIElement, label: String? = nil, expanded: Bool, timeout: TimeInterval = 5
    ) -> Bool {
        var format = "exists == true AND value == %@"
        var arguments: [Any] = [expanded ? "Expanded" : "Collapsed"]
        if let label {
            format += " AND label == %@"
            arguments.append(label)
        }
        let predicate = NSPredicate(format: format, argumentArray: arguments)
        return XCTWaiter().wait(
            for: [XCTNSPredicateExpectation(predicate: predicate, object: header)], timeout: timeout
        ) == .completed
    }

    /// Scrolls the sidebar until `target` is on screen: on iPhone the folder
    /// fixture's sidebar is taller than the display.
    @discardableResult
    private func reveal(_ target: XCUIElement) -> Bool {
        if target.waitForExistence(timeout: 2), target.isHittable { return true }
        #if os(iOS)
        let list = app.collectionViews.firstMatch
        for direction in [true, false] {
            for _ in 0..<6 {
                if direction { list.swipeUp() } else { list.swipeDown() }
                if target.exists, target.isHittable { return true }
            }
        }
        #endif
        return target.exists
    }

    private func typeIntoFolderNameField(_ text: String) {
        let field = element("folder.name")
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        #if os(macOS)
        activate(field)
        field.typeKey("a", modifierFlags: .command)
        #else
        // Tap the trailing edge so the caret follows any text already there.
        field.coordinate(withNormalizedOffset: CGVector(dx: 0.97, dy: 0.5)).tap()
        if let current = field.value as? String, !current.isEmpty, current != field.placeholderValue {
            field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: current.count))
        }
        #endif
        field.typeText(text)
        // iOS exposes a toolbar item's identifier on both the item container and
        // its button, so an untyped query finds two elements; ask for the button.
        activate(app.buttons["folder.save"])
    }

    /// Retains a folder rendering for visual review even when the test passes.
    private func attachFolderScreenshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func testRecentChatsIncludesFiledChatsAndCollapses() {
        addFolderFixture()
        app.launch()
        let header = app.buttons["sidebar.recent-chats"]
        XCTAssertTrue(header.waitForExistence(timeout: 10))
        XCTAssertEqual(header.value as? String, "Expanded")
        let recentIDs = [Self.firstSessionID, Self.secondSessionID, Self.planningSessionID,
                         "00000000-0000-0000-0000-0000000004A2",
                         "00000000-0000-0000-0000-0000000004A3"]
        for id in recentIDs {
            XCTAssertTrue(element("sidebar.recent.session.\(id)").waitForExistence(timeout: 5), app.debugDescription)
        }
        XCTAssertFalse(element("sidebar.recent.session.00000000-0000-0000-0000-0000000004A4").exists)
        attachFolderScreenshot("Five recent chats including filed conversations")
        activate(header)
        XCTAssertTrue(waitForFolder(header, expanded: false))
        XCTAssertFalse(element("sidebar.recent.session.\(Self.secondSessionID)").exists)
        XCTAssertTrue(waitForFolder(folderHeader(Self.folderID), expanded: false))
        activate(header)
        XCTAssertTrue(waitForFolder(header, expanded: true))
        let filedChat = element("sidebar.recent.session.\(Self.secondSessionID)")
        activate(filedChat)
        XCTAssertTrue(app.staticTexts["Second historical answer loaded"].waitForExistence(timeout: 5))
    }

    func testRecentChatsWorksWithoutFolders() {
        app.launch()
        let header = app.buttons["sidebar.recent-chats"]
        XCTAssertTrue(header.waitForExistence(timeout: 10))
        let recent = element("sidebar.recent.session.\(Self.firstSessionID)")
        XCTAssertTrue(recent.exists)
        XCTAssertFalse(element("sidebar.new-folder").exists)
        activate(recent)
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].waitForExistence(timeout: 5))
    }

    /// The default fixture is an older server whose index has no `folder_id`
    /// key: the sidebar stays flat and shows no folder control at all.
    func testOlderServerSidebarStaysFlatWithoutFolderControls() {
        app.launch()
        let historicalRow = element("sidebar.session.00000000-0000-0000-0000-000000000123")
        XCTAssertTrue(historicalRow.waitForExistence(timeout: 10))
        XCTAssertFalse(element("sidebar.new-folder").exists)
        XCTAssertFalse(element("sidebar.session.move.00000000-0000-0000-0000-000000000123").exists)
        XCTAssertFalse(element("sidebar.proposal.\(Self.proposalID)").exists)
    }

    /// Folders group their conversations beside the suggested folders and the
    /// new-folder control. In solo mode, the default, a folder starts collapsed
    /// and opening it shows its conversations.
    func testFolderSectionsGroupConversations() {
        addFolderFixture()
        app.launch()
        collapseRecentChats()
        let travel = folderHeader(Self.folderID)
        XCTAssertTrue(travel.waitForExistence(timeout: 10))
        XCTAssertTrue(waitForFolder(travel, label: "Travel", expanded: false))
        XCTAssertTrue(waitForFolder(folderHeader(Self.workFolderID), label: "Work", expanded: false))
        XCTAssertFalse(sessionRow(Self.secondSessionID).exists)
        XCTAssertTrue(element("sidebar.proposal.\(Self.proposalID)").exists)
        activate(travel)
        XCTAssertTrue(waitForFolder(travel, expanded: true))
        XCTAssertTrue(sessionRow(Self.secondSessionID).waitForExistence(timeout: 5))
        attachFolderScreenshot("Folders with Travel open")
        XCTAssertTrue(reveal(sessionRow(Self.firstSessionID)))
        XCTAssertTrue(reveal(element("sidebar.new-folder")))
    }

    /// Solo mode keeps one folder open: opening Work closes Travel.
    func testOpeningAFolderInSoloModeClosesTheOpenOne() {
        addFolderFixture()
        app.launch()
        collapseRecentChats()
        let travel = folderHeader(Self.folderID)
        let work = folderHeader(Self.workFolderID)
        XCTAssertTrue(travel.waitForExistence(timeout: 10))
        activate(travel)
        XCTAssertTrue(sessionRow(Self.secondSessionID).waitForExistence(timeout: 5))
        activate(work)
        XCTAssertTrue(waitForFolder(work, expanded: true))
        XCTAssertTrue(waitForFolder(travel, expanded: false))
        XCTAssertTrue(sessionRow(Self.planningSessionID).waitForExistence(timeout: 5))
        waitForDisappearance(of: sessionRow(Self.secondSessionID))
        activate(work)
        XCTAssertTrue(waitForFolder(work, expanded: false))
        waitForDisappearance(of: sessionRow(Self.planningSessionID))
    }

    /// A newly proposed folder leaves every folder as the owner left it.
    func testANewProposalLeavesFolderExpansionAlone() {
        addFolderFixture()
        app.launch()
        collapseRecentChats()
        let travel = folderHeader(Self.folderID)
        let work = folderHeader(Self.workFolderID)
        XCTAssertTrue(travel.waitForExistence(timeout: 10))
        activate(travel)
        XCTAssertTrue(waitForFolder(travel, expanded: true))
        // Declining refreshes the index, and the next pass has a new proposal.
        activate(element("sidebar.proposal.decline.\(Self.proposalID)"))
        XCTAssertTrue(element("sidebar.proposal.\(Self.followUpProposalID)").waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Add to “Travel”"].exists)
        XCTAssertTrue(waitForFolder(travel, expanded: true))
        XCTAssertTrue(waitForFolder(work, expanded: false))
        XCTAssertTrue(sessionRow(Self.secondSessionID).exists)
        XCTAssertFalse(sessionRow(Self.planningSessionID).exists)
    }

    func testNewFolderSheetCreatesAFolder() {
        addFolderFixture()
        app.launch()
        collapseRecentChats()
        let newFolder = element("sidebar.new-folder")
        XCTAssertTrue(folderHeader(Self.folderID).waitForExistence(timeout: 10))
        XCTAssertTrue(reveal(newFolder))
        activate(newFolder)
        typeIntoFolderNameField("Errands")
        let created = folderHeader(Self.proposedFolderID)
        XCTAssertTrue(reveal(created))
        XCTAssertTrue(waitForFolder(created, label: "Errands", expanded: false))
    }

    #if os(macOS)
    func testRenamingAFolderThroughItsMenuUpdatesTheSection() {
        addFolderFixture()
        app.launch()
        let header = folderHeader(Self.folderID)
        XCTAssertTrue(header.waitForExistence(timeout: 10))
        header.rightClick()
        let rename = app.menuItems["Rename…"]
        XCTAssertTrue(rename.waitForExistence(timeout: 5))
        rename.click()
        typeIntoFolderNameField("Trips")
        XCTAssertTrue(waitForFolder(header, label: "Trips", expanded: false))
    }

    /// The name sheet is a compact Mac dialog sized for its field and buttons,
    /// not a navigation split squeezed into a minimum-size sheet.
    func testFolderNameSheetIsSizedForItsContentOnMac() {
        addFolderFixture()
        app.launch()
        let header = folderHeader(Self.folderID)
        XCTAssertTrue(header.waitForExistence(timeout: 10))
        header.rightClick()
        let rename = app.menuItems["Rename…"]
        XCTAssertTrue(rename.waitForExistence(timeout: 5))
        rename.click()
        let field = element("folder.name")
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        let sheet = app.sheets.firstMatch
        XCTAssertTrue(sheet.exists)
        attachFolderScreenshot("Rename folder sheet")
        XCTAssertEqual(field.value as? String, "Travel")
        XCTAssertGreaterThanOrEqual(sheet.frame.width, 380)
        XCTAssertLessThanOrEqual(sheet.frame.height, 240)
        XCTAssertGreaterThanOrEqual(field.frame.width, sheet.frame.width * 0.8)
        let save = app.buttons["folder.save"]
        let cancel = app.buttons["folder.cancel"]
        XCTAssertTrue(save.isHittable)
        XCTAssertTrue(cancel.isHittable)
        XCTAssertGreaterThan(save.frame.minY, field.frame.maxY)
        activate(cancel)
        waitForDisappearance(of: field)
    }

    /// Adjacent folders sit one row apart in a shared section, rather than
    /// each in a section of its own with a section gap between them. The list
    /// rows holding them are measured, not the labels inside: the system sets
    /// the row height and the label height, and both differ between macOS
    /// releases, while rows of one section always touch.
    func testAdjacentFoldersSitOneRowApartOnMac() {
        addFolderFixture()
        app.launch()
        XCTAssertTrue(folderHeader(Self.folderID).waitForExistence(timeout: 10))
        let travel = folderListRow(Self.folderID)
        let work = folderListRow(Self.workFolderID)
        XCTAssertTrue(travel.exists)
        XCTAssertTrue(work.exists)
        attachFolderScreenshot("Folder rows")
        let geometry = "travel row \(travel.frame) work row \(work.frame)"
        let measured = XCTAttachment(string: geometry)
        measured.name = "Folder row geometry"
        measured.lifetime = .keepAlways
        add(measured)
        XCTAssertGreaterThan(travel.frame.height, 0, geometry)
        XCTAssertEqual(work.frame.minY, travel.frame.maxY, accuracy: 1, geometry)
    }

    /// The sidebar list row that holds a folder's header.
    private func folderListRow(_ id: String) -> XCUIElement {
        app.outlineRows
            .containing(NSPredicate(format: "identifier == %@", "sidebar.folder.\(id)"))
            .firstMatch
    }
    #endif

    func testAcceptingASuggestedFolderFilesTheConversation() {
        addFolderFixture()
        app.launch()
        let proposal = element("sidebar.proposal.\(Self.proposalID)")
        XCTAssertTrue(proposal.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["New folder “Lisbon Trip”"].exists)
        activate(element("sidebar.proposal.accept.\(Self.proposalID)"))
        let created = folderHeader(Self.proposedFolderID)
        XCTAssertTrue(created.waitForExistence(timeout: 10))
        waitForDisappearance(of: proposal)
        XCTAssertTrue(waitForFolder(created, label: "Lisbon Trip", expanded: false))
        activate(created)
        XCTAssertTrue(reveal(sessionRow(Self.firstSessionID)))
        XCTAssertTrue(waitForFolder(created, expanded: true))
    }

    /// Every conversation a proposal would file is listed on a line of its own.
    func testASuggestedFolderListsEachConversationItWouldFile() {
        addFolderFixture()
        app.launch()
        collapseRecentChats()
        let proposal = element("sidebar.proposal.\(Self.proposalID)")
        XCTAssertTrue(proposal.waitForExistence(timeout: 10))
        attachFolderScreenshot("Suggested folder")
        var previous: CGRect?
        for title in Self.proposedTitles {
            let line = proposal.staticTexts[title]
            XCTAssertTrue(line.exists, "missing \(title)")
            XCTAssertTrue(line.isHittable, "\(title) is not on screen")
            if let previous {
                XCTAssertGreaterThan(line.frame.minY, previous.minY, "\(title) shares a line")
            }
            previous = line.frame
        }
    }

    /// The owner can change a suggested folder's name while accepting it; a
    /// taken name is shown in the sheet, which stays open for another.
    func testRenamingASuggestedFolderBeforeAcceptingIt() {
        addFolderFixture()
        app.launch()
        let proposal = element("sidebar.proposal.\(Self.proposalID)")
        XCTAssertTrue(proposal.waitForExistence(timeout: 10))
        activate(element("sidebar.proposal.rename.\(Self.proposalID)"))
        let field = element("folder.name")
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        XCTAssertEqual(field.value as? String, "Lisbon Trip")
        attachFolderScreenshot("Accept suggested folder sheet")
        typeIntoFolderNameField("travel")
        XCTAssertTrue(element("folder.error").waitForExistence(timeout: 5))
        XCTAssertTrue(field.exists)
        typeIntoFolderNameField("Portugal")
        waitForDisappearance(of: field)
        let created = folderHeader(Self.proposedFolderID)
        XCTAssertTrue(created.waitForExistence(timeout: 10))
        XCTAssertTrue(waitForFolder(created, label: "Portugal", expanded: false))
        waitForDisappearance(of: proposal)
    }

    /// A conversation's move menu files it in a folder and takes it out again
    /// with an explicit unfile; the server's answer places the row.
    func testMovingAConversationIntoAFolderAndBackOut() {
        addFolderFixture()
        app.launch()
        collapseRecentChats()
        let travel = folderHeader(Self.folderID)
        XCTAssertTrue(waitForFolder(travel, label: "Travel", expanded: false, timeout: 10))
        let row = sessionRow(Self.firstSessionID)
        openMoveMenu(for: row, sessionID: Self.firstSessionID)
        let intoTravel = menuItem("sidebar.session.move.to.\(Self.folderID)", label: "Travel")
        XCTAssertTrue(intoTravel.waitForExistence(timeout: 5), app.debugDescription)
        XCTAssertFalse(
            menuItem("sidebar.session.move.none", label: "Remove from folder").exists,
            "An unfiled conversation has no folder to leave"
        )
        activate(intoTravel)
        // Filed in the collapsed Travel folder, the row leaves the history.
        waitForDisappearance(of: row, timeout: 10)
        // The move withdraws the suggestion that named the conversation.
        let proposal = element("sidebar.proposal.\(Self.proposalID)")
        waitForDisappearance(of: proposal)
        let filedAt = Date()
        XCTAssertTrue(reveal(travel))
        activate(travel)
        XCTAssertTrue(waitForFolder(travel, expanded: true))
        XCTAssertTrue(reveal(row))
        XCTAssertTrue(sessionRow(Self.secondSessionID).exists)

        openMoveMenu(for: row, sessionID: Self.firstSessionID)
        let unfile = menuItem("sidebar.session.move.none", label: "Remove from folder")
        XCTAssertTrue(unfile.waitForExistence(timeout: 5))
        activate(unfile)
        // Back in the history, the row stays in view when Travel closes again.
        XCTAssertTrue(reveal(travel))
        activate(travel)
        XCTAssertTrue(waitForFolder(travel, expanded: false))
        waitForDisappearance(of: sessionRow(Self.secondSessionID))
        XCTAssertTrue(reveal(row), "An unfiled conversation returns to the history")
        // The sidebar refreshes every ten seconds; a refresh that brought the
        // suggestion back would shift every row beneath it.
        let untilNextRefresh = max(0, 12 - Date().timeIntervalSince(filedAt))
        XCTAssertFalse(
            proposal.waitForExistence(timeout: untilNextRefresh),
            "A withdrawn suggestion stays withdrawn across a refresh"
        )
    }

    func testDecliningASuggestedFolderRemovesIt() {
        addFolderFixture()
        app.launch()
        let proposal = element("sidebar.proposal.\(Self.proposalID)")
        XCTAssertTrue(proposal.waitForExistence(timeout: 10))
        activate(element("sidebar.proposal.decline.\(Self.proposalID)"))
        waitForDisappearance(of: proposal)
        XCTAssertFalse(element("sidebar.folder.\(Self.proposedFolderID)").exists)
        XCTAssertTrue(reveal(sessionRow(Self.firstSessionID)))
    }

    // MARK: - Subscriptions (Milestone 31)

    private static let newsSubscriptionID = "00000000-0000-0000-0000-000000000B01"
    private static let dealsSubscriptionID = "00000000-0000-0000-0000-000000000B02"
    private static let clubSubscriptionID = "00000000-0000-0000-0000-000000000B03"
    private static let promoSubscriptionID = "00000000-0000-0000-0000-000000000B04"
    private static let bulkThreadID = "00000000-0000-0000-0000-000000000897"
    private static let spamThreadID = "00000000-0000-0000-0000-000000000898"
    /// One status poll waits two seconds before the census is read back.
    private static let subscriptionSettleTimeout: TimeInterval = 20

    /// Adds the census fixture before the one launch. The census and its
    /// confirmation are sheets, sized on the Mac for a larger window.
    private func addSubscriptionsFixture(_ extra: [String] = []) {
        app.launchArguments.append("--ui-testing-email-subscriptions")
        app.launchArguments += extra
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
    }

    private func openEmailMode() {
        let mode = app.buttons["mode.email"]
        XCTAssertTrue(mode.waitForExistence(timeout: 10))
        activate(mode)
        XCTAssertTrue(app.buttons["email.thread.00000000-0000-0000-0000-000000000801"].waitForExistence(timeout: 10))
    }

    /// On the Mac what describes a thread (Unsubscribe, the spam flag, Show
    /// original) lives in the right panel, which a narrow window folds away.
    /// Elsewhere it sits in the thread.
    private func revealThreadContext() {
        #if os(macOS)
        showEmailPanel()
        #endif
    }

    /// The census, where the server advertises it, opened from the Email toolbar.
    private func openSubscriptions() {
        let entry = app.buttons["email.subscriptions.open"]
        XCTAssertTrue(entry.waitForExistence(timeout: 10), "An advertising account must offer Subscriptions")
        activate(entry)
        XCTAssertTrue(subscriptionRow(Self.newsSubscriptionID).waitForExistence(timeout: 10), app.debugDescription)
    }

    private func subscriptionRow(_ id: String) -> XCUIElement {
        element("email.subscription.row.\(id)")
    }

    /// macOS combines a navigation row into its button label; iOS also exposes its text children.
    private func subscriptionRowContains(_ id: String, _ content: String) -> Bool {
        let row = subscriptionRow(id)
        return row.exists && (row.label.contains(content)
            || row.staticTexts.matching(NSPredicate(format: "label == %@ OR value == %@", content, content)).count > 0)
    }

    private func subscriptionStateCount(_ state: String) -> Int {
        [Self.newsSubscriptionID, Self.dealsSubscriptionID, Self.clubSubscriptionID, Self.promoSubscriptionID]
            .filter { subscriptionRowContains($0, state) }.count
    }

    private func waitForSubscriptionState(_ state: String, count: Int) {
        let settled = expectation(for: NSPredicate { _, _ in
            self.subscriptionStateCount(state) == count
        }, evaluatedWith: nil)
        wait(for: [settled], timeout: Self.subscriptionSettleTimeout)
    }

    private func subscriptionAction(_ action: String) -> XCUIElement {
        app.buttons["email.subscription.action.\(action)"]
    }

    /// Text elements carry their string as the label on iOS and as the value on the Mac.
    private func texts(_ string: String) -> XCUIElementQuery {
        app.staticTexts.matching(NSPredicate(format: "label == %@ OR value == %@", string, string))
    }

    private func text(of element: XCUIElement) -> String {
        element.label.isEmpty ? (element.value as? String ?? "") : element.label
    }

    private func isOn(_ toggle: XCUIElement) -> Bool {
        (toggle.value as? String) == "1" || (toggle.value as? Int) == 1
    }

    /// Checks the one confirmation both surfaces present: each sender with its
    /// mechanism in plain words, the warning, and the archive option left off.
    private func checkConfirmation(title: String, naming senders: [(name: String, sentence: String)]) {
        XCTAssertTrue(app.buttons["email.unsubscribe.confirm"].waitForExistence(timeout: 5))
        XCTAssertTrue(texts(title).firstMatch.exists, app.debugDescription)
        XCTAssertTrue(texts("An unsubscribe cannot be undone.").firstMatch.exists)
        let targets = element("email.unsubscribe.targets")
        XCTAssertTrue(targets.exists, app.debugDescription)
        for sender in senders {
            XCTAssertTrue(targets.staticTexts[sender.name].exists, "The confirmation must name \(sender.name)")
            XCTAssertTrue(targets.staticTexts[sender.sentence].exists, "The confirmation must say \(sender.sentence)")
        }
        let archive = element("email.unsubscribe.archive")
        XCTAssertTrue(archive.exists)
        XCTAssertFalse(isOn(archive), "Archiving existing mail is off until the owner asks for it")
    }

    /// Opening one sender's detail, confirming its mechanism, and watching the
    /// row settle from the durable operation. The fixture refuses a consent
    /// that does not name the sender's identity, evidence and revision.
    func testSubscriptionsCensusUnsubscribesOneSenderAfterConfirmation() {
        addSubscriptionsFixture()
        app.launch()
        app.activate()
        openEmailMode()
        openSubscriptions()
        XCTAssertTrue(subscriptionRowContains(Self.newsSubscriptionID, "One-click request to daily.example.test"))
        XCTAssertTrue(subscriptionRowContains(Self.dealsSubscriptionID, "Sends an email to unsubscribe@shop.example.test"))
        XCTAssertTrue(subscriptionRowContains(Self.promoSubscriptionID, "Checking…"))
        XCTAssertEqual(subscriptionStateCount("Unsubscribed"), 0)

        activate(subscriptionRow(Self.newsSubscriptionID))
        let unsubscribe = subscriptionAction("unsubscribe")
        XCTAssertTrue(unsubscribe.waitForExistence(timeout: 5), app.debugDescription)
        XCTAssertTrue(subscriptionAction("reportSpam").exists)
        XCTAssertTrue(subscriptionAction("keep").exists)
        activate(unsubscribe)
        checkConfirmation(
            title: "Unsubscribe from this sender?",
            naming: [("Daily Brief", "One-click request to daily.example.test")])
        activate(app.buttons["email.unsubscribe.confirm"])

        #if os(macOS)
        waitForSubscriptionState("Unsubscribed", count: 1)
        #else
        // iPhone replaces the census with the detail page; iPad may keep both visible.
        XCTAssertTrue(texts("Unsubscribed").firstMatch.waitForExistence(timeout: Self.subscriptionSettleTimeout),
                      app.debugDescription)
        #endif
        // Success is quiet, and an unsubscribed sender offers nothing more.
        XCTAssertFalse(element("email.subscription.detail.status").exists)
        for action in ["unsubscribe", "tryAgain", "reportSpam", "keep"] {
            XCTAssertFalse(subscriptionAction(action).exists, "\(action) must not be offered after unsubscribing")
        }
    }

    /// Select all skips the protected correspondent and the sender still being
    /// checked; the protected one can still be chosen by hand, and one
    /// confirmation names all three before one consent unsubscribes them.
    func testSubscriptionsSelectAllSkipsProtectedAndUncheckedSenders() {
        addSubscriptionsFixture()
        app.launch()
        app.activate()
        openEmailMode()
        openSubscriptions()
        activate(app.buttons["email.subscriptions.select"])
        let selectAll = app.buttons["email.subscriptions.select-all"]
        XCTAssertTrue(selectAll.waitForExistence(timeout: 5))
        activate(selectAll)
        let selected = app.buttons["email.subscriptions.unsubscribe-selected"]
        XCTAssertTrue(selected.waitForExistence(timeout: 5), app.debugDescription)
        XCTAssertEqual(selected.label, "Unsubscribe 2")
        // A crowded iPhone navigation bar folds trailing items into an overflow menu.
        XCTAssertTrue(app.buttons["email.subscriptions.select"].isHittable, "Done stays in reach while selecting")
        attachEmailScreenshot("Selecting subscriptions")
        func choice(_ id: String) -> XCUIElement { element("email.subscription.select.\(id)") }
        XCTAssertEqual(choice(Self.newsSubscriptionID).value as? String, "Selected")
        XCTAssertEqual(choice(Self.dealsSubscriptionID).value as? String, "Selected")
        XCTAssertEqual(choice(Self.clubSubscriptionID).value as? String, "Not selected",
                       "Select all must skip a protected sender")
        XCTAssertFalse(choice(Self.promoSubscriptionID).isEnabled, "An unchecked sender cannot be selected")

        activate(choice(Self.clubSubscriptionID))
        XCTAssertEqual(choice(Self.clubSubscriptionID).value as? String, "Selected",
                       "A protected sender stays individually selectable")
        XCTAssertEqual(selected.label, "Unsubscribe 3")
        activate(selected)
        checkConfirmation(
            title: "Unsubscribe from 3 senders?",
            naming: [
                ("Daily Brief", "One-click request to daily.example.test"),
                ("Shop Deals", "Sends an email to unsubscribe@shop.example.test"),
                ("Running Club", "One-click request to run.example.test"),
            ])
        let archive = element("email.unsubscribe.archive")
        #if os(macOS)
        activate(archive)
        #else
        // The row's trailing switch, not its label, changes the value.
        archive.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: 0.5)).tap()
        #endif
        XCTAssertTrue(isOn(archive))
        activate(app.buttons["email.unsubscribe.confirm"])

        waitForSubscriptionState("Unsubscribed", count: 3)
        XCTAssertFalse(app.buttons["email.subscriptions.select-all"].exists, "Confirming ends selection")
        XCTAssertTrue(subscriptionRowContains(Self.promoSubscriptionID, "Checking…"))
    }

    /// A request the sender did not accept restores the row with an
    /// actionable error and offers what remains; Keep is then reversible.
    func testSubscriptionsFailedUnsubscribeRestoresTheRowWithWhatRemains() {
        addSubscriptionsFixture(["--ui-testing-email-subscriptions-failure"])
        app.launch()
        app.activate()
        openEmailMode()
        openSubscriptions()
        activate(subscriptionRow(Self.newsSubscriptionID))
        let unsubscribe = subscriptionAction("unsubscribe")
        XCTAssertTrue(unsubscribe.waitForExistence(timeout: 5))
        activate(unsubscribe)
        XCTAssertTrue(app.buttons["email.unsubscribe.confirm"].waitForExistence(timeout: 5))
        activate(app.buttons["email.unsubscribe.confirm"])

        let status = element("email.subscription.detail.status")
        let restored = expectation(
            for: NSPredicate(
                format: "label == %@ OR value == %@",
                "This request did not complete. Try again, report the sender as spam, or keep it.",
                "This request did not complete. Try again, report the sender as spam, or keep it."),
            evaluatedWith: status)
        wait(for: [restored], timeout: Self.subscriptionSettleTimeout)
        XCTAssertTrue(texts("Unsubscribe failed").firstMatch.exists)
        XCTAssertFalse(texts("Unsubscribed").firstMatch.exists, "A refused request must never read as a success")
        XCTAssertTrue(subscriptionAction("tryAgain").exists)
        XCTAssertTrue(subscriptionAction("reportSpam").exists)
        XCTAssertFalse(unsubscribe.exists)

        activate(subscriptionAction("keep"))
        let unkeep = subscriptionAction("unkeep")
        XCTAssertTrue(unkeep.waitForExistence(timeout: 10))
        XCTAssertTrue(texts("Kept").firstMatch.exists)
        XCTAssertFalse(status.exists, "A settled decision clears the earlier error")
        activate(unkeep)
        XCTAssertTrue(unsubscribe.waitForExistence(timeout: 10), "Stopping keeping restores the sender's actions")
        XCTAssertTrue(subscriptionAction("keep").exists)
    }

    /// A bulk conversation offers Unsubscribe beside its sender and opens the
    /// same confirmation with that one sender; the census then settles it.
    func testSubscriptionsThreadActionUnsubscribesTheConversationsSender() {
        addSubscriptionsFixture(["--ui-testing-email-bulk-thread"])
        app.launch()
        app.activate()
        openEmailMode()
        let bulk = app.buttons["email.thread.\(Self.bulkThreadID)"]
        XCTAssertTrue(bulk.waitForExistence(timeout: 10))
        activate(bulk)
        XCTAssertTrue(texts("Everything in the store is on sale this week.").firstMatch.waitForExistence(timeout: 5))
        revealThreadContext()
        let action = app.buttons["email.unsubscribe.thread"]
        XCTAssertTrue(action.waitForExistence(timeout: 5), app.debugDescription)
        XCTAssertEqual(action.label, "Unsubscribe from deals@shop.example.test")
        activate(action)
        checkConfirmation(
            title: "Unsubscribe from this sender?",
            naming: [("deals@shop.example.test", "Sends an email to unsubscribe@shop.example.test")])
        activate(app.buttons["email.unsubscribe.confirm"])
        XCTAssertTrue(app.buttons["email.unsubscribe.confirm"].waitForNonExistence(timeout: 5))

        #if os(iOS)
        // Compact navigation shows the Email toolbar again once back at the inbox.
        let entry = app.buttons["email.subscriptions.open"]
        if !(entry.exists && entry.isHittable) {
            app.navigationBars.buttons["BackButton"].firstMatch.tap()
        }
        #endif
        openSubscriptions()
        let settled = subscriptionRow(Self.dealsSubscriptionID)
        XCTAssertTrue(settled.exists)
        waitForSubscriptionState("Unsubscribed", count: 1)
        XCTAssertEqual(subscriptionStateCount("Unsubscribed"), 1, "Only the conversation's own sender was consented")
    }

    /// An account that advertises nothing offers no entry and no thread
    /// action, even on a conversation whose projection names a sender.
    func testSubscriptionsStayHiddenWhereNoAccountAdvertisesThem() {
        app.launchArguments.append("--ui-testing-email-bulk-thread")
        app.launch()
        openEmailMode()
        XCTAssertFalse(app.buttons["email.subscriptions.open"].exists)
        let bulk = app.buttons["email.thread.\(Self.bulkThreadID)"]
        XCTAssertTrue(bulk.waitForExistence(timeout: 10))
        activate(bulk)
        XCTAssertTrue(texts("Everything in the store is on sale this week.").firstMatch.waitForExistence(timeout: 5))
        revealThreadContext()
        XCTAssertFalse(app.buttons["email.unsubscribe.thread"].exists)
        XCTAssertFalse(app.buttons["email.subscriptions.open"].exists)
    }

    /// ADR-0165: opens the conversation the server flagged as suspected spam, after one launch.
    private func openSuspectedSpam() -> XCUIElement {
        app.activate()
        openEmailMode()
        let row = app.buttons["email.thread.\(Self.spamThreadID)"]
        XCTAssertTrue(row.waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertTrue(row.label.contains("Suspected spam") || firstElement("email.spam.tag.\(Self.spamThreadID)").exists,
                      "The row carries the flag: \(row.label)")
        activate(row)
        XCTAssertTrue(texts("Your account is locked. Sign in within 24 hours to keep it.").firstMatch
            .waitForExistence(timeout: 5), app.debugDescription)
        revealThreadContext()
        XCTAssertTrue(firstElement("email.spam.tag.detail").waitForExistence(timeout: 5), app.debugDescription)
        return row
    }

    private func firstElement(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any).matching(identifier: identifier).firstMatch
    }

    /// Not spam clears the flag in the detail and the row, and offers nothing that touches Gmail in its place.
    func testSuspectedSpamNotSpamClearsTheFlag() {
        app.launchArguments.append("--ui-testing-email-suspected-spam")
        app.launch()
        let row = openSuspectedSpam()
        let notSpam = app.buttons["email.spam.clear"]
        XCTAssertTrue(notSpam.waitForExistence(timeout: 5), app.debugDescription)
        XCTAssertTrue(app.buttons["email.spam.report"].exists, "A flag offers both of the owner's answers")
        activate(notSpam)
        XCTAssertTrue(firstElement("email.spam.tag.detail").waitForNonExistence(timeout: 10), app.debugDescription)
        XCTAssertFalse(app.buttons["email.spam.report"].exists)
        XCTAssertTrue(texts("Your account is locked. Sign in within 24 hours to keep it.").firstMatch.exists,
                      "Clearing the flag keeps the conversation open")
        #if os(iOS)
        if UIDevice.current.userInterfaceIdiom == .phone {
            app.navigationBars.buttons["BackButton"].firstMatch.tap()
        }
        #endif
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        let cleared = expectation(for: NSPredicate(format: "NOT (label CONTAINS %@)", "Suspected spam"),
                                  evaluatedWith: row)
        wait(for: [cleared], timeout: 10)
    }

    /// Report spam removes the conversation at once, opens the next one, and keeps it out of the inbox once settled.
    func testSuspectedSpamReportSpamRemovesTheConversation() {
        app.launchArguments.append("--ui-testing-email-suspected-spam")
        app.launch()
        _ = openSuspectedSpam()
        let report = app.buttons["email.spam.report"]
        XCTAssertTrue(report.waitForExistence(timeout: 5), app.debugDescription)
        activate(report)
        XCTAssertTrue(texts("Please review the agenda before Friday.").firstMatch.waitForExistence(timeout: 10),
                      "The reading pane moves on to the next conversation")
        #if os(iOS)
        if UIDevice.current.userInterfaceIdiom == .phone {
            app.navigationBars.buttons["BackButton"].firstMatch.tap()
        }
        #endif
        XCTAssertTrue(app.buttons["email.thread.00000000-0000-0000-0000-000000000801"].waitForExistence(timeout: 5))
        // The initial removal is optimistic. Observe the confirmed state in
        // Other mail before returning to the inbox and asserting it stays out.
        selectLegacyMailView("Other mail")
        let reported = app.buttons["email.thread.\(Self.spamThreadID)"]
        XCTAssertTrue(reported.waitForExistence(timeout: 10))
        activate(reported)
        revealThreadContext()
        XCTAssertTrue(texts("Reported as spam. Gmail empties Spam after thirty days.").firstMatch
            .waitForExistence(timeout: Self.archiveOutcomeTimeout), app.debugDescription)
        XCTAssertTrue(app.buttons["email.spam.restore"].exists)
        #if os(iOS)
        if UIDevice.current.userInterfaceIdiom == .phone {
            app.navigationBars.buttons["BackButton"].firstMatch.tap()
        }
        #endif
        selectLegacyMailView("Important")
        XCTAssertTrue(reported.waitForNonExistence(timeout: 10),
                      "A confirmed report keeps the conversation out of the inbox")
    }

    private func activate(_ element: XCUIElement) {
        #if os(macOS)
        element.click()
        #else
        element.tap()
        #endif
    }

    /// Settings shows one group per page: the Mac and iPad pick it from a
    /// sidebar, the iPhone pushes it from the list of groups, going back to
    /// that list first when another group is open.
    private func openSettingsGroup(_ group: String) {
        let row = app.buttons["settings.group.\(group)"]
        #if os(iOS)
        // A pushed group leaves the list's rows out of the tree; its own
        // navigation bar's Back returns to them (the Chat bar behind the
        // sheet has no Back button).
        if !(row.waitForExistence(timeout: 2) && row.isHittable) {
            let back = app.buttons["BackButton"].firstMatch
            if back.waitForExistence(timeout: 2) { back.tap() }
        }
        #endif
        XCTAssertTrue(row.waitForExistence(timeout: 5), app.debugDescription)
        activate(row)
    }

    /// Retains the synthetic mailbox rendering for visual verification even when the test passes.
    private func attachEmailScreenshot(_ name: String) {
        let attachment = XCTAttachment(screenshot: app.screenshot())
        attachment.name = name
        attachment.lifetime = .keepAlways
        add(attachment)
    }

    func testPeopleAccessibilityAtLargeText() throws {
        continueAfterFailure = true
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #if os(macOS)
        // Set the preference in isolated fixture defaults without passing a
        // positional argument that AppKit can interpret as a document to open.
        app.launchEnvironment["VEETBOT_UI_TEST_TEXT_SIZE"] = "large"
        #else
        app.launchArguments += ["-UIPreferredContentSizeCategoryName", "UICTContentSizeCategoryAccessibilityXXXL"]
        #endif
        app.launch()
        app.activate()
        // At accessibility text sizes, the recent shortcut remains above History.
        let historical = app.descendants(matching: .any)["sidebar.recent.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(historical.waitForExistence(timeout: 10))
        activate(historical)
        XCTAssertTrue(app.buttons["chat.people"].waitForExistence(timeout: 5))
        var unlocatedBaseline: [String: Int] = [:]
        #if os(macOS)
        try app.performAccessibilityAudit { issue in
            let attachment = XCTAttachment(string: "\(issue.compactDescription): \(issue.element?.debugDescription ?? "No element")")
            attachment.name = "Background accessibility baseline"
            attachment.lifetime = .keepAlways
            self.add(attachment)
            if issue.element == nil {
                unlocatedBaseline["\(issue.auditType.rawValue):\(issue.compactDescription)", default: 0] += 1
            }
            return true
        }
        #endif
        activate(app.buttons["chat.people"])
        let person = app.descendants(matching: .any)["people.row.00000000-0000-0000-0000-000000000777"]
        let browser = app.descendants(matching: .any)["people.browser"]
        XCTAssertTrue(browser.waitForExistence(timeout: 5))
        try auditPeopleSurface(unlocatedBaseline: unlocatedBaseline)
        #if !os(macOS)
        for _ in 0..<10 where !person.exists || !person.isHittable { browser.swipeUp() }
        #endif
        XCTAssertTrue(person.waitForExistence(timeout: 5))
        activate(person)
        XCTAssertTrue(app.descendants(matching: .any)["people.detail"].waitForExistence(timeout: 5))
        try auditPeopleSurface(unlocatedBaseline: unlocatedBaseline)
    }

    private func auditPeopleSurface(unlocatedBaseline: [String: Int]) throws {
        #if os(macOS)
        let sheet = app.sheets.firstMatch
        XCTAssertTrue(sheet.exists)
        var remainingBaseline = unlocatedBaseline
        #endif
        try app.performAccessibilityAudit { issue in
            let attachment = XCTAttachment(string: "\(issue.compactDescription): \(issue.element?.debugDescription ?? "No element")")
            attachment.name = "People accessibility element"
            attachment.lifetime = .keepAlways
            self.add(attachment)
            #if os(macOS)
            // XCTest also audits the dimmed parent and app-wide Touch Bar.
            // Keep those findings as attachments; this test owns the People sheet.
            guard let element = issue.element else {
                let key = "\(issue.auditType.rawValue):\(issue.compactDescription)"
                guard remainingBaseline[key, default: 0] > 0 else { return false }
                remainingBaseline[key, default: 0] -= 1
                return true
            }
            let inSheet = sheet.descendants(matching: element.elementType).allElementsBoundByIndex.contains {
                $0.frame == element.frame && $0.identifier == element.identifier && $0.label == element.label
            }
            return !inSheet
            #else
            return self.verifyPeopleContrast(issue)
            #endif
        }
    }

    #if os(iOS)
    private func verifyPeopleContrast(_ issue: XCUIAccessibilityAuditIssue) -> Bool {
        var stage = "Category \(issue.auditType.rawValue), contrast category \(XCUIAccessibilityAuditType.contrast.rawValue), element \(String(describing: issue.element?.elementType))"
        defer {
            let diagnostic = XCTAttachment(string: stage)
            diagnostic.name = "People contrast evidence decision"
            diagnostic.lifetime = .keepAlways
            add(diagnostic)
        }
        guard issue.auditType == .contrast, let element = issue.element,
              element.elementType == .staticText,
              ["Everyone", "Import history", "Add person"].contains(element.label) else { return false }
        stage = "Recognized contrast label \(element.label); verifying directory membership"
        let browser = app.descendants(matching: .any)["people.browser"]
        guard browser.exists, browser.descendants(matching: .staticText).allElementsBoundByIndex.contains(where: {
            $0.frame == element.frame && $0.identifier == element.identifier && $0.label == element.label
        }) else { return false }
        stage = "Verified directory membership; decoding screenshot"
        let screenshot = element.screenshot()
        guard let image = screenshot.image.cgImage,
              image.width > 0, image.height > 0, image.width <= 4096, image.height <= 4096,
              image.width * image.height <= 4_000_000,
              let colorSpace = CGColorSpace(name: CGColorSpace.sRGB) else { return false }
        var rgba = [UInt8](repeating: 0, count: image.width * image.height * 4)
        let decoded = rgba.withUnsafeMutableBytes { buffer -> Bool in
            guard let context = CGContext(
                data: buffer.baseAddress, width: image.width, height: image.height,
                bitsPerComponent: 8, bytesPerRow: image.width * 4, space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue | CGBitmapInfo.byteOrder32Big.rawValue
            ) else { return false }
            context.draw(image, in: CGRect(x: 0, y: 0, width: image.width, height: image.height))
            return true
        }
        stage = "Decoded \(image.width)x\(image.height) screenshot: \(decoded); measuring palette and contrast"
        guard decoded, let measurement = PeopleContrastEvidence.measure(
            label: element.label, isContrast: true, isDirectory: true,
            width: image.width, height: image.height, rgba: rgba
        ) else { return false }
        stage = "Accepted independently measured contrast"
        let evidence = XCTAttachment(string: "ADR-0235: \(element.label), contrast \(measurement.ratio):1; foreground pixels \(measurement.foregroundPixels), background pixels \(measurement.backgroundPixels).")
        evidence.name = "Independent People contrast measurement"
        evidence.lifetime = .keepAlways
        add(evidence)
        let rendered = XCTAttachment(screenshot: screenshot)
        rendered.name = "Measured People text"
        rendered.lifetime = .keepAlways
        add(rendered)
        return true
    }
    #endif

    func testPeopleEvidencePreservesTheOpenConversation() {
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
        app.launch()
        app.activate()
        let historical = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(historical.waitForExistence(timeout: 10))
        activate(historical)
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].waitForExistence(timeout: 5))
        let people = app.buttons["chat.people"]
        XCTAssertTrue(people.waitForExistence(timeout: 5))
        activate(people)
        let person = app.descendants(matching: .any)["people.row.00000000-0000-0000-0000-000000000777"]
        XCTAssertTrue(person.waitForExistence(timeout: 5))
        activate(person)
        XCTAssertTrue(app.descendants(matching: .any)["people.detail"].waitForExistence(timeout: 5))
        let source = app.buttons["View source"].firstMatch
        XCTAssertTrue(source.waitForExistence(timeout: 5))
        if !source.isHittable {
            #if os(macOS)
            let scrolls = app.sheets.firstMatch.scrollViews
            let detailScroll = scrolls.element(boundBy: scrolls.count - 1)
            for _ in 0..<6 where !source.isHittable { detailScroll.scroll(byDeltaX: 0, deltaY: -250) }
            #else
            scrollUntilVisible(source)
            #endif
        }
        activate(source)
        XCTAssertTrue(app.descendants(matching: .any)["people.source"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Maya is my sister."].waitForExistence(timeout: 5))
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "People original source"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        activate(app.buttons["people.conversation.close"])
        let close = app.buttons["Close"].firstMatch
        XCTAssertTrue(close.waitForExistence(timeout: 5))
        activate(close)
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["chat.people"].isHittable)
    }

    /// ADR-0122: a finished answer can be copied whole or opened for selection.
    func testFinishedAnswerOffersCopyAndSelectText() {
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
        app.launch()
        app.activate()
        let historical = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(historical.waitForExistence(timeout: 10))
        activate(historical)
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].firstMatch.waitForExistence(timeout: 5))

        // Copy and Select Text show on hover on the Mac, after a tap on an
        // iPad without a pointer, and always on the iPhone.
        revealMessageActions(app.staticTexts["Historical question"].firstMatch)
        XCTAssertTrue(app.buttons["chat.message.copy.event-1"].waitForExistence(timeout: 5))
        revealMessageActions(app.staticTexts["Historical answer loaded"].firstMatch)
        let copy = app.buttons["chat.message.copy.event-2"]
        XCTAssertTrue(copy.waitForExistence(timeout: 5))
        XCTAssertEqual(copy.label, "Copy message")
        let select = app.buttons["chat.message.select.event-2"]
        XCTAssertTrue(select.exists)

        activate(select)
        let text = app.descendants(matching: .any)["chat.message.selection.text"]
        XCTAssertTrue(text.waitForExistence(timeout: 5))
        XCTAssertEqual(text.value as? String, "Historical answer loaded")
        activate(app.buttons["chat.message.selection.done"])
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].firstMatch.waitForExistence(timeout: 5))
    }

    private func revealMessageActions(_ message: XCUIElement) {
        XCTAssertTrue(message.waitForExistence(timeout: 5))
        #if os(macOS)
        message.hover()
        #else
        if UIDevice.current.userInterfaceIdiom == .pad { message.tap() }
        #endif
    }

    func testTaskApprovalWebsitesCanBeAddedAndRemovedInSettings() {
        app.launch()
        #if os(macOS)
        let settings = app.buttons["sidebar.settings"]
        XCTAssertTrue(settings.waitForExistence(timeout: 10))
        settings.click()
        #else
        openSidebarDestination(identifier: "sidebar.settings")
        #endif
        openSettingsGroup("websiteAccess")
        func reveal(_ element: XCUIElement) {
            #if os(macOS)
            let scroll = app.scrollViews.firstMatch
            XCTAssertTrue(scroll.waitForExistence(timeout: 5), app.debugDescription)
            for _ in 0..<12 {
                let viewport = scroll.frame.insetBy(dx: 0, dy: 20)
                if element.exists && element.isHittable && viewport.contains(element.frame) {
                    // A synthesized scroll keeps decelerating after the
                    // events; a click computed before it stops lands ~20 pt
                    // off, so wait for half a second of stillness.
                    waitForSettledFrame(of: element, stableFor: 5)
                    return
                }
                let delta: CGFloat = element.exists && element.frame.minY < viewport.minY ? 180 : -180
                scroll.scroll(byDeltaX: 0, deltaY: delta)
            }
            #else
            scrollUntilVisible(element)
            #endif
        }
        let disclosure = app.descendants(matching: .any).matching(NSPredicate(format: "label == %@", "Task approval websites")).firstMatch
        reveal(disclosure)
        XCTAssertTrue(disclosure.waitForExistence(timeout: 5))
        let field = app.textFields["task-scopes.url"]
        #if os(macOS)
        // Only the chevron toggles a Mac disclosure group, and its glyph sits
        // a point or two further right on a 2x display than on CI's 1x one,
        // so try a few offsets along the row until the content appears.
        for dx in [0.13, 0.16, 0.10, 0.19] as [CGFloat] {
            waitForSettledFrame(of: disclosure, stableFor: 5)
            disclosure.coordinate(withNormalizedOffset: CGVector(dx: dx, dy: 0.5)).click()
            if field.waitForExistence(timeout: 2) { break }
        }
        #else
        activate(disclosure)
        #endif
        reveal(field)
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        #if os(macOS)
        // Typing needs the field focused. On a 1x display the element's own
        // click lands about 20 pt above an A2 field (its hit point, not its
        // frame, is off), so click the frame's centre, and again if the
        // field still reports no keyboard focus.
        let focused = NSPredicate(format: "hasKeyboardFocus == true")
        for _ in 0..<3 {
            waitForSettledFrame(of: field, stableFor: 5)
            field.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5)).click()
            if XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: focused, object: field)], timeout: 2) == .completed { break }
        }
        XCTAssertTrue(field.value(forKey: "hasKeyboardFocus") as? Bool ?? false, "The website field never took keyboard focus")
        #else
        activate(field)
        #endif
        field.typeText("https://www.duolingo.com/lesson")
        if app.keyboards.buttons["Return"].exists { app.keyboards.buttons["Return"].tap() }
        let add = app.buttons["task-scopes.add"]
        reveal(add)
        XCTAssertTrue(add.isEnabled)
        activate(add)
        #if os(macOS)
        let confirm = app.sheets.buttons["Add website"].firstMatch
        #else
        let confirm = app.buttons.matching(NSPredicate(format: "label == %@ AND identifier != %@", "Add website", "task-scopes.add")).firstMatch
        #endif
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        activate(confirm)
        let remove = app.buttons["task-scopes.remove.https://www.duolingo.com/lesson"]
        XCTAssertTrue(remove.waitForExistence(timeout: 5))
        reveal(remove)
        activate(remove)
        XCTAssertTrue(app.staticTexts["No websites added."].waitForExistence(timeout: 5))
        activate(app.buttons["task-scopes.refresh"])
        XCTAssertTrue(app.staticTexts["No websites added."].exists)
    }

    /// ADR-0129: a `browser.act` card offers Allow all actions for this task; the owner
    /// confirms the server's offer text, the resolve repeats the offer's scope
    /// (the fixture refuses anything else), the banner counts the permission,
    /// and Stop ends it without a confirmation.
    func testBrowserApprovalAllowsForThisTaskAfterConfirmation() {
        app.launchArguments.append("--ui-testing-browser-task-grant")
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
        app.launch()
        app.activate()
        let conversation = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(conversation.waitForExistence(timeout: 10))
        activate(conversation)

        let allowForTask = app.buttons["approval.allow-for-task"]
        XCTAssertTrue(allowForTask.waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertEqual(allowForTask.label, "Allow all actions for this task")
        let scope = app.staticTexts["approval.task-scope"]
        XCTAssertTrue(scope.exists)
        XCTAssertTrue(scope.label.contains("www.duolingo.com/lesson"))
        XCTAssertTrue(scope.label.contains("asks again"))
        XCTAssertTrue(app.buttons["approval.allow-once"].exists)
        XCTAssertTrue(app.staticTexts["Text from the website"].exists)
        activate(allowForTask)

        let summary = app.staticTexts["task-grant.confirm.summary"]
        XCTAssertTrue(summary.waitForExistence(timeout: 5))
        XCTAssertTrue(summary.label.hasPrefix("Clicks and typing on www.duolingo.com/lesson"))
        activate(app.buttons["task-grant.confirm.allow"])

        let banner = app.staticTexts["task-grant.banner.text"]
        XCTAssertTrue(banner.waitForExistence(timeout: 10), app.debugDescription)
        XCTAssertTrue(banner.label.hasPrefix("Allowed on www.duolingo.com/lesson · 0 of 200"))
        XCTAssertTrue(app.staticTexts["Allowed for this task"].waitForExistence(timeout: 5))

        activate(app.buttons["task-grant.banner.stop"])
        XCTAssertTrue(banner.waitForNonExistence(timeout: 10))
    }

    /// Approved chat checkpoints keep their details accessible but out of the way.
    func testChatApprovalCollapsesAfterApproval() {
        configureApprovalChat(generic: true)
        app.launch()
        openApprovalChat()
        XCTAssertTrue(app.staticTexts["Approval checkpoint"].waitForExistence(timeout: 10))
        #if os(iOS)
        let status = app.staticTexts["chat.status"]
        XCTAssertTrue(status.waitForExistence(timeout: 5))
        XCTAssertEqual(status.label, "Needs your approval")
        #endif
        activate(app.buttons["Approve once"])
        assertCollapsedApproval(detail: "Approval checkpoint")
        // The run is resuming: the header asks for nothing and the activity row shows work.
        XCTAssertTrue(app.staticTexts["Working…"].waitForExistence(timeout: 5))
        #if os(iOS)
        XCTAssertFalse(status.exists)
        #endif
    }

    func testChatApprovalLoadedApprovedStartsCollapsed() {
        app.launchArguments.append("--ui-testing-approved-checkpoint")
        configureApprovalChat(generic: true)
        app.launch()
        openApprovalChat()
        assertCollapsedApproval(detail: "Approval checkpoint")
    }

    func testBrowserChatApprovalCollapsesAfterTaskApproval() {
        configureApprovalChat(generic: false)
        app.launch()
        openApprovalChat()
        let allow = app.buttons["approval.allow-for-task"]
        XCTAssertTrue(allow.waitForExistence(timeout: 10))
        activate(allow)
        let confirm = app.buttons["task-grant.confirm.allow"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        activate(confirm)
        assertCollapsedApproval(detail: "Text from the website")
    }

    private func configureApprovalChat(generic: Bool) {
        app.launchArguments.append("--ui-testing-browser-task-grant")
        if generic { app.launchArguments.append("--ui-testing-generic-checkpoint") }
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
    }

    private func openApprovalChat() {
        app.activate()
        let conversation = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(conversation.waitForExistence(timeout: 10))
        activate(conversation)
    }

    private func assertCollapsedApproval(detail: String) {
        let summary = app.buttons["approval.details"]
        XCTAssertTrue(summary.waitForExistence(timeout: 10))
        XCTAssertEqual(summary.value as? String, "Collapsed")
        XCTAssertFalse(app.staticTexts[detail].exists)
        activate(summary)
        XCTAssertTrue(app.staticTexts[detail].waitForExistence(timeout: 5))
        XCTAssertEqual(summary.value as? String, "Expanded")
        XCTAssertFalse(app.buttons["Approve once"].exists)
        XCTAssertFalse(app.buttons["approval.allow-once"].exists)
        XCTAssertFalse(app.buttons["approval.allow-for-task"].exists)
        activate(summary)
        XCTAssertFalse(app.staticTexts[detail].exists)
    }

    private func openSynthesis() {
        #if os(macOS)
        app.activate()
        let memory = app.buttons["sidebar.memory"]
        XCTAssertTrue(memory.waitForExistence(timeout: 10))
        activate(memory)
        // The Mac sheet's group sidebar holds Synthesis under Maintenance.
        let collection = app.buttons["memory.group.synthesis"]
        #else
        openSidebarDestination(identifier: "sidebar.memory")
        let sidebar = app.buttons["memory.group.synthesis"]
        let collection = sidebar.waitForExistence(timeout: 2) ? sidebar : app.segmentedControls.buttons["Synthesis"]
        #endif
        XCTAssertTrue(collection.waitForExistence(timeout: 5))
        activate(collection)
    }

    private func openSynthesisEntry() {
        let row = app.buttons["memory.synthesis.row.00000000-0000-0000-0000-000000003201"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        activate(row)
        XCTAssertTrue(app.descendants(matching: .any)["memory.synthesis.detail"].waitForExistence(timeout: 5))
    }

    func testSynthesisExplainsSourcesAndOpensAnOriginal() {
        app.launchArguments.append("--ui-testing-synthesis")
        app.launch()
        openSynthesis()
        openSynthesisEntry()
        XCTAssertTrue(app.staticTexts["Related summary · derived memory"].waitForExistence(timeout: 5))
        let omitted = app.descendants(matching: .any)["memory.synthesis.source.00000000-0000-0000-0000-000000003203"].firstMatch
        revealSynthesisAction(omitted)
        XCTAssertTrue(omitted.label.contains("Original · not included in summary text"), omitted.debugDescription)
        let screenshot = XCTAttachment(screenshot: app.screenshot())
        screenshot.name = "Memory synthesis evidence trail"
        screenshot.lifetime = .keepAlways
        add(screenshot)
        let original = app.descendants(matching: .any)["memory.synthesis.source.00000000-0000-0000-0000-000000000321"].firstMatch
        revealSynthesisAction(original)
        activate(original)
        XCTAssertTrue(app.descendants(matching: .any)["memory.detail"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["The user prefers dark mode."].exists)
    }

    func testSynthesisUnavailableOriginalDoesNotReuseItsPreview() {
        app.launchArguments.append("--ui-testing-synthesis")
        app.launch()
        openSynthesis()
        openSynthesisEntry()
        let original = app.descendants(matching: .any)["memory.synthesis.source.00000000-0000-0000-0000-000000003203"].firstMatch
        revealSynthesisAction(original)
        activate(original)
        XCTAssertTrue(app.staticTexts["This memory is no longer available."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.descendants(matching: .any)["memory.detail"].exists)
    }

    func testSynthesisConnectionLabelsUncertaintyAndEvidence() {
        app.launchArguments += ["--ui-testing-synthesis", "--ui-testing-synthesis-connection"]
        app.launch()
        openSynthesis()
        openSynthesisEntry()
        XCTAssertTrue(app.staticTexts["User may prefer calm environments."].firstMatch.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Tentative connection · derived memory"].waitForExistence(timeout: 5))
        let reviewed = app.buttons["memory.synthesis.review"]
        revealSynthesisAction(reviewed)
        XCTAssertTrue(reviewed.waitForExistence(timeout: 5))
        activate(reviewed)
        XCTAssertTrue(app.staticTexts["Synthesis updated."].waitForExistence(timeout: 5))
    }

    func testSynthesisReviewAndDeletePreserveOriginals() {
        app.launchArguments.append("--ui-testing-synthesis")
        app.launch()
        openSynthesis()
        openSynthesisEntry()
        let reviewed = app.buttons["memory.synthesis.review"]
        revealSynthesisAction(reviewed)
        activate(reviewed)
        XCTAssertTrue(app.staticTexts["Synthesis updated."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["memory.synthesis.review"].exists)
        let delete = app.buttons["memory.synthesis.delete"]
        revealSynthesisAction(delete)
        activate(delete)
        #if os(macOS)
        let confirm = app.windows.buttons["Delete synthesis"].firstMatch
        #else
        let confirm = app.buttons["Delete synthesis"].firstMatch
        #endif
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        activate(confirm)
        XCTAssertTrue(app.staticTexts["Synthesis deleted. Original memories are kept."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["The user prefers dark mode."].exists)
    }

    func testSynthesisUndoShowsAffectedOriginalsBeforeCommit() {
        app.launchArguments += ["--ui-testing-synthesis", "--ui-testing-synthesis-merge"]
        app.launch()
        openSynthesis()
        openSynthesisEntry()
        let preview = app.buttons["memory.synthesis.undo.preview"]
        revealSynthesisAction(preview)
        activate(preview)
        let confirm = app.buttons["memory.synthesis.undo.confirm"]
        XCTAssertTrue(confirm.waitForExistence(timeout: 5))
        let original = app.descendants(matching: .any)["memory.synthesis.undo.sheet"]
            .descendants(matching: .any)["memory.synthesis.source.00000000-0000-0000-0000-000000003203"].firstMatch
        XCTAssertTrue(original.waitForExistence(timeout: 5))
        XCTAssertTrue(original.isHittable, original.debugDescription)
        XCTAssertTrue(original.label.contains("The user prefers quiet mornings."), original.debugDescription)
        activate(confirm)
        XCTAssertTrue(app.staticTexts["Merge undone. Original memories remain available."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["memory.synthesis.undo.preview"].exists)
    }

    func testSynthesisOpaqueHistoryHasNoSourcesOrActions() {
        app.launchArguments += ["--ui-testing-synthesis", "--ui-testing-synthesis-hidden"]
        app.launch()
        openSynthesis()
        openSynthesisEntry()
        XCTAssertTrue(app.staticTexts["Source details unavailable"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["The user prefers dark mode."].exists)
        XCTAssertFalse(app.buttons["memory.synthesis.review"].exists)
        XCTAssertFalse(app.buttons["memory.synthesis.undo.preview"].exists)
    }

    func testSynthesisUnavailableOnOlderServer() {
        app.launch()
        openSynthesis()
        XCTAssertTrue(app.staticTexts["This server does not support synthesis browsing yet."].waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["memory.synthesis.row.00000000-0000-0000-0000-000000003201"].exists)
    }

    private func revealSynthesisAction(_ element: XCUIElement) {
        #if os(macOS)
        // Scroll until the control lies inside the detail column's viewport: macOS 26
        // reports a control just below it, but still inside the window, as hittable.
        // The identifier sits on the column's content; its scroll view is the viewport.
        let detail = app.descendants(matching: .any)["memory.synthesis.detail"]
        XCTAssertTrue(detail.waitForExistence(timeout: 5))
        let column = app.scrollViews.containing(.any, identifier: "memory.synthesis.detail").firstMatch
        XCTAssertTrue(column.waitForExistence(timeout: 5), app.debugDescription)
        let viewport = column.frame
        for _ in 0..<16 {
            if element.exists && viewport.contains(element.frame) && element.isHittable { break }
            let down = element.exists && element.frame.minY < viewport.minY
            column.scroll(byDeltaX: 0, deltaY: down ? 150 : -150)
        }
        XCTAssertTrue(viewport.contains(element.frame), element.debugDescription)
        #else
        let detail = app.descendants(matching: .any)["memory.synthesis.detail"]
        XCTAssertTrue(detail.waitForExistence(timeout: 5))
        for _ in 0..<16 {
            let top = max(detail.frame.minY + 70, app.navigationBars["Synthesis"].frame.maxY + 12)
            let bottom = detail.frame.maxY - 90
            if element.exists && element.isHittable && element.frame.minY >= top && element.frame.maxY < bottom { break }
            let down = element.exists && element.frame.minY < top
            detail.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: down ? 0.4 : 0.7))
                .press(forDuration: 0.05, thenDragTo: detail.coordinate(withNormalizedOffset: CGVector(dx: 0.95, dy: down ? 0.6 : 0.5)))
        }
        #endif
        XCTAssertTrue(element.waitForExistence(timeout: 5))
        XCTAssertTrue(element.isHittable)
    }

    /// Choosing one person after another in Memory's People collection replaces
    /// the open profile. The directory is longer than the window, as a real one
    /// is: on the Mac, a second click there once left the first profile open.
    func testMemoryPeopleShowsEachChosenPerson() {
        app.launchArguments.append("--ui-testing-people-directory")
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
        app.launch()
        #if os(macOS)
        app.activate()
        let memory = app.buttons["sidebar.memory"]
        XCTAssertTrue(memory.waitForExistence(timeout: 10))
        activate(memory)
        // The Mac sheet's group sidebar lists the People directory under Browse.
        let peopleCollection = app.buttons["memory.group.people"]
        #else
        openSidebarDestination(identifier: "sidebar.memory")
        XCTAssertTrue(app.descendants(matching: .any)["memory.browser"].waitForExistence(timeout: 5))
        let sidebar = app.buttons["memory.group.people"]
        let peopleCollection = sidebar.waitForExistence(timeout: 2) ? sidebar : app.segmentedControls.buttons["People"]
        #endif
        XCTAssertTrue(peopleCollection.waitForExistence(timeout: 5))
        activate(peopleCollection)
        let detail = app.descendants(matching: .any)["people.detail"]
        let fifth = directoryRow("00000000-0000-0000-0000-0000000C0005")
        let sixth = directoryRow("00000000-0000-0000-0000-0000000C0006")
        let mayaChen = directoryRow("00000000-0000-0000-0000-000000000782")

        choosePerson(fifth, showing: "contact5@example.com", in: detail)
        choosePerson(sixth, showing: "contact6@example.com", in: detail)
        XCTAssertFalse(detail.staticTexts["contact5@example.com"].exists, "The previous profile must close")
        #if os(macOS)
        XCTAssertTrue(sixth.isSelected, "The directory marks the person whose profile is open")
        XCTAssertFalse(fifth.isSelected)
        #endif
        // Maya Chen's profile links to Maya and to a fact's evidence; the
        // directory must still replace it. She is second in the directory, so
        // a short list has scrolled past her to reach the contacts.
        choosePerson(mayaChen, showing: "maya.chen@example.com", in: detail, above: true)
        choosePerson(fifth, showing: "contact5@example.com", in: detail)
    }

    private func directoryRow(_ id: String) -> XCUIElement {
        app.descendants(matching: .any)["people.row.\(id)"]
    }

    #if os(macOS)
    /// Scrolls the directory column until the row sits inside it. A row the
    /// column has scrolled past keeps a frame outside the column, and a click
    /// on that frame lands on whatever the sheet shows there instead.
    private func revealDirectoryRow(_ row: XCUIElement, above: Bool) {
        let column = macDirectoryColumn
        XCTAssertTrue(column.waitForExistence(timeout: 5), app.debugDescription)
        for _ in 0..<16 {
            if row.exists && column.frame.contains(row.frame) { return }
            let up = row.exists ? row.frame.minY < column.frame.minY : above
            column.scroll(byDeltaX: 0, deltaY: up ? 150 : -150)
        }
    }

    /// The directory's scroll view: the element that carries `people.browser`
    /// when that is the scroll view, otherwise the scroll view inside it.
    private var macDirectoryColumn: XCUIElement {
        let browser = app.descendants(matching: .any)["people.browser"].firstMatch
        if browser.exists && browser.elementType == .scrollView { return browser }
        let inner = browser.scrollViews.firstMatch
        if inner.exists { return inner }
        return app.sheets.firstMatch.scrollViews.containing(.any, identifier: "people.browser").firstMatch
    }
    #endif

    #if os(iOS)
    /// Drags the lazily rendered directory until the row sits clear of the
    /// search field that floats over its lower edge. A row above the rows in
    /// view is out of the hierarchy, so the caller says to drag the other way,
    /// until the row also clears the list's top edge.
    private func revealDirectoryRow(_ row: XCUIElement, above: Bool) {
        let list = app.descendants(matching: .any)["people.browser"].firstMatch
        XCTAssertTrue(list.waitForExistence(timeout: 5))
        let (from, to): (CGFloat, CGFloat) = above ? (0.45, 0.7) : (0.7, 0.45)
        for _ in 0..<8 {
            if row.exists && row.isHittable && row.frame.maxY < list.frame.maxY - 100
                && (!above || row.frame.minY >= list.frame.minY) { return }
            // A slow drag that holds at its end moves the list without a fling.
            list.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: from)).press(
                forDuration: 0.1, thenDragTo: list.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: to)),
                withVelocity: .slow, thenHoldForDuration: 0.3)
        }
    }
    #endif

    /// Opens a person from the directory, returning to it first where a compact
    /// layout pushed the previous profile over it. `above` marks a person
    /// listed before the rows in view.
    private func choosePerson(_ row: XCUIElement, showing text: String, in detail: XCUIElement, above: Bool = false) {
        #if os(iOS)
        // The sheet's back button, not the first button of the window's bar behind it.
        if detail.exists && !row.isHittable && !app.buttons["memory.group.people"].isHittable {
            app.navigationBars.buttons["BackButton"].firstMatch.tap()
        }
        #endif
        revealDirectoryRow(row, above: above)
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        activate(row)
        XCTAssertTrue(
            detail.staticTexts[text].waitForExistence(timeout: 5),
            "Choosing a person must open their profile (\(text))"
        )
    }

    func testPeopleForgetExplainsSourceRetentionAndPendingCleanup() {
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
        app.launch()
        app.activate()
        let historical = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(historical.waitForExistence(timeout: 10))
        activate(historical)
        XCTAssertTrue(app.buttons["chat.people"].waitForExistence(timeout: 5))
        activate(app.buttons["chat.people"])
        let person = app.descendants(matching: .any)["people.row.00000000-0000-0000-0000-000000000777"]
        XCTAssertTrue(person.waitForExistence(timeout: 5))
        activate(person)
        let forget = app.buttons["Forget person…"]
        XCTAssertTrue(app.descendants(matching: .any)["people.detail"].waitForExistence(timeout: 5))
        #if os(macOS)
        let scrolls = app.sheets.firstMatch.scrollViews
        let detailScroll = scrolls.element(boundBy: scrolls.count - 1)
        // SwiftUI may report an offscreen scroll child as hittable. Bring its
        // complete frame into the viewport before exercising the action.
        for _ in 0..<6 where !forget.isHittable || !detailScroll.frame.contains(forget.frame) {
            detailScroll.scroll(byDeltaX: 0, deltaY: -250)
        }
        #else
        scrollUntilVisible(forget)
        #endif
        activate(forget)
        XCTAssertTrue(app.staticTexts["Forget derived memories about Maya. Original Chat and Email messages remain at their sources."].waitForExistence(timeout: 5))
        #if os(macOS)
        let apply = app.windows.buttons["Forget person"]
        #else
        let apply = app.buttons["Forget person"]
        #endif
        activate(apply)
        let cleanup = app.buttons["Check cleanup"]
        XCTAssertTrue(cleanup.waitForExistence(timeout: 5))
        XCTAssertFalse(app.buttons["Repair identity…"].exists)
        activate(cleanup)
        XCTAssertTrue(app.staticTexts["Derived memories and history have been removed. Original messages remain at their source."].waitForExistence(timeout: 5))
        XCTAssertFalse(cleanup.exists)
    }

    func testPeopleMailboxImportPreviewShowsExplicitCoverage() {
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
        app.launch()
        app.activate()
        let historical = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(historical.waitForExistence(timeout: 10))
        activate(historical)
        XCTAssertTrue(app.buttons["chat.people"].waitForExistence(timeout: 5))
        activate(app.buttons["chat.people"])
        let open = app.buttons["Import history"]
        XCTAssertTrue(open.waitForExistence(timeout: 5))
        activate(open)
        #if os(macOS)
        let mailbox = app.checkBoxes["people.import.mailbox"]
        let account = app.checkBoxes["people.import.account.work"]
        #else
        let mailbox = app.switches["people.import.mailbox"].firstMatch
        let account = app.switches["people.import.account.work"].firstMatch
        #endif
        XCTAssertTrue(mailbox.waitForExistence(timeout: 5))
        #if os(macOS)
        activate(mailbox)
        #else
        mailbox.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5)).tap()
        #endif
        XCTAssertTrue((mailbox.value as? String) == "1" || (mailbox.value as? Int) == 1)
        #if os(iOS)
        scrollUntilVisible(account)
        #endif
        XCTAssertTrue(account.waitForExistence(timeout: 5))
        #if os(macOS)
        activate(account)
        #else
        account.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.5)).tap()
        #endif
        XCTAssertTrue((account.value as? String) == "1" || (account.value as? Int) == 1)
        let preview = app.buttons["Preview import"]
        #if os(macOS)
        let scrolls = app.sheets.firstMatch.scrollViews
        let importScroll = scrolls.element(boundBy: scrolls.count - 1)
        for _ in 0..<6 where !preview.isHittable { importScroll.scroll(byDeltaX: 0, deltaY: -250) }
        #else
        scrollUntilVisible(preview)
        #endif
        XCTAssertTrue(preview.waitForExistence(timeout: 5))
        activate(preview)
        XCTAssertTrue(app.staticTexts["Selected mailbox history"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Start import"].exists)
    }

    func testPeopleIdentityPreviewRemainsReviewableAfterEditorCloses() {
        #if os(macOS)
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        #endif
        app.launch()
        app.activate()
        let historical = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(historical.waitForExistence(timeout: 10))
        activate(historical)
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].waitForExistence(timeout: 5))
        activate(app.buttons["chat.people"])
        let person = app.descendants(matching: .any)["people.row.00000000-0000-0000-0000-000000000777"]
        XCTAssertTrue(person.waitForExistence(timeout: 5))
        activate(person)
        let repair = app.buttons["Repair identity…"]
        XCTAssertTrue(app.descendants(matching: .any)["people.detail"].waitForExistence(timeout: 5))
        #if os(macOS)
        let scrolls = app.sheets.firstMatch.scrollViews
        let detailScroll = scrolls.element(boundBy: scrolls.count - 1)
        // SwiftUI may report an offscreen scroll child as hittable. Bring its
        // complete frame into the viewport before exercising the action.
        for _ in 0..<6 where !repair.isHittable || !detailScroll.frame.contains(repair.frame) {
            detailScroll.scroll(byDeltaX: 0, deltaY: -250)
        }
        #else
        scrollUntilVisible(repair)
        #endif
        XCTAssertTrue(repair.waitForExistence(timeout: 5))
        activate(repair)
        let destination = app.buttons["people.identity.destination.00000000-0000-0000-0000-000000000782"]
        XCTAssertTrue(destination.waitForExistence(timeout: 5))
        activate(destination)
        activate(app.buttons["Preview"])
        #if os(macOS)
        let apply = app.windows.buttons["Apply change"]
        #else
        let apply = app.buttons["Apply change"]
        #endif
        XCTAssertTrue(apply.waitForExistence(timeout: 5), "The editor must close before its preview asks for confirmation")
        activate(apply)
        let saved = app.staticTexts["Identity change saved."]
        #if os(macOS)
        for _ in 0..<6 where !saved.isHittable || !detailScroll.frame.contains(saved.frame) {
            detailScroll.scroll(byDeltaX: 0, deltaY: -250)
        }
        #else
        scrollUntilVisible(saved)
        #endif
        XCTAssertTrue(saved.waitForExistence(timeout: 5))
        let undo = app.buttons["Preview undo"]
        #if os(macOS)
        for _ in 0..<6 where !undo.isHittable || !detailScroll.frame.contains(undo.frame) {
            detailScroll.scroll(byDeltaX: 0, deltaY: -250)
        }
        #else
        scrollUntilVisible(undo)
        #endif
        XCTAssertTrue(undo.waitForExistence(timeout: 5))
    }

    #if os(iOS)
    func testEmailCompactTraitNavigationReturnsToSelectedInbox() {
        app.launchEnvironment["VEETBOT_UI_TEST_SIZE_CLASS"] = "compact"
        app.launch()
        let emailMode = app.buttons["mode.email"]
        XCTAssertTrue(emailMode.waitForExistence(timeout: 10))
        emailMode.tap()
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()
        XCTAssertTrue(app.staticTexts["Please review the agenda before Friday."].waitForExistence(timeout: 5))
        XCTAssertFalse(row.isHittable, "Compact navigation must show the selected detail, not a side-by-side inbox")
        let back = app.navigationBars.buttons.firstMatch
        XCTAssertTrue(back.waitForExistence(timeout: 5))
        back.tap()
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        XCTAssertTrue(row.isHittable)
    }

    func testEmailLearningControlsShowCurrentState() {
        app.launch()
        let emailMode = app.buttons["mode.email"]
        XCTAssertTrue(emailMode.waitForExistence(timeout: 10))
        emailMode.tap()
        let learning = app.buttons["email.learning"]
        XCTAssertTrue(learning.waitForExistence(timeout: 5))
        learning.tap()
        XCTAssertTrue(app.staticTexts["Email learning"].waitForExistence(timeout: 5))
        XCTAssertTrue(app.buttons["Pause learning"].waitForExistence(timeout: 5))
        app.buttons["Pause learning"].tap()
        XCTAssertTrue(app.buttons["Resume learning"].waitForExistence(timeout: 5))
    }

    /// Exercises handled state, feedback, editing and exact-send approval on the native thread screen.
    func testEmailThreadFeedbackEditingAndExplicitSend() {
        app.launch()
        let emailMode = app.buttons["mode.email"]
        XCTAssertTrue(emailMode.waitForExistence(timeout: 10))
        emailMode.tap()
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.tap()
        XCTAssertTrue(app.staticTexts["Please review the agenda before Friday."].waitForExistence(timeout: 5))
        checkHandledActionInDetail()
        app.buttons["email.feedback"].tap()
        app.buttons["email.feedback-important"].tap()
        XCTAssertTrue(app.staticTexts["Marked this thread as important."].waitForExistence(timeout: 5))
        let editor = app.textViews["email.draft-body"]
        scrollEmailUntilVisible(editor)
        XCTAssertTrue(editor.isHittable, app.debugDescription)
        editor.tap()
        editor.typeText(" Thanks again.")
        let keyboardDone = app.buttons["email.keyboard-done"]
        XCTAssertTrue(keyboardDone.waitForExistence(timeout: 5))
        keyboardDone.tap()
        let review = app.buttons["email.review-send"]
        scrollEmailUntilVisible(review)
        XCTAssertTrue(review.isHittable)
        review.tap()
        let send = app.buttons["email.approve-send"]
        XCTAssertTrue(send.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["To: alex@example.test"].exists)
        XCTAssertFalse(app.staticTexts["Sent"].exists)
        send.tap()
        XCTAssertTrue(app.staticTexts["Sent"].waitForExistence(timeout: 10))
    }

    func testEmailModePreservesAnUnsentChatMessage() {
        app.launch()
        let historicalRow = app.descendants(matching: .any)[
            "sidebar.session.00000000-0000-0000-0000-000000000123"
        ]
        XCTAssertTrue(historicalRow.waitForExistence(timeout: 10))
        historicalRow.tap()
        let composer = app.descendants(matching: .any)["chat.composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText("Keep my unfinished message")

        let emailMode = app.buttons["mode.email"]
        XCTAssertTrue(emailMode.waitForExistence(timeout: 5))
        emailMode.tap()
        XCTAssertTrue(app.descendants(matching: .any)["email.inbox"].waitForExistence(timeout: 5))
        app.buttons["mode.chat"].tap()

        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        XCTAssertEqual(composer.value as? String, "Keep my unfinished message")
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].exists)
    }

    func testHistoricalAndNewConversationRowsOpenChat() {
        app.launch()
        let historicalRow = app.descendants(matching: .any)[
            "sidebar.session.00000000-0000-0000-0000-000000000123"
        ]
        XCTAssertTrue(historicalRow.waitForExistence(timeout: 10))

        historicalRow.tap()

        XCTAssertTrue(app.staticTexts["Historical question"].waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Historical answer loaded"].exists)

        let newConversationRow = app.descendants(matching: .any)[
            "sidebar.new-conversation"
        ]
        revealSidebarIfNeeded(for: newConversationRow)
        XCTAssertTrue(newConversationRow.waitForExistence(timeout: 5))
        newConversationRow.tap()

        // The conversation's title heads the transcript; the toolbar shows it
        // only once that heading has scrolled away (type revision 2).
        let heading = app.staticTexts["chat.heading"]
        XCTAssertTrue(heading.waitForExistence(timeout: 5))
        XCTAssertEqual(heading.label, "New conversation")
        XCTAssertTrue(app.descendants(matching: .any)["chat.composer"].exists)
    }

    func testSwitchesBetweenHistoricalConversations() {
        app.launch()
        let firstRow = app.descendants(matching: .any)[
            "sidebar.session.00000000-0000-0000-0000-000000000123"
        ]
        let secondRow = app.descendants(matching: .any)[
            "sidebar.session.00000000-0000-0000-0000-000000000456"
        ]
        XCTAssertTrue(firstRow.waitForExistence(timeout: 10))
        XCTAssertTrue(secondRow.waitForExistence(timeout: 10))

        firstRow.tap()

        XCTAssertTrue(app.staticTexts["Historical answer loaded"].waitForExistence(timeout: 10))

        revealSidebarIfNeeded(for: secondRow)
        XCTAssertTrue(secondRow.waitForExistence(timeout: 5))
        secondRow.tap()

        XCTAssertTrue(
            app.staticTexts["Second historical answer loaded"].waitForExistence(timeout: 10)
        )
        XCTAssertFalse(app.staticTexts["Historical answer loaded"].exists)
    }

    /// The owner reads the morning's scheduled briefing on an iPad after
    /// triaging Email. Opening it from Chat must acknowledge it, so the
    /// schedule's new-report dot clears while the report is on screen.
    func testReadingAScheduledReportAfterEmailClearsItsNewReportDot() {
        app.launchArguments.append("--ui-testing-unread-report")
        app.launch()
        let group = app.descendants(matching: .any)[
            "sidebar.schedule.00000000-0000-0000-0000-000000000654"
        ]
        XCTAssertTrue(group.waitForExistence(timeout: 10))
        waitForLabel(of: group, toContain: "New report", true)

        let emailMode = app.buttons["mode.email"]
        XCTAssertTrue(emailMode.waitForExistence(timeout: 5))
        emailMode.tap()
        XCTAssertTrue(app.descendants(matching: .any)["email.inbox"].waitForExistence(timeout: 5))
        app.buttons["mode.chat"].tap()

        let report = app.descendants(matching: .any)[
            "sidebar.session.00000000-0000-0000-0000-0000000005A1"
        ]
        // The mode switch can still be settling, so expand until the row shows.
        for _ in 0..<3 where !report.waitForExistence(timeout: 2) {
            if group.value as? String != "Expanded" { group.tap() }
        }
        XCTAssertTrue(report.waitForExistence(timeout: 5))
        report.tap()
        XCTAssertTrue(app.staticTexts["Weekday briefing loaded"].waitForExistence(timeout: 10))

        // A compact window stacks the report over the sidebar; reading it
        // first leaves the acknowledgement time to land before going back.
        if !group.isHittable { sleep(3) }
        revealSidebarIfNeeded(for: group)
        waitForLabel(of: group, toContain: "New report", false)
    }

    /// The owner opens the app from the background each morning; the report
    /// read after returning must still be acknowledged.
    func testReadingAScheduledReportAfterReturningToTheAppClearsItsNewReportDot() {
        app.launchArguments.append("--ui-testing-unread-report")
        app.launch()
        let group = app.descendants(matching: .any)[
            "sidebar.schedule.00000000-0000-0000-0000-000000000654"
        ]
        XCTAssertTrue(group.waitForExistence(timeout: 10))
        waitForLabel(of: group, toContain: "New report", true)

        XCUIDevice.shared.press(.home)
        XCTAssertTrue(app.wait(for: .runningBackground, timeout: 10))
        app.activate()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))

        let report = app.descendants(matching: .any)[
            "sidebar.session.00000000-0000-0000-0000-0000000005A1"
        ]
        for _ in 0..<3 where !report.waitForExistence(timeout: 2) {
            if group.value as? String != "Expanded" { group.tap() }
        }
        XCTAssertTrue(report.waitForExistence(timeout: 5))
        report.tap()
        XCTAssertTrue(app.staticTexts["Weekday briefing loaded"].waitForExistence(timeout: 10))

        if !group.isHittable { sleep(3) }
        revealSidebarIfNeeded(for: group)
        waitForLabel(of: group, toContain: "New report", false)
    }

    private func waitForLabel(
        of element: XCUIElement, toContain text: String, _ contains: Bool, timeout: TimeInterval = 15
    ) {
        let predicate = NSPredicate(format: contains ? "label CONTAINS %@" : "NOT (label CONTAINS %@)", text)
        let met = XCTNSPredicateExpectation(predicate: predicate, object: element)
        XCTAssertEqual(
            XCTWaiter().wait(for: [met], timeout: timeout), .completed,
            "\(element.identifier) label is \"\(element.label)\""
        )
    }

    private func submitSlowChatMessage(fails: Bool = false, useReturn: Bool = false, holdSubmission: Bool = false) {
        app.launchArguments.append("--ui-testing-chat-slow-send")
        if fails { app.launchArguments.append("--ui-testing-chat-send-failure") }
        if holdSubmission { app.launchArguments.append("--ui-testing-chat-hold-submission") }
        app.launch()
        let historicalRow = app.descendants(matching: .any)[
            "sidebar.session.00000000-0000-0000-0000-000000000123"
        ]
        XCTAssertTrue(historicalRow.waitForExistence(timeout: 10))
        historicalRow.tap()

        let composer = app.descendants(matching: .any)["chat.composer"]
        XCTAssertTrue(composer.waitForExistence(timeout: 5))
        composer.tap()
        composer.typeText("Follow up")
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5))

        if useReturn {
            composer.typeText("\n")
        } else {
            let send = app.buttons["Send"]
            XCTAssertTrue(send.isHittable)
            send.tap()
        }
    }

    func testSendingMessageDismissesKeyboard() {
        // Keep acceptance pending even if a hosted accessibility snapshot is slow.
        submitSlowChatMessage(holdSubmission: true)
        // Predicate expectations delay their first poll; a slow accessibility
        // snapshot can then exhaust this deadline even with the keyboard gone.
        XCTAssertTrue(
            app.keyboards.firstMatch.waitForNonExistence(timeout: 2),
            "the keyboard must dismiss before the delayed submission returns"
        )
        XCTAssertTrue(app.staticTexts["Sending…"].exists)
    }

    func testSendingMessageShowsActivityBeforeAcceptance() {
        submitSlowChatMessage(useReturn: true)
        XCTAssertTrue(app.staticTexts["Sending…"].waitForExistence(timeout: 2))
        XCTAssertFalse(app.keyboards.firstMatch.exists)
        XCTAssertEqual(app.descendants(matching: .any)["chat.composer"].value as? String, "")
        XCTAssertFalse(app.buttons["Send"].isEnabled)
        XCTAssertTrue(app.staticTexts["Working…"].waitForExistence(timeout: 12))
        let finished = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "exists == false"),
            object: app.staticTexts["Working…"]
        )
        XCTAssertEqual(XCTWaiter.wait(for: [finished], timeout: 12), .completed)
        XCTAssertFalse(app.staticTexts["Sending…"].exists)
    }

    func testFailedSubmissionRestoresDraft() {
        submitSlowChatMessage(fails: true)
        XCTAssertTrue(app.staticTexts["Sending…"].waitForExistence(timeout: 2))
        XCTAssertTrue(app.alerts.firstMatch.waitForExistence(timeout: 12))
        app.alerts.buttons["OK"].tap()
        let restored = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "value == %@", "Follow up"),
            object: app.descendants(matching: .any)["chat.composer"]
        )
        XCTAssertEqual(XCTWaiter.wait(for: [restored], timeout: 12), .completed)
        XCTAssertFalse(app.staticTexts["Sending…"].exists)
        XCTAssertTrue(app.buttons["Send"].isEnabled)
    }

    func testMemoryBrowserListsAndOpensDetail() {
        app.launch()
        openSidebarDestination(identifier: "sidebar.memory")

        XCTAssertTrue(
            app.descendants(matching: .any)["memory.browser"].waitForExistence(timeout: 5)
        )
        let memoryRow = app.descendants(matching: .any)[
            "memory.row.00000000-0000-0000-0000-000000000321"
        ]
        XCTAssertTrue(memoryRow.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["The user prefers dark mode."].exists)

        memoryRow.tap()

        XCTAssertTrue(
            app.descendants(matching: .any)["memory.detail"].waitForExistence(timeout: 5)
        )
        XCTAssertTrue(app.staticTexts["The user prefers dark mode."].exists)
        XCTAssertTrue(app.staticTexts["User"].exists)
    }

    /// ADR-0117: the detail's review menu deletes a belief once its
    /// confirmation says what is removed; the detail closes, the row leaves
    /// the browser, and a later read of the server no longer returns it.
    func testMemoryDeletionRemovesTheBeliefAfterConfirmation() {
        app.launch()
        openSidebarDestination(identifier: "sidebar.memory")
        let row = app.descendants(matching: .any)["memory.row.00000000-0000-0000-0000-000000000321"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        row.tap()
        let detail = app.descendants(matching: .any)["memory.detail"]
        XCTAssertTrue(detail.waitForExistence(timeout: 5))
        let review = app.buttons["memory.detail.review"]
        XCTAssertTrue(review.waitForExistence(timeout: 5))
        review.tap()
        let delete = app.buttons["memory.detail.delete"]
        XCTAssertTrue(delete.waitForExistence(timeout: 5))
        // An active, portable belief also offers both rejection outcomes.
        XCTAssertTrue(app.buttons["memory.detail.review.untrue"].exists)
        XCTAssertTrue(app.buttons["memory.detail.review.not_here"].exists)
        XCTAssertFalse(app.buttons["memory.detail.review.dismiss"].exists, "Only a flagged belief can be marked reviewed")
        delete.tap()
        // A query identifier is limited to 128 characters, so match the explanation by predicate.
        let explanation = app.staticTexts.matching(NSPredicate(
            format: "label == %@",
            "The memory and its generated copies are removed, and the same statement will not form again. Original messages remain at their source."
        )).firstMatch
        XCTAssertTrue(explanation.waitForExistence(timeout: 5), app.debugDescription)
        let confirm = app.buttons["Delete memory"].firstMatch
        XCTAssertTrue(confirm.exists)
        confirm.tap()
        XCTAssertTrue(row.waitForNonExistence(timeout: 10), app.debugDescription)
        XCTAssertFalse(app.buttons["memory.detail.review"].exists, "The deleted belief's detail must close")

        // Reopening the browser reads the server again.
        let close = app.buttons["Close"].firstMatch
        XCTAssertTrue(close.waitForExistence(timeout: 5), app.debugDescription)
        close.tap()
        XCTAssertTrue(app.descendants(matching: .any)["memory.browser"].waitForNonExistence(timeout: 5))
        openSidebarDestination(identifier: "sidebar.memory")
        XCTAssertTrue(app.descendants(matching: .any)["memory.browser"].waitForExistence(timeout: 5))
        XCTAssertFalse(row.waitForExistence(timeout: 3))
        XCTAssertFalse(app.staticTexts["The user prefers dark mode."].exists)
    }

    /// ADR-0224: the detail quotes the owner's own words and names other
    /// sources without their text; Edit wording supersedes the memory with the
    /// owner's words, and the detail then shows the new memory.
    func testMemoryWordingIsCorrectedAndTheOwnersWordsAreQuoted() {
        app.launch()
        openSidebarDestination(identifier: "sidebar.memory")
        let row = app.descendants(matching: .any)["memory.row.00000000-0000-0000-0000-000000000321"]
        XCTAssertTrue(row.waitForExistence(timeout: 5))
        row.tap()
        let evidence = app.descendants(matching: .any)["memory.detail.evidence"]
        XCTAssertTrue(evidence.waitForExistence(timeout: 5), app.debugDescription)
        XCTAssertTrue(
            app.staticTexts.matching(NSPredicate(format: "label CONTAINS %@", "I prefer dark mode everywhere."))
                .firstMatch.exists,
            app.debugDescription
        )
        XCTAssertTrue(
            app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "From an email"))
                .firstMatch.exists
        )

        let review = app.buttons["memory.detail.review"]
        XCTAssertTrue(review.waitForExistence(timeout: 5))
        review.tap()
        let edit = app.buttons["memory.detail.edit"]
        XCTAssertTrue(edit.waitForExistence(timeout: 5), app.debugDescription)
        edit.tap()
        let save = app.buttons["memory.wording.save"]
        XCTAssertTrue(save.waitForExistence(timeout: 5), app.debugDescription)
        XCTAssertFalse(save.isEnabled, "The unchanged wording cannot be saved")
        let field = app.textFields["memory.wording.text"].exists
            ? app.textFields["memory.wording.text"] : app.textViews["memory.wording.text"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        // The sheet focuses the field with the caret after the wording, so a
        // tap (which would move the caret) is not needed to clear it.
        XCTAssertTrue(app.keyboards.firstMatch.waitForExistence(timeout: 5), app.debugDescription)
        field.typeText(String(repeating: XCUIKeyboardKey.delete.rawValue, count: 40) + "The user prefers light mode.")
        XCTAssertTrue(save.isEnabled)
        save.tap()
        XCTAssertTrue(
            app.staticTexts["Updated. The earlier wording is kept as history."].waitForExistence(timeout: 3),
            app.debugDescription
        )
        XCTAssertTrue(save.waitForNonExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["The user prefers light mode."].waitForExistence(timeout: 5))
    }

    func testScheduleBrowserListsAndOpensPointReadDetail() {
        app.launch()
        openSidebarDestination(identifier: "sidebar.schedules")

        XCTAssertTrue(
            app.descendants(matching: .any)["schedule.browser"].waitForExistence(timeout: 5)
        )
        let scheduleRow = app.descendants(matching: .any)[
            "schedule.row.00000000-0000-0000-0000-000000000654"
        ]
        XCTAssertTrue(scheduleRow.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Daily review"].exists)
        XCTAssertTrue(app.staticTexts["Preview from the schedule index."].exists)

        scheduleRow.tap()

        XCTAssertTrue(
            app.descendants(matching: .any)["schedule.detail"].waitForExistence(timeout: 5)
        )
        XCTAssertTrue(app.staticTexts["Full instruction from the schedule point read."].exists)
    }

    /// ADR-0225 part A: Pause confirms with a toast whose Undo resumes, and a
    /// recent run opens its report in Chat.
    func testSchedulePausesResumesAndOpensARecentReport() {
        app.launchArguments.append("--ui-testing-unread-report")
        app.launch()
        openSidebarDestination(identifier: "sidebar.schedules")
        let scheduleRow = app.descendants(matching: .any)[
            "schedule.row.00000000-0000-0000-0000-000000000654"
        ]
        XCTAssertTrue(scheduleRow.waitForExistence(timeout: 5))
        scheduleRow.tap()

        // ADR-0225 part C: Run now starts one run and says so.
        let runNow = app.buttons["schedule.run-now"]
        XCTAssertTrue(runNow.waitForExistence(timeout: 5), app.debugDescription)
        runNow.tap()
        XCTAssertTrue(app.staticTexts["Running now. The report arrives in Chat."].waitForExistence(timeout: 3))

        let pause = app.buttons["schedule.pause"]
        XCTAssertTrue(pause.waitForExistence(timeout: 5), app.debugDescription)
        pause.tap()
        XCTAssertTrue(app.buttons["schedule.resume"].waitForExistence(timeout: 5), app.debugDescription)
        XCTAssertTrue(app.staticTexts["Paused. Nothing runs until you resume it."].waitForExistence(timeout: 3))
        let undo = app.buttons["toast.undo"]
        XCTAssertTrue(undo.exists)
        undo.tap()
        XCTAssertTrue(app.buttons["schedule.pause"].waitForExistence(timeout: 5), app.debugDescription)

        let open = app.buttons["schedule.run.open.00000000-0000-0000-0000-0000000006A1"]
        scrollUntilVisible(open)
        XCTAssertTrue(open.exists, app.debugDescription)
        XCTAssertTrue(app.staticTexts["Skipped: the last run was still going"].exists)
        open.tap()
        XCTAssertTrue(
            app.descendants(matching: .any)["schedule.browser"].waitForNonExistence(timeout: 5),
            "Opening a report closes the schedule browser"
        )
    }

    /// ADR-0225 part B: a sentence is read and shown before Create saves it.
    func testANewScheduleIsDescribedInPlainWordsAndConfirmedBeforeSaving() {
        app.launch()
        openSidebarDestination(identifier: "sidebar.schedules")
        let new = app.buttons["schedule.new"]
        XCTAssertTrue(new.waitForExistence(timeout: 5), app.debugDescription)
        new.tap()
        let create = app.buttons["schedule.new.create"]
        XCTAssertTrue(create.waitForExistence(timeout: 5))
        XCTAssertFalse(create.isEnabled, "Nothing can be saved before the timing is read")
        let field = app.textFields["schedule.new.text"].exists
            ? app.textFields["schedule.new.text"] : app.textViews["schedule.new.text"]
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        field.tap()
        field.typeText("Every weekday at 7 am, tell me what needs me today.")
        let understood = app.staticTexts["schedule.new.understood"]
        XCTAssertTrue(understood.waitForExistence(timeout: 8), app.debugDescription)
        XCTAssertTrue(create.isEnabled)
        create.tap()
        XCTAssertTrue(
            app.staticTexts.matching(NSPredicate(format: "label BEGINSWITH %@", "Scheduled. The first run is"))
                .firstMatch.waitForExistence(timeout: 3),
            app.debugDescription
        )
        XCTAssertTrue(create.waitForNonExistence(timeout: 5))
    }

    func testScheduleDetailBindsAReadySignInThroughTheServer() {
        app.launchArguments.append("--ui-testing-schedule-website-access")
        app.launch()
        openSidebarDestination(identifier: "sidebar.schedules")

        let scheduleRow = app.descendants(matching: .any)[
            "schedule.row.00000000-0000-0000-0000-000000000654"
        ]
        XCTAssertTrue(scheduleRow.waitForExistence(timeout: 5))
        scheduleRow.tap()

        let picker = app.buttons["schedule.websiteAccess"]
        XCTAssertTrue(picker.waitForExistence(timeout: 5), app.debugDescription)
        picker.tap()
        let choice = app.buttons["x.com"]
        XCTAssertTrue(choice.waitForExistence(timeout: 5), app.debugDescription)
        choice.tap()

        // The picker shows the server's record, so it only reads x.com once the
        // fixture accepted the update; a rejected one snaps back to None.
        let bound = NSPredicate(format: "label CONTAINS %@ OR value CONTAINS %@", "x.com", "x.com")
        expectation(for: bound, evaluatedWith: picker)
        waitForExpectations(timeout: 5)
        XCTAssertFalse(
            app.descendants(matching: .any)["schedule.websiteAccess.error"].exists,
            app.debugDescription
        )
    }

    func testScheduleBrowserMakesRecentTerminalHistoryAccessible() {
        app.launch()
        openSidebarDestination(identifier: "sidebar.schedules")

        XCTAssertTrue(
            app.descendants(matching: .any)["schedule.browser"].waitForExistence(timeout: 5)
        )
        let history = app.buttons["Recent History"]
        XCTAssertTrue(history.waitForExistence(timeout: 5))
        history.tap()

        let historyRow = app.descendants(matching: .any)[
            "schedule.row.00000000-0000-0000-0000-000000000656"
        ]
        XCTAssertTrue(historyRow.waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Finished review"].exists)
        XCTAssertTrue(app.staticTexts["Recent completed schedule."].exists)
        XCTAssertFalse(app.staticTexts["Daily review"].exists)
    }

    func testOverflowMenuOpensPersonaEditor() {
        app.launch()
        openSidebarDestination(identifier: "sidebar.persona")

        XCTAssertTrue(
            app.descendants(matching: .any)["persona.editor"].waitForExistence(timeout: 5)
        )
    }

    /// ADR-0118: the Models card shows the server's choices, each picker change
    /// saves the whole document under its version (the fixture refuses a stale
    /// version or a tuple it did not offer), a memory model brings its one
    /// evaluated effort, and Settings opened again reads the choices back.
    func testModelSettingsSaveEachChoiceAndReadItBackFromTheServer() {
        app.launch()
        openSidebarDestination(identifier: "sidebar.settings")
        XCTAssertTrue(app.staticTexts["Settings"].waitForExistence(timeout: 5))
        openSettingsGroup("models")
        let chatModel = app.buttons["settings.models.chat.model"]
        let memoryModel = app.buttons["settings.models.memory.model"]
        let memoryEffort = app.buttons["settings.models.memory.effort"]
        scrollUntilVisible(memoryEffort)
        XCTAssertEqual(chatModel.label, "Chat model, GPT-6 Astra")
        XCTAssertEqual(app.buttons["settings.models.chat.effort"].label, "Chat reasoning, High")
        XCTAssertEqual(memoryModel.label, "Memory model, GPT-5.6 Sol")
        XCTAssertFalse(memoryEffort.isEnabled, "A model with one evaluated effort offers no choice")

        chooseMenuItem("Claude Fable 5.1", from: chatModel)
        waitForSavedLabel("Chat model, Claude Fable 5.1", on: chatModel)
        chooseMenuItem("GPT-6 Astra", from: memoryModel)
        waitForSavedLabel("Memory model, GPT-6 Astra", on: memoryModel)
        XCTAssertEqual(memoryEffort.label, "Memory reasoning, Medium",
                       "Choosing a memory model applies the effort it was evaluated with")
        XCTAssertFalse(app.staticTexts["settings.models.status"].exists, "A confirmed save reports nothing")

        let close = app.buttons["Close"].firstMatch
        XCTAssertTrue(close.exists)
        close.tap()
        XCTAssertTrue(close.waitForNonExistence(timeout: 5))
        openSidebarDestination(identifier: "sidebar.settings")
        openSettingsGroup("models")
        scrollUntilVisible(memoryEffort)
        waitForSavedLabel("Chat model, Claude Fable 5.1", on: chatModel)
        XCTAssertEqual(memoryModel.label, "Memory model, GPT-6 Astra")
        XCTAssertEqual(memoryEffort.label, "Memory reasoning, Medium")
    }

    private func chooseMenuItem(_ title: String, from picker: XCUIElement) {
        XCTAssertTrue(picker.isEnabled)
        picker.tap()
        let item = app.buttons[title].firstMatch
        XCTAssertTrue(item.waitForExistence(timeout: 5))
        let window = app.windows.firstMatch
        // iOS 27 can ignore the first synthesized menu tap. Use settled
        // geometry and prove dismissal before checking the server's saved choice.
        for _ in 0..<3 {
            waitForSettledFrame(of: item, stableFor: 3)
            let ready = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "hittable == true"), object: item)
            XCTAssertEqual(XCTWaiter.wait(for: [ready], timeout: 10), .completed)
            let target = item.frame
            let windowFrame = window.frame
            XCTAssertFalse(target.isEmpty || target.isInfinite || target.isNull)
            XCTAssertTrue(windowFrame.contains(target))
            window.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(
                dx: target.midX - windowFrame.minX, dy: target.midY - windowFrame.minY
            )).tap()
            if item.waitForNonExistence(timeout: 2) { return }
        }
        XCTFail("The model menu stayed open after three taps on \(title)")
    }

    /// Waits for the picker to report the server-confirmed choice and unlock.
    private func waitForSavedLabel(_ label: String, on picker: XCUIElement) {
        let saved = NSPredicate(format: "label == %@ AND enabled == true", label)
        XCTAssertEqual(
            XCTWaiter.wait(for: [XCTNSPredicateExpectation(predicate: saved, object: picker)], timeout: 10),
            .completed, "\(picker.label) did not settle on \(label)")
    }

    func testWebsiteAccessCreatesARecoverableBrowserHandoff() {
        app.launch()
        openSidebarDestination(identifier: "sidebar.settings")
        XCTAssertTrue(app.staticTexts["Settings"].waitForExistence(timeout: 5))
        openSettingsGroup("websiteAccess")

        let websiteAccess = app.staticTexts["Website Access"]
        scrollUntilVisible(websiteAccess)
        XCTAssertTrue(websiteAccess.exists)

        let website = app.textFields["website-access.url"]
        scrollUntilVisible(website)
        XCTAssertTrue(website.exists)
        XCTAssertFalse(app.textFields["website-access.origin"].exists)
        XCTAssertFalse(app.textFields["website-access.login-url"].exists)
        website.tap()
        website.typeText("example.org")
        if app.keyboards.buttons["Return"].exists {
            app.keyboards.buttons["Return"].tap()
        }

        // Sign in on this device is the primary action (ADR-0128); the remote
        // browser keeps the recoverable handoff this journey checks.
        let signInHere = app.buttons["website-access.sign-in-on-device"]
        scrollUntilVisible(signInHere)
        XCTAssertTrue(signInHere.isEnabled)
        let create = app.buttons["website-access.remote-browser"]
        scrollUntilVisible(create)
        XCTAssertTrue(create.isEnabled)
        create.tap()

        let continueInBrowser = app.buttons["Continue in web browser"]
        XCTAssertTrue(continueInBrowser.waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Start over"].exists)

        openSettingsGroup("dataAndPrivacy")
        let clientBuild = app.staticTexts["Client build"]
        scrollUntilVisible(clientBuild)
        XCTAssertTrue(clientBuild.exists)
        XCTAssertTrue(app.staticTexts["Version 0.1.1 (2)"].exists)
    }
    #endif

    #if os(macOS)
    /// Prevents implicit person feedback and stale values from crossing feedback scopes.
    func testEmailFeedbackRequiresAnExplicitPersonAndClearsChangedTargets() {
        app.launch()
        activate(app.buttons["mode.email"])
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        activate(row)
        #if os(macOS)
        // Teach Veetbot lives in the right panel's Context tab (ADR-0237).
        showEmailPanel()
        #endif
        activate(app.buttons["email.feedback"])
        let target = app.popUpButtons["email.feedback-target"]
        let important = app.buttons["email.feedback-important"]
        let lessImportant = app.buttons["email.feedback-less-important"]
        XCTAssertTrue(target.waitForExistence(timeout: 5))
        target.click()
        app.menuItems["This person"].click()
        XCTAssertFalse(important.isEnabled, "Person feedback requires an explicit selection")
        XCTAssertFalse(lessImportant.isEnabled)
        app.popUpButtons["email.feedback-person"].click()
        app.menuItems["alex@example.test"].click()
        XCTAssertTrue(important.isEnabled)
        XCTAssertTrue(lessImportant.isEnabled)
        target.click()
        app.menuItems["This kind of content"].click()
        let topic = app.buttons["email.feedback-topic-chip.Board planning"]
        XCTAssertFalse(important.isEnabled, "Content feedback requires an explicit topic")
        XCTAssertFalse(lessImportant.isEnabled)
        XCTAssertTrue(topic.waitForExistence(timeout: 5))
        XCTAssertFalse(topic.isSelected)
        topic.click()
        XCTAssertTrue(important.isEnabled)
        XCTAssertTrue(lessImportant.isEnabled)
        important.click()
        XCTAssertTrue(app.staticTexts["Marked Board planning as important."].waitForExistence(timeout: 5))
        target.click()
        app.menuItems["This person"].click()
        XCTAssertEqual(app.popUpButtons["email.feedback-person"].value as? String, "Choose a person")
        XCTAssertFalse(important.isEnabled)
        XCTAssertFalse(lessImportant.isEnabled)
        target.click()
        app.menuItems["This thread"].click()
        XCTAssertTrue(important.isEnabled)
        XCTAssertTrue(lessImportant.isEnabled)
        target.click()
        app.menuItems["This kind of content"].click()
        XCTAssertFalse(topic.isSelected)
        XCTAssertFalse(important.isEnabled)
    }

    /// Checks usable mail space with a full priority page at a normal desktop window size.
    func testEmailSidebarShowsFivePrioritiesWithoutScrolling() {
        launchFullEmailInbox()
        let window = app.windows.firstMatch
        let first = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        let fifth = app.buttons["email.thread.00000000-0000-0000-0000-000000000805"]
        XCTAssertTrue(first.waitForExistence(timeout: 10))
        XCTAssertLessThan(first.frame.minY - window.frame.minY, 230,
                          "Mail should begin near the top, below compact controls")
        XCTAssertTrue(fifth.isHittable, "All five initial priorities should be visible without scrolling")
        XCTAssertTrue(window.frame.contains(fifth.frame), "The fifth row must be fully visible")
        attachEmailScreenshot("Compact priority inbox with five threads")
    }

    /// A paused budget leaves the inbox usable and reveals accounting only on demand.
    func testEmailBudgetPauseKeepsAccountingCollapsed() {
        app.launchArguments.append("--ui-testing-email-budget-pause")
        launchFullEmailInbox()
        let reason = app.staticTexts["Daily processing budget is in use."]
        XCTAssertTrue(reason.waitForExistence(timeout: 10))
        let pending = app.staticTexts.matching(NSPredicate(
            format: "label BEGINSWITH %@ OR value BEGINSWITH %@", "Pending costs:", "Pending costs:"
        )).firstMatch
        XCTAssertFalse(pending.exists)
        let first = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(first.isHittable)
        XCTAssertLessThan(first.frame.minY - app.windows.firstMatch.frame.minY, 340,
                          "A pause must leave useful mail space in the sidebar")
        attachEmailScreenshot("Compact email budget pause")
        let details = app.buttons["email.budget-details"]
        XCTAssertTrue(details.exists)
        details.click()
        XCTAssertTrue(pending.waitForExistence(timeout: 3))
        XCTAssertTrue(app.buttons["Check again"].isHittable)
        attachEmailScreenshot("Expanded email budget accounting")
        details.click()
        XCTAssertFalse(pending.exists)
    }

    /// Drags the actual divider and verifies both columns and the selected thread survive mode switching.
    func testEmailSidebarResizesWithItsDivider() {
        launchFullEmailInbox()
        let first = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(first.waitForExistence(timeout: 10))
        first.click()
        let inbox = app.descendants(matching: .any)["email.inbox"]
        let originalWidth = inbox.frame.width
        let divider = app.splitters.firstMatch
        XCTAssertTrue(divider.waitForExistence(timeout: 5))
        let start = divider.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        // macOS 27 ignores the touch-style press-drag on a divider; the mouse
        // click-drag still moves it.
        start.click(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 130, dy: 0)))
        XCTAssertTrue(waitForFrame(of: inbox, timeout: 5) { $0.width > originalWidth + 80 },
                      "Dragging the divider must widen the email list\n\(app.debugDescription)")
        let resizedWidth = inbox.frame.width
        XCTAssertTrue(app.buttons["email.jump-to-reply"].isHittable)
        app.buttons["mode.chat"].click()
        app.buttons["mode.email"].click()
        XCTAssertEqual(inbox.frame.width, resizedWidth, accuracy: 3)
        XCTAssertTrue(app.buttons["email.jump-to-reply"].isHittable)
        attachEmailScreenshot("Resized priority inbox")
    }

    /// Drags the actual divider, quits, and finds the inbox at the dragged width after relaunch.
    func testEmailSidebarWidthPersistsAcrossApplicationRestart() {
        launchFullEmailInbox()
        let inbox = app.descendants(matching: .any)["email.inbox"]
        XCTAssertTrue(inbox.waitForExistence(timeout: 10))
        let originalWidth = inbox.frame.width
        let divider = app.splitters.firstMatch
        XCTAssertTrue(divider.waitForExistence(timeout: 5))
        let start = divider.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.click(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 130, dy: 0)))
        XCTAssertTrue(waitForFrame(of: inbox, timeout: 5) { $0.width > originalWidth + 80 },
                      "Dragging the divider must widen the email list\n\(app.debugDescription)")
        let resizedWidth = inbox.frame.width

        app.launchEnvironment["VEETBOT_UI_TEST_KEEP_EMAIL_INBOX_WIDTH"] = "1"
        defer { app.launchEnvironment.removeValue(forKey: "VEETBOT_UI_TEST_KEEP_EMAIL_INBOX_WIDTH") }
        // Launching again ends the running app first, as quitting it would.
        app.launch()
        XCTAssertTrue(app.buttons["mode.email"].waitForExistence(timeout: 10))
        app.buttons["mode.email"].click()
        XCTAssertTrue(inbox.waitForExistence(timeout: 10))
        XCTAssertTrue(waitForFrame(of: inbox, timeout: 5) { abs($0.width - resizedWidth) <= 3 },
                      "The inbox must reopen at \(resizedWidth) points, not \(inbox.frame.width)")
        attachEmailScreenshot("Inbox width after relaunch")
    }

    /// Launch the five-priority email fixture in a deterministic Mac window.
    private func launchFullEmailInbox() {
        app.launchArguments.append("--ui-testing-email-full-inbox")
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1200,900"
        app.launch()
        XCTAssertTrue(app.buttons["mode.email"].waitForExistence(timeout: 10))
        app.buttons["mode.email"].click()
    }

    /// Reproduces the default CI window and keeps mode, reply and Chat controls inside its bounds.
    func testEmailReadingAtDefaultMacWindowSize() {
        app.launch()
        let initialWindow = app.windows.firstMatch
        XCTAssertTrue(initialWindow.waitForExistence(timeout: 10))
        let initialFrame = initialWindow.frame
        defer {
            app.terminate()
            app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] =
                "\(initialFrame.width),\(initialFrame.height)"
            app.launch()
            let restoredWindow = app.windows.firstMatch
            _ = restoredWindow.waitForExistence(timeout: 10)
            _ = waitForFrame(of: restoredWindow, timeout: 5) {
                abs($0.width - initialFrame.width) <= 3 && abs($0.height - initialFrame.height) <= 3
            }
            app.launchEnvironment.removeValue(forKey: "VEETBOT_UI_TEST_MAIN_WINDOW_FRAME")
        }
        app.terminate()
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "900,612"
        app.launch()
        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 10))
        XCTAssertTrue(waitForFrame(of: window, timeout: 5) {
            abs($0.width - 900) <= 3 && abs($0.height - 612) <= 3
        })
        let emailMode = app.buttons["mode.email"]
        XCTAssertTrue(emailMode.waitForExistence(timeout: 5))
        XCTAssertTrue(window.frame.contains(emailMode.frame), "Mode controls must stay inside the default Mac window")
        checkEmailReadingFlow()
        let composer = app.descendants(matching: .any)["chat.composer"]
        XCTAssertTrue(window.frame.contains(composer.frame), "The Chat composer must also fit after returning from Email")
    }

    /// Exercises Mac thread attention and approval controls through actual native interactions.
    func testEmailModeAndExactDraftApprovalOnMac() {
        app.launch()
        let emailMode = app.buttons["mode.email"]
        XCTAssertTrue(emailMode.waitForExistence(timeout: 10))
        emailMode.click()
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.click()
        XCTAssertTrue(app.staticTexts["Please review the agenda before Friday."].waitForExistence(timeout: 5), app.debugDescription)
        checkHandledActionInDetail()
        let review = app.buttons["email.review-send"]
        // Scroll until the button lies inside the reading column: macOS 26 reports a
        // control just below the viewport, but still inside the window, as hittable.
        let reading = app.scrollViews["email.detail"]
        for _ in 0..<8 where !reading.frame.contains(review.frame) {
            reading.scroll(byDeltaX: 0, deltaY: -350)
        }
        XCTAssertTrue(reading.frame.contains(review.frame), app.debugDescription)
        XCTAssertTrue(review.isHittable)
        review.click()
        let send = app.buttons["email.approve-send"]
        XCTAssertTrue(send.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["To: alex@example.test"].exists)
        send.click()
        XCTAssertTrue(app.staticTexts["Sent"].waitForExistence(timeout: 10))
        // A sent reply is read-only text the owner can still select and copy; a disabled
        // container blocks that. The owner's clipboard is restored afterwards.
        let sentBody = app.staticTexts["email.draft-body"]
        XCTAssertTrue(sentBody.waitForExistence(timeout: 5), app.debugDescription)
        let pasteboard = NSPasteboard.general
        let savedClipboard = pasteboard.string(forType: .string)
        defer {
            pasteboard.clearContents()
            if let savedClipboard { pasteboard.setString(savedClipboard, forType: .string) }
        }
        pasteboard.clearContents()
        sentBody.doubleClick()
        app.typeKey("c", modifierFlags: .command)
        let copied = pasteboard.string(forType: .string) ?? ""
        XCTAssertFalse(copied.isEmpty, "The sent reply must stay selectable")
        XCTAssertTrue((sentBody.value as? String ?? "").contains(copied), copied)
        app.buttons["mode.chat"].click()
        XCTAssertTrue(app.descendants(matching: .any)["sidebar.new-conversation"].waitForExistence(timeout: 5))
    }

    func testMainWindowSizePersistsAcrossApplicationRestart() {
        app.launch()
        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 10))
        let initialFrame = window.frame
        let widthDelta: CGFloat = initialFrame.width >= 1_000 ? -160 : 160
        let heightDelta: CGFloat = initialFrame.height >= 700 ? -100 : 100
        let requestedSize = CGSize(
            width: initialFrame.width + widthDelta,
            height: initialFrame.height + heightDelta
        )

        app.terminate()
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] =
            "\(requestedSize.width),\(requestedSize.height)"
        app.launch()

        let resizedWindow = app.windows.firstMatch
        let resizeAccepted = resizedWindow.waitForExistence(timeout: 10)
            && waitForFrame(of: resizedWindow, timeout: 5) {
                abs($0.width - requestedSize.width) <= 3
                    && abs($0.height - requestedSize.height) <= 3
            }
        let resizedFrame = resizedWindow.frame

        app.launchEnvironment.removeValue(forKey: "VEETBOT_UI_TEST_MAIN_WINDOW_FRAME")
        app.terminate()
        app.launch()

        let relaunchedWindow = app.windows.firstMatch
        let relaunched = relaunchedWindow.waitForExistence(timeout: 10)
        let restoredFrame = relaunchedWindow.frame

        app.terminate()
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] =
            "\(initialFrame.width),\(initialFrame.height)"
        app.launch()
        let cleanupWindow = app.windows.firstMatch
        _ = cleanupWindow.waitForExistence(timeout: 10)
        _ = waitForFrame(of: cleanupWindow, timeout: 5) {
            abs($0.width - initialFrame.width) <= 3
                && abs($0.height - initialFrame.height) <= 3
        }
        app.launchEnvironment.removeValue(forKey: "VEETBOT_UI_TEST_MAIN_WINDOW_FRAME")

        XCTAssertTrue(
            resizeAccepted,
            "the real SwiftUI window did not accept the test resize"
        )
        XCTAssertTrue(relaunched, "the application did not expose its window after relaunch")
        XCTAssertEqual(restoredFrame.width, resizedFrame.width, accuracy: 3)
        XCTAssertEqual(restoredFrame.height, resizedFrame.height, accuracy: 3)
    }

    private func waitForFrame(
        of window: XCUIElement,
        timeout: TimeInterval,
        matching predicate: (CGRect) -> Bool
    ) -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        repeat {
            if predicate(window.frame) { return true }
            RunLoop.current.run(until: Date().addingTimeInterval(0.05))
        } while Date() < deadline
        return predicate(window.frame)
    }
    #endif

    /// Waits until the element stops moving: a click computed from a frame
    /// that a scroll or an expanding disclosure is still animating lands
    /// beside the control.
    private func waitForSettledFrame(of element: XCUIElement, stableFor samples: Int = 1) {
        var frame = element.frame
        var stable = 0
        for _ in 0..<40 {
            Thread.sleep(forTimeInterval: 0.1)
            let next = element.frame
            if next == frame && next.width > 0 && next.height > 0 {
                stable += 1
                if stable >= samples { return }
            } else {
                stable = 0
                frame = next
            }
        }
    }

    #if os(iOS)
    private func openSidebarDestination(identifier: String) {
        app.activate()
        XCTAssertTrue(app.wait(for: .runningForeground, timeout: 10))
        let window = app.windows.firstMatch
        XCTAssertTrue(window.waitForExistence(timeout: 10))
        let moreButton = window.buttons["sidebar.more"]
        XCTAssertTrue(moreButton.waitForExistence(timeout: 10))
        // On iOS 27 the compact drawer's visible More button can have valid
        // geometry while XCTest's hittability query throws or stays false.
        // Touch its observed centre through the window, then require the
        // requested menu item to appear as proof that More actually opened.
        waitForSettledFrame(of: moreButton, stableFor: 3)
        let moreFrame = moreButton.frame
        let mainFrame = window.frame
        XCTAssertFalse(mainFrame.isEmpty || mainFrame.isInfinite || mainFrame.isNull)
        XCTAssertFalse(moreFrame.isEmpty || moreFrame.isInfinite || moreFrame.isNull)
        XCTAssertTrue(mainFrame.contains(moreFrame))
        window.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(
            dx: moreFrame.midX - mainFrame.minX, dy: moreFrame.midY - mainFrame.minY
        )).tap()

        let destination = window.buttons[identifier]
        XCTAssertTrue(destination.waitForExistence(timeout: 5))
        // Menu elements appear before their presentation becomes interactive.
        waitForSettledFrame(of: destination, stableFor: 3)
        let destinationReady = XCTNSPredicateExpectation(
            predicate: NSPredicate(format: "hittable == true"), object: destination)
        XCTAssertEqual(XCTWaiter.wait(for: [destinationReady], timeout: 10), .completed)
        XCTAssertTrue(destination.isHittable)
        // iOS 27.0 can synthesize a menu-element coordinate tap with a zero-size
        // parent window. Anchor the observed target point to the actual window;
        // require the menu to close before treating the navigation as complete.
        for _ in 0..<3 {
            waitForSettledFrame(of: destination, stableFor: 3)
            let target = destination.frame
            let windowFrame = window.frame
            XCTAssertTrue(windowFrame.contains(target))
            window.coordinate(withNormalizedOffset: .zero).withOffset(CGVector(
                dx: target.midX - windowFrame.minX, dy: target.midY - windowFrame.minY
            )).tap()
            if destination.waitForNonExistence(timeout: 2) { return }
        }
        XCTFail("The menu stayed open after three taps on \(identifier)")
    }

    /// Brings the chat list back over an open conversation. On a compact
    /// window the list slides over the conversation and slides away as a
    /// conversation opens, so a row that is still hittable may be mid-slide:
    /// the list is reopened from the navigation bar's toggle once the row
    /// has gone, and left alone if it stays. The pushed list of a window
    /// without the toggle comes back through the navigation bar's back button.
    private func revealSidebarIfNeeded(for row: XCUIElement) {
        let toggle = app.buttons["sidebar.toggle"]
        if toggle.exists {
            // A slow runner can drop the hidden list's rows from the tree; a
            // missing row never turns unhittable, so it counts as hidden too.
            // Settling a missing row takes XCTest several existence retries,
            // which outlast a 1.5 s wait on CircleCI's simulators.
            let gone = XCTNSPredicateExpectation(
                predicate: NSPredicate(format: "exists == false OR hittable == false"), object: row
            )
            guard XCTWaiter().wait(for: [gone], timeout: 5) == .completed else { return }
            XCTAssertTrue(toggle.waitForExistence(timeout: 5))
            toggle.tap()
            return
        }
        guard !row.isHittable else { return }
        let backButton = app.navigationBars.buttons.element(boundBy: 0)
        XCTAssertTrue(backButton.waitForExistence(timeout: 5))
        backButton.tap()
    }

    private func scrollUntilVisible(_ element: XCUIElement) {
        let scrollView = app.scrollViews.firstMatch
        XCTAssertTrue(scrollView.waitForExistence(timeout: 5))
        for _ in 0..<6 where !element.exists || !element.isHittable {
            scrollView.swipeUp()
        }
    }

    private func scrollEmailUntilVisible(_ element: XCUIElement) {
        let scrollView = app.scrollViews["email.detail"]
        XCTAssertTrue(scrollView.waitForExistence(timeout: 5))
        for _ in 0..<8 {
            let bottom = app.keyboards.firstMatch.exists ? app.keyboards.firstMatch.frame.minY - 12 : app.frame.maxY - 50
            if element.exists && element.isHittable && element.frame.maxY < bottom { return }
            // Inside the content's right margin: a drag that starts in the
            // 30-point scroll-indicator strip (dx 0.98) can scrub the
            // indicator instead and leave the view where it was.
            scrollView.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.7))
                .press(forDuration: 0.05, thenDragTo: scrollView.coordinate(withNormalizedOffset: CGVector(dx: 0.9, dy: 0.3)))
        }
    }
    #endif
}
