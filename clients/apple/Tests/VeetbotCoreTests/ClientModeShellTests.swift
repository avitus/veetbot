import Foundation
import Testing

@testable import VeetbotCore

/// The mode shell over three modes (docs/plan/cockpit.md, "The mode shell"):
/// the switch reads Cockpit · Chat · Email, every mode-dependent surface
/// handles all three, and Cockpit is the first segment of each mode's left
/// pane (MagicPath merry-moon-8243, cozy-evening-6871, merrily-city-4157).
@Suite struct ClientModeShellTests {
    @Test
    func testTheSwitchReadsCockpitChatEmailInTheOwnersOrder() {
        #expect(ClientMode.allCases.map(\.rawValue) == ["cockpit", "chat", "email"])
        #expect(ClientMode.allCases.map(\.title) == ["Cockpit", "Chat", "Email"])
        #expect(ClientMode(rawValue: "cockpit")?.symbol == "gauge.with.needle")
        #expect(ClientMode(rawValue: "chat")?.symbol == "bubble.left.and.bubble.right")
        #expect(ClientMode(rawValue: "email")?.symbol == "envelope")
    }

    @Test
    func testTheModeHasItsOwnFile() throws {
        let mode = try source(at: "Veetbot/Models/ClientMode.swift")
        let email = try source(at: "Veetbot/Models/EmailModels.swift")
        #expect(mode.contains("public enum ClientMode"))
        #expect(!email.contains("enum ClientMode"))
    }

    /// No surface may silently drop a third mode.
    @Test
    func testEveryModeSurfaceHandlesAllThreeModes() throws {
        let root = try source(at: "Veetbot/Views/RootView.swift")
        let email = try source(at: "Veetbot/Views/EmailModeView.swift")
        let panel = try source(at: "Veetbot/Views/InspectorPanel.swift")
        let layout = try source(at: "Veetbot/ViewModels/InspectorModel.swift")
        #expect(!email.contains("if $0 == .chat { openChat() }"), "The mailbox segment must route every mode")
        #expect(email.contains("ModeSegment(selected: .email, choose: openMode)"))
        #expect(root.contains("CockpitModeView("))
        #expect(root.contains(".opacity(coordinator.mode == .cockpit ? 1 : 0)"))
        #expect(!panel.contains("mode == .chat ? \"Conversation panel\" : \"Thread panel\""))
        #expect(!layout.contains("mode == .chat ? 340 : 320"))
        #expect(!layout.contains("mode == .chat ? 1100 : 1300"))
    }

    /// On the Mac the switch is the first thing at the top of each mode's left pane.
    @Test
    func testTheMacSwitchLeadsEachModesLeftPane() throws {
        let root = try source(at: "Veetbot/Views/RootView.swift")
        let cockpit = try source(at: "Veetbot/Views/CockpitModeView.swift")
        #expect(root.contains("ModeSegment(selected: .chat, choose: chooseMode)"))
        #expect(cockpit.contains("ModeSegment(selected: .cockpit, choose: chooseMode)"))
        #expect(cockpit.contains(".accessibilityIdentifier(\"cockpit.unavailable\")"))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
