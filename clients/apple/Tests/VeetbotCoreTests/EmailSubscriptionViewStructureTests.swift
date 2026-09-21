import Foundation
import Testing

@testable import VeetbotCore

/// The Subscriptions surface exists only where the server advertises it, and
/// each surface presents exactly the confirmation it opened.
@Suite struct EmailSubscriptionViewStructureTests {
    /// No advertised support means no entry at all, on either platform's toolbar.
    @Test func testEmailModeOffersSubscriptionsOnlyWhereTheServerAdvertisesIt() throws {
        let mode = try source(at: "Veetbot/Views/EmailModeView.swift")
        let root = try source(at: "Veetbot/Views/RootView.swift")

        #expect(mode.contains("if model.unsubscribeAvailable {"))
        #expect(mode.contains(".accessibilityIdentifier(\"email.subscriptions.open\")"))
        #expect(mode.contains("EmailSubscriptionsScreen(model: model.subscriptions, accounts: model.accounts)"))
        #expect(root.contains("if coordinator.email.unsubscribeAvailable {"))
        #expect(root.contains(
            "EmailSubscriptionsScreen(model: coordinator.email.subscriptions, accounts: coordinator.email.accounts)"))
    }

    /// A bulk conversation's own account must advertise the action that sits beside its sender.
    @Test func testTheThreadActionSitsBesideTheSenderOnAnEligibleBulkThread() throws {
        let mode = try source(at: "Veetbot/Views/EmailModeView.swift")

        #expect(mode.contains("if let block = thread.subscription, block.canUnsubscribe,"))
        #expect(mode.contains("model.unsubscribeSupported(for: thread.accountID)"))
        #expect(mode.contains("subscriptions.beginUnsubscribe(thread: thread)"))
        #expect(mode.contains(".accessibilityIdentifier(\"email.unsubscribe.thread\")"))
    }

    /// One list that pushes its detail on iPhone and shows it alongside elsewhere.
    @Test func testTheCensusPushesOnIPhoneAndSitsBesideItsDetailElsewhere() throws {
        let view = try source(at: "Veetbot/Views/EmailSubscriptionsView.swift")

        #expect(view.contains("NavigationView {"))
        #expect(view.contains("NavigationLink {"))
        #expect(view.contains("EmailSubscriptionDetailView(model: model, row: row)"))
        #expect(view.contains(".navigationTitle(\"Subscriptions\")"))
        #expect(view.contains(#".accessibilityIdentifier("email.subscription.row.\(row.id)")"#))
        #expect(view.contains("model.actions(for: current)"))
        #expect(view.contains(".task(id: model.filterKey) { await model.reload() }"))
    }

    /// The confirmation names each mechanism in plain words before anything is sent.
    @Test func testTheConfirmationNamesEachMechanismTheWarningAndTheArchiveOption() throws {
        let view = try source(at: "Veetbot/Views/EmailSubscriptionsView.swift")

        #expect(view.contains("Text(target.sentence)"))
        #expect(view.contains("Text(EmailUnsubscribeConfirmation.warning)"))
        #expect(view.contains(
            "Toggle(\"Also archive existing mail from these senders\", isOn: $model.archiveExisting)"))
        #expect(view.contains("await model.confirmUnsubscribe()"))
        #expect(view.contains(".accessibilityIdentifier(\"email.unsubscribe.confirm\")"))
    }

    /// Two surfaces share one confirmation state, so each presents only its own.
    @Test func testEachSurfacePresentsOnlyTheConfirmationItOpened() throws {
        let view = try source(at: "Veetbot/Views/EmailSubscriptionsView.swift")
        let mode = try source(at: "Veetbot/Views/EmailModeView.swift")

        #expect(view.contains("model.confirmation?.source == .list"))
        #expect(mode.contains("subscriptions.confirmation?.source == .thread"))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
