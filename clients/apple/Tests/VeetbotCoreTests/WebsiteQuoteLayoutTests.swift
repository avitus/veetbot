#if os(macOS)
import AppKit
import SwiftUI
import Testing

@testable import VeetbotCore

/// 0129-design §14 item 3: website text on the `browser.act` card is capped
/// at three lines with an expand control. The control appears exactly when
/// three lines cut the text short at the width the card has, whatever its
/// length in characters. Each case hosts the quote in a window and compares
/// its settled height with the same text capped at three lines: only the
/// control under it makes it taller.
@Suite(.serialized) @MainActor struct WebsiteQuoteLayoutTests {
    @Test func aQuoteThatThreeLinesCutShortOffersShowMoreAtAnyLength() async throws {
        let name = String(repeating: "word ", count: 22) + "end"
        #expect(name.count <= 120)
        let width: CGFloat = 150
        #expect(Self.textHeight(name, width: width, lineLimit: nil) > Self.textHeight(name, width: width, lineLimit: 3))

        #expect(try await Self.offersExpansion(name, width: width))
    }

    @Test func aQuoteThatFitsOffersNoControlAtAnyLength() async throws {
        let long = String(repeating: "word ", count: 30) + "end"
        #expect(long.count > 120)

        #expect(!(try await Self.offersExpansion(long, width: 2_400)))
        #expect(!(try await Self.offersExpansion("el gato", width: 150)))
    }

    private static func offersExpansion(_ text: String, width: CGFloat) async throws -> Bool {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 2_600, height: 600),
            styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false
        )
        window.isReleasedWhenClosed = false
        let host = NSHostingView(rootView: WebsiteQuote(text: text).frame(width: width, alignment: .leading))
        window.contentView = host
        window.orderFront(nil)
        defer {
            window.orderOut(nil)
            window.contentView = nil
        }
        // The quote measures itself after it appears; let that settle.
        for _ in 0..<8 {
            try await Task.sleep(nanoseconds: 50_000_000)
            host.layoutSubtreeIfNeeded()
        }
        return host.fittingSize.height > textHeight(text, width: width, lineLimit: 3) + 4
    }

    private static func textHeight(_ text: String, width: CGFloat, lineLimit: Int?) -> CGFloat {
        NSHostingView(
            rootView: Text(verbatim: "“\(text)”")
                .appFont(.body)
                .lineLimit(lineLimit)
                .fixedSize(horizontal: false, vertical: true)
                .frame(width: width, alignment: .leading)
        ).fittingSize.height
    }
}
#endif
