import Foundation
import Testing

@testable import VeetbotCore

/// Source-parsing structure pins for `PersonaEditorView`, matching the
/// pattern `MemoryBrowserViewStructureTests` uses: this package has no
/// view-inspection dependency, so identifier placement is pinned at the
/// source level where a future edit cannot silently move it.
@Suite struct PersonaEditorViewStructureTests {
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
            // ADR-0226.
            "persona.voice",
            "persona.sample",
            "persona.entry.enabled",
        ] {
            #expect(
                source.contains(".accessibilityIdentifier(\"\(identifier)\")"),
                "\(identifier) anchors the UI tests and must not be renamed silently"
            )
        }
    }

    /// ADR-0226: the tone sections appear only for a server that has them,
    /// and Boundaries lists only what the server says is enforced.
    @Test
    func testToneAndBoundariesFollowTheServer() throws {
        let source = try personaEditorViewSource()

        #expect(source.split(whereSeparator: { $0.isWhitespace }).joined(separator: " ")
            .contains("if model.toneSupported { voiceSection"))
        #expect(source.contains(".accessibilityIdentifier(\"persona.trait.\\(trait)\")"))
        #expect(source.contains("if !model.boundaries.isEmpty"))
        #expect(source.contains("Turn one off to stop Veetbot treating it as a standing instruction."))
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
