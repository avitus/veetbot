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
        /// A new profile's sign-in moved to another address of the same website;
        /// it stays held back unless the owner allows that origin (ADR-0287).
        case askToAdd(origin: String)
    }

    /// Second-level labels under a country code, as the server's website
    /// offers approximate a registrable domain.
    private static let secondLevelLabels: Set<Substring> = ["ac", "co", "com", "edu", "gov", "net", "org"]

    /// Main-frame loads stay on the profile's HTTPS origins (and `about:blank`);
    /// subframes load normally.
    public static func decide(
        url: URL?,
        isMainFrame: Bool,
        allowedOrigins: Set<String>,
        adoptableOrigin: String?,
        canAddOrigins: Bool = false
    ) -> Decision {
        guard isMainFrame else { return .allow }
        guard let url else { return .cancel(blockedHost: "") }
        if url.absoluteString == "about:blank" { return .allow }
        let origin = WebsiteSessionScope.origin(of: url)
        let allowed = normalized(allowedOrigins)
        if let origin, allowed.contains(origin) { return .allow }
        if let origin, let adoptableOrigin, origin == normalized(adoptableOrigin) {
            return .adopt(origin: origin)
        }
        if canAddOrigins, let origin, isAddable(origin, to: allowed) {
            return .askToAdd(origin: origin)
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

    /// Another address of a website the window is on: the same registrable
    /// domain on the default port, while the server would still accept the list.
    private static func isAddable(_ origin: String, to allowed: Set<String>) -> Bool {
        guard allowed.count < WebsiteSessionScope.maximumOrigins,
            let host = URL(string: origin)?.host, origin == "https://\(host)",
            let site = registrableDomain(of: host)
        else { return false }
        return allowed.contains { URL(string: $0)?.host.flatMap(registrableDomain(of:)) == site }
    }

    /// The public suffix list stays inside the isolated service (ADR-0128), so
    /// this approximates. A wrong match only asks the owner, who sees the host.
    private static func registrableDomain(of host: String) -> String? {
        let labels = host.split(separator: ".", omittingEmptySubsequences: false)
        guard labels.count >= 2, !labels.allSatisfy({ $0.allSatisfy(\.isNumber) }) else { return nil }
        let isCountrySuffix = labels.count >= 3 && labels[labels.count - 1].count == 2
            && secondLevelLabels.contains(labels[labels.count - 2])
        return labels.suffix(isCountrySuffix ? 3 : 2).joined(separator: ".")
    }

    private static func normalized(_ origins: Set<String>) -> Set<String> {
        Set(origins.compactMap(normalized))
    }

    private static func normalized(_ origin: String) -> String? {
        URL(string: origin).flatMap(WebsiteSessionScope.origin(of:))
    }
}
