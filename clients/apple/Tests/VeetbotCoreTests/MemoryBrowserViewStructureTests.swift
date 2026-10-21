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
    /// The A2 Mac design (MagicPath coolly-breeze-5701): a sidebar with
    /// Review, Browse and Maintenance groups beside the list and the detail,
    /// each memory group with its number from the counts route (ADR-0224).
    @Test
    func testTheMacSheetGroupsMemoryInASidebar() throws {
        let source = try memoryBrowserViewSource()

        #expect(source.contains("private var groupSidebar: some View"))
        #expect(source.contains("groupHeading(\"Review\")"))
        #expect(source.contains("groupHeading(\"Browse\")"))
        #expect(source.contains("groupHeading(\"Maintenance\")"))
        #expect(source.contains(".accessibilityIdentifier(\"memory.group.\\(group.id)\")"))
        #expect(source.contains("NavigationSplitView(columnVisibility: $columnVisibility) {\n            groupSidebar"))
        #expect(source.contains(".task { await model.loadCounts() }"))
        #expect(source.contains(".accessibilityIdentifier(\"memory.group.\\(group.id).count\")"))
    }

    @Test
    func testAFlaggedMemoryAsksIsThisRightWithLooksRightFirst() throws {
        let detail = try String(
            contentsOf: URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
                .appendingPathComponent("Veetbot/Views/MemoryDetailView.swift"),
            encoding: .utf8
        )

        #expect(detail.contains("Label(\"Is this right?\", systemImage: \"flag\")"))
        #expect(detail.contains(".accessibilityIdentifier(\"memory.review.looks-right\")"))
        #expect(detail.contains("Button(\"Looks right\")"))
        #expect(!detail.contains("Mark reviewed"))
        // ADR-0224: the wording is corrected by supersession, from the card
        // and, for any live memory, from the Review menu.
        #expect(detail.contains("Button(\"Fix the wording\") { editingWording = true }"))
        #expect(detail.contains(".accessibilityIdentifier(\"memory.review.fix-wording\")"))
        #expect(detail.contains(".accessibilityIdentifier(\"memory.detail.edit\")"))
        #expect(detail.contains("MemoryWordingSheet(memory: memory, model: model) { memory = $0 }"))
        #expect(detail.contains(".accessibilityIdentifier(\"memory.detail.evidence\")"))
        #expect(detail.contains(".task(id: memory.id)"))
    }

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
            "memory.browser must be attached to the navigation container, not nested inside its searchable/toolbar content chain"
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
                "#if os(macOS) // The ideal height sizes the fitted sheet before a saved size exists; // without it a column whose content wants all the height it is offered // (an empty or unavailable state) lays out at the screen's height and // is cut off above the sheet. .frame( minWidth: 860, maxWidth: .infinity, minHeight: 520, idealHeight: 600, maxHeight: .infinity ) .background(MemoryBrowserWindowResizeView()) .memoryBrowserPresentationSizing() #endif"
            )
        )
        #expect(source.contains(".background(MemoryBrowserWindowResizeView())"))
    }

    @Test
    func testTheMacModalUsesTheResizableSwiftUIPresentationSizing() throws {
        let source = try memoryBrowserViewSource()

        #expect(source.contains(".memoryBrowserPresentationSizing()"))
        // Fitted sizing is unconditional at the macOS floor (ADR-0211).
        #expect(!source.contains("#available(macOS"))
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
