import SwiftUI

@main
struct VeetbotApp: App {
    @StateObject private var model: ChatViewModel
    @StateObject private var appearance = AppearancePreferences()
    @StateObject private var smsIntegration = SmsIntegrationPreferences()
    #if os(iOS)
    @UIApplicationDelegateAdaptor(NotificationApplicationDelegate.self)
    private var notificationDelegate
    #elseif os(macOS)
    @NSApplicationDelegateAdaptor(NotificationApplicationDelegate.self)
    private var notificationDelegate
    #endif

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
        // AppKit derives window and split-view autosave keys from the content type.
        // Keep WindowGroup's child as a stable name rather than an inline modifier chain.
        WindowGroup {
            VeetbotSceneRoot(
                model: model,
                appearance: appearance,
                smsIntegration: smsIntegration
            )
            .onAppear {
                notificationDelegate.attach(to: model, smsPreferences: smsIntegration)
            }
        }
    }
}
