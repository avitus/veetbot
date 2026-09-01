import Foundation
import Testing

@testable import VeetbotCore

/// Source-parsing structure pins for `PersonaEditorView`, matching the
/// pattern `MemoryBrowserViewStructureTests` uses: this package has no
/// view-inspection dependency, so identifier placement is pinned at the
/// source level where a future edit cannot silently move it.
@Suite struct PersonaEditorViewStructureTests {
    @Test
    func testTheEditorRootCarriesTheIdentifierOnTheNavigationResult() throws {
        let source = try personaEditorViewSource()

        #expect(
            source.contains(
                "}\n        .accessibilityIdentifier(\"persona.editor\")\n        .task { await model.load() }"
            ),
            "persona.editor must be attached to the NavigationView result so XCUITest finds the sheet, not a nested content chain"
        )
    }

    @Test
    func testTheActionButtonsCarryStableIdentifiers() throws {
        let source = try personaEditorViewSource()

        for identifier in [
            "persona.save",
            "persona.add-entry",
            "persona.nomination.affirm",
            "persona.nomination.decline",
            "persona.conflict.resolve",
            "persona.conflict.use-server",
        ] {
            #expect(
                source.contains(".accessibilityIdentifier(\"\(identifier)\")"),
                "\(identifier) anchors the UI tests and must not be renamed silently"
            )
        }
    }

    @Test
    func testTheMacModalCanGrowInBothDimensions() throws {
        let source = try personaEditorViewSource()
        let normalized = source.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")

        #expect(normalized.contains("maxWidth: .infinity"))
        #expect(normalized.contains("maxHeight: .infinity"))
    }

    private func personaEditorViewSource() throws -> String {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Veetbot/Views/PersonaEditorView.swift")
        return try String(contentsOf: url, encoding: .utf8)
    }
}
