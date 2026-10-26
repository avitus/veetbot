import Foundation
import Testing

@testable import VeetbotCore

@Suite struct ScheduleBrowserViewStructureTests {
    /// ADR-0225 part A: Pause or Resume, Stop behind a confirmation (never
    /// "Delete"), and Recent runs with Open report in Chat.
    @Test
    func testTheDetailOffersLifecycleControlsAndRecentRuns() throws {
        let detail = try String(
            contentsOf: URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
                .appendingPathComponent("Veetbot/Views/ScheduleDetailView.swift"),
            encoding: .utf8
        )
        #expect(detail.contains(".accessibilityIdentifier(\"schedule.pause\")"))
        #expect(detail.contains(".accessibilityIdentifier(\"schedule.resume\")"))
        #expect(detail.contains(".accessibilityIdentifier(\"schedule.stop.confirm\")"))
        #expect(detail.contains("A2SectionHeading(title: \"Recent runs\")"))
        #expect(detail.contains("Button(\"Open report in Chat\")"))
        #expect(!detail.contains("Button(\"Delete"))
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
    func testCadenceOnlyShowsATimeZoneWhenItDiffersFromTheDevice() {
        let matching = ScheduleCadenceView(
            kind: "DAILY",
            at: nil,
            localTime: "09:00:00",
            timezone: "America/Los_Angeles",
            weekdays: nil,
            daysOfMonth: nil,
            lastDay: nil,
            dates: nil
        )
        let exceptional = ScheduleCadenceView(
            kind: "WEEKLY",
            at: nil,
            localTime: "09:00:00",
            timezone: "America/New_York",
            weekdays: [1],
            daysOfMonth: nil,
            lastDay: nil,
            dates: nil
        )

        #expect(
            scheduleCadenceSummary(
                matching,
                deviceTimeZoneIdentifier: "America/Los_Angeles"
            ) == "Daily · 09:00:00"
        )
        #expect(
            scheduleCadenceSummary(
                exceptional,
                deviceTimeZoneIdentifier: "America/Los_Angeles"
            ) == "Weekly · Mon · 09:00:00 · America/New_York"
        )
    }

    /// ADR-0254: a feed-keeping schedule's interval reads in minutes or hours.
    @Test
    func testAnIntervalCadenceReadsAsHowOftenItFires() throws {
        func summary(_ minutes: Int) throws -> String {
            let cadence = try JSONDecoder.server.decode(
                ScheduleCadenceView.self,
                from: Data(#"{"kind": "INTERVAL", "every_minutes": \#(minutes), "timezone": "UTC"}"#.utf8)
            )
            return scheduleCadenceSummary(cadence, deviceTimeZoneIdentifier: "UTC")
        }
        #expect(try summary(1) == "Every minute")
        #expect(try summary(15) == "Every 15 min")
        #expect(try summary(60) == "Every hour")
        #expect(try summary(90) == "Every 90 min")
        #expect(try summary(120) == "Every 2 h")
        #expect(try summary(1440) == "Every day")
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
        // Fitted sizing is unconditional at the macOS floor (ADR-0211).
        #expect(!source.contains("#available(macOS"))
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
