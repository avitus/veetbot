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

    var dynamicTypeSize: DynamicTypeSize? {
        switch self {
        case .system: return nil
        case .compact: return .medium
        case .comfortable: return .xLarge
        case .large: return .xxLarge
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

extension EnvironmentValues {
    var appFontStyle: AppFontStyle {
        get { self[AppFontStyleKey.self] }
        set { self[AppFontStyleKey.self] = newValue }
    }
}

private struct AppTypographyModifier: ViewModifier {
    @ObservedObject var preferences: AppearancePreferences

    @ViewBuilder
    func body(content: Content) -> some View {
        let styled = content
            .environment(\.appFontStyle, preferences.fontStyle)
            .font(.system(.body, design: preferences.fontStyle.design))

        if let size = preferences.textSize.dynamicTypeSize {
            styled.dynamicTypeSize(size)
        } else {
            styled
        }
    }
}

private struct AppFontModifier: ViewModifier {
    @Environment(\.appFontStyle) private var fontStyle
    let textStyle: Font.TextStyle
    let weight: Font.Weight?

    @ViewBuilder
    func body(content: Content) -> some View {
        if let weight {
            content.font(.system(textStyle, design: fontStyle.design).weight(weight))
        } else {
            content.font(.system(textStyle, design: fontStyle.design))
        }
    }
}

private struct AppCodeFontModifier: ViewModifier {
    let textStyle: Font.TextStyle

    func body(content: Content) -> some View {
        content.font(.system(textStyle, design: .monospaced))
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

extension DynamicTypeSize {
    var uiContentSizeCategory: UIContentSizeCategory {
        switch self {
        case .xSmall: return .extraSmall
        case .small: return .small
        case .medium: return .medium
        case .large: return .large
        case .xLarge: return .extraLarge
        case .xxLarge: return .extraExtraLarge
        case .xxxLarge: return .extraExtraExtraLarge
        case .accessibility1: return .accessibilityMedium
        case .accessibility2: return .accessibilityLarge
        case .accessibility3: return .accessibilityExtraLarge
        case .accessibility4: return .accessibilityExtraExtraLarge
        case .accessibility5: return .accessibilityExtraExtraExtraLarge
        @unknown default: return .large
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

extension DynamicTypeSize {
    var appScaleFactor: CGFloat {
        switch self {
        case .xSmall: return 0.82
        case .small: return 0.88
        case .medium: return 0.94
        case .large: return 1
        case .xLarge: return 1.12
        case .xxLarge: return 1.24
        case .xxxLarge: return 1.36
        case .accessibility1: return 1.6
        case .accessibility2: return 1.84
        case .accessibility3: return 2.08
        case .accessibility4: return 2.32
        case .accessibility5: return 2.56
        @unknown default: return 1
        }
    }
}
#endif
