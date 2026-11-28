import SwiftUI

@main
struct VeetbotApp: App {
    @StateObject private var model: ChatViewModel
    @StateObject private var appearance: AppearancePreferences
    @StateObject private var smsIntegration = SmsIntegrationPreferences()
    @StateObject private var folderSidebar: FolderSidebarPreferences
    #if os(iOS)
    @UIApplicationDelegateAdaptor(NotificationApplicationDelegate.self)
    private var notificationDelegate
    #elseif os(macOS)
    @NSApplicationDelegateAdaptor(NotificationApplicationDelegate.self)
    private var notificationDelegate
    #endif

    init() {
        #if DEBUG
        _model = StateObject(
            wrappedValue: ConversationNavigationUITestFixture.makeModelIfRequested()
                ?? ChatViewModel()
        )
        _appearance = StateObject(
            wrappedValue: ConversationNavigationUITestFixture.makeAppearanceIfRequested()
                ?? AppearancePreferences()
        )
        _folderSidebar = StateObject(
            wrappedValue: ConversationNavigationUITestFixture.makeFolderSidebarIfRequested()
                ?? FolderSidebarPreferences()
        )
        #else
        _model = StateObject(wrappedValue: ChatViewModel())
        _appearance = StateObject(wrappedValue: AppearancePreferences())
        _folderSidebar = StateObject(wrappedValue: FolderSidebarPreferences())
        #endif
    }

    var body: some Scene {
        // AppKit derives window and split-view autosave keys from the content type.
        // Keep WindowGroup's child as a stable name rather than an inline modifier chain.
        WindowGroup {
            VeetbotSceneRoot(
                model: model,
                appearance: appearance,
                smsIntegration: smsIntegration,
                folderSidebar: folderSidebar
            )
            .onAppear {
                notificationDelegate.attach(to: model, smsPreferences: smsIntegration)
            }
        }
        #if os(macOS)
        WindowGroup("Webpage", for: GeneratedPageRequest.self) { $request in
            if let request {
                GeneratedPageWindow(model: model, state: model.runState, request: request)
            }
        }
        .defaultSize(width: 860, height: 680)
        .defaultLaunchBehavior(.suppressed)
        .restorationBehavior(.disabled)
        .commandsRemoved()
        #endif
    }
}
