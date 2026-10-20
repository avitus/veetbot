import Foundation
import Testing

@testable import VeetbotCore

/// The A2 Dock Persona, Settings and Connect surfaces: a serif title over
/// each page, settings as titled groups of hairline rows on surface cards,
/// fields on the surface colour behind a strong hairline, capsule buttons for
/// the actions, and every identifier and pinned line the journeys drive kept
/// in place.
@Suite struct PersonaSettingsConnectSurfaceStructureTests {
    @Test
    func testPersonaReadsAsAnA2Page() throws {
        let source = try source(at: "Veetbot/Views/PersonaEditorView.swift")

        #expect(source.contains(".veetType(.display)"))
        #expect(source.contains("struct PersonaNominationCard"))
        #expect(source.contains("AppTheme.amberLine"))
        #expect(source.contains("A2EmptyState("))
        #expect(source.contains(".primaryCapsule()"))
        #expect(source.contains("QuietCapsuleButtonStyle"))
        #expect(source.contains(".accessibilityIdentifier(\"persona.nomination.affirm\")"))
        #expect(source.contains(".accessibilityIdentifier(\"persona.nomination.decline\")"))
        #expect(source.contains(".accessibilityIdentifier(\"persona.entry\")"))
        #expect(!source.contains(".buttonStyle(.borderedProminent)"))
        #expect(!source.contains(".foregroundColor(.secondary)"))
    }

    @Test
    func testSettingsAndConnectWearTheA2System() throws {
        let source = try source(at: "Veetbot/Views/ConnectionSettingsView.swift")

        #expect(source.contains(".veetType(.display)"))
        #expect(source.contains("AppTheme.paper"))
        #expect(source.contains(".a2Card()"))
        #expect(source.contains(".a2Field("))
        #expect(source.contains("HairlineDivider()"))
        #expect(source.contains(".primaryCapsule()"))
        #expect(source.contains("SecondaryCapsuleButtonStyle"))
        #expect(source.contains("QuietCapsuleButtonStyle"))
        #expect(source.contains("VeetbotBrandMark("))
        #expect(source.contains("Connect this"))
        #expect(source.contains(".accessibilityIdentifier(\"website-access.url\")"))
        #expect(source.contains(".accessibilityIdentifier(\"task-scopes.add\")"))
        #expect(source.contains(".accessibilityIdentifier(\"sms-integration.enabled\")"))
        #expect(!source.contains("brandGradient"))
        #expect(!source.contains("Color.primary.opacity"))
        #expect(!source.contains(".foregroundColor(.secondary)"))
        #expect(!source.contains(".textFieldStyle(.roundedBorder)"))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
