#if os(macOS)
import AppKit
import SwiftUI
import Testing

@testable import VeetbotCore

/// A reply chain quotes every earlier reply one level deeper, so the email
/// reading view routinely meets quotes eight or ten levels deep. Layout must
/// stay linear in that depth: a reader that nested one stack per level made
/// SwiftUI's layout cost grow about sevenfold per level, and a ten-level
/// thread kept the Mac app at full CPU for hours.
@Suite(.serialized) @MainActor struct MarkdownQuoteLayoutTests {
    @Test func aDeeplyQuotedReplyChainLaysOutWithinSeconds() {
        let text = Self.replyChain(depth: 8)
        let start = DispatchTime.now()
        let height = Self.layoutHeight(text, width: 720)
        let seconds = Double(DispatchTime.now().uptimeNanoseconds - start.uptimeNanoseconds) / 1e9

        #expect(height > 0)
        #expect(seconds < 2, "an eight-level quote took \(seconds) seconds to lay out")
    }

    /// Each level quotes the one below it, the way mail clients prefix replies.
    private static func replyChain(depth: Int) -> String {
        (1...depth).map { level in
            let prefix = String(repeating: ">", count: level)
            return """
                \(prefix) Reply at level \(level) with enough words to wrap across a couple of lines at a comfortable reading width.
                \(prefix)
                \(prefix) A second paragraph at level \(level).
                """
        }.joined(separator: "\n")
    }

    private static func layoutHeight(_ text: String, width: CGFloat) -> CGFloat {
        let host = NSHostingView(
            rootView: ScrollView {
                MarkdownContentView(text: text).padding(24).frame(maxWidth: width, alignment: .leading)
            }
        )
        host.frame = NSRect(x: 0, y: 0, width: width + 48, height: 600)
        host.layoutSubtreeIfNeeded()
        return host.fittingSize.height
    }
}
#endif
