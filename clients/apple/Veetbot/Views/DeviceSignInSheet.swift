import SwiftUI
import WebKit

/// The private sign-in window of a device sign-in (ADR-0128, 0128-design §5.1).
@MainActor
final class DeviceSignInWebSession {
    /// A configuration that keeps nothing and runs nothing of Veetbot's in the
    /// page: a non-persistent store, no user script or message handler, the
    /// platform's own user agent, and no window the page opens by itself
    /// (§2.5 item 1).
    static func makeConfiguration(dataStore: WKWebsiteDataStore) -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = dataStore
        configuration.userContentController = WKUserContentController()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        #if os(iOS)
        configuration.dataDetectorTypes = []
        #endif
        return configuration
    }

    static func makeDataStore() -> WKWebsiteDataStore {
        WKWebsiteDataStore.nonPersistent()
    }
}
