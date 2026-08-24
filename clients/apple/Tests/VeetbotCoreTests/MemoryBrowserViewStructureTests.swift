import Foundation
import Testing

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

        #expect(source.contains("} else if let errorMessage = model.errorMessage {\n                loadMoreFailureFooter(errorMessage)"))
        #expect(source.contains("Task { await model.loadMore() }"))
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
