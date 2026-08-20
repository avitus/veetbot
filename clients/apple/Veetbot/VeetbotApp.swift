import SwiftUI

@main
struct VeetbotApp: App {
    @StateObject private var model: ChatViewModel
    @StateObject private var appearance = AppearancePreferences()

    init() {
        #if DEBUG && os(iOS)
        _model = StateObject(
            wrappedValue: ConversationNavigationUITestFixture.makeModelIfRequested()
                ?? ChatViewModel()
        )
        #else
        _model = StateObject(wrappedValue: ChatViewModel())
        #endif
    }

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
