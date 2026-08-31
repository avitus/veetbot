import Foundation
import Testing

#if os(macOS)
import AppKit
#endif

@testable import VeetbotCore

/// `MemoryBrowserView`'s degradation and identifier placement cannot be
/// exercised by a hosted-rendering test (this package has no view-inspection
/// dependency), so — matching the source-parsing pattern
/// `SessionSidebarNavigationTests` already uses for `RootView` — these pin
/// the exact source shape so a future edit cannot silently reintroduce a
/// misplaced identifier or a full-screen state that clobbers a populated
/// list.
@Suite struct MemoryBrowserViewStructureTests {
    @Test
    func testTheBrowserRootCarriesTheIdentifierRatherThanTheHoistedSearchField() throws {
        let source = try memoryBrowserViewSource()

        #expect(
            !source.contains("memory.search"),
            "the search field is hoisted into the navigation bar by .searchable and is not a descendant of the content it's attached to, so an identifier there would land on the content underneath instead of the field; XCUITest reaches the field via app.searchFields"
        )
        #expect(
            source.contains(
                "}\n        .accessibilityIdentifier(\"memory.browser\")\n        .task { await model.reload() }"
            ),
            "memory.browser must be attached to the NavigationView result, not nested inside its searchable/toolbar content chain"
        )
    }

    @Test
    func testTheMacModalCanGrowInBothDimensions() throws {
        let source = try memoryBrowserViewSource()
        let normalizedSource = source
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")

        #expect(
            normalizedSource.contains(
                "#if os(macOS) .frame( minWidth: 560, maxWidth: .infinity, minHeight: 520, maxHeight: .infinity ) .background(MemoryBrowserWindowResizeView()) .memoryBrowserPresentationSizing() #endif"
            )
        )
        #expect(source.contains(".background(MemoryBrowserWindowResizeView())"))
    }

    @Test
    func testTheMacModalUsesTheResizableSwiftUIPresentationSizing() throws {
        let source = try memoryBrowserViewSource()

        #expect(source.contains(".memoryBrowserPresentationSizing()"))
        #expect(source.contains("if #available(macOS 15.0, *)"))
        #expect(source.contains("presentationSizing(.fitted)"))
    }

    @Test
    func testTheMacModalAttachesExplicitResizePersistence() throws {
        let source = try memoryBrowserViewSource()
        let normalizedSource = source
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")

        #expect(
            normalizedSource.contains(
                "resizePersistence = PopupWindowResizePersistence( window: window, key: MemoryBrowserWindowConfiguration.storageKey )"
            )
        )
    }

    #if os(macOS)
    @Test @MainActor
    func testTheMacModalWindowAllowsUserResizing() {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: MemoryBrowserWindowConfiguration.minimumSize),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )

        MemoryBrowserWindowConfiguration.apply(to: window)

        #expect(window.styleMask.contains(.resizable))
        #expect(window.contentMinSize == MemoryBrowserWindowConfiguration.minimumSize)
        #expect(window.contentMaxSize == MemoryBrowserWindowConfiguration.maximumSize)
    }
    #endif

    @Test
    func testAPopulatedListIsNeverReplacedByAFullScreenDegradedState() throws {
        let source = try memoryBrowserViewSource()

        #expect(source.contains("if model.unavailable && model.items.isEmpty {"))
        #expect(source.contains("} else if let errorMessage = model.errorMessage, model.items.isEmpty {"))
        #expect(
            source.contains("} else {\n            list\n        }"),
            "once items is non-empty, unavailable and errorMessage must fall through to list"
        )
    }

    @Test
    func testTheListFootersASurvivableLoadMoreFailureWithRetry() throws {
        let source = try memoryBrowserViewSource()

        #expect(
            source.contains(
                "} else if let errorMessage = model.errorMessage {\n                loadMoreFailureFooter(errorMessage)"
            )
        )
        #expect(source.contains("Task { await model.loadMore() }"))
    }

    @Test
    func testBothRetrySurfacesCallTheUnifiedRetryMethodRatherThanHardCodingWhichFetch() throws {
        let source = try memoryBrowserViewSource()

        let occurrences = source.components(separatedBy: "Task { await model.retry() }").count - 1
        #expect(
            occurrences == 2,
            "the inline loadMore footer and the full-screen error state must each retry through model.retry() — which re-runs whichever fetch actually failed — rather than one of them hard-coding loadMore() or reload() directly"
        )
    }

    private func memoryBrowserViewSource() throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(
            contentsOf: packageRoot.appendingPathComponent(
                "Veetbot/Views/MemoryBrowserView.swift"
            ),
            encoding: .utf8
        )
    }
}
