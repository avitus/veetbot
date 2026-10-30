#if os(macOS)
import Foundation
import Testing

@testable import VeetbotCore

/// The Mac Email columns (MagicPath cozy-evening-6871): the mailbox sidebar,
/// the inbox and the right panel each keep the width the owner drags them to
/// on this Mac, and the reader takes what they leave. A drag never takes the
/// reader below the design's minimum; a narrow window may squeeze a column
/// without replacing the width the owner chose.
@MainActor
@Suite struct EmailColumnWidthTests {
    private func defaults() -> UserDefaults {
        let suite = "EmailColumnWidthTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defaults.removePersistentDomain(forName: suite)
        return defaults
    }

    @Test
    func testTheSidebarStartsAtTheDesignWidthAndKeepsTheOwnersChoice() {
        let defaults = defaults()
        let store = EmailSidebarWidthStore(defaults: defaults)
        #expect(store.width == 320, "Before any drag the sidebar is the design's 320 points")
        store.remember(300)
        #expect(store.width == 300)
        #expect(EmailSidebarWidthStore(defaults: defaults).width == 300, "The next launch reads the same width")
        store.remember(90)
        #expect(store.width == 300)
        store.remember(9_000)
        #expect(store.width == 420)
        store.remember(.nan)
        #expect(store.width == 420, "An unusable width is never remembered")
        store.forget()
        #expect(store.width == 320, "Resetting returns the sidebar to the design width")
        #expect(defaults.object(forKey: EmailSidebarWidthStore.key) == nil)
    }

    @Test
    func testTabletWidthAdaptsWithoutReplacingTheOwnersPreference() {
        let saved: CGFloat = 420
        #expect(ModeSidebarLayout.shown(saved, available: 768) == 396)
        #expect(ModeSidebarLayout.shown(saved, available: 1_024) == saved)
        #expect(ModeSidebarLayout.clamped(100) == 300)
        #expect(ModeSidebarLayout.clamped(1_000) == 420)
        #expect(ModeSidebarLayout.clamped(.nan) == 320)
        #expect(ModeSidebarLayout.clamped(.infinity) == 320)
    }

    @Test
    func testTabletResizeAtTheWindowCapPreservesTheRememberedWidth() {
        #expect(ModeSidebarLayout.preference(saved: 420, displayed: 396, proposed: 416, available: 768) == 420)
        #expect(ModeSidebarLayout.preference(saved: 420, displayed: 396, proposed: 396, available: 768) == 420)
        #expect(ModeSidebarLayout.preference(saved: 420, displayed: 396, proposed: 376, available: 768) == 376)
        #expect(ModeSidebarLayout.preference(saved: 320, displayed: 320, proposed: 340, available: 1_024) == 340)
    }

    @Test
    func testTheInboxRangeAndStartingWidthMatchTheDesign() {
        #expect(EmailInboxWidthStore.bounds == 260...480)
        #expect(EmailInboxWidthStore.idealWidth == 300)
    }

    @Test
    func testASidebarDragStopsWhereTheReaderWouldFallBelowItsMinimum() {
        // A wide window permits the full sidebar range beside a 300-point inbox.
        #expect(EmailColumnLayout.sidebarDrag(to: 420, from: 320, available: 1_140, inbox: 300) == 420)
        // Resizing cannot take more reading space from an already narrow window.
        #expect(EmailColumnLayout.sidebarDrag(to: 380, from: 320, available: 900, inbox: 300) == 320)
        #expect(EmailColumnLayout.sidebarDrag(to: 380, from: 320, available: 1_060, inbox: 300) == 340)
        // Narrowing remains possible down to the floor needed by the mode labels.
        #expect(EmailColumnLayout.sidebarDrag(to: 300, from: 340, available: 900, inbox: 300) == 300)
        #expect(EmailColumnLayout.sidebarDrag(to: 200, from: 340, available: 900, inbox: 300) == 300)
        // A squeezed column starts dragging from the width actually on screen.
        #expect(EmailColumnLayout.sidebarDrag(to: 380, from: 320, available: 832, inbox: 260) == 320)
        #expect(EmailColumnLayout.readerMinimum == 420)
    }

    @Test
    func testANarrowWindowSqueezesTheSidebarWithoutChangingItsWidth() {
        #expect(EmailColumnLayout.sidebarShown(stored: 340, available: 1_440) == 340)
        // A narrow window preserves the sidebar floor; the owner can fold it away.
        #expect(EmailColumnLayout.sidebarShown(stored: 340, available: 780) == 300)
        #expect(EmailColumnLayout.sidebarShown(stored: 260, available: 600) == 300, "Never below the sidebar's floor")
    }

    @Test
    func testAPanelDragStopsWhereTheReaderWouldFallBelowItsMinimum() {
        let suite = defaults()
        let model = InspectorModel(store: InspectorLayoutStore(defaults: suite))
        model.windowWidthChanged(1_440)
        #expect(model.width(.email) == 320)
        model.setWidthLimit(330, for: .email)
        model.setWidth(440, for: .email, remember: true)
        #expect(model.width(.email) == 330, "The drag stops where the reader keeps its minimum")
        model.setWidth(280, for: .email, remember: true)
        #expect(model.width(.email) == 280, "Narrowing is always allowed")
        model.setWidthLimit(nil, for: .email)
        model.setWidth(440, for: .email, remember: true)
        #expect(model.width(.email) == 440)
        #expect(model.width(.chat) == 340, "A limit in Email leaves Chat alone")
    }
}
#endif
