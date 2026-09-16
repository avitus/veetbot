import XCTest

final class ConversationNavigationUITests: XCTestCase {
    private var app: XCUIApplication!

    override func setUp() {
        super.setUp()
        continueAfterFailure = false
        app = XCUIApplication()
        app.launchArguments.append("--ui-testing-conversation-navigation")
        app.launch()
    }

    override func tearDown() {
        app?.terminate()
        super.tearDown()
    }

    /// Twenty mixed calls occupy one compact row; each original result remains expandable.
    func testMixedToolSummaryKeepsAnswerVisibleAndExpandsDetails() {
        app.terminate()
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
        XCTAssertTrue(summary.waitForExistence(timeout: 10))
        XCTAssertTrue(summary.label.contains("20 tool calls"))
        XCTAssertTrue(summary.label.contains("6 Failed"))
        XCTAssertEqual(summary.value as? String, "Collapsed")
        XCTAssertLessThan(summary.frame.height, 80)
        let answer = app.staticTexts["Your answer is visible below the tool summary."]
        XCTAssertTrue(answer.waitForExistence(timeout: 5))
        XCTAssertTrue(answer.isHittable)
        #if os(macOS)
        summary.click()
        #else
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
        summary.click()
        #else
        summary.tap()
        #endif
        XCTAssertEqual(summary.value as? String, "Collapsed")
        XCTAssertFalse(app.staticTexts["Example result 1"].exists)
        XCTAssertTrue(answer.isHittable)
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
        #if os(macOS)
        app.radioButtons["Other mail"].click()
        #else
        app.buttons["Other mail"].tap()
        #endif
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        #if os(macOS)
        row.click()
        #else
        row.tap()
        #endif
        let handled = NSPredicate(format: "label == %@ AND enabled == true", "Move to Inbox")
        expectation(for: handled, evaluatedWith: action)
        waitForExpectations(timeout: 5)
        #if os(macOS)
        action.click()
        #else
        action.tap()
        #endif
        let unhandled = NSPredicate(format: "label == %@ AND enabled == true", "Archive in Gmail")
        expectation(for: unhandled, evaluatedWith: action)
        waitForExpectations(timeout: 5)
    }

    /// The detail checkbox replaces the archived message with the next conversation's actual content.
    func testEmailDetailArchiveDisplaysNextConversation() {
        app.terminate()
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

    /// Removes a row immediately without progress messages, then finds its confirmed state in Other mail.
    func testEmailCanBeCheckedOffFromInboxWithoutOpeningThread() {
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
        #if os(macOS)
        let other = app.radioButtons["Other mail"]
        XCTAssertTrue(other.waitForExistence(timeout: 5))
        other.click()
        #else
        let other = app.buttons["Other mail"]
        other.tap()
        #endif
        XCTAssertTrue(check.waitForExistence(timeout: 5))
        XCTAssertEqual(check.label, "Move to Inbox")
    }

    /// A failed Gmail operation restores the optimistically removed row with an explicit retryable outcome.
    func testEmailArchiveFailurePreservesInboxRow() {
        app.terminate()
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
        waitForExpectations(timeout: 5)
        XCTAssertTrue(action.exists)
        XCTAssertTrue(action.isEnabled)
        XCTAssertEqual(action.label, "Archive in Gmail")
    }

    /// Verifies the reading and reply flow with normal platform appearance.
    func testEmailReadingKeepsFeedbackOptionalAndReplyReachable() {
        checkEmailReadingFlow()
    }

    /// Exercises the same native controls with a deterministic dark appearance.
    func testEmailReadingInDarkAppearance() {
        app.terminate()
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
    private static let proposalID = "00000000-0000-0000-0000-000000000E01"

    private func launchWithFolders() {
        app.terminate()
        app.launchArguments.append("--ui-testing-folders")
        app.launch()
    }

    private func element(_ identifier: String) -> XCUIElement {
        app.descendants(matching: .any)[identifier]
    }

    private func waitForDisappearance(of element: XCUIElement, timeout: TimeInterval = 5) {
        let gone = expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: element)
        wait(for: [gone], timeout: timeout)
    }

    private func typeIntoFolderNameField(_ text: String) {
        let field = element("folder.name")
        XCTAssertTrue(field.waitForExistence(timeout: 5))
        activate(field)
        #if os(macOS)
        field.typeKey("a", modifierFlags: .command)
        #endif
        field.typeText(text)
        activate(element("folder.save"))
    }

    /// A folder section groups its conversations under a collapsible header, and
    /// the older-server journey (every other test) shows no folder control at all.
    func testFolderSectionsGroupConversationsAndOlderServersStayFlat() {
        let historicalRow = element("sidebar.session.00000000-0000-0000-0000-000000000123")
        XCTAssertTrue(historicalRow.waitForExistence(timeout: 10))
        XCTAssertFalse(element("sidebar.new-folder").exists)
        XCTAssertFalse(element("sidebar.session.move.00000000-0000-0000-0000-000000000123").exists)

        launchWithFolders()
        XCTAssertTrue(element("sidebar.folder.\(Self.folderID)").waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["Travel"].exists)
        XCTAssertTrue(element("sidebar.session.00000000-0000-0000-0000-000000000456").waitForExistence(timeout: 5))
        XCTAssertTrue(element("sidebar.session.00000000-0000-0000-0000-000000000123").exists)
        XCTAssertTrue(element("sidebar.new-folder").exists)
        XCTAssertTrue(element("sidebar.proposal.\(Self.proposalID)").exists)
    }

    func testNewFolderSheetCreatesAFolder() {
        launchWithFolders()
        let newFolder = element("sidebar.new-folder")
        XCTAssertTrue(newFolder.waitForExistence(timeout: 10))
        activate(newFolder)
        typeIntoFolderNameField("Errands")
        XCTAssertTrue(element("sidebar.folder.\(Self.proposedFolderID)").waitForExistence(timeout: 5))
        XCTAssertTrue(app.staticTexts["Errands"].exists)
    }

    #if os(macOS)
    func testRenamingAFolderThroughItsMenuUpdatesTheSection() {
        launchWithFolders()
        let header = element("sidebar.folder.\(Self.folderID)")
        XCTAssertTrue(header.waitForExistence(timeout: 10))
        header.rightClick()
        let rename = app.menuItems["Rename…"]
        XCTAssertTrue(rename.waitForExistence(timeout: 5))
        rename.click()
        typeIntoFolderNameField("Trips")
        XCTAssertTrue(app.staticTexts["Trips"].waitForExistence(timeout: 5))
        XCTAssertFalse(app.staticTexts["Travel"].exists)
    }
    #endif

    func testAcceptingASuggestedFolderFilesTheConversation() {
        launchWithFolders()
        let proposal = element("sidebar.proposal.\(Self.proposalID)")
        XCTAssertTrue(proposal.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["New folder “Lisbon Trip”"].exists)
        activate(element("sidebar.proposal.accept.\(Self.proposalID)"))
        XCTAssertTrue(element("sidebar.folder.\(Self.proposedFolderID)").waitForExistence(timeout: 10))
        waitForDisappearance(of: proposal)
        XCTAssertTrue(element("sidebar.session.00000000-0000-0000-0000-000000000123").exists)
    }

    func testDecliningASuggestedFolderRemovesIt() {
        launchWithFolders()
        let proposal = element("sidebar.proposal.\(Self.proposalID)")
        XCTAssertTrue(proposal.waitForExistence(timeout: 10))
        activate(element("sidebar.proposal.decline.\(Self.proposalID)"))
        waitForDisappearance(of: proposal)
        XCTAssertFalse(element("sidebar.folder.\(Self.proposedFolderID)").exists)
        XCTAssertTrue(element("sidebar.session.00000000-0000-0000-0000-000000000123").exists)
    }

    private func activate(_ element: XCUIElement) {
        #if os(macOS)
        element.click()
        #else
        element.tap()
        #endif
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
        app.terminate()
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
        let historical = app.descendants(matching: .any)["sidebar.session.00000000-0000-0000-0000-000000000123"]
        XCTAssertTrue(historical.waitForExistence(timeout: 10))
        activate(historical)
        XCTAssertTrue(app.buttons["chat.people"].waitForExistence(timeout: 5))
        var unlocatedBaseline: [String: Int] = [:]
        #if os(macOS)
        if #available(macOS 14, *) {
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
        }
        #endif
        activate(app.buttons["chat.people"])
        let person = app.descendants(matching: .any)["people.row.00000000-0000-0000-0000-000000000777"]
        let browser = app.descendants(matching: .any)["people.browser"]
        XCTAssertTrue(browser.waitForExistence(timeout: 5))
        if #available(macOS 14, iOS 17, *) {
            try auditPeopleSurface(unlocatedBaseline: unlocatedBaseline)
            #if !os(macOS)
            for _ in 0..<10 where !person.exists || !person.isHittable { browser.swipeUp() }
            #endif
            XCTAssertTrue(person.waitForExistence(timeout: 5))
            activate(person)
            XCTAssertTrue(app.descendants(matching: .any)["people.detail"].waitForExistence(timeout: 5))
            try auditPeopleSurface(unlocatedBaseline: unlocatedBaseline)
        } else {
            XCTFail("People accessibility verification requires the platform accessibility auditor")
        }
    }

    @available(macOS 14, iOS 17, *)
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
            return false
            #endif
        }
    }

    func testPeopleEvidencePreservesTheOpenConversation() {
        #if os(macOS)
        app.terminate()
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        app.launch()
        #endif
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

    func testPeopleForgetExplainsSourceRetentionAndPendingCleanup() {
        #if os(macOS)
        app.terminate()
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        app.launch()
        #endif
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
        app.terminate()
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        app.launch()
        #endif
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
        app.terminate()
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1100,900"
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] = "1"
        app.launch()
        #endif
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
        for _ in 0..<6 where !repair.isHittable { detailScroll.scroll(byDeltaX: 0, deltaY: -250) }
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
        for _ in 0..<6 where !saved.isHittable { detailScroll.scroll(byDeltaX: 0, deltaY: -250) }
        #else
        scrollUntilVisible(saved)
        #endif
        XCTAssertTrue(saved.waitForExistence(timeout: 5))
        let undo = app.buttons["Preview undo"]
        #if os(macOS)
        for _ in 0..<6 where !undo.isHittable { detailScroll.scroll(byDeltaX: 0, deltaY: -250) }
        #else
        scrollUntilVisible(undo)
        #endif
        XCTAssertTrue(undo.waitForExistence(timeout: 5))
    }

    #if os(iOS)
    func testEmailCompactTraitNavigationReturnsToSelectedInbox() {
        app.terminate()
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

        let heading = app.staticTexts["chat.heading"]
        XCTAssertTrue(heading.waitForExistence(timeout: 5))
        XCTAssertEqual(heading.label, "New conversation")
        XCTAssertTrue(app.descendants(matching: .any)["chat.composer"].exists)
    }

    func testSwitchesBetweenHistoricalConversations() {
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

    private func submitSlowChatMessage(fails: Bool = false, useReturn: Bool = false, holdSubmission: Bool = false) {
        app.terminate()
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

    func testScheduleBrowserListsAndOpensPointReadDetail() {
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

    func testScheduleBrowserMakesRecentTerminalHistoryAccessible() {
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
        openSidebarDestination(identifier: "sidebar.persona")

        XCTAssertTrue(
            app.descendants(matching: .any)["persona.editor"].waitForExistence(timeout: 5)
        )
    }

    func testWebsiteAccessCreatesARecoverableBrowserHandoff() {
        openSidebarDestination(identifier: "sidebar.settings")
        XCTAssertTrue(app.staticTexts["Settings"].waitForExistence(timeout: 5))

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

        let create = app.buttons["Create secure login"]
        scrollUntilVisible(create)
        XCTAssertTrue(create.isEnabled)
        create.tap()

        let continueInBrowser = app.buttons["Continue in web browser"]
        XCTAssertTrue(continueInBrowser.waitForExistence(timeout: 10))
        XCTAssertTrue(app.buttons["Start over"].exists)

        let clientBuild = app.staticTexts["Client build"]
        scrollUntilVisible(clientBuild)
        XCTAssertTrue(clientBuild.exists)
        XCTAssertTrue(app.staticTexts["Version 0.1.1 (2)"].exists)
    }
    #endif

    #if os(macOS)
    /// Prevents implicit person feedback and stale values from crossing feedback scopes.
    func testEmailFeedbackRequiresAnExplicitPersonAndClearsChangedTargets() {
        activate(app.buttons["mode.email"])
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        activate(row)
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
        let topic = app.popUpButtons["email.feedback-topic"]
        XCTAssertFalse(important.isEnabled, "Content feedback requires an explicit topic")
        XCTAssertFalse(lessImportant.isEnabled)
        XCTAssertTrue(topic.waitForExistence(timeout: 5))
        XCTAssertEqual(topic.value as? String, "Choose a topic")
        topic.click()
        app.menuItems["Board planning"].click()
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
        XCTAssertEqual(topic.value as? String, "Choose a topic")
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

    /// Drags the actual divider and verifies both columns and the selected thread survive mode switching.
    func testEmailSidebarResizesWithItsDivider() {
        launchFullEmailInbox()
        let first = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(first.waitForExistence(timeout: 10))
        first.click()
        let search = app.textFields["email.search"]
        let originalWidth = search.frame.width
        let divider = app.splitters.firstMatch
        XCTAssertTrue(divider.waitForExistence(timeout: 5))
        let start = divider.coordinate(withNormalizedOffset: CGVector(dx: 0.5, dy: 0.5))
        start.press(forDuration: 0.1, thenDragTo: start.withOffset(CGVector(dx: 130, dy: 0)))
        XCTAssertTrue(waitForFrame(of: search, timeout: 5) { $0.width > originalWidth + 80 },
                      "Dragging the divider must widen the email list")
        let resizedWidth = search.frame.width
        XCTAssertTrue(app.buttons["email.jump-to-reply"].isHittable)
        app.buttons["mode.chat"].click()
        app.buttons["mode.email"].click()
        XCTAssertEqual(search.frame.width, resizedWidth, accuracy: 3)
        XCTAssertTrue(app.buttons["email.jump-to-reply"].isHittable)
        attachEmailScreenshot("Resized priority inbox")
    }

    /// Launch the five-priority email fixture in a deterministic Mac window.
    private func launchFullEmailInbox() {
        app.terminate()
        app.launchArguments.append("--ui-testing-email-full-inbox")
        app.launchEnvironment["VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"] = "1200,900"
        app.launch()
        XCTAssertTrue(app.buttons["mode.email"].waitForExistence(timeout: 10))
        app.buttons["mode.email"].click()
    }

    /// Reproduces the default CI window and keeps mode, reply and Chat controls inside its bounds.
    func testEmailReadingAtDefaultMacWindowSize() {
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
        let emailMode = app.buttons["mode.email"]
        XCTAssertTrue(emailMode.waitForExistence(timeout: 10))
        emailMode.click()
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        row.click()
        XCTAssertTrue(app.staticTexts["Please review the agenda before Friday."].waitForExistence(timeout: 5), app.debugDescription)
        checkHandledActionInDetail()
        let review = app.buttons["email.review-send"]
        for _ in 0..<8 where !review.isHittable {
            app.scrollViews["email.detail"].scroll(byDeltaX: 0, deltaY: -350)
        }
        XCTAssertTrue(review.isHittable)
        review.click()
        let send = app.buttons["email.approve-send"]
        XCTAssertTrue(send.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts["To: alex@example.test"].exists)
        send.click()
        XCTAssertTrue(app.staticTexts["Sent"].waitForExistence(timeout: 10))
        app.buttons["mode.chat"].click()
        XCTAssertTrue(app.descendants(matching: .any)["sidebar.new-conversation"].waitForExistence(timeout: 5))
    }

    func testMainWindowSizePersistsAcrossApplicationRestart() {
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

    #if os(iOS)
    private func openSidebarDestination(identifier: String) {
        let moreButton = app.buttons["sidebar.more"]
        XCTAssertTrue(moreButton.waitForExistence(timeout: 10))
        XCTAssertTrue(moreButton.isHittable)
        moreButton.tap()

        let destination = app.buttons[identifier]
        XCTAssertTrue(destination.waitForExistence(timeout: 5))
        XCTAssertTrue(destination.isHittable)
        destination.tap()
    }

    private func revealSidebarIfNeeded(for row: XCUIElement) {
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
            scrollView.coordinate(withNormalizedOffset: CGVector(dx: 0.98, dy: 0.7))
                .press(forDuration: 0.05, thenDragTo: scrollView.coordinate(withNormalizedOffset: CGVector(dx: 0.98, dy: 0.3)))
        }
    }
    #endif
}
