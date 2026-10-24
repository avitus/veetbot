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
        #expect(store.width == 260, "Before any drag the sidebar is the design's 260 points")
        store.remember(300)
        #expect(store.width == 300)
        #expect(EmailSidebarWidthStore(defaults: defaults).width == 300, "The next launch reads the same width")
        store.remember(90)
        #expect(store.width == 200)
        store.remember(9_000)
        #expect(store.width == 340)
        store.remember(.nan)
        #expect(store.width == 340, "An unusable width is never remembered")
        store.forget()
        #expect(store.width == 260, "Resetting returns the sidebar to the design width")
        #expect(defaults.object(forKey: EmailSidebarWidthStore.key) == nil)
    }

    @Test
    func testTheInboxRangeAndStartingWidthMatchTheDesign() {
        #expect(EmailInboxWidthStore.bounds == 260...480)
        #expect(EmailInboxWidthStore.idealWidth == 300)
    }

    @Test
    func testASidebarDragStopsWhereTheReaderWouldFallBelowItsMinimum() {
        // 1,120 points beside a 320-point panel leave room for the widest sidebar.
        #expect(EmailColumnLayout.sidebarDrag(to: 340, from: 260, available: 1_120, inbox: 300) == 340)
        // 860 points with a 300-point inbox leave the reader 300: the sidebar cannot grow.
        #expect(EmailColumnLayout.sidebarDrag(to: 330, from: 260, available: 860, inbox: 300) == 260)
        // 1,000 points leave room for 280 before the reader reaches 420.
        #expect(EmailColumnLayout.sidebarDrag(to: 330, from: 260, available: 1_000, inbox: 300) == 280)
        // Narrowing is always allowed.
        #expect(EmailColumnLayout.sidebarDrag(to: 220, from: 260, available: 860, inbox: 300) == 220)
        // A squeezed sidebar drags from the 268 it shows, so widening stops there instead
        // of jumping to a width the owner never saw; from the stored 340 it would reach 300.
        #expect(EmailColumnLayout.sidebarDrag(to: 300, from: 268, available: 780, inbox: 260) == 268)
        #expect(EmailColumnLayout.sidebarDrag(to: 300, from: 340, available: 780, inbox: 260) == 300)
        #expect(EmailColumnLayout.readerMinimum == 420)
    }

    @Test
    func testANarrowWindowSqueezesTheSidebarWithoutChangingItsWidth() {
        #expect(EmailColumnLayout.sidebarShown(stored: 340, available: 1_440) == 340)
        // 780 points keep the inbox's 260, the reader's 250 and two hairlines before the sidebar.
        #expect(EmailColumnLayout.sidebarShown(stored: 340, available: 780) == 268)
        #expect(EmailColumnLayout.sidebarShown(stored: 260, available: 600) == 200, "Never below the sidebar's floor")
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
