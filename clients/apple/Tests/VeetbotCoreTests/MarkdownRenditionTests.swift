import Foundation
import Testing

#if os(macOS)
import AppKit
#elseif os(iOS)
import UIKit
#endif

@testable import VeetbotCore

/// Copy and Select Text render a reply once, from the transcript's own parser,
/// as formatted text, as plain text without Markdown symbols, and as semantic
/// HTML (ADR-0122).
@Suite struct MarkdownRenditionTests {
    private func render(_ markdown: String) -> MarkdownRendition {
        MarkdownRenditionBuilder.render(MarkdownContentParser.parse(markdown), bodySize: 13)
    }

    private func range(of text: String, in rendition: MarkdownRendition) throws -> NSRange {
        let range = (rendition.attributedText.string as NSString).range(of: text)
        try #require(range.location != NSNotFound, "\(text) is not in the rendition")
        return range
    }

    private func font(at range: NSRange, in rendition: MarkdownRendition) throws -> RenditionFont {
        try #require(
            rendition.attributedText.attribute(.font, at: range.location, effectiveRange: nil)
                as? RenditionFont
        )
    }

    @Test func plainTextDropsEveryMarkdownSymbol() {
        let rendition = render(
            "# Bottom line\n\nJev is **strong**, *early*, `typed`, and ~~unproven~~."
        )

        #expect(
            rendition.plainText == "Bottom line\n\nJev is strong, early, typed, and unproven."
        )
    }

    @Test func headingsAreLargerAndBolderThanBody() throws {
        let rendition = render("## Signals\n\nBody text")
        let heading = try font(at: try range(of: "Signals", in: rendition), in: rendition)
        let body = try font(at: try range(of: "Body text", in: rendition), in: rendition)

        #expect(heading.pointSize > body.pointSize)
        #expect(isBold(heading))
        #expect(!isBold(body))
        #expect(rendition.html.contains("<h2>Signals</h2>"))
    }

    @Test func inlineEmphasisBecomesFontTraitsAndTags() throws {
        let rendition = render("A **bold** and *slanted* `call()` ~~gone~~")

        #expect(isBold(try font(at: try range(of: "bold", in: rendition), in: rendition)))
        #expect(isItalic(try font(at: try range(of: "slanted", in: rendition), in: rendition)))
        #expect(isMonospaced(try font(at: try range(of: "call()", in: rendition), in: rendition)))
        let struck = rendition.attributedText.attribute(
            .strikethroughStyle, at: try range(of: "gone", in: rendition).location,
            effectiveRange: nil) as? Int
        #expect(struck == NSUnderlineStyle.single.rawValue)
        #expect(
            rendition.html.contains(
                "<p>A <strong>bold</strong> and <em>slanted</em> <code>call()</code> <del>gone</del></p>"
            ))
    }

    @Test func listsKeepMarkersDepthAndHangingIndents() throws {
        let rendition = render("- one\n  - nested\n- [ ] todo\n- [x] done")

        #expect(rendition.plainText == "• one\n    • nested\n☐ todo\n☑ done")
        let style = try #require(
            rendition.attributedText.attribute(
                .paragraphStyle, at: try range(of: "nested", in: rendition).location,
                effectiveRange: nil) as? NSParagraphStyle)
        #expect(style.headIndent > style.firstLineHeadIndent)
        #expect(style.firstLineHeadIndent > 0)
        #expect(
            rendition.html.contains(
                "<ul><li>one<ul><li>nested</li></ul></li><li>☐ todo</li><li>☑ done</li></ul>"
            ))
    }

    @Test func orderedListsKeepTheirNumbers() {
        let rendition = render("3. third\n4. fourth")

        #expect(rendition.plainText == "3. third\n4. fourth")
        #expect(rendition.html.contains("<ol start=\"3\"><li>third</li><li>fourth</li></ol>"))
    }

    @Test func tablesBecomeTabSeparatedRowsAndHTMLTables() throws {
        let rendition = render("| Name | Value |\n| :--- | ---: |\n| **Alpha** | 12 |")

        #expect(rendition.plainText == "Name\tValue\nAlpha\t12")
        let style = try #require(
            rendition.attributedText.attribute(
                .paragraphStyle, at: try range(of: "Alpha", in: rendition).location,
                effectiveRange: nil) as? NSParagraphStyle)
        #expect(style.tabStops.count == 1)
        #expect(isBold(try font(at: try range(of: "Name", in: rendition), in: rendition)))
        #expect(rendition.html.contains("<th>Name</th>"))
        #expect(rendition.html.contains("<th style=\"text-align:right\">Value</th>"))
        #expect(rendition.html.contains("<td><strong>Alpha</strong></td>"))
    }

    @Test func linksKeepTheirTargetOnlyWhenSafe() throws {
        let rendition = render(
            "[Docs](https://example.com/docs), <https://example.com>, [bad](javascript:alert(1))"
        )

        #expect(
            rendition.plainText
                == "Docs (https://example.com/docs), https://example.com, bad"
        )
        let link = rendition.attributedText.attribute(
            .link, at: try range(of: "Docs", in: rendition).location, effectiveRange: nil)
        #expect((link as? URL)?.absoluteString == "https://example.com/docs")
        let unsafe = rendition.attributedText.attribute(
            .link, at: try range(of: "bad", in: rendition).location, effectiveRange: nil)
        #expect(unsafe == nil)
        #expect(rendition.html.contains("<a href=\"https://example.com/docs\">Docs</a>"))
        #expect(!rendition.html.contains("javascript"))
    }

    @Test func codeBlocksStayVerbatimAndMonospaced() throws {
        let rendition = render("```swift\nlet a = 1 < 2\n```")

        #expect(rendition.plainText == "let a = 1 < 2")
        #expect(isMonospaced(try font(at: try range(of: "let a", in: rendition), in: rendition)))
        #expect(rendition.html.contains("<pre><code>let a = 1 &lt; 2</code></pre>"))
    }

    @Test func quotesAndBreaksLoseTheirSymbols() {
        let rendition = render("> quoted\n\n---\n\nafter")

        #expect(rendition.plainText == "quoted\n\n———\n\nafter")
        #expect(rendition.html.contains("<blockquote><p>quoted</p></blockquote><hr><p>after</p>"))
    }

    @Test func htmlIsAnEscapedUTF8Document() {
        let rendition = render("Fish & chips <script>")

        #expect(rendition.html.hasPrefix("<html><head><meta charset=\"utf-8\"></head><body>"))
        #expect(rendition.html.hasSuffix("</body></html>"))
        #expect(rendition.html.contains("Fish &amp; chips &lt;script&gt;"))
    }

    @Test func theRenditionCarriesNoColorSoDarkModeNeverPastesWhiteText() {
        let rendition = render("# Title\n\n**Bold** [link](https://example.com)")
        var colored = false
        rendition.attributedText.enumerateAttributes(
            in: NSRange(location: 0, length: rendition.attributedText.length)
        ) { attributes, _, _ in
            colored = colored || attributes[.foregroundColor] != nil
                || attributes[.backgroundColor] != nil
        }

        #expect(!colored)
    }

    @Test func rtfRoundTripsTheTextAndItsBoldHeading() throws {
        let rendition = render("# Title\n\nBody")
        let data = try #require(rendition.rtfData())
        let decoded = try NSAttributedString(
            data: data,
            options: [.documentType: NSAttributedString.DocumentType.rtf],
            documentAttributes: nil
        )

        #expect(decoded.string.hasPrefix("Title\n"))
        #expect(decoded.string.contains("Body"))
        let heading = try #require(
            decoded.attribute(.font, at: 0, effectiveRange: nil) as? RenditionFont)
        #expect(isBold(heading))
    }

    @Test func theAppTextSizeScalesTheBody() throws {
        let rendition = MarkdownRendition(markdown: "Body", textSize: .large)
        let body = try font(at: try range(of: "Body", in: rendition), in: rendition)

        #if os(macOS)
        #expect(abs(body.pointSize - 13 * 1.3) < 0.01)
        #else
        #expect(abs(body.pointSize - 17 * 1.3) < 0.01)
        #endif
    }

    @Test func aMessageCopiesItsTextBlocksSeparatedByABlankLine() {
        let artifact = UUID()
        let item = TimelineItem(
            id: "event-2", role: .assistant,
            content: [.text("First"), .file(artifactID: artifact, mediaType: "text/plain", filename: "a.txt"), .text("Second")]
        )

        #expect(item.copyableMarkdown == "First\n\nSecond")
        #expect(item.offersMessageActions)
        #expect(!TimelineItem(id: "s", role: .assistant, content: [.text("Hi")], isStreaming: true).offersMessageActions)
        #expect(
            !TimelineItem(
                id: "f", role: .assistant,
                content: [.file(artifactID: artifact, mediaType: "text/plain", filename: "a.txt")]
            ).offersMessageActions)
        #expect(!TimelineItem(id: "b", role: .user, content: [.text("  \n")]).offersMessageActions)
    }

    #if os(macOS)
    @Test @MainActor func copyWritesRichHTMLAndPlainRepresentations() throws {
        let pasteboard = NSPasteboard(name: NSPasteboard.Name("veetbot.tests.\(UUID().uuidString)"))
        defer { pasteboard.releaseGlobally() }
        let rendition = render("# Title\n\n**Bold** text")

        SystemClipboard.copy(rendition, to: pasteboard)

        let types = pasteboard.types ?? []
        #expect(types.contains(.rtf))
        #expect(types.contains(.html))
        #expect(types.contains(.string))
        #expect(pasteboard.string(forType: .string) == "Title\n\nBold text")
        #expect(pasteboard.string(forType: .html) == rendition.html)
        #expect(pasteboard.data(forType: .rtf) == rendition.rtfData())
    }
    #endif

    private func isBold(_ font: RenditionFont) -> Bool {
        #if os(macOS)
        return font.fontDescriptor.symbolicTraits.contains(.bold)
        #else
        return font.fontDescriptor.symbolicTraits.contains(.traitBold)
        #endif
    }

    private func isItalic(_ font: RenditionFont) -> Bool {
        #if os(macOS)
        return font.fontDescriptor.symbolicTraits.contains(.italic)
        #else
        return font.fontDescriptor.symbolicTraits.contains(.traitItalic)
        #endif
    }

    private func isMonospaced(_ font: RenditionFont) -> Bool {
        #if os(macOS)
        return font.fontDescriptor.symbolicTraits.contains(.monoSpace)
        #else
        return font.fontDescriptor.symbolicTraits.contains(.traitMonoSpace)
        #endif
    }
}
