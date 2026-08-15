import SwiftUI

@main
struct VeetbotApp: App {
    @StateObject private var model = ChatViewModel()
    @StateObject private var appearance = AppearancePreferences()

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
                .environmentObject(appearance)
                .appTypography(appearance)
                .tint(AppTheme.turquoise)
#if os(macOS)
                .frame(minWidth: 780, minHeight: 560)
#endif
        }
    }
}
