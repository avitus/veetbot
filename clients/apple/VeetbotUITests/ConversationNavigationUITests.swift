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

    /// Verifies both confirmed attention transitions without leaving the open thread.
    private func checkHandledActionInDetail() {
        let action = app.buttons["email.handled.detail"]
        XCTAssertTrue(action.waitForExistence(timeout: 5))
        XCTAssertEqual(action.label, "Mark handled")
        #if os(macOS)
        action.click()
        #else
        action.tap()
        #endif
        let handled = NSPredicate(format: "label == %@ AND enabled == true", "Mark unhandled")
        expectation(for: handled, evaluatedWith: action)
        waitForExpectations(timeout: 5)
        #if os(macOS)
        action.click()
        #else
        action.tap()
        #endif
        let unhandled = NSPredicate(format: "label == %@ AND enabled == true", "Mark handled")
        expectation(for: unhandled, evaluatedWith: action)
        waitForExpectations(timeout: 5)
    }

    /// Checks off a row directly, then finds its reversible handled state in Other mail.
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
        expectation(for: NSPredicate(format: "exists == false"), evaluatedWith: check)
        waitForExpectations(timeout: 5)
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
        XCTAssertEqual(check.label, "Mark unhandled")
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
        activate(emailMode)
        let row = app.buttons["email.thread.00000000-0000-0000-0000-000000000801"]
        XCTAssertTrue(row.waitForExistence(timeout: 10))
        XCTAssertTrue(app.textFields["email.search"].exists)
        XCTAssertFalse(app.buttons["sidebar.settings"].isHittable, "The hidden Chat toolbar must not leak into Email")
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
        XCTAssertTrue(app.buttons["sidebar.settings"].isHittable)
        #endif
    }

    /// Activates a control using the platform's native input action.
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

    func testSendingMessageDismissesKeyboard() {
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

        let send = app.buttons["Send"]
        XCTAssertTrue(send.isHittable)
        send.tap()

        let keyboardDismissed = NSPredicate(format: "exists == false")
        let expectation = XCTNSPredicateExpectation(
            predicate: keyboardDismissed,
            object: app.keyboards.firstMatch
        )
        XCTAssertEqual(
            XCTWaiter.wait(for: [expectation], timeout: 5),
            .completed,
            "the keyboard remained visible after the message was sent"
        )
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
        let topic = app.textFields["Content topic"]
        XCTAssertEqual(topic.value as? String, "")
        topic.click()
        topic.typeText("Board planning")
        target.click()
        app.menuItems["This person"].click()
        XCTAssertEqual(app.popUpButtons["email.feedback-person"].value as? String, "Choose a person")
        XCTAssertFalse(important.isEnabled)
        XCTAssertFalse(lessImportant.isEnabled)
        target.click()
        app.menuItems["This thread"].click()
        XCTAssertTrue(important.isEnabled)
        XCTAssertTrue(lessImportant.isEnabled)
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
