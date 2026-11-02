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

/// ADR-0287: the window holds back a move to another address of the website
/// until the owner allows it, then goes there.
@Suite(.serialized) @MainActor struct DeviceSignInOfferedAddressTests {
    private final class StubNavigationAction: WKNavigationAction {
        private let stubbedRequest: URLRequest

        init(_ raw: String) {
            stubbedRequest = URLRequest(url: URL(string: raw)!)
            super.init()
        }

        override var request: URLRequest { stubbedRequest }
        override var targetFrame: WKFrameInfo? { nil }
    }

    private let www = "https://www.sign-in.test"
    private let secure = "https://secure.sign-in.test"

    private func session(
        profileID: UUID? = nil, adoptable: String? = nil
    ) -> DeviceSignInWebSession {
        DeviceSignInWebSession(
            request: DeviceSignInRequest(
                startURL: URL(string: www + "/")!, allowedOrigins: [www],
                profileID: profileID, adoptableOrigin: adoptable
            )
        )
    }

    private func navigate(
        _ session: DeviceSignInWebSession, to raw: String
    ) throws -> WKNavigationActionPolicy {
        let webView = try #require(session.webView)
        var policy: WKNavigationActionPolicy?
        session.webView(webView, decidePolicyFor: StubNavigationAction(raw)) { policy = $0 }
        return try #require(policy)
    }

    @Test
    func theMoveIsHeldBackUntilTheOwnerAllowsIt() async throws {
        let session = session()
        defer { Task { await session.destroy() } }

        #expect(try navigate(session, to: secure + "/login?step=1") == .cancel)
        #expect(session.offeredOrigin == secure)
        #expect(session.blockedHost == nil)
        #expect(session.allowedOrigins == [www])
        #expect(session.addedOrigins.isEmpty)

        session.addOfferedOrigin()

        #expect(session.allowedOrigins == [www, secure])
        #expect(session.addedOrigins == [secure])
        #expect(session.offeredOrigin == nil)
        // The window goes on to the page it held back.
        let webView = try #require(session.webView)
        for _ in 0..<40 where webView.url?.host != "secure.sign-in.test" {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        #expect(webView.url?.absoluteString == secure + "/login?step=1")
        #expect(try navigate(session, to: secure + "/next") == .allow)
    }

    @Test
    func anExistingProfileGetsTheOldRefusal() throws {
        let session = session(profileID: UUID())
        defer { Task { await session.destroy() } }

        #expect(try navigate(session, to: secure + "/login") == .cancel)
        #expect(session.offeredOrigin == nil)
        #expect(session.blockedHost == "secure.sign-in.test")
        session.addOfferedOrigin()
        #expect(session.allowedOrigins == [www])
    }

    /// Adoption replaces the list, which would lose an address the owner allowed.
    @Test
    func anAllowedAddressEndsFirstLoadAdoption() throws {
        let session = session(adoptable: "https://sign-in.test")
        defer { Task { await session.destroy() } }

        _ = try navigate(session, to: secure + "/login")
        session.addOfferedOrigin()

        #expect(try navigate(session, to: "https://sign-in.test/") == .cancel)
        #expect(session.offeredOrigin == "https://sign-in.test")
        #expect(session.adoptedOrigin == nil)
        #expect(session.allowedOrigins == [www, secure])
    }

    @Test
    func aRefusedPageDoesNotLeaveAnOldOfferBehind() throws {
        let session = session()
        defer { Task { await session.destroy() } }

        _ = try navigate(session, to: secure + "/login")
        #expect(try navigate(session, to: "https://identity.example/") == .cancel)
        #expect(session.offeredOrigin == nil)
        #expect(session.blockedHost == "identity.example")
        session.addOfferedOrigin()
        #expect(session.allowedOrigins == [www])
    }

    /// ADR-0288: a website may refuse this window, as Chase does, so a saved
    /// login needs the remote browser here too.
    @Test
    func theWindowOffersTheRemoteBrowserForASavedLogin() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/DeviceSignInSheet.swift"),
            encoding: .utf8
        )
        #expect(source.contains("if failure == nil || failure?.offersRemoteBrowser == true {"))
        #expect(!source.contains("request.profileID == nil, failure == nil"))
    }

    @Test
    func theSheetOffersTheAddressByName() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/DeviceSignInSheet.swift"),
            encoding: .utf8
        )
        #expect(source.contains("device-sign-in.allow-address"))
        #expect(source.contains("session.addOfferedOrigin()"))
        #expect(source.contains("addedOrigins: session.addedOrigins"))
    }
}
