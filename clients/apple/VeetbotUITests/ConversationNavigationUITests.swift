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

    func testMemoryBrowserListsAndOpensDetail() {
        let memoryButton = app.buttons["sidebar.memory"]
        XCTAssertTrue(memoryButton.waitForExistence(timeout: 10))
        memoryButton.tap()

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

    func testWebsiteAccessCreatesARecoverableBrowserHandoff() {
        let settingsButton = app.buttons["Settings"]
        XCTAssertTrue(settingsButton.waitForExistence(timeout: 10))
        settingsButton.tap()
        XCTAssertTrue(app.staticTexts["Settings"].waitForExistence(timeout: 5))

        let websiteAccess = app.staticTexts["Website Access"]
        scrollUntilVisible(websiteAccess)
        XCTAssertTrue(websiteAccess.exists)

        let origin = app.textFields["website-access.origin"]
        let login = app.textFields["website-access.login-url"]
        scrollUntilVisible(origin)
        XCTAssertTrue(origin.exists)
        origin.tap()
        origin.typeText("https://example.org")
        scrollUntilVisible(login)
        XCTAssertTrue(login.exists)
        login.tap()
        login.typeText("https://example.org/login")
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
}
