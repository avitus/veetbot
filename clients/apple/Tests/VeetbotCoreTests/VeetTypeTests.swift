import Foundation
import SwiftUI
import Testing

#if os(macOS)
import AppKit
#endif

@testable import VeetbotCore

/// Design-system type revision 2 (MagicPath skill veetbot-design-system-a2,
/// 19 Oct 2026): seven roles and nothing else, three weights, one spacing
/// scale, transcript-only face preference, and Mac-wide text sizing.
@Suite struct VeetTypeTests {
    // MARK: - Roles

    @Test
    func testMacRolesUseTheFixedRevisionTwoSizes() {
        let sizes = VeetType.Role.allCases.map { VeetType.pointSize($0, on: .mac) }
        #expect(VeetType.Role.allCases == [.display, .title, .body, .ui, .dense, .meta, .label])
        #expect(sizes == [24, 17, 15, 13, 13, 12, 11])
    }

    @Test
    func testTouchRolesFollowTheRevisionTwoTable() {
        #expect(VeetType.Role.allCases.map { VeetType.pointSize($0, on: .iPad) } == [26, 20, 17, 15, 15, 13, 12])
        #expect(VeetType.Role.allCases.map { VeetType.pointSize($0, on: .iPhone) } == [28, 20, 17, 17, 15, 13, 13])
    }

    @Test
    func testTouchRolesScaleWithTheMatchingDynamicTypeStyles() {
        let phone = VeetType.Role.allCases.map { VeetType.textStyle($0, on: .iPhone) }
        let pad = VeetType.Role.allCases.map { VeetType.textStyle($0, on: .iPad) }
        #expect(phone == [.title, .title3, .body, .body, .subheadline, .footnote, .footnote])
        #expect(pad == [.title, .title3, .body, .subheadline, .subheadline, .footnote, .caption])
    }

    @Test
    func testOnlyRegularMediumAndSemiboldExist() {
        #expect(VeetType.Weight.allCases == [.regular, .medium, .semibold])
        #expect(VeetType.defaultWeight(.label) == .semibold)
        #expect(VeetType.defaultWeight(.ui) == .regular)
    }

    @Test
    func testDisplayIsSerifAndEveryOtherRoleIgnoresTheFacePreference() {
        for style in AppFontStyle.allCases {
            #expect(VeetType.design(.display, fontStyle: style, inTranscript: true) == .serif)
            for role in VeetType.Role.allCases where role != .display && role != .body {
                #expect(VeetType.design(role, fontStyle: style, inTranscript: true) == .default)
            }
            #expect(VeetType.design(.body, fontStyle: style, inTranscript: false) == .default)
            #expect(VeetType.design(.body, fontStyle: style, inTranscript: true) == style.design)
        }
    }

    @Test
    func testTouchTextSizePreferenceScalesOnlyTranscriptBody() {
        #expect(VeetType.preferenceScale(.body, textSize: .large, inTranscript: true, platform: .iPhone) == 1.3)
        #expect(VeetType.preferenceScale(.body, textSize: .large, inTranscript: false, platform: .iPad) == nil)
        #expect(VeetType.preferenceScale(.ui, textSize: .large, inTranscript: true, platform: .iPhone) == nil)
        #expect(VeetType.preferenceScale(.dense, textSize: .compact, inTranscript: true, platform: .iPhone) == nil)
    }

    @Test
    func testMacPanelFontsFollowEveryTextSizeAndKeepTheirHierarchy() {
        for size in AppTextSize.allCases {
            let scale = size.fontScale ?? 1
            for role in VeetType.Role.allCases {
                let expected = Font.system(
                    size: VeetType.pointSize(role, on: .mac) * scale,
                    weight: VeetType.defaultWeight(role).fontWeight,
                    design: role == .display ? .serif : .default
                )
                #expect(VeetType.font(role, platform: .mac, fontStyle: .rounded, textSize: size) == expected)
            }
        }
    }

    @Test
    func testMacToolTextScalesAndStaysMonospaced() {
        for size in AppTextSize.allCases {
            #expect(VeetType.monoFont(platform: .mac, textSize: size) == .system(
                size: 12 * (size.fontScale ?? 1), design: .monospaced
            ))
            for platform in [VeetType.Platform.iPhone, .iPad] {
                #expect(VeetType.monoFont(platform: platform, textSize: size)
                    == VeetType.monoFont(platform: platform))
                #expect(VeetType.font(.ui, platform: platform, textSize: size)
                    == VeetType.font(.ui, platform: platform))
            }
        }
    }

    #if os(macOS)
    @Test @MainActor
    func testPanelTextModifiersReadTheAppearanceEnvironment() {
        func renderedSize(_ size: AppTextSize, mono: Bool) -> CGSize {
            let text = Text("Panel text")
            let view = mono ? AnyView(text.veetMono()) : AnyView(text.veetType(.ui))
            return NSHostingView(rootView: view.environment(\.appTextSize, size).fixedSize()).fittingSize
        }
        for mono in [false, true] {
            let standard = renderedSize(.system, mono: mono)
            let comfortable = renderedSize(.comfortable, mono: mono)
            let large = renderedSize(.large, mono: mono)
            #expect(comfortable.width > standard.width)
            #expect(comfortable.height > standard.height)
            #expect(large.width > comfortable.width)
            #expect(large.height > comfortable.height)
        }
    }
    #endif

    @Test
    func testTheFaceDefaultsToSystem() throws {
        let defaults = try #require(UserDefaults(suiteName: "veet-type-\(UUID().uuidString)"))
        #expect(AppearancePreferences(defaults: defaults).fontStyle == .system)
    }

    // MARK: - Spacing

    @Test
    func testSpacingScaleIsTheSevenStepScale() {
        #expect(VeetSpace.scale == [4, 8, 12, 16, 24, 32, 48])
        #expect([VeetSpace.s1, VeetSpace.s2, VeetSpace.s3, VeetSpace.s4, VeetSpace.s5, VeetSpace.s6, VeetSpace.s7] == VeetSpace.scale)
    }

    // MARK: - Source guards

    /// No view names a literal size, a raw text style or bold; it names a role.
    @Test
    func testViewsNameATypeRoleNeverALiteralFont() throws {
        let banned = [
            #"\.font\(\.system\(size:"#,
            #"\.appFont\("#,
            #"\.appDisplayFont\("#,
            #"\.appCodeFont\("#,
            #"\.font\(\.(largeTitle|title|title2|title3|headline|subheadline|body|callout|footnote|caption|caption2)\b"#,
            #"\.bold\(\)"#,
            #"fontWeight\(\.(bold|heavy|black)\)"#,
            #"weight: \.(bold|heavy|black)"#,
        ].map { try! NSRegularExpression(pattern: $0) }
        var offences: [String] = []
        for file in try viewFiles() where file.lastPathComponent != "VeetType.swift" {
            let lines = try String(contentsOf: file, encoding: .utf8).components(separatedBy: "\n")
            for (index, line) in lines.enumerated() where !line.trimmingCharacters(in: .whitespaces).hasPrefix("//") {
                let range = NSRange(line.startIndex..., in: line)
                if banned.contains(where: { $0.firstMatch(in: line, range: range) != nil }) {
                    offences.append("\(file.lastPathComponent):\(index + 1): \(line.trimmingCharacters(in: .whitespaces))")
                }
            }
        }
        #expect(offences.isEmpty, "Use .veetType(role): \n\(offences.joined(separator: "\n"))")
    }

    /// The Chat window's gaps come from the scale, never a literal.
    @Test
    func testChatViewsTakeEveryGapFromTheSpacingScale() throws {
        let literal = try NSRegularExpression(
            pattern: #"(\.padding\((\.[a-zA-Z]+, )?-?[1-9][0-9.]*\)|spacing: -?[1-9][0-9.]*\b)"#
        )
        var offences: [String] = []
        for name in [
            "ChatView.swift", "ConversationFolderViews.swift", "A2Controls.swift",
            "MarkdownContentView.swift", "MessageActions.swift", "SidebarRow.swift",
        ] {
            let lines = try source(at: "Veetbot/Views/\(name)").components(separatedBy: "\n")
            for (index, line) in lines.enumerated() {
                let range = NSRange(line.startIndex..., in: line)
                if literal.firstMatch(in: line, range: range) != nil {
                    offences.append("\(name):\(index + 1): \(line.trimmingCharacters(in: .whitespaces))")
                }
            }
        }
        #expect(offences.isEmpty, "Use VeetSpace: \n\(offences.joined(separator: "\n"))")
    }

    // MARK: - Sidebar anatomy

    @Test
    func testEverySidebarRowSharesOneRowViewWithAFixedIconColumn() throws {
        let row = try source(at: "Veetbot/Views/SidebarRow.swift")
        #expect(row.contains("struct SidebarRow"))
        #expect(row.contains("A2Metrics.sidebarIconColumn"))

        let root = try source(at: "Veetbot/Views/RootView.swift")
        let folders = try source(at: "Veetbot/Views/ConversationFolderViews.swift")
        // Needs you rows and chat rows in the sidebar; folders and schedules
        // through their header rows.
        #expect(root.contains("SidebarRow {"))
        #expect(root.contains("SidebarChatRow("))
        #expect(folders.components(separatedBy: "SidebarRow {").count == 3)
        #expect(!folders.contains("struct SidebarDisclosureHeader"))
    }

    @Test
    func testNewChatIsAComposeButtonBesideSearchAndNewFolderSitsOnItsLabel() throws {
        let root = try source(at: "Veetbot/Views/RootView.swift")
        #expect(!root.contains("PrimaryCapsuleButtonStyle(expands: true)"))
        #expect(root.contains("SidebarSearchField("))
        #expect(root.contains("ComposeButton {"))
        #expect(root.contains("SidebarSectionLabel(title: \"Folders\""))
        #expect(!root.contains("Label(\"New folder\", systemImage: \"folder.badge.plus\")"))
    }

    // MARK: - Transcript

    @Test
    func testTablesUseTheDenseRoleAndTheIPhoneScrollsInsteadOfWrapping() throws {
        let markdown = try source(at: "Veetbot/Views/MarkdownContentView.swift")
        #expect(markdown.contains(".veetType(.dense, weight: isHeader ? .semibold : .regular)"))
        #expect(markdown.contains("VeetType.Platform.current == .iPhone"))
        #expect(markdown.contains(".veetType(level <= 3 ? .title : .body"))
    }

    @Test
    func testMessageActionsShowOnHoverAndTheToolbarTitleWaitsForTheHeading() throws {
        let chat = try source(at: "Veetbot/Views/ChatView.swift")
        #expect(chat.contains(".onHover"))
        #expect(chat.contains("headingScrolledAway"))
        #expect(chat.contains(".transcriptBody()"))
        #expect(chat.contains("showsMessageActions"))
        // The iPhone keeps its bar: a long-press menu would take the gesture
        // text selection uses (ADR-0122, MessageActionsStructureTests).
        #expect(!chat.contains(".contextMenu"))
    }

    // MARK: - Helpers

    private var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }

    private func viewFiles() throws -> [URL] {
        let directory = packageRoot.appendingPathComponent("Veetbot/Views")
        return try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }

    private func source(at relativePath: String) throws -> String {
        try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
