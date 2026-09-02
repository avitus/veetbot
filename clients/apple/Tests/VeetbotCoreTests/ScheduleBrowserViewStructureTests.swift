import Foundation
import Testing

@testable import VeetbotCore

@Suite struct ScheduleBrowserViewStructureTests {
    @Test
    func testSidebarProvidesASeparateReadOnlyScheduleBrowserEntry() throws {
        let source = try source(at: "Veetbot/Views/RootView.swift")

        #expect(source.contains("@StateObject private var scheduleViewModel = ScheduleViewModel()"))
        #expect(source.contains(".accessibilityIdentifier(\"sidebar.schedules\")"))
        #expect(source.contains("ScheduleBrowserView(model: scheduleViewModel)"))
    }

    @Test
    func testBrowserRetainsPopulatedRowsAcrossLaterFailuresAndPagesByCursor() throws {
        let source = try source(at: "Veetbot/Views/ScheduleBrowserView.swift")

        #expect(source.contains("if model.unavailable && model.items.isEmpty {"))
        #expect(source.contains("} else if let errorMessage = model.errorMessage, model.items.isEmpty {"))
        #expect(source.contains("Task { await model.loadMore() }"))
        #expect(source.contains("Task { await model.retry() }"))
        #expect(source.contains(".accessibilityIdentifier(\"schedule.browser\")"))
        #expect(
            source.contains(#".accessibilityIdentifier("schedule.row.\(item.id.uuidString)")"#)
        )
    }

    @Test
    func testDetailLoadsThePointReadAndShowsTheFullInstruction() throws {
        let source = try source(at: "Veetbot/Views/ScheduleDetailView.swift")

        #expect(source.contains("await model.loadDetail(summary.id)"))
        #expect(source.contains("Text(record.revision.instruction)"))
        #expect(source.contains("await model.retryDetail(summary.id)"))
        #expect(source.contains(".accessibilityIdentifier(\"schedule.detail\")"))
    }

    @Test
    func testMacBrowserUsesTheSameResizablePresentationContractAsMemory() throws {
        let source = try source(at: "Veetbot/Views/ScheduleBrowserView.swift")

        #expect(source.contains(".background(ScheduleBrowserWindowResizeView())"))
        #expect(source.contains(".scheduleBrowserPresentationSizing()"))
        #expect(source.contains("if #available(macOS 15.0, *)"))
        #expect(source.contains("presentationSizing(.fitted)"))
    }

    @Test
    func testTheMacModalAttachesExplicitResizePersistence() throws {
        let source = try source(at: "Veetbot/Views/ScheduleBrowserView.swift")
        let normalizedSource = source
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")

        #expect(
            normalizedSource.contains(
                "resizePersistence = PopupWindowResizePersistence( window: window, key: ScheduleBrowserWindowConfiguration.storageKey )"
            )
        )
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(
            contentsOf: packageRoot.appendingPathComponent(relativePath),
            encoding: .utf8
        )
    }
}
