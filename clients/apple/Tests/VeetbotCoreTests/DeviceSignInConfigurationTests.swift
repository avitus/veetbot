import Foundation
import Testing
import WebKit

@testable import VeetbotCore

/// ADR-0128, 0128-design §2.5 item 1: the sign-in window keeps nothing and runs
/// nothing of Veetbot's in the page.
@Suite(.serialized) @MainActor struct DeviceSignInConfigurationTests {
    @Test
    func theWindowUsesANonPersistentStoreAndNoInjectedCode() {
        let store = WKWebsiteDataStore.nonPersistent()
        let configuration = DeviceSignInWebSession.makeConfiguration(dataStore: store)

        #expect(configuration.websiteDataStore === store)
        #expect(!configuration.websiteDataStore.isPersistent)
        #expect(configuration.userContentController.userScripts.isEmpty)
        #expect(configuration.preferences.javaScriptCanOpenWindowsAutomatically == false)
        #expect(
            configuration.applicationNameForUserAgent
                == WKWebViewConfiguration().applicationNameForUserAgent
        )
        #if os(iOS)
        #expect(configuration.dataDetectorTypes == [])
        #endif
        // WebKit reports an unset custom user agent as nil or as empty text.
        let webView = WKWebView(frame: .zero, configuration: configuration)
        #expect((webView.customUserAgent ?? "").isEmpty)
    }

    @Test
    func theSessionSourceAddsNoScriptHandlerOrInspection() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/DeviceSignInSheet.swift"),
            encoding: .utf8
        )
        #expect(source.contains("WKWebsiteDataStore.nonPersistent()"))
        #expect(!source.contains("WKUserScript"))
        #expect(!source.contains("add(self"))
        #expect(!source.contains("addScriptMessageHandler"))
        #expect(!source.contains("customUserAgent ="))
        #expect(!source.contains("isInspectable"))
        #expect(!source.contains("print("))
        #expect(!source.contains("os_log"))
        #expect(!source.contains("Logger("))
    }
}
