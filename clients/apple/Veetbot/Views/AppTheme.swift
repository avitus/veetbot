import Foundation
import SwiftUI

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

enum AppTheme {
    static let turquoise = Color(
        red: 0,
        green: 112.0 / 255.0,
        blue: 109.0 / 255.0
    )
    static let orange = Color(red: 167.0 / 255.0, green: 62.0 / 255.0, blue: 0)
    static let ink = Color(
        red: 13.0 / 255.0,
        green: 23.0 / 255.0,
        blue: 42.0 / 255.0
    )

    static let brandGradient = LinearGradient(
        colors: [turquoise.opacity(0.18), orange.opacity(0.14)],
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

final class AppearancePreferences: ObservableObject {
    private enum Key {
        static let textSize = "veetbot.appearance.textSize"
        static let fontStyle = "veetbot.appearance.fontStyle"
    }

    @Published var textSize: AppTextSize {
        didSet { defaults.set(textSize.rawValue, forKey: Key.textSize) }
    }

    @Published var fontStyle: AppFontStyle {
        didSet { defaults.set(fontStyle.rawValue, forKey: Key.fontStyle) }
    }

    private let defaults: UserDefaults

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        textSize = AppTextSize(rawValue: defaults.string(forKey: Key.textSize) ?? "") ?? .system
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
}

struct VeetbotBrandMark: View {
    var size: CGFloat = 38
    @Environment(\.colorScheme) private var colorScheme

    var body: some View {
        HStack(spacing: -size * 0.08) {
            Image(systemName: "chevron.left")
                .foregroundColor(AppTheme.turquoise)
            HStack(spacing: size * 0.08) {
                Circle()
                Circle()
            }
            .foregroundColor(colorScheme == .dark ? .white.opacity(0.9) : AppTheme.ink)
            .frame(width: size * 0.28, height: size * 0.09)
            Image(systemName: "chevron.right")
                .foregroundColor(AppTheme.orange)
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
