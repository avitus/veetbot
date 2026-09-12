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

    /// Decodes the wire representation, including backwards-compatible omission of optional errors.
    private func decode(status: String, error: String?) throws -> EmailAccountView {
        var value: [String: Any] = [
            "id": "personal", "label": "Personal", "status": status,
            "history_complete": false, "history_processed": 0,
        ]
        if let error { value["error"] = error }
        return try JSONDecoder.server.decode(EmailAccountView.self, from: JSONSerialization.data(withJSONObject: value))
    }
}
