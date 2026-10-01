import Foundation
import Testing
@testable import VeetbotCore

struct EmailReadingTests {
    /// Reader content is additive; older servers and retained originals stay readable.
    @Test func testReadingProjectionPreservesOriginalAndSupportsOlderServers() throws {
        let original = "Article text.\nUnsubscribe\n<https://example.com/leave>"
        var payload: [String: Any] = [
            "id": "message", "sender": "writer@example.com", "to": ["owner@example.com"],
            "cc": [], "subject": "Article", "body": original,
            "sent_at": "2026-09-15T10:00:00Z", "complete": true,
        ]
        let older = try JSONDecoder.server.decode(
            EmailMessageView.self, from: JSONSerialization.data(withJSONObject: payload))
        #expect(older.readerBody == nil)
        #expect(older.body == original)
        payload["reader_body"] = "## Article\n\nRead the [report](https://example.com/report)."
        let current = try JSONDecoder.server.decode(
            EmailMessageView.self, from: JSONSerialization.data(withJSONObject: payload))
        #expect(current.body == original)
        #expect(current.readerBody != nil)
        let blocks = MarkdownContentParser.parse(try #require(current.readerBody))
        #expect(blocks.count == 2)
        #expect(blocks.first == .heading(level: 2, text: "Article"))
        // Match the native inline Text renderer, rather than clipboard export,
        // which deliberately appends URLs to its plain-text representation.
        let inline = try AttributedString(markdown: "Read the [report](https://example.com/report).",
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))
        #expect(String(inline.characters) == "Read the report.")
        #expect(inline.runs.contains { $0.link == URL(string: "https://example.com/report") })
    }
}
