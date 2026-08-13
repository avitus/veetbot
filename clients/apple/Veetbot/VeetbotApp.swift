import SwiftUI

@main
struct VeetbotApp: App {
    @StateObject private var model = ChatViewModel()

    var body: some Scene {
        WindowGroup {
            RootView(model: model)
#if os(macOS)
                .frame(minWidth: 720, minHeight: 520)
#endif
        }
    }
}
