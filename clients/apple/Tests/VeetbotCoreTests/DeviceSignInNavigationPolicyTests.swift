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

    // MARK: - Another address of the same website (ADR-0287)

    private func decideForNewWebsite(
        _ raw: String, allowed: Set<String>, mainFrame: Bool = true, adoptable: String? = nil
    ) -> DeviceSignInNavigationPolicy.Decision {
        DeviceSignInNavigationPolicy.decide(
            url: URL(string: raw), isMainFrame: mainFrame,
            allowedOrigins: allowed, adoptableOrigin: adoptable, canAddOrigins: true
        )
    }

    /// The owner's report: www.chase.com signs in on secure.chase.com, whose
    /// pages lead back to www.chase.com, so neither address alone could finish.
    @Test
    func aNewWebsiteAsksBeforeItsSignInMovesToAnotherAddressOfTheSite() {
        #expect(
            decideForNewWebsite("https://secure.chase.com/web/auth/dashboard", allowed: ["https://www.chase.com"])
                == .askToAdd(origin: "https://secure.chase.com")
        )
        #expect(
            decideForNewWebsite("https://www.chase.com/", allowed: ["https://secure.chase.com"])
                == .askToAdd(origin: "https://www.chase.com")
        )
        #expect(
            decideForNewWebsite("https://LOGIN.example.co.uk:443/in", allowed: ["https://www.example.co.uk"])
                == .askToAdd(origin: "https://login.example.co.uk")
        )
    }

    @Test
    func anExistingProfileNeverAsks() {
        #expect(
            DeviceSignInNavigationPolicy.decide(
                url: URL(string: "https://secure.chase.com/"), isMainFrame: true,
                allowedOrigins: ["https://www.chase.com"], adoptableOrigin: nil, canAddOrigins: false
            ) == .cancel(blockedHost: "secure.chase.com")
        )
    }

    @Test
    func onlyAnotherAddressOfTheSameSiteIsAsked() {
        let chase: Set<String> = ["https://www.chase.com"]
        #expect(decideForNewWebsite("https://accounts.identity.example/", allowed: chase) == .cancel(blockedHost: "accounts.identity.example"))
        #expect(decideForNewWebsite("https://secure.chase.com.evil.example/", allowed: chase) == .cancel(blockedHost: "secure.chase.com.evil.example"))
        #expect(decideForNewWebsite("https://notchase.com/", allowed: chase) == .cancel(blockedHost: "notchase.com"))
        #expect(decideForNewWebsite("http://secure.chase.com/", allowed: chase) == .cancel(blockedHost: "secure.chase.com"))
        #expect(decideForNewWebsite("https://secure.chase.com:8443/", allowed: chase) == .cancel(blockedHost: "secure.chase.com"))
        #expect(decideForNewWebsite("mailto:owner@chase.com", allowed: chase) == .cancel(blockedHost: "mailto"))
        // A country's second-level suffix is not a website.
        #expect(
            decideForNewWebsite("https://other.co.uk/", allowed: ["https://www.example.co.uk"])
                == .cancel(blockedHost: "other.co.uk")
        )
        // Neither is the tail of an IP address.
        #expect(decideForNewWebsite("https://10.0.0.2/", allowed: ["https://10.0.0.1"]) == .cancel(blockedHost: "10.0.0.2"))
    }

    @Test
    func adoptionAndSubframesKeepTheirOwnRules() {
        #expect(
            decideForNewWebsite("https://www.chase.com/", allowed: ["https://chase.com"], adoptable: "https://www.chase.com")
                == .adopt(origin: "https://www.chase.com")
        )
        #expect(decideForNewWebsite("https://secure.chase.com/frame", allowed: ["https://www.chase.com"], mainFrame: false) == .allow)
        #expect(decideForNewWebsite("https://secure.chase.com/", allowed: ["https://secure.chase.com"]) == .allow)
    }

    /// The server refuses a profile with more origins than this.
    @Test
    func theAskingStopsAtTheServersOriginLimit() {
        let full = Set((0..<WebsiteSessionScope.maximumOrigins).map { "https://host\($0).chase.com" })
        #expect(decideForNewWebsite("https://secure.chase.com/", allowed: full) == .cancel(blockedHost: "secure.chase.com"))
        var roomForOne = full
        roomForOne.remove("https://host0.chase.com")
        #expect(decideForNewWebsite("https://secure.chase.com/", allowed: roomForOne) == .askToAdd(origin: "https://secure.chase.com"))
    }
}
