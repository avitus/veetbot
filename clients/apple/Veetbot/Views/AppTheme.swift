import Foundation
import SwiftUI

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

/// One sRGB colour, channels 0…1.
public struct AppRGBA: Equatable, Sendable {
    public let red: Double
    public let green: Double
    public let blue: Double
    public let alpha: Double

    public init(red: Double, green: Double, blue: Double, alpha: Double = 1) {
        self.red = red
        self.green = green
        self.blue = blue
        self.alpha = alpha
    }

    /// `0xRRGGBB`, as the reference stylesheet writes it.
    public init(hex: UInt32, alpha: Double = 1) {
        self.init(
            red: Double((hex >> 16) & 0xFF) / 255,
            green: Double((hex >> 8) & 0xFF) / 255,
            blue: Double(hex & 0xFF) / 255,
            alpha: alpha
        )
    }
}

/// A semantic colour with its light value and its night-green dark value.
public struct AppColorToken: Equatable, Sendable {
    public let light: AppRGBA
    public let dark: AppRGBA

    public init(light: AppRGBA, dark: AppRGBA) {
        self.light = light
        self.dark = dark
    }

    /// Resolves with the view's appearance, so one token serves both modes.
    var color: Color {
        let light = self.light
        let dark = self.dark
        #if os(macOS)
        return Color(
            nsColor: NSColor(name: nil) { appearance in
                let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
                let value = isDark ? dark : light
                return NSColor(
                    srgbRed: value.red, green: value.green, blue: value.blue, alpha: value.alpha
                )
            }
        )
        #else
        return Color(
            uiColor: UIColor { traits in
                let value = traits.userInterfaceStyle == .dark ? dark : light
                return UIColor(
                    red: value.red, green: value.green, blue: value.blue, alpha: value.alpha
                )
            }
        )
        #endif
    }
}

/// The A2 Dock palette (MagicPath, October 2026): warm near-white paper, a
/// deep-green ink, one teal, and an amber reserved for what needs the owner.
/// Dark is the night-green palette from the same stylesheet.
public enum AppPalette {
    public static let paper = AppColorToken(light: AppRGBA(hex: 0xFCFAF6), dark: AppRGBA(hex: 0x0C211D))
    public static let paperDeep = AppColorToken(light: AppRGBA(hex: 0xF3F0E8), dark: AppRGBA(hex: 0x091A17))
    public static let surface = AppColorToken(light: AppRGBA(hex: 0xFFFFFF), dark: AppRGBA(hex: 0x14302A))
    public static let field = AppColorToken(
        light: AppRGBA(hex: 0x102923, alpha: 0.05), dark: AppRGBA(hex: 0xECE6D8, alpha: 0.08)
    )
    public static let ink = AppColorToken(light: AppRGBA(hex: 0x102923), dark: AppRGBA(hex: 0xECE6D8))
    public static let inkSoft = AppColorToken(light: AppRGBA(hex: 0x2F4640), dark: AppRGBA(hex: 0xBCCBC5))
    public static let inkFaint = AppColorToken(light: AppRGBA(hex: 0x566A64), dark: AppRGBA(hex: 0x95A8A1))
    public static let teal = AppColorToken(light: AppRGBA(hex: 0x0E8B78), dark: AppRGBA(hex: 0x36D5BB))
    public static let tealFill = AppColorToken(light: AppRGBA(hex: 0x0B7A69), dark: AppRGBA(hex: 0x36D5BB))
    public static let tealText = AppColorToken(light: AppRGBA(hex: 0x086657), dark: AppRGBA(hex: 0x5EE3C9))
    public static let tealWash = AppColorToken(
        light: AppRGBA(hex: 0x0E8B78, alpha: 0.10), dark: AppRGBA(hex: 0x36D5BB, alpha: 0.12)
    )
    public static let onTeal = AppColorToken(light: AppRGBA(hex: 0xFFFFFF), dark: AppRGBA(hex: 0x0C211D))
    public static let amber = AppColorToken(light: AppRGBA(hex: 0xE0A23A), dark: AppRGBA(hex: 0xF2A93B))
    public static let amberText = AppColorToken(light: AppRGBA(hex: 0x7A4A0E), dark: AppRGBA(hex: 0xF2A93B))
    public static let amberLine = AppColorToken(
        light: AppRGBA(hex: 0xC9801A, alpha: 0.55), dark: AppRGBA(hex: 0xF2A93B, alpha: 0.50)
    )
    public static let amberWash = AppColorToken(
        light: AppRGBA(hex: 0xF2A93B, alpha: 0.12), dark: AppRGBA(hex: 0xF2A93B, alpha: 0.10)
    )
    public static let red = AppColorToken(light: AppRGBA(hex: 0xB4452E), dark: AppRGBA(hex: 0xE07A62))
    public static let hairline = AppColorToken(
        light: AppRGBA(hex: 0x102923, alpha: 0.14), dark: AppRGBA(hex: 0xECE6D8, alpha: 0.10)
    )
    public static let hairlineStrong = AppColorToken(
        light: AppRGBA(hex: 0x102923, alpha: 0.28), dark: AppRGBA(hex: 0xECE6D8, alpha: 0.22)
    )
    public static let selected = AppColorToken(
        light: AppRGBA(hex: 0x102923, alpha: 0.09), dark: AppRGBA(hex: 0xECE6D8, alpha: 0.12)
    )
    /// The toast inverts the page: an ink ground with paper text, its Undo
    /// in the opposite mode's teal so it reads on that ground.
    public static let toastBackground = ink
    public static let toastText = paper
    public static let toastAction = AppColorToken(light: AppRGBA(hex: 0x5EE3C9), dark: AppRGBA(hex: 0x086657))
    public static let toastSoft = AppColorToken(light: AppRGBA(hex: 0xBCCBC5), dark: AppRGBA(hex: 0x2F4640))
}

/// The semantic colours every screen draws with, as the A2 reference uses
/// them: Chat, Email, Memory, People, Schedules, Persona, Settings and Connect.
enum AppTheme {
    /// The page: the transcript and every reading surface.
    static let paper = AppPalette.paper.color
    /// The sidebar and other secondary surfaces behind the page.
    static let paperDeep = AppPalette.paperDeep.color
    /// A card that sits on the page: an approval, a sheet.
    static let surface = AppPalette.surface.color
    /// A sunken input or segmented control.
    static let field = AppPalette.field.color
    static let ink = AppPalette.ink.color
    static let inkSoft = AppPalette.inkSoft.color
    static let inkFaint = AppPalette.inkFaint.color
    /// Marks and icons.
    static let teal = AppPalette.teal.color
    /// Filled buttons; `onTeal` is their text.
    static let tealFill = AppPalette.tealFill.color
    static let onTeal = AppPalette.onTeal.color
    /// Teal as text or a link.
    static let tealText = AppPalette.tealText.color
    /// The owner's message capsule and selected teal chips.
    static let tealWash = AppPalette.tealWash.color
    /// Attention: the dot beside an approval waiting.
    static let amber = AppPalette.amber.color
    /// Attention as text: "Waiting for your approval".
    static let amberText = AppPalette.amberText.color
    /// The border of a card that needs the owner.
    static let amberLine = AppPalette.amberLine.color
    static let amberWash = AppPalette.amberWash.color
    static let red = AppPalette.red.color
    static let hairline = AppPalette.hairline.color
    static let hairlineStrong = AppPalette.hairlineStrong.color
    /// The selected sidebar row.
    static let selected = AppPalette.selected.color
    static let toastBackground = AppPalette.toastBackground.color
    static let toastText = AppPalette.toastText.color
    static let toastAction = AppPalette.toastAction.color
    static let toastSoft = AppPalette.toastSoft.color

    static let brandGradient = LinearGradient(
        colors: [tealWash, amberWash],
        startPoint: .topLeading,
        endPoint: .bottomTrailing
    )
}

enum AppTextSize: String, CaseIterable, Identifiable {
    case system
    case compact
    case comfortable
    case large

    var id: String { rawValue }

    var label: String {
        switch self {
        case .system: return "System"
        case .compact: return "Compact"
        case .comfortable: return "Comfortable"
        case .large: return "Large"
        }
    }

    var fontScale: CGFloat? {
        switch self {
        case .system: return nil
        case .compact: return 0.9
        case .comfortable: return 1.12
        case .large: return 1.3
        }
    }
}

enum AppFontStyle: String, CaseIterable, Identifiable {
    case system
    case rounded
    case serif
    case monospaced

    var id: String { rawValue }

    var label: String {
        switch self {
        case .system: return "System"
        case .rounded: return "Rounded"
        case .serif: return "Serif"
        case .monospaced: return "Monospaced"
        }
    }

    var design: Font.Design {
        switch self {
        case .system: return .default
        case .rounded: return .rounded
        case .serif: return .serif
        case .monospaced: return .monospaced
        }
    }
}

/// The owner's light or dark choice; System follows the device.
enum AppThemePreference: String, CaseIterable, Identifiable {
    case system, light, dark
    var id: String { rawValue }

    var label: String {
        switch self {
        case .system: return "System"
        case .light: return "Light"
        case .dark: return "Dark"
        }
    }

    var colorScheme: ColorScheme? {
        switch self {
        case .system: return nil
        case .light: return .light
        case .dark: return .dark
        }
    }
}

/// Applies the theme to every window the app owns, sheets and the Mac
/// Settings window included; System hands the choice back to the device.
enum AppThemeApplier {
    @MainActor
    static func apply(_ theme: AppThemePreference) {
        #if os(macOS)
        switch theme {
        case .system: NSApp?.appearance = nil
        case .light: NSApp?.appearance = NSAppearance(named: .aqua)
        case .dark: NSApp?.appearance = NSAppearance(named: .darkAqua)
        }
        #else
        let style: UIUserInterfaceStyle
        switch theme {
        case .system: style = .unspecified
        case .light: style = .light
        case .dark: style = .dark
        }
        for case let scene as UIWindowScene in UIApplication.shared.connectedScenes {
            for window in scene.windows { window.overrideUserInterfaceStyle = style }
        }
        #endif
    }
}

final class AppearancePreferences: ObservableObject {
    private enum Key {
        static let textSize = "veetbot.appearance.textSize"
        static let fontStyle = "veetbot.appearance.fontStyle"
        static let theme = "veetbot.appearance.theme"
    }

    @Published var textSize: AppTextSize {
        didSet { defaults.set(textSize.rawValue, forKey: Key.textSize) }
    }

    @Published var theme: AppThemePreference {
        didSet { defaults.set(theme.rawValue, forKey: Key.theme) }
    }

    @Published var fontStyle: AppFontStyle {
        didSet { defaults.set(fontStyle.rawValue, forKey: Key.fontStyle) }
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        textSize = AppTextSize(rawValue: defaults.string(forKey: Key.textSize) ?? "") ?? .system
        theme = AppThemePreference(rawValue: defaults.string(forKey: Key.theme) ?? "") ?? .system
        // The rounded face stays the default: the system face runs about two
        // points wider per toolbar label, which folds the iPhone Subscriptions
        // toolbar into an overflow menu. The Email restyle may revisit this.
        fontStyle =
            AppFontStyle(rawValue: defaults.string(forKey: Key.fontStyle) ?? "") ?? .rounded
    }
}

private struct AppFontStyleKey: EnvironmentKey {
    static let defaultValue = AppFontStyle.rounded
}

private struct AppTextSizeKey: EnvironmentKey {
    static let defaultValue = AppTextSize.system
}

extension EnvironmentValues {
    var appFontStyle: AppFontStyle {
        get { self[AppFontStyleKey.self] }
        set { self[AppFontStyleKey.self] = newValue }
    }

    var appTextSize: AppTextSize {
        get { self[AppTextSizeKey.self] }
        set { self[AppTextSizeKey.self] = newValue }
    }
}

func appPointSize(for textStyle: Font.TextStyle, textSize: AppTextSize) -> CGFloat? {
    guard let scale = textSize.fontScale else { return nil }
    return textStyle.appBasePointSize * scale
}

private func resolvedAppFont(
    _ textStyle: Font.TextStyle,
    textSize: AppTextSize,
    design: Font.Design,
    weight: Font.Weight? = nil
) -> Font {
    var font: Font
    if let pointSize = appPointSize(for: textStyle, textSize: textSize) {
        font = .system(size: pointSize, design: design)
    } else {
        font = .system(textStyle, design: design)
    }
    if let weight {
        font = font.weight(weight)
    }
    return font
}

extension Font.TextStyle {
    fileprivate var appBasePointSize: CGFloat {
        #if os(macOS)
        switch self {
        case .largeTitle: return 26
        case .title: return 22
        case .title2: return 17
        case .title3: return 15
        case .headline, .body: return 13
        case .callout: return 12
        case .subheadline: return 11
        case .footnote, .caption, .caption2: return 10
        @unknown default: return 13
        }
        #else
        switch self {
        case .largeTitle: return 34
        case .title: return 28
        case .title2: return 22
        case .title3: return 20
        case .headline, .body: return 17
        case .callout: return 16
        case .subheadline: return 15
        case .footnote: return 13
        case .caption: return 12
        case .caption2: return 11
        @unknown default: return 17
        }
        #endif
    }
}

private struct AppTypographyModifier: ViewModifier {
    @ObservedObject var preferences: AppearancePreferences

    func body(content: Content) -> some View {
        content
            .environment(\.appFontStyle, preferences.fontStyle)
            .environment(\.appTextSize, preferences.textSize)
            .font(
                resolvedAppFont(
                    .body,
                    textSize: preferences.textSize,
                    design: preferences.fontStyle.design
                )
            )
    }
}

private struct AppFontModifier: ViewModifier {
    @Environment(\.appFontStyle) private var fontStyle
    @Environment(\.appTextSize) private var textSize
    let textStyle: Font.TextStyle
    let weight: Font.Weight?

    func body(content: Content) -> some View {
        content.font(
            resolvedAppFont(
                textStyle,
                textSize: textSize,
                design: fontStyle.design,
                weight: weight
            )
        )
    }
}

/// Titles: the serif display face at a medium weight, scaled with the text
/// size preference but never with the body font style, so a title reads the
/// same whichever body face the owner chose.
private struct AppDisplayFontModifier: ViewModifier {
    @Environment(\.appTextSize) private var textSize
    let textStyle: Font.TextStyle
    let weight: Font.Weight

    func body(content: Content) -> some View {
        content.font(
            resolvedAppFont(textStyle, textSize: textSize, design: .serif, weight: weight)
        )
    }
}

private struct AppCodeFontModifier: ViewModifier {
    @Environment(\.appTextSize) private var textSize
    let textStyle: Font.TextStyle

    func body(content: Content) -> some View {
        content.font(
            resolvedAppFont(textStyle, textSize: textSize, design: .monospaced)
        )
    }
}

extension View {
    func appTypography(_ preferences: AppearancePreferences) -> some View {
        modifier(AppTypographyModifier(preferences: preferences))
    }

    func appFont(_ style: Font.TextStyle, weight: Font.Weight? = nil) -> some View {
        modifier(AppFontModifier(textStyle: style, weight: weight))
    }

    func appCodeFont(_ style: Font.TextStyle = .caption) -> some View {
        modifier(AppCodeFontModifier(textStyle: style))
    }

    /// A display title in the serif face.
    func appDisplayFont(_ style: Font.TextStyle = .title2, weight: Font.Weight = .medium) -> some View {
        modifier(AppDisplayFontModifier(textStyle: style, weight: weight))
    }

    /// Frames sheet content so a Mac sheet opens at `idealWidth`: macOS
    /// honors a sheet's ideal size only with fitted presentation sizing.
    /// `macMinWidth` is the narrowest the sheet can be resized to. iOS keeps
    /// the compact `minWidth`.
    @ViewBuilder
    func sheetFrame(
        minWidth: CGFloat,
        macMinWidth: CGFloat? = nil,
        idealWidth: CGFloat,
        maxWidth: CGFloat? = nil,
        minHeight: CGFloat,
        idealHeight: CGFloat? = nil,
        maxHeight: CGFloat? = nil
    ) -> some View {
        #if os(macOS)
        let framed = frame(
            minWidth: macMinWidth ?? minWidth, idealWidth: idealWidth, maxWidth: maxWidth,
            minHeight: minHeight, idealHeight: idealHeight, maxHeight: maxHeight
        )
        framed.presentationSizing(.fitted)
        #else
        frame(
            minWidth: minWidth, idealWidth: idealWidth, maxWidth: maxWidth,
            minHeight: minHeight, idealHeight: idealHeight, maxHeight: maxHeight
        )
        #endif
    }
}

struct VeetbotBrandMark: View {
    var size: CGFloat = 38

    var body: some View {
        HStack(spacing: -size * 0.08) {
            Image(systemName: "chevron.left")
                .foregroundColor(AppTheme.teal)
            HStack(spacing: size * 0.08) {
                Circle()
                Circle()
            }
            .foregroundColor(AppTheme.ink)
            .frame(width: size * 0.28, height: size * 0.09)
            Image(systemName: "chevron.right")
                .foregroundColor(AppTheme.amber)
        }
        .font(.system(size: size, weight: .bold, design: .rounded))
        .accessibilityHidden(true)
    }
}

#if os(iOS)
extension AppFontStyle {
    var uiDesign: UIFontDescriptor.SystemDesign {
        switch self {
        case .system: return .default
        case .rounded: return .rounded
        case .serif: return .serif
        case .monospaced: return .monospaced
        }
    }
}

#elseif os(macOS)
extension AppFontStyle {
    var nsDesign: NSFontDescriptor.SystemDesign {
        switch self {
        case .system: return .default
        case .rounded: return .rounded
        case .serif: return .serif
        case .monospaced: return .monospaced
        }
    }
}

#endif
