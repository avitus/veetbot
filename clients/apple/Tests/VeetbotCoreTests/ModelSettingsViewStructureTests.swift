import Foundation
import Testing

@testable import VeetbotCore

/// Source-level pins for the Models card in `ConnectionSettingsView`, matching
/// `PersonaEditorViewStructureTests`: this package has no view-inspection
/// dependency, so identifiers, copy, and the load gate are pinned where a
/// future edit cannot silently move them.
@Suite struct ModelSettingsViewStructureTests {
    @Test
    func testThePickersAndStatusCarryStableIdentifiers() throws {
        let section = try modelsSectionSource()

        for identifier in [
            "settings.models.chat.model",
            "settings.models.chat.effort",
            "settings.models.memory.model",
            "settings.models.memory.effort",
            "settings.models.status",
        ] {
            #expect(
                section.contains(".accessibilityIdentifier(\"\(identifier)\")"),
                "\(identifier) anchors the UI tests and must not be renamed silently"
            )
        }
    }

    @Test
    func testTheCardExplainsWhenEachChoiceApplies() throws {
        let section = try modelsSectionSource()

        #expect(section.contains("\"Chat\""))
        #expect(section.contains("\"Memory\""))
        #expect(
            section.contains(
                "\"A model change applies to new chats. Reasoning applies from your next message.\""
            )
        )
        #expect(
            section.contains(
                "\"Only combinations that passed Veetbot's memory evaluation are offered.\""
            )
        )
    }

    @Test
    func testThePickersLockWhileASaveIsInFlight() throws {
        let section = try modelsSectionSource()
        let normalized = section.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")

        #expect(normalized.contains(".disabled(!modelSettings.isEditable)"))
    }

    /// The card owns its view model and loads it when it appears. The
    /// Settings view must not own it: every published change would re-render
    /// the whole lazy stack, and on an iPad sheet that loses a half-typed
    /// website-access entry (testWebsiteAccessCreatesARecoverableBrowserHandoff).
    @Test
    func testTheCardAloneOwnsAndLoadsItsSettingsWhenConfigured() throws {
        let source = try connectionSettingsSource()
        let card = try modelsSectionSource()
        let normalized = card.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
        let settingsView = try #require(source.range(of: "private struct ModelSettingsCard"))

        #expect(normalized.contains("@StateObject private var modelSettings = ModelSettingsViewModel()"))
        #expect(
            normalized.contains(
                ".onAppear { if isConfigured { Task { await modelSettings.load() } } }"
            )
        )
        #expect(!source[..<settingsView.lowerBound].contains("ModelSettingsViewModel()"))
    }

    private func modelsSectionSource() throws -> Substring {
        let source = try connectionSettingsSource()
        let start = try #require(source.range(of: "private struct ModelSettingsCard"))
        let end = try #require(
            source.range(of: "private struct SettingsCard", range: start.upperBound..<source.endIndex)
        )
        return source[start.lowerBound..<end.lowerBound]
    }

    private func connectionSettingsSource() throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Veetbot/Views/ConnectionSettingsView.swift")
        return try String(contentsOf: url, encoding: .utf8)
    }
}
