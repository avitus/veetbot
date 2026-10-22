#if os(macOS)
import AppKit
import SwiftUI
import Testing

@testable import VeetbotCore

@Suite(.serialized) @MainActor struct ChatColumnLayoutTests {
    @Test(arguments: [800.0, 1100.0, 1440.0])
    func wideningTheChatPaneKeepsItsSideGutters(width: CGFloat) throws {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore()
        )
        let host = NSHostingView(rootView: ChatView(model: model))
        host.frame = NSRect(x: 0, y: 0, width: 600, height: 800)
        host.layoutSubtreeIfNeeded()
        let composer = try #require(findComposer(in: host))
        let narrow = host.convert(composer.bounds, from: composer)
        try #require(narrow.width > 100)

        host.frame.size.width = width
        host.layoutSubtreeIfNeeded()
        let wide = host.convert(composer.bounds, from: composer)
        #expect(abs(wide.minX - narrow.minX) < 1,
                "Widening the center panel must not add empty side margins: \(narrow) -> \(wide)")
        #expect(abs((wide.width - narrow.width) - (width - 600)) < 1,
                "The composer must use the added center-panel width")
    }

    private func findComposer(in view: NSView) -> ComposerNSTextView? {
        if let composer = view as? ComposerNSTextView { return composer }
        return view.subviews.lazy.compactMap { findComposer(in: $0) }.first
    }
}
#endif
