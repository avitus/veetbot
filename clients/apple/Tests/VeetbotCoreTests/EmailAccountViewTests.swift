import Foundation
import Testing
@testable import VeetbotCore

struct EmailAccountViewTests {
    /// An account with no recorded error remains pending rather than appearing failed or ready.
    @Test func testNeverSynchronizedAccountWaitsWithoutReportingFailure() throws {
        let account = try decode(status: "unavailable", error: nil)
        #expect(account.status == "unavailable")
        #expect(account.lastSyncedAt == nil)
        #expect(!account.hasRefreshFailure)
        #expect(account.updateMessage == "Waiting for an email update.")
    }

    /// A failed first attempt remains a failure even without a prior successful synchronization.
    @Test func testFailedFirstRefreshIsNotMistakenForAnUnstartedAccount() throws {
        let account = try decode(status: "unavailable", error: "Mailbox refresh is incomplete.")
        #expect(account.error == "Mailbox refresh is incomplete.")
        #expect(account.lastSyncedAt == nil)
        #expect(account.hasRefreshFailure)
        #expect(account.updateMessage == "Account could not be updated. Try refreshing again.")
    }

    /// Recovery keeps the incomplete-state disclosure until the server confirms readiness.
    @Test func testRecoveryRemainsIncompleteUntilServerReportsReady() throws {
        let failed = try decode(status: "unavailable", error: "Mailbox refresh is incomplete.")
        #expect(failed.hasRefreshFailure)
        let recovering = try decode(status: "syncing", error: nil)
        #expect(recovering.status == "syncing")
        #expect(!recovering.hasRefreshFailure)
        #expect(recovering.updateMessage == "Updating — results may be incomplete.")
        let completed = try decode(status: "ready", error: nil)
        #expect(!completed.hasRefreshFailure)
        #expect(completed.updateMessage == nil)
    }

    /// An unfinished catch-up names its floor and how far back it has reached; new mail still comes first.
    @Test func testCatchUpProgressNamesFloorAndReachedDate() throws {
        let account = try decode(status: "syncing", error: nil, coverage: [
            "inbox_complete": false,
            "catch_up_since": "2026-06-16T05:00:00Z",
            "inbox_reached_at": "2026-07-14T09:30:00Z",
        ])
        #expect(account.inboxComplete == false)
        #expect(account.coverageMessage(locale: Locale(identifier: "en_US"), timeZone: .gmt)
            == "Checking inbox mail back to Jun 16 — reached Jul 14. New mail still arrives first.")
        let starting = try decode(status: "syncing", error: nil, coverage: [
            "inbox_complete": false, "catch_up_since": "2026-06-16T05:00:00Z",
        ])
        #expect(starting.coverageMessage(locale: Locale(identifier: "en_US"), timeZone: .gmt)
            == "Checking inbox mail back to Jun 16. New mail still arrives first.")
    }

    /// A finished catch-up names the covered floor; an older server without coverage fields shows nothing.
    @Test func testCompletedCatchUpAndOlderServerCoverage() throws {
        let complete = try decode(status: "ready", error: nil, coverage: [
            "inbox_complete": true, "catch_up_since": "2026-06-16T05:00:00Z",
        ])
        #expect(complete.coverageMessage(locale: Locale(identifier: "en_US"), timeZone: .gmt)
            == "Inbox mail back to Jun 16 has been checked.")
        let older = try decode(status: "ready", error: nil)
        #expect(older.inboxComplete == nil && older.catchUpSince == nil && older.inboxReachedAt == nil)
        #expect(older.coverageMessage() == nil)
    }

    /// Decodes the wire representation, including backwards-compatible omission of optional errors.
    private func decode(
        status: String, error: String?, coverage: [String: Any] = [:]
    ) throws -> EmailAccountView {
        var value: [String: Any] = [
            "id": "personal", "label": "Personal", "status": status,
            "history_complete": false, "history_processed": 0,
        ]
        if let error { value["error"] = error }
        value.merge(coverage) { _, new in new }
        return try JSONDecoder.server.decode(EmailAccountView.self, from: JSONSerialization.data(withJSONObject: value))
    }
}
