#if os(macOS)
import AppKit
import SwiftUI
import Testing

@testable import VeetbotCore

/// Every block of a reply must draw every line its text wraps to. In these
/// replies the transcript gave list items and paragraphs a line less than they
/// needed and cut each off with an ellipsis; every block drawn alone was whole.
@Suite(.serialized) @MainActor struct MarkdownListLayoutTests {
    private static let reply = """
        That sounds like **baked-on food or oil plus underlying staining**. Focus on removing the raised deposits first; the smooth brown discoloration may be harmless.

        1. **Rinse away any remaining Bar Keepers Friend.** With the pot cool, add enough water to cover the deposits and **2 tablespoons of baking soda**.
        2. **Gently simmer for 10 minutes**, keeping the deposits submerged. Use a wooden spoon or silicone spatula to gently nudge them—don’t forcefully scrape.
        3. **Let it cool, then leave the solution soaking overnight.** Wash with a non-scratch sponge. Repeat once if it’s clearly making progress.

        Once the crust is gone, try a **wet baking-soda paste on the remaining stains**, left for several hours before gently wiping off.

        **Don’t chase perfectly cream-colored enamel with increasingly aggressive scrubbing.** If the surface is smooth and intact, residual staining is generally cosmetic. If the “deposits” don’t soften at all, send a photo before trying stronger chemicals—it’s worth checking that you’re seeing buildup rather than damaged enamel.
        """

    @Test(arguments: [590.0, 600.0, 610.0])
    func everyListItemInAReplyDrawsAllItsLines(width: CGFloat) {
        let blocks = Self.reply.components(separatedBy: "\n\n")
        let alone = blocks.map { Self.drawnLines($0, width: width) }
        let together = Self.drawnLines(Self.reply, width: width)

        #expect(alone[1] >= 6, "each item wraps to at least two lines at \(width) points")
        #expect(together == alone.reduce(0, +), "at \(width) points the reply drew \(together) of \(alone) lines")
    }

    /// A quote stretched past its text and squeezed the paragraphs around it:
    /// the first and last paragraphs of this reply each lost their final line
    /// to an ellipsis, so the answer read as cut short.
    private static let quotedReply = """
        **For a tow to a repair shop, request a flatbed tow truck—not a rope or strap tow behind another car.** That avoids drivetrain damage regardless of which Cherokee drivetrain she has.

        Tell the dispatcher:
        > “It’s a 2014 Jeep Cherokee with no visible front tow hooks. Please send a flatbed.”

        You don’t need to install a hitch or find a front hook yourself; the operator should use the proper loading equipment and attachment points. **If it won’t shift into Neutral or the electronic parking brake won’t release, mention that when booking** so they bring skates or dollies.
        """

    @Test(arguments: [400.0, 600.0, 720.0])
    func everyParagraphBesideAQuoteDrawsAllItsLines(width: CGFloat) {
        let blocks = Self.quotedReply.components(separatedBy: "\n\n")
        let alone = blocks.map { Self.drawnLines($0, width: width) }
        let together = Self.drawnLines(Self.quotedReply, width: width)

        #expect(together == alone.reduce(0, +), "at \(width) points the reply drew \(together) of \(alone) lines")
    }

    /// Counts the bands of ink the reply draws, laid out the way the
    /// transcript lays out an assistant reply: the body role beside the mark.
    private static func drawnLines(_ text: String, width: CGFloat) -> Int {
        let row = HStack(alignment: .top, spacing: VeetSpace.s3) {
            AssistantMark().padding(.top, VeetSpace.s2)
            VStack(alignment: .leading, spacing: VeetSpace.s2) {
                MarkdownContentView(text: text)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .transcriptBody()
        .frame(width: width)
        .background(Color.white)
        .environment(\.colorScheme, .light)

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: width, height: 800),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        let host = NSHostingView(rootView: ScrollView { row })
        window.contentView = host
        host.layoutSubtreeIfNeeded()
        guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return -1 }
        host.cacheDisplay(in: host.bounds, to: bitmap)
        guard bitmap.samplesPerPixel == 4, bitmap.bitsPerSample == 8,
            !bitmap.bitmapFormat.contains(.alphaFirst)
        else { return -1 }

        // Right of the mark, so it never counts as a line.
        let firstColumn = Int((VeetSpace.s5 * CGFloat(bitmap.pixelsWide) / host.bounds.width).rounded())
        var pixel = [Int](repeating: 0, count: 4)
        var lines = 0
        var inLine = false
        for y in 0..<bitmap.pixelsHigh {
            let inked = (firstColumn..<bitmap.pixelsWide).contains { x in
                // RGBA: below the content the bitmap is transparent, not paper.
                bitmap.getPixel(&pixel, atX: x, y: y)
                return pixel[3] > 128 && pixel[0] + pixel[1] + pixel[2] < 3 * 128
            }
            if inked, !inLine { lines += 1 }
            inLine = inked
        }
        return lines
    }
}
#endif
