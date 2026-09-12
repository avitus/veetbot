import Foundation
import Testing

@testable import VeetbotCore

/// Archive capability and durable outcomes must survive the native wire boundary without guessing Gmail state.
@Suite struct EmailArchiveModelTests {
    /// One account's advertised archive support never supplies support or a write binding to another account.
    @Test func testArchiveCapabilityRemainsPerAccountAndSurvivesRoundTrip() throws {
        let payload: [String: Any] = [
            "items": [
                accountPayload(id: "legacy"),
                accountPayload(id: "personal").merging(["archive_supported": false]) { _, new in new },
                accountPayload(id: "work").merging([
                    "archive_supported": true,
                    "write_server_id": "gmail_work_write"
                ]) { _, new in new }
            ],
            "next_cursor": NSNull()
        ]
        let page = try decode(Page<EmailAccountView>.self, payload: payload)
        let accounts = try page.items.map(encodedObject)

        #expect(accounts.count == 3)
        #expect(page.items[0].archiveSupported == nil)
        #expect(page.items[1].archiveSupported == false)
        #expect(page.items[2].archiveSupported == true)
        #expect(page.items[2].writeServerID == "gmail_work_write")
        #expect(accounts[0]["archive_supported"] as? Bool == nil)
        #expect(accounts[0]["write_server_id"] as? String == nil)
        #expect(accounts[1]["archive_supported"] as? Bool == false)
        #expect(accounts[1]["write_server_id"] as? String == nil)
        #expect(accounts[2]["archive_supported"] as? Bool == true)
        #expect(accounts[2]["write_server_id"] as? String == "gmail_work_write")
    }

    /// Legacy handled state is preserved without inventing Inbox membership or a completed Gmail operation.
    @Test func testLegacyThreadDoesNotAcquireArchiveState() throws {
        let thread = try decode(EmailThreadView.self, payload: threadPayload())
        let encoded = try encodedObject(thread)

        #expect(thread.isHandled)
        #expect(thread.inInbox == nil)
        #expect(!thread.isArchived)
        #expect(thread.archiveOperation == nil)
        #expect(encoded["in_inbox"] as? Bool == nil)
        #expect(encoded["archive_operation"] as? [String: Any] == nil)
    }

    /// Confirmed Inbox membership is independent of the older local dismissal revision.
    @Test(arguments: [true, false])
    func testInboxStateSurvivesRoundTrip(inInbox: Bool) throws {
        var payload = threadPayload()
        payload["in_inbox"] = inInbox
        let thread = try decode(EmailThreadView.self, payload: payload)
        let encoded = try encodedObject(thread)

        #expect(thread.isHandled)
        #expect(thread.isArchived == !inInbox)
        #expect(encoded["in_inbox"] as? Bool == inInbox)
        #expect(encoded["dismissed_revision"] as? Int == 3)
    }

    /// Pending, terminal, uncertain, and future outcomes retain their exact operation and target across devices.
    @Test(arguments: ["pending", "completed", "failed", "uncertain", "future_state"], [true, false])
    func testArchiveOperationSurvivesRoundTrip(status: String, targetArchived: Bool) throws {
        let operationID = "00000000-0000-0000-0000-000000000901"
        let runID = "00000000-0000-0000-0000-000000000902"
        let message = ["failed", "uncertain"].contains(status) ? "The account could not confirm the change." : nil
        var payload = threadPayload()
        payload["in_inbox"] = true
        payload["archive_operation"] = [
            "operation_id": operationID,
            "run_id": runID,
            "target_archived": targetArchived,
            "status": status,
            "error": message as Any? ?? NSNull()
        ]
        let thread = try decode(EmailThreadView.self, payload: payload)
        let encoded = try encodedObject(thread)
        let operation = try #require(encoded["archive_operation"] as? [String: Any])

        #expect(operation["operation_id"] as? String == operationID)
        #expect(operation["run_id"] as? String == runID)
        #expect(operation["target_archived"] as? Bool == targetArchived)
        #expect(operation["status"] as? String == status)
        #expect(operation["error"] as? String == message)
        #expect(!thread.isArchived, "Pending or completed operation metadata cannot replace observed Inbox membership.")
        #expect(encoded["in_inbox"] as? Bool == true, "An operation target cannot substitute for observed Gmail state.")
    }

    /// Supplies only the existing required account fields so omitted capability fields exercise old-server compatibility.
    private func accountPayload(id: String) -> [String: Any] {
        [
            "id": id,
            "label": id.capitalized,
            "status": "ready",
            "history_complete": false,
            "history_processed": 0
        ]
    }

    /// Builds a synthetic legacy thread that is locally handled without asserting any Gmail labels.
    private func threadPayload() -> [String: Any] {
        [
            "id": "00000000-0000-0000-0000-000000000801",
            "account_id": "work",
            "subject": "Project update",
            "senders": ["sender@example.test"],
            "updated_at": "2026-09-12T00:00:00Z",
            "revision": 3,
            "dismissed_revision": 3,
            "summary": "Review the project update.",
            "reason": "A direct request.",
            "needs_reply": true,
            "priority": 0.9,
            "complete": true
        ]
    }

    /// Decodes through the production date and coding-key configuration rather than constructing native DTOs directly.
    private func decode<Value: Decodable>(_ type: Value.Type, payload: [String: Any]) throws -> Value {
        try JSONDecoder.server.decode(type, from: JSONSerialization.data(withJSONObject: payload))
    }

    /// Re-encoding catches silently ignored server fields while keeping the red test independent of new Swift properties.
    private func encodedObject<Value: Encodable>(_ value: Value) throws -> [String: Any] {
        try #require(JSONSerialization.jsonObject(with: JSONEncoder.server.encode(value)) as? [String: Any])
    }
}
