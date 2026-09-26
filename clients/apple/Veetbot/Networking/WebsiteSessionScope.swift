import Foundation

/// Why a device session cannot be handed off. The cases carry fixed text
/// only, never a cookie, storage value or address.
public enum WebsiteSessionScopeError: Error, Equatable, Sendable {
    /// The body would exceed 1 MiB, or a count bound of 0128-design §2.3.
    case tooLarge
    /// A value breaks a shape rule the service would refuse with `400`.
    case invalid(String)
    /// `localStorage` did not read back as a list of name and value pairs.
    case notAPair
}

/// The profile's allowed origins, and the client-side filter a device sign-in
/// applies before anything leaves the device (ADR-0128, 0128-design §2.5 item
/// 4). The isolated service filters again and is authoritative; this filter
/// only keeps a correct client from sending what the service would drop, and
/// the encoder from sending what it would refuse (§2.3).
public struct WebsiteSessionScope: Sendable {
    public static let maximumBodyBytes = 1_048_576
    static let maximumCookies = 300
    static let maximumOrigins = 64
    static let maximumStorageItems = 2_000
    static let maximumStorageNameCharacters = 1_024
    static let maximumCookieBytes = 4_096
    static let maximumConfirmedURLCharacters = 4_096
    static let maximumExpiry = 9_007_199_254_740_992.0

    public let allowedOrigins: Set<String>
    public let allowedHosts: Set<String>

    public init(allowedOrigins: [String]) {
        let origins = allowedOrigins.compactMap { URL(string: $0).flatMap(Self.origin(of:)) }
        self.allowedOrigins = Set(origins)
        self.allowedHosts = Set(origins.compactMap { URL(string: $0)?.host })
    }

    /// The origin of an HTTPS URL, lowercased, with a non-default port kept.
    public static func origin(of url: URL) -> String? {
        guard
            let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
            components.scheme?.lowercased() == "https",
            let host = components.host?.lowercased(), !host.isEmpty,
            components.user == nil, components.password == nil
        else { return nil }
        if let port = components.port, port != 443 {
            return "https://\(host):\(port)"
        }
        return "https://\(host)"
    }

    /// Whether a main-frame URL is on one of the profile's origins.
    public func allows(_ url: URL) -> Bool {
        Self.origin(of: url).map(allowedOrigins.contains) ?? false
    }

    /// A host-only cookie must name an allowed host exactly; a domain cookie
    /// must name an allowed host or one of its parents, never a bare label
    /// such as `.org` (§2.5 item 4).
    public func keeps(cookieDomain: String) -> Bool {
        let domain = cookieDomain.lowercased()
        guard domain.hasPrefix(".") else { return allowedHosts.contains(domain) }
        let parent = String(domain.dropFirst())
        guard parent.contains("."), !parent.hasPrefix("."), !parent.hasSuffix(".") else {
            return false
        }
        return allowedHosts.contains { $0 == parent || $0.hasSuffix("." + parent) }
    }

    /// The wire form of one cookie from the sign-in store, or nil when it is
    /// out of scope, expired, or one the service would refuse (D10: an unset
    /// SameSite is sent as Lax, Chromium's default).
    public func handoffCookie(from cookie: HTTPCookie, now: Date) -> HandoffCookie? {
        guard keeps(cookieDomain: cookie.domain) else { return nil }
        let expires: Double
        if let expiresDate = cookie.expiresDate {
            guard expiresDate > now else { return nil }
            expires = expiresDate.timeIntervalSince1970
        } else {
            expires = -1
        }
        let sameSite: HandoffSameSite =
            cookie.sameSitePolicy == .sameSiteStrict ? .strict : .lax
        let candidate = HandoffCookie(
            name: cookie.name,
            value: cookie.value,
            domain: cookie.domain.lowercased(),
            path: cookie.path.isEmpty ? "/" : cookie.path,
            expires: expires,
            httpOnly: cookie.isHTTPOnly,
            secure: cookie.isSecure,
            sameSite: sameSite
        )
        return Self.cookieProblem(candidate) == nil ? candidate : nil
    }

    /// Decodes the sign-in page's `localStorage` read, a JSON list of
    /// `[name, value]` string pairs.
    public func storageItems(fromJSON json: String) throws -> [HandoffStorageItem] {
        guard
            let decoded = try? JSONSerialization.jsonObject(with: Data(json.utf8)),
            let pairs = decoded as? [Any]
        else { throw WebsiteSessionScopeError.notAPair }
        return try pairs.map { entry in
            guard let pair = entry as? [Any], pair.count == 2,
                let name = pair[0] as? String, let value = pair[1] as? String
            else { throw WebsiteSessionScopeError.notAPair }
            return HandoffStorageItem(name: name, value: value)
        }
    }

    /// The payload for a page the owner confirmed on: in-scope cookies and the
    /// allowed origins' storage. Nil when the page is not on an allowed origin.
    /// The first of any duplicate cookie or storage name wins.
    public func handoff(
        confirmedURL: URL,
        cookies: [HTTPCookie],
        localStorage: [String: [HandoffStorageItem]],
        now: Date
    ) -> DeviceSessionHandoff? {
        guard allows(confirmedURL),
            var components = URLComponents(url: confirmedURL, resolvingAgainstBaseURL: false)
        else { return nil }
        components.fragment = nil
        guard let confirmed = components.string else { return nil }

        var seenCookies: Set<String> = []
        let kept = cookies.compactMap { handoffCookie(from: $0, now: now) }.filter { cookie in
            seenCookies.insert(Self.cookieKey(cookie)).inserted
        }
        let origins = localStorage.keys.sorted().compactMap { rawOrigin -> HandoffOrigin? in
            guard let origin = URL(string: rawOrigin).flatMap(Self.origin(of:)),
                allowedOrigins.contains(origin)
            else { return nil }
            var seenNames: Set<String> = []
            let items = (localStorage[rawOrigin] ?? []).filter { item in
                item.name.count <= Self.maximumStorageNameCharacters
                    && seenNames.insert(item.name).inserted
            }
            return items.isEmpty ? nil : HandoffOrigin(origin: origin, localStorage: items)
        }
        return DeviceSessionHandoff(confirmedURL: confirmed, cookies: kept, origins: origins)
    }

    /// The request body, refused unless every §2.3 rule holds, so nothing the
    /// service would reject, and nothing over 1 MiB, ever leaves the device.
    public func encode(_ handoff: DeviceSessionHandoff) throws -> Data {
        guard handoff.confirmedURL.count <= Self.maximumConfirmedURLCharacters,
            let confirmed = URL(string: handoff.confirmedURL), allows(confirmed),
            !handoff.confirmedURL.contains("#")
        else { throw WebsiteSessionScopeError.invalid("confirmed_url") }
        guard handoff.cookies.count <= Self.maximumCookies,
            handoff.origins.count <= Self.maximumOrigins,
            handoff.origins.allSatisfy({ $0.localStorage.count <= Self.maximumStorageItems })
        else { throw WebsiteSessionScopeError.tooLarge }
        var cookieKeys: Set<String> = []
        for cookie in handoff.cookies {
            if let problem = Self.cookieProblem(cookie) {
                throw WebsiteSessionScopeError.invalid(problem)
            }
            guard keeps(cookieDomain: cookie.domain) else {
                throw WebsiteSessionScopeError.invalid("cookie domain")
            }
            guard cookieKeys.insert(Self.cookieKey(cookie)).inserted else {
                throw WebsiteSessionScopeError.invalid("duplicate cookie")
            }
        }
        var origins: Set<String> = []
        for origin in handoff.origins {
            guard allowedOrigins.contains(origin.origin), origins.insert(origin.origin).inserted
            else { throw WebsiteSessionScopeError.invalid("origin") }
            var names: Set<String> = []
            for item in origin.localStorage {
                guard item.name.count <= Self.maximumStorageNameCharacters,
                    names.insert(item.name).inserted
                else { throw WebsiteSessionScopeError.invalid("storage name") }
            }
        }
        let body = try JSONEncoder.server.encode(handoff)
        guard body.count <= Self.maximumBodyBytes else { throw WebsiteSessionScopeError.tooLarge }
        return body
    }

    private static func cookieKey(_ cookie: HandoffCookie) -> String {
        [cookie.name, cookie.domain, cookie.path].joined(separator: "\u{0}")
    }

    /// The first §2.3 cookie rule a cookie breaks, as fixed text, or nil.
    static func cookieProblem(_ cookie: HandoffCookie) -> String? {
        let forbiddenInName: Set<UInt32> = [0x3B, 0x2C, 0x3D, 0x22, 0x5C]
        let name = cookie.name.unicodeScalars
        guard (1...256).contains(name.count),
            name.allSatisfy({ (0x21...0x7E).contains($0.value) && !forbiddenInName.contains($0.value) })
        else { return "cookie name" }
        guard cookie.value.utf8.count <= maximumCookieBytes,
            cookie.value.unicodeScalars.allSatisfy({
                $0.value >= 0x20 && $0.value != 0x7F && $0.value != 0x3B
            }),
            cookie.name.utf8.count + cookie.value.utf8.count <= maximumCookieBytes
        else { return "cookie value" }
        guard isValidDomain(cookie.domain) else { return "cookie domain" }
        guard (1...1_024).contains(cookie.path.count), cookie.path.hasPrefix("/"),
            cookie.path.unicodeScalars.allSatisfy({
                $0.value >= 0x20 && $0.value != 0x7F && $0.value != 0x3B
            })
        else { return "cookie path" }
        guard cookie.expires == -1 || (cookie.expires > 0 && cookie.expires <= maximumExpiry)
        else { return "cookie expiry" }
        guard cookie.sameSite != .none || cookie.secure else { return "cookie same-site" }
        if cookie.name.hasPrefix("__Host-") {
            guard cookie.secure, !cookie.domain.hasPrefix("."), cookie.path == "/" else {
                return "cookie prefix"
            }
        } else if cookie.name.hasPrefix("__Secure-"), !cookie.secure {
            return "cookie prefix"
        }
        return nil
    }

    /// Lowercase LDH labels with at least one inner dot, at most one leading
    /// dot and no trailing dot.
    static func isValidDomain(_ domain: String) -> Bool {
        guard (1...255).contains(domain.count) else { return false }
        let body = domain.hasPrefix(".") ? String(domain.dropFirst()) : domain
        let labels = body.split(separator: ".", omittingEmptySubsequences: false)
        guard labels.count >= 2 else { return false }
        return labels.allSatisfy { label in
            (1...63).contains(label.count)
                && label.first != "-" && label.last != "-"
                && label.unicodeScalars.allSatisfy {
                    ("a"..."z").contains($0) || ("0"..."9").contains($0) || $0 == "-"
                }
        }
    }
}
