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

    @Test
    func testModelsLoadOnAppearOnlyWhenConfigured() throws {
        let source = try connectionSettingsSource()
        let normalized = source.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")

        #expect(normalized.contains("@StateObject private var modelSettings = ModelSettingsViewModel()"))
        #expect(
            normalized.contains(
                "if model.isConfigured { Task { await model.refreshBrowserProfiles() } Task { await modelSettings.load() } }"
            )
        )
    }

    private func modelsSectionSource() throws -> Substring {
        let source = try connectionSettingsSource()
        let start = try #require(source.range(of: "private var modelsSection:"))
        let end = try #require(
            source.range(of: "private var smsIntegrationSection:", range: start.upperBound..<source.endIndex)
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
