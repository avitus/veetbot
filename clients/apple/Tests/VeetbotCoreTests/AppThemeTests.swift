import Foundation
import Testing

@testable import VeetbotCore

/// The A2 Dock palette (MagicPath "Mac — Chat · A2 Dock"): every token carries
/// the light and the night-green dark value from the reference stylesheet.
@Suite struct AppThemeTests {
    @Test
    func testPaperInkAndTealTokensMatchTheReferenceStylesheet() {
        #expect(AppPalette.paper.light == AppRGBA(hex: 0xFCFAF6))
        #expect(AppPalette.paper.dark == AppRGBA(hex: 0x0C211D))
        #expect(AppPalette.paperDeep.light == AppRGBA(hex: 0xF3F0E8))
        #expect(AppPalette.paperDeep.dark == AppRGBA(hex: 0x091A17))
        #expect(AppPalette.surface.light == AppRGBA(hex: 0xFFFFFF))
        #expect(AppPalette.surface.dark == AppRGBA(hex: 0x14302A))
        #expect(AppPalette.ink.light == AppRGBA(hex: 0x102923))
        #expect(AppPalette.ink.dark == AppRGBA(hex: 0xECE6D8))
        #expect(AppPalette.inkSoft.light == AppRGBA(hex: 0x2F4640))
        #expect(AppPalette.inkSoft.dark == AppRGBA(hex: 0xBCCBC5))
        #expect(AppPalette.inkFaint.light == AppRGBA(hex: 0x566A64))
        #expect(AppPalette.inkFaint.dark == AppRGBA(hex: 0x95A8A1))
        #expect(AppPalette.teal.light == AppRGBA(hex: 0x0E8B78))
        #expect(AppPalette.teal.dark == AppRGBA(hex: 0x36D5BB))
        #expect(AppPalette.tealFill.light == AppRGBA(hex: 0x0B7A69))
        #expect(AppPalette.tealFill.dark == AppRGBA(hex: 0x36D5BB))
        #expect(AppPalette.tealText.light == AppRGBA(hex: 0x086657))
        #expect(AppPalette.tealText.dark == AppRGBA(hex: 0x5EE3C9))
        #expect(AppPalette.tealWash.light == AppRGBA(hex: 0x0E8B78, alpha: 0.10))
        #expect(AppPalette.tealWash.dark == AppRGBA(hex: 0x36D5BB, alpha: 0.12))
        #expect(AppPalette.onTeal.light == AppRGBA(hex: 0xFFFFFF))
        #expect(AppPalette.onTeal.dark == AppRGBA(hex: 0x0C211D))
    }

    @Test
    func testAttentionAndHairlineTokensMatchTheReferenceStylesheet() {
        #expect(AppPalette.amber.light == AppRGBA(hex: 0xE0A23A))
        #expect(AppPalette.amber.dark == AppRGBA(hex: 0xF2A93B))
        #expect(AppPalette.amberText.light == AppRGBA(hex: 0x7A4A0E))
        #expect(AppPalette.amberText.dark == AppRGBA(hex: 0xF2A93B))
        #expect(AppPalette.amberLine.light == AppRGBA(hex: 0xC9801A, alpha: 0.55))
        #expect(AppPalette.amberLine.dark == AppRGBA(hex: 0xF2A93B, alpha: 0.50))
        #expect(AppPalette.amberWash.light == AppRGBA(hex: 0xF2A93B, alpha: 0.12))
        #expect(AppPalette.amberWash.dark == AppRGBA(hex: 0xF2A93B, alpha: 0.10))
        #expect(AppPalette.red.light == AppRGBA(hex: 0xB4452E))
        #expect(AppPalette.red.dark == AppRGBA(hex: 0xE07A62))
        #expect(AppPalette.hairline.light == AppRGBA(hex: 0x102923, alpha: 0.14))
        #expect(AppPalette.hairline.dark == AppRGBA(hex: 0xECE6D8, alpha: 0.10))
        #expect(AppPalette.hairlineStrong.light == AppRGBA(hex: 0x102923, alpha: 0.28))
        #expect(AppPalette.hairlineStrong.dark == AppRGBA(hex: 0xECE6D8, alpha: 0.22))
        #expect(AppPalette.field.light == AppRGBA(hex: 0x102923, alpha: 0.05))
        #expect(AppPalette.field.dark == AppRGBA(hex: 0xECE6D8, alpha: 0.08))
        #expect(AppPalette.selected.light == AppRGBA(hex: 0x102923, alpha: 0.09))
        #expect(AppPalette.selected.dark == AppRGBA(hex: 0xECE6D8, alpha: 0.12))
    }

    @Test
    func testHexInitialiserSplitsChannels() {
        let value = AppRGBA(hex: 0x102923, alpha: 0.5)
        #expect(abs(value.red - 16.0 / 255.0) < 0.0001)
        #expect(abs(value.green - 41.0 / 255.0) < 0.0001)
        #expect(abs(value.blue - 35.0 / 255.0) < 0.0001)
        #expect(value.alpha == 0.5)
    }

    /// Titles use the serif display face; body text follows the owner's font
    /// style preference and the monospace ledger follows the text size.
    @Test
    func testDisplayFontUsesTheSerifDesignAndTheTextSizePreference() throws {
        let source = try source(at: "Veetbot/Views/AppTheme.swift")
        #expect(source.contains("func appDisplayFont("))
        #expect(source.contains("design: .serif"))
        #expect(!source.contains("static let turquoise"))
        #expect(!source.contains("static let orange"))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
