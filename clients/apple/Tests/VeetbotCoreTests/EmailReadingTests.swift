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

    /// The server marks each line a writer ended; a quote level must not lose it.
    @Test func testLineBreaksSurviveInsideAQuote() {
        #expect(MarkdownContentParser.parse("Riley Example\\\nPartner")
            == [.paragraph("Riley Example\nPartner")])
        #expect(MarkdownContentParser.parse(">> Riley Example\\\n>> Partner")
            == [.blockquote([.blockquote([.paragraph("Riley Example\nPartner")])])])
    }

    /// A chain below a message's own text folds, the forward's note stays in view.
    @Test func testEarlierRepliesFoldBelowAMessagesOwnText() {
        let chain = "On Fri, Oct 30, 2026 at 6:15 PM Sam \\<sam@example.com> wrote:\n\n"
            + "> Earlier.\n>\n> On Friday Riley wrote:\n>\n>> Earlier still."
        let own = "My note.\n\n> Forwarded from Sam\n\nSam's note."
        let sections = EmailReaderSections(markdown: own + "\n\n" + chain)
        #expect(sections.current == MarkdownContentParser.parse(own))
        #expect(sections.earlier == MarkdownContentParser.parse(chain))
    }

    /// Folding must never hide what the message itself says.
    @Test func testEarlierRepliesStayWhenFoldingCouldHideTheMessage() {
        let unfolded = [
            // Nothing of its own would be left to show.
            "On Friday Sam wrote:\n\n> The whole message.",
            // A reply written below the quote answers it.
            "Intro.\n\nOn Friday Sam wrote:\n\n> A question?\n\nThe answer.",
            // The writer is quoting on purpose, not replying below a chain.
            "My note.\n\nHere is what Sam said:\n\n> A quote.",
            "My note.\n\n> A quote with no attribution.",
            "On Friday we met. Sam wrote:\n\nNo quote follows.",
        ]
        for markdown in unfolded {
            let sections = EmailReaderSections(markdown: markdown)
            #expect(sections.current == MarkdownContentParser.parse(markdown))
            #expect(sections.earlier.isEmpty)
        }
    }
}
