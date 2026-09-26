import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0128, 0128-design §2.5 items 2 and 3.
@Suite struct DeviceSignInNavigationPolicyTests {
    private let allowed: Set<String> = ["https://www.example.org"]

    private func decide(
        _ raw: String?, mainFrame: Bool = true, adoptable: String? = nil
    ) -> DeviceSignInNavigationPolicy.Decision {
        DeviceSignInNavigationPolicy.decide(
            url: raw.flatMap(URL.init(string:)), isMainFrame: mainFrame,
            allowedOrigins: allowed, adoptableOrigin: adoptable
        )
    }

    @Test
    func anAllowedMainFrameLoads() {
        #expect(decide("https://www.example.org/log-in?next=%2Flearn") == .allow)
        #expect(decide("https://WWW.EXAMPLE.org:443/learn") == .allow)
        #expect(decide("about:blank") == .allow)
    }

    @Test
    func aForeignMainFrameIsCancelledWithItsHost() {
        #expect(decide("https://accounts.identity.example/authorize") == .cancel(blockedHost: "accounts.identity.example"))
        #expect(decide("https://www.example.org:8443/") == .cancel(blockedHost: "www.example.org"))
        #expect(decide(nil) == .cancel(blockedHost: ""))
    }

    @Test
    func plainHTTPAndOtherSchemesAreCancelled() {
        #expect(decide("http://www.example.org/learn") == .cancel(blockedHost: "www.example.org"))
        #expect(decide("itms-apps://apps.example/app") == .cancel(blockedHost: "apps.example"))
        #expect(decide("mailto:owner@example.org") == .cancel(blockedHost: "mailto"))
    }

    @Test
    func subframesLoadNormally() {
        #expect(decide("https://challenges.captcha.example/frame", mainFrame: false) == .allow)
        #expect(decide("http://ads.example/frame", mainFrame: false) == .allow)
    }

    @Test
    func bareAndWWWAreAdoptedOnlyWhenOffered() {
        #expect(decide("https://example.org/", adoptable: "https://example.org") == .adopt(origin: "https://example.org"))
        #expect(decide("https://example.org/") == .cancel(blockedHost: "example.org"))
        #expect(
            DeviceSignInNavigationPolicy.adoptableOrigin(for: "https://example.org")
                == "https://www.example.org"
        )
        #expect(
            DeviceSignInNavigationPolicy.adoptableOrigin(for: "https://www.example.org")
                == "https://example.org"
        )
        #expect(DeviceSignInNavigationPolicy.adoptableOrigin(for: "https://learn.example.org") == nil)
        #expect(DeviceSignInNavigationPolicy.adoptableOrigin(for: "https://org") == nil)
    }

    @Test
    func iAmSignedInNeedsAnAllowedPageThatHasFinishedLoading() {
        let learn = URL(string: "https://www.example.org/learn")
        #expect(DeviceSignInNavigationPolicy.canConfirm(currentURL: learn, isLoading: false, allowedOrigins: allowed))
        #expect(!DeviceSignInNavigationPolicy.canConfirm(currentURL: learn, isLoading: true, allowedOrigins: allowed))
        #expect(
            !DeviceSignInNavigationPolicy.canConfirm(
                currentURL: URL(string: "https://identity.example/"), isLoading: false, allowedOrigins: allowed
            )
        )
        #expect(
            !DeviceSignInNavigationPolicy.canConfirm(
                currentURL: URL(string: "about:blank"), isLoading: false, allowedOrigins: allowed
            )
        )
        #expect(!DeviceSignInNavigationPolicy.canConfirm(currentURL: nil, isLoading: false, allowedOrigins: allowed))
    }
}
