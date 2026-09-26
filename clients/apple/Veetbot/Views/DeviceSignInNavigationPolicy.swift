import Foundation

/// Where the device sign-in window may go (ADR-0128, 0128-design §2.5 items 2
/// and 3). Pure, so every rule is testable without WebKit.
public enum DeviceSignInNavigationPolicy {
    public enum Decision: Equatable, Sendable {
        case allow
        case cancel(blockedHost: String)
        /// A new profile's first load moved between the bare host and `www`;
        /// the profile takes that origin instead (D11).
        case adopt(origin: String)
    }

    /// Main-frame loads stay on the profile's HTTPS origins (and `about:blank`);
    /// subframes load normally.
    public static func decide(
        url: URL?,
        isMainFrame: Bool,
        allowedOrigins: Set<String>,
        adoptableOrigin: String?
    ) -> Decision {
        guard isMainFrame else { return .allow }
        guard let url else { return .cancel(blockedHost: "") }
        if url.absoluteString == "about:blank" { return .allow }
        let origin = WebsiteSessionScope.origin(of: url)
        if let origin, normalized(allowedOrigins).contains(origin) { return .allow }
        if let origin, let adoptableOrigin, origin == normalized(adoptableOrigin) {
            return .adopt(origin: origin)
        }
        return .cancel(blockedHost: url.host?.lowercased() ?? url.scheme ?? "")
    }

    /// I'm signed in is offered only on an allowed page that has finished loading.
    public static func canConfirm(
        currentURL: URL?,
        isLoading: Bool,
        allowedOrigins: Set<String>
    ) -> Bool {
        guard !isLoading, let currentURL, let origin = WebsiteSessionScope.origin(of: currentURL)
        else { return false }
        return normalized(allowedOrigins).contains(origin)
    }

    /// The bare or `www` counterpart of an origin, which a new profile's first
    /// load may adopt; nil for any other host.
    public static func adoptableOrigin(for origin: String) -> String? {
        guard let normalized = normalized(origin),
            let host = URL(string: normalized)?.host,
            normalized == "https://\(host)"
        else { return nil }
        if host.hasPrefix("www.") {
            let bare = String(host.dropFirst(4))
            return bare.contains(".") ? "https://\(bare)" : nil
        }
        guard host.split(separator: ".").count == 2 else { return nil }
        return "https://www.\(host)"
    }

    private static func normalized(_ origins: Set<String>) -> Set<String> {
        Set(origins.compactMap(normalized))
    }

    private static func normalized(_ origin: String) -> String? {
        URL(string: origin).flatMap(WebsiteSessionScope.origin(of:))
    }
}
