import SwiftUI

#if os(iOS)
import UIKit
#endif

/// Design-system type revision 2 (MagicPath skill veetbot-design-system-a2).
/// Seven roles and nothing else: a view names a role, never a point size, a
/// raw text style or a family. The Mac uses fixed sizes; the iPhone and iPad
/// map each role to a Dynamic Type text style. Display is serif; every other
/// role uses the system face. The owner's Appearance face and text size reach
/// transcript body text only (`.transcriptBody()`), never chrome, sidebars,
/// tables or controls. `VeetTypeTests` holds the table and fails the build of
/// any view that sets a font outside this file.
enum VeetType {
    enum Role: CaseIterable {
        /// Conversation and page titles, serif.
        case display
        /// Headings inside content and sheets.
        case title
        /// Transcript prose and the composer.
        case body
        /// Sidebar rows, buttons, fields, segmented controls.
        case ui
        /// Tables and tabular data.
        case dense
        /// Counts, times, tool lines, help text.
        case meta
        /// Sidebar and list section labels.
        case label
    }

    /// 400, 500 and 600. Bold is never used, `strong` included.
    enum Weight: CaseIterable {
        case regular, medium, semibold

        var fontWeight: Font.Weight {
            switch self {
            case .regular: return .regular
            case .medium: return .medium
            case .semibold: return .semibold
            }
        }
    }

    enum Platform {
        case mac, iPad, iPhone

        static var current: Platform {
            #if os(macOS)
            return .mac
            #else
            return UIDevice.current.userInterfaceIdiom == .pad ? .iPad : .iPhone
            #endif
        }
    }

    static func pointSize(_ role: Role, on platform: Platform) -> CGFloat {
        switch (role, platform) {
        case (.display, .mac): return 24
        case (.display, .iPad): return 26
        case (.display, .iPhone): return 28
        case (.title, .mac): return 17
        case (.title, _): return 20
        case (.body, .mac): return 15
        case (.body, _): return 17
        case (.ui, .mac): return 13
        case (.ui, .iPad): return 15
        case (.ui, .iPhone): return 17
        case (.dense, .mac): return 13
        case (.dense, _): return 15
        case (.meta, .mac): return 12
        case (.meta, _): return 13
        case (.label, .mac): return 11
        case (.label, .iPad): return 12
        case (.label, .iPhone): return 13
        }
    }

    static func lineHeight(_ role: Role, on platform: Platform) -> CGFloat {
        switch (role, platform) {
        case (.display, .mac): return 30
        case (.display, .iPad): return 32
        case (.display, .iPhone): return 34
        case (.title, .mac): return 22
        case (.title, _): return 25
        case (.body, .mac): return 23
        case (.body, _): return 25
        case (.ui, .mac), (.dense, .mac): return 18
        case (.ui, .iPad), (.dense, _): return 20
        case (.ui, .iPhone): return 22
        case (.meta, .mac): return 16
        case (.meta, _): return 18
        case (.label, .mac): return 14
        case (.label, .iPad): return 16
        case (.label, .iPhone): return 18
        }
    }

    /// The Dynamic Type style a touch role scales with.
    static func textStyle(_ role: Role, on platform: Platform) -> Font.TextStyle {
        switch role {
        case .display: return .title
        case .title: return .title3
        case .body: return .body
        case .ui: return platform == .iPad ? .subheadline : .body
        case .dense: return .subheadline
        case .meta: return .footnote
        case .label: return platform == .iPad ? .caption : .footnote
        }
    }

    static func defaultWeight(_ role: Role) -> Weight {
        switch role {
        case .title, .label: return .semibold
        default: return .regular
        }
    }

    static func design(_ role: Role, fontStyle: AppFontStyle, inTranscript: Bool) -> Font.Design {
        switch role {
        case .display: return .serif
        case .body where inTranscript: return fontStyle.design
        default: return .default
        }
    }

    static func preferenceScale(_ role: Role, textSize: AppTextSize, inTranscript: Bool) -> CGFloat? {
        guard role == .body, inTranscript else { return nil }
        return textSize.fontScale
    }

    /// The font for a role, for `Text` that is concatenated or handed to a
    /// view that takes a `Font`. Views otherwise call `.veetType(_:)`.
    static func font(
        _ role: Role,
        weight: Weight? = nil,
        platform: Platform = .current,
        fontStyle: AppFontStyle = .system,
        textSize: AppTextSize = .system,
        inTranscript: Bool = false,
        dynamicTypeSize: DynamicTypeSize = .large
    ) -> Font {
        let weight = (weight ?? defaultWeight(role)).fontWeight
        let design = design(role, fontStyle: fontStyle, inTranscript: inTranscript)
        let size = pointSize(role, on: platform)
        if let scale = preferenceScale(role, textSize: textSize, inTranscript: inTranscript) {
            return .system(size: size * scale, weight: weight, design: design)
        }
        switch platform {
        case .mac:
            return .system(size: size, weight: weight, design: design)
        case .iPad where role == .display:
            // No text style is 26 pt; scale the iPad display size as Title does.
            return .system(size: scaled(size, relativeTo: .title, dynamicTypeSize), weight: weight, design: design)
        case .iPad, .iPhone:
            return .system(textStyle(role, on: platform), design: design, weight: weight)
        }
    }

    /// SF Mono at a role's size: the work ledger and code.
    static func monoFont(_ role: Role = .meta, platform: Platform = .current) -> Font {
        switch platform {
        case .mac: return .system(size: pointSize(role, on: .mac), design: .monospaced)
        case .iPad, .iPhone: return .system(textStyle(role, on: platform), design: .monospaced)
        }
    }

    /// Extra leading that brings a role's natural line up to its line height.
    static func lineSpacing(_ role: Role, on platform: Platform) -> CGFloat {
        max(0, (lineHeight(role, on: platform) - pointSize(role, on: platform) * 1.2).rounded())
    }

    /// The brand glyph artwork, which is a drawing rather than text.
    static func brandMark(size: CGFloat) -> Font {
        .system(size: size, weight: .bold, design: .rounded)
    }

    private static func scaled(_ size: CGFloat, relativeTo style: Font.TextStyle, _ typeSize: DynamicTypeSize) -> CGFloat {
        #if os(iOS)
        let traits = UITraitCollection(preferredContentSizeCategory: UIContentSizeCategory(typeSize))
        return UIFontMetrics(forTextStyle: .title1).scaledValue(for: size, compatibleWith: traits)
        #else
        return size
        #endif
    }
}

/// Icon sizes keyed to the row's icon size (15 Mac, 18 iPad, 20 iPhone).
enum VeetIcon {
    /// Chevrons and other marks inside a row: three points under the row icon.
    case small
    /// A row's own icon.
    case row
    /// Toolbar and composer icons.
    case regular
    /// The compose button.
    case large
    /// The symbol above an empty state.
    case emptyState

    static func base(on platform: VeetType.Platform) -> CGFloat {
        switch platform {
        case .mac: return 15
        case .iPad: return 18
        case .iPhone: return 20
        }
    }

    func pointSize(on platform: VeetType.Platform = .current) -> CGFloat {
        let base = Self.base(on: platform)
        switch self {
        case .small: return base - 3
        case .row: return base - 1
        case .regular: return base
        case .large: return base + 1
        case .emptyState: return platform == .mac ? 28 : 32
        }
    }
}

/// The one spacing scale: 8 inside a group, 12 between paragraphs, 16 between
/// sidebar sections, 24 between turns and before a heading, 32 transcript side
/// padding, 48 above an empty state. Nothing in between.
enum VeetSpace {
    static let s1: CGFloat = 4
    static let s2: CGFloat = 8
    static let s3: CGFloat = 12
    static let s4: CGFloat = 16
    static let s5: CGFloat = 24
    static let s6: CGFloat = 32
    static let s7: CGFloat = 48

    static let scale: [CGFloat] = [s1, s2, s3, s4, s5, s6, s7]
}

private struct VeetTranscriptKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    /// Inside the transcript the owner's face and size reach the body role.
    var veetTranscript: Bool {
        get { self[VeetTranscriptKey.self] }
        set { self[VeetTranscriptKey.self] = newValue }
    }
}

private struct VeetTypeModifier: ViewModifier {
    @Environment(\.appFontStyle) private var fontStyle
    @Environment(\.appTextSize) private var textSize
    @Environment(\.veetTranscript) private var inTranscript
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    let role: VeetType.Role
    let weight: VeetType.Weight?

    func body(content: Content) -> some View {
        let platform = VeetType.Platform.current
        content
            .font(
                VeetType.font(
                    role, weight: weight, platform: platform, fontStyle: fontStyle,
                    textSize: textSize, inTranscript: inTranscript, dynamicTypeSize: dynamicTypeSize
                )
            )
            .lineSpacing(VeetType.lineSpacing(role, on: platform))
    }
}

private struct VeetIconModifier: ViewModifier {
    @ScaledMetric(relativeTo: .body) private var scale: CGFloat = 1
    let icon: VeetIcon
    let weight: VeetType.Weight

    func body(content: Content) -> some View {
        #if os(macOS)
        content.font(.system(size: icon.pointSize(), weight: weight.fontWeight))
        #else
        content.font(.system(size: icon.pointSize() * scale, weight: weight.fontWeight))
        #endif
    }
}

extension View {
    /// Sets text in a design-system role.
    func veetType(_ role: VeetType.Role, weight: VeetType.Weight? = nil) -> some View {
        modifier(VeetTypeModifier(role: role, weight: weight))
    }

    /// SF Mono at a role's size, for the work ledger and code.
    func veetMono(_ role: VeetType.Role = .meta) -> some View {
        font(VeetType.monoFont(role))
    }

    /// Sizes an SF Symbol to the platform's icon scale.
    func veetIcon(_ icon: VeetIcon, weight: VeetType.Weight = .regular) -> some View {
        modifier(VeetIconModifier(icon: icon, weight: weight))
    }

    /// Marks transcript prose: the body role here follows the owner's face
    /// and text size, and text without a role of its own reads as body.
    func transcriptBody() -> some View {
        // The environment sits outside the role so the role reads it.
        veetType(.body).environment(\.veetTranscript, true)
    }
}
