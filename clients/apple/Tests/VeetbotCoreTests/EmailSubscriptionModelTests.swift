import Foundation
import Testing

@testable import VeetbotCore

/// The census must cross the wire without acquiring capability an older server
/// never advertised, and without ever modelling the address it unsubscribes at.
@Suite struct EmailSubscriptionModelTests {
    /// A decided or unverified sender carries no evidence, no mailto and no operation.
    @Test func testRowDecodesWithoutAnyOptionalServerField() throws {
        let row = try decode(EmailSubscriptionView.self, payload: rowPayload())
        let encoded = try encodedObject(row)

        #expect(row.id == Self.subscriptionID)
        #expect(row.accountID == "personal")
        #expect(row.displayName == "Example News")
        #expect(row.listID == "news.example.com")
        #expect(row.threadCount == 12)
        #expect(!row.threadCountOverflow)
        #expect(!row.verified)
        #expect(!row.linkOnly)
        #expect(row.destination.isEmpty)
        #expect(row.mailto == nil)
        #expect(row.evidenceDigest == nil)
        #expect(row.protectedReason == nil)
        #expect(row.requestedAt == nil)
        #expect(row.operation == nil)
        #expect(encoded["mailto"] as? [String: Any] == nil)
        #expect(encoded["evidence_digest"] as? String == nil)
        #expect(encoded["operation"] as? [String: Any] == nil)
    }

    /// An account and a thread from a server without Milestone 30 advertise nothing.
    @Test func testOlderServerOmitsSupportAndTheThreadBlock() throws {
        let account = try decode(EmailAccountView.self, payload: [
            "id": "personal", "label": "Personal", "status": "ready",
            "history_complete": false, "history_processed": 0
        ])
        let thread = try decode(EmailThreadView.self, payload: threadPayload())

        let encodedAccount = try encodedObject(account)
        let encodedThread = try encodedObject(thread)

        #expect(account.unsubscribeSupported == nil)
        #expect(thread.subscription == nil)
        #expect(encodedAccount["unsubscribe_supported"] as? Bool == nil)
        #expect(encodedThread["subscription"] as? [String: Any] == nil)
    }

    /// Advertised support and a thread's bulk block survive the decoder unchanged.
    @Test func testSupportAndTheThreadBlockSurviveRoundTrip() throws {
        var accountPayload: [String: Any] = [
            "id": "work", "label": "Work", "status": "ready",
            "history_complete": false, "history_processed": 0
        ]
        accountPayload["unsubscribe_supported"] = true
        var payload = threadPayload()
        payload["subscription"] = [
            "id": Self.subscriptionID, "state": "active", "mechanism": "one_click",
            "destination": "news.example.com", "evidence_digest": Self.digest, "revision": 4
        ]
        let account = try decode(EmailAccountView.self, payload: accountPayload)
        let thread = try decode(EmailThreadView.self, payload: payload)
        let block = try #require(thread.subscription)

        #expect(account.unsubscribeSupported == true)
        #expect(block.id == Self.subscriptionID)
        #expect(block.destination == "news.example.com")
        #expect(block.evidenceDigest == Self.digest)
        #expect(block.revision == 4)
        #expect(block.canUnsubscribe)
    }

    /// The durable operation keeps its outcome code and the state it will restore.
    @Test(arguments: ["pending", "completed", "failed", "uncertain"])
    func testOperationSurvivesRoundTrip(status: String) throws {
        var payload = rowPayload()
        payload["operation"] = [
            "operation_id": "00000000-0000-0000-0000-000000000a01",
            "run_id": "00000000-0000-0000-0000-000000000a02",
            "action": "unsubscribe", "status": status,
            "code": status == "failed" ? "unsubscribe.rejected" : NSNull(),
            "requested_at": "2026-09-19T00:00:00Z", "prior_state": "active"
        ]
        let row = try decode(EmailSubscriptionView.self, payload: payload)
        let operation = try #require(row.operation)
        let round = try encodedObject(row)
        let encoded = try #require(round["operation"] as? [String: Any])

        #expect(operation.action == "unsubscribe")
        #expect(operation.status == status)
        #expect(operation.priorState == "active")
        #expect(operation.code == (status == "failed" ? "unsubscribe.rejected" : nil))
        #expect(encoded["operation_id"] as? String == "00000000-0000-0000-0000-000000000A01")
    }

    /// The confirmation must name the host it will call, or the mailbox it will write to.
    @Test func testMechanismWordingNamesTheHostAndTheRecipient() throws {
        let oneClick = try row(mechanism: "one_click", verified: true, destination: "news.example.com")
        let mailto = try row(mechanism: "mailto", verified: true, destination: "unsub@lists.example.org",
                             mailto: ["to": "unsub@lists.example.org", "subject": "unsubscribe", "body": ""])
        let refused = try row(mechanism: "none", verified: true)
        let link = try row(mechanism: "none", verified: true, linkOnly: true)
        let checking = try row(mechanism: "none", verified: false)

        #expect(oneClick.mechanismSentence == "One-click request to news.example.com")
        #expect(mailto.mechanismSentence == "Sends an email to unsub@lists.example.org")
        #expect(refused.mechanismSentence == "No automated unsubscribe")
        #expect(link.mechanismSentence == "Unsubscribe link only")
        #expect(checking.mechanismSentence == "Checking…")
        #expect(checking.isVerifying)
        #expect(!refused.isVerifying)
    }

    /// Only verified evidence in an actionable state may be unsubscribed from.
    @Test(arguments: ["active", "failed", "still_sending", "kept", "pending", "unsubscribed", "reported_spam"])
    func testEligibilityRequiresVerifiedEvidenceAndAnActionableState(state: String) throws {
        let eligible = try row(mechanism: "one_click", verified: true,
                               destination: "news.example.com", digest: Self.digest, state: state)
        let unverified = try row(mechanism: "none", verified: false, state: state)
        let withoutDigest = try row(mechanism: "one_click", verified: true,
                                    destination: "news.example.com", state: state)

        #expect(eligible.canUnsubscribe == ["active", "failed", "still_sending"].contains(state))
        #expect(!unverified.canUnsubscribe)
        #expect(!withoutDigest.canUnsubscribe)
    }

    /// The row states what happened without claiming an unsubscribe succeeded.
    @Test(arguments: [
        ("active", "Active"), ("kept", "Kept"), ("pending", "Unsubscribing…"),
        ("unsubscribed", "Unsubscribed"), ("failed", "Unsubscribe failed"),
        ("still_sending", "Still sending after unsubscribing"),
        ("reported_spam", "Reported as spam"), ("future_state", "future_state")
    ])
    func testStateWordingDistinguishesEachDurableOutcome(state: String, described: String) throws {
        let value = try row(state: state)

        #expect(value.stateDescription == described)
    }

    /// Volume is what the window observed, and an overflowing count says so.
    @Test func testVolumeReportsTheCountedThreadsAndItsOverflow() throws {
        let single = try row(threads: 1)
        let several = try row(threads: 12)
        let overflowing = try row(threads: 200, overflow: true)

        #expect(single.volumeDescription == "1 conversation")
        #expect(several.volumeDescription == "12 conversations")
        #expect(overflowing.volumeDescription == "More than 200 conversations")
    }

    /// The address and its recipient token exist only inside the server's TLS tunnel.
    @Test func testNoClientTypeModelsAnUnsubscribeAddress() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let sources = try FileManager.default
            .subpathsOfDirectory(atPath: root.appendingPathComponent("Veetbot").path)
            .filter { $0.hasSuffix(".swift") }
        #expect(!sources.isEmpty)
        for path in sources {
            let source = try String(
                contentsOf: root.appendingPathComponent("Veetbot").appendingPathComponent(path),
                encoding: .utf8)
            #expect(!source.contains("https_uri"), "\(path) must never model the unsubscribe address")
            #expect(!source.contains("unsubscribe_url"), "\(path) must never model the unsubscribe address")
        }
        let model = try String(
            contentsOf: root.appendingPathComponent("Veetbot/Models/EmailSubscriptionModels.swift"),
            encoding: .utf8)
        #expect(!model.contains(": URL"), "No subscription field may be typed as a URL")
    }

    private static let subscriptionID = String(repeating: "a1", count: 32)
    private static let digest = String(repeating: "b2", count: 32)

    /// Builds one row through the production decoder rather than a native initializer.
    private func row(
        mechanism: String = "none", verified: Bool = false, destination: String = "",
        mailto: [String: Any]? = nil, digest: String? = nil, linkOnly: Bool = false,
        state: String = "active", threads: Int = 12, overflow: Bool = false
    ) throws -> EmailSubscriptionView {
        var payload = rowPayload()
        payload["mechanism"] = mechanism
        payload["verified"] = verified
        payload["destination"] = destination
        payload["link_only"] = linkOnly
        payload["state"] = state
        payload["thread_count"] = threads
        payload["thread_count_overflow"] = overflow
        if let mailto { payload["mailto"] = mailto }
        if let digest { payload["evidence_digest"] = digest }
        return try decode(EmailSubscriptionView.self, payload: payload)
    }

    /// Supplies only the fields the route always returns, so optional ones stay exercised.
    private func rowPayload() -> [String: Any] {
        [
            "id": Self.subscriptionID, "account_id": "personal", "display_name": "Example News",
            "address": "news@example.com", "list_id": "news.example.com", "thread_count": 12,
            "thread_count_overflow": false, "first_seen_at": "2026-07-01T00:00:00Z",
            "last_received_at": "2026-09-18T00:00:00Z", "mechanism": "none", "verified": false,
            "link_only": false, "destination": "", "mailto": NSNull(), "evidence_digest": NSNull(),
            "state": "active", "protected": false, "protected_reason": NSNull(),
            "requested_at": NSNull(), "operation": NSNull(), "revision": 3
        ]
    }

    private func threadPayload() -> [String: Any] {
        [
            "id": "00000000-0000-0000-0000-000000000801", "account_id": "personal",
            "subject": "Weekly digest", "senders": ["news@example.com"],
            "updated_at": "2026-09-18T00:00:00Z", "revision": 3, "summary": "A weekly digest.",
            "reason": "A newsletter you read.", "needs_reply": false, "priority": 0.2, "complete": true
        ]
    }

    private func decode<Value: Decodable>(_ type: Value.Type, payload: [String: Any]) throws -> Value {
        try JSONDecoder.server.decode(type, from: JSONSerialization.data(withJSONObject: payload))
    }

    private func encodedObject<Value: Encodable>(_ value: Value) throws -> [String: Any] {
        try #require(JSONSerialization.jsonObject(with: JSONEncoder.server.encode(value)) as? [String: Any])
    }
}
