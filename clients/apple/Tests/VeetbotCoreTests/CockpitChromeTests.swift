import Foundation
import Testing

@testable import VeetbotCore

/// The native chrome around the Cockpit board follows "Mac — Cockpit · A2"
/// (MagicPath merrily-city-4157): the toolbar carries the title, the Live
/// pill and what needs the owner; the left pane lists Needs you, Widgets
/// with each one's age and This screen; both side columns resize; and the
/// board drops to three columns while a widget's details are open.
@MainActor
@Suite struct CockpitChromeTests {
    @Test
    func testAWidgetRowSaysHowFreshItIsAsTheDesignDoes() {
        #expect(CockpitDevice.freshnessText(.live, age: 2) == "now")
        #expect(CockpitDevice.freshnessText(.live, age: 6) == "6 s")
        #expect(CockpitDevice.freshnessText(.live, age: 120) == "2 min")
        #expect(CockpitDevice.freshnessText(.aging, age: 900) == "15 min ago")
        #expect(CockpitDevice.freshnessText(.stale, age: 47 * 60) == "Stale · 47 min")
        #expect(CockpitDevice.freshnessText(.error, age: 3 * 3600) == "Failed · 3 h")
        #expect(CockpitDevice.freshnessText(.pending, age: nil) == "Waiting")
        #expect(CockpitDevice.freshnessText(.live, age: 72 * 3600) == "3 d")
    }

    @Test
    func testThePillNamesTheStreamsState() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        #expect(CockpitDevice.pillText(.live, lastUpdated: now, now: now) == "Live")
        #expect(CockpitDevice.pillText(.reconnecting(attempt: 1), lastUpdated: now.addingTimeInterval(-180), now: now)
            == "Reconnecting · last update 3 min ago")
        #expect(CockpitDevice.pillText(.connecting, lastUpdated: nil, now: now) == "Connecting")
        #expect(CockpitDevice.pillText(.idle, lastUpdated: nil, now: now) == "Offline")
    }

    @Test
    func testTheSummarySaysWhatNeedsTheOwnerAndWhatIsStale() {
        #expect(CockpitDevice.summary(needYou: 2, stale: 1) == ("2 need you", " · 1 stale"))
        #expect(CockpitDevice.summary(needYou: 1, stale: 0) == ("1 needs you", nil))
        #expect(CockpitDevice.summary(needYou: 0, stale: 2) == (nil, "2 stale"))
        #expect(CockpitDevice.summary(needYou: 0, stale: 0) == (nil, nil))
    }

    @Test
    func testBothSideColumnsKeepTheOwnersWidthOnThisDevice() {
        let suite = "CockpitChromeTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let sidebar = MacColumnWidthStore.cockpitSidebar(defaults: defaults)
        let panel = MacColumnWidthStore.cockpitPanel(defaults: defaults)
        #expect(sidebar.width == 260)
        #expect(panel.width == 320)
        sidebar.remember(300)
        panel.remember(9_000)
        #expect(MacColumnWidthStore.cockpitSidebar(defaults: defaults).width == 300)
        #expect(MacColumnWidthStore.cockpitPanel(defaults: defaults).width == 440)
        sidebar.remember(10)
        #expect(sidebar.width == 200)
        sidebar.forget()
        #expect(sidebar.width == 260)
        #expect(sidebar.key == "veetbot.cockpit.sidebarWidth")
        #expect(panel.key == "veetbot.cockpit.panelWidth")
    }

    @Test
    func testTheBoardTakesThreeColumnsWhileDetailsAreOpen() {
        let snapshot = CockpitBoardTests.snapshot()
        let open = CockpitBoard.hostSnapshot(
            snapshot, deviceClass: .wide, theme: "light", platform: "mac",
            offsetMilliseconds: 0, excluded: [], selected: "weather", columns: 3
        )
        let closed = CockpitBoard.hostSnapshot(
            snapshot, deviceClass: .wide, theme: "light", platform: "mac",
            offsetMilliseconds: 0, excluded: [], selected: nil
        )
        guard case let .object(withPanel) = open, case let .object(without) = closed else {
            Issue.record("not objects"); return
        }
        #expect(withPanel["columns"] == .number(3))
        #expect(without["columns"] == nil, "Without a panel the board keeps its own column count")
    }

    /// The left pane and the toolbar carry the design's pieces; the controls
    /// the pane's foot used to hold move to the toolbar.
    @Test
    func testTheMacChromeCarriesTheDesignsPieces() throws {
        let cockpit = try source(at: "Veetbot/Views/CockpitModeView.swift")
        let root = try source(at: "Veetbot/Views/RootView.swift")
        #expect(cockpit.contains("SidebarSectionLabel(title: \"Needs you\")"))
        #expect(cockpit.contains("SidebarSectionLabel(title: \"Widgets\")"))
        #expect(cockpit.contains("SidebarSectionLabel(title: \"This screen\")"))
        #expect(cockpit.contains("identifier: \"cockpit.sidebar.resize\""))
        #expect(cockpit.contains("identifier: \"cockpit.panel.resize\""))
        #expect(!cockpit.contains("private var sidebarControls"), "Night watch and Arrange live in the toolbar")
        #expect(root.contains("CockpitToolbarTitle(model: coordinator.cockpit"))
        #expect(root.contains("CockpitAppearanceButton()"))
        #expect(root.contains("CockpitArrangeButton(model: coordinator.cockpit)"))
        #expect(root.contains("CockpitPanelToggle(model: coordinator.cockpit)"))
        for identifier in ["cockpit.memory", "cockpit.people", "cockpit.schedules", "cockpit.persona", "cockpit.settings"] {
            #expect(root.contains("\"\(identifier)\""), "\(identifier)")
        }
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
