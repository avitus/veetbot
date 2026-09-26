import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0128, 0128-design §2.3 and §2.5 item 4: what a device sign-in may send.
@Suite struct WebsiteSessionScopeTests {
    private let now = Date(timeIntervalSince1970: 1_790_000_000)
    private let scope = WebsiteSessionScope(allowedOrigins: ["https://www.example.org"])

    @Test
    func hostOnlyCookiesAreKeptOnlyForAnAllowedHost() {
        #expect(scope.keeps(cookieDomain: "www.example.org"))
        #expect(!scope.keeps(cookieDomain: "example.org"))
        #expect(!scope.keeps(cookieDomain: "api.example.org"))
        #expect(!scope.keeps(cookieDomain: "evil.example.net"))
    }

    @Test
    func aDottedDomainIsKeptWhenItIsTheHostOrAParentWithAnInnerDot() {
        #expect(scope.keeps(cookieDomain: ".www.example.org"))
        #expect(scope.keeps(cookieDomain: ".example.org"))
        #expect(!scope.keeps(cookieDomain: ".api.example.org"))
        #expect(!scope.keeps(cookieDomain: ".org"))
        #expect(!scope.keeps(cookieDomain: ".com"))
        #expect(!scope.keeps(cookieDomain: ".le.org"))
    }

    @Test
    func aMixedCaseDomainIsKeptAndSentLowercase() throws {
        let cookie = try #require(
            scope.handoffCookie(from: makeCookie(domain: ".Example.ORG"), now: now)
        )
        #expect(cookie.domain == ".example.org")
        let hostOnly = try #require(
            scope.handoffCookie(from: makeCookie(domain: "WWW.example.org"), now: now)
        )
        #expect(hostOnly.domain == "www.example.org")
    }

    @Test
    func expiredCookiesAreDroppedAndSessionCookiesExpireAtMinusOne() throws {
        #expect(
            scope.handoffCookie(
                from: makeCookie(expires: now.addingTimeInterval(-1)), now: now
            ) == nil
        )
        let session = try #require(scope.handoffCookie(from: makeCookie(expires: nil), now: now))
        #expect(session.expires == -1)
    }

    @Test
    func anExpiryIsSentAsItsUnixSecondsAndAFractionSurvives() throws {
        let expiry = Date(timeIntervalSince1970: 1_800_000_000)
        let mapped = try #require(scope.handoffCookie(from: makeCookie(expires: expiry), now: now))
        #expect(mapped.expires == 1_800_000_000)
        // Foundation's cookie parser keeps whole seconds; the wire keeps any fraction.
        let cookie = HandoffCookie(
            name: "sid", value: "v", domain: "www.example.org", path: "/",
            expires: 1_800_000_000.5, httpOnly: false, secure: true, sameSite: .lax
        )
        let body = try scope.encode(
            DeviceSessionHandoff(
                confirmedURL: "https://www.example.org/learn", cookies: [cookie], origins: []
            )
        )
        #expect(String(decoding: body, as: UTF8.self).contains(#""expires":1800000000.5"#))
    }

    @Test
    func sameSiteStrictLaxAndUnsetMapToStrictLaxAndLax() throws {
        let strict = try #require(
            scope.handoffCookie(from: makeCookie(sameSite: .sameSiteStrict), now: now)
        )
        let lax = try #require(scope.handoffCookie(from: makeCookie(sameSite: .sameSiteLax), now: now))
        let unset = try #require(scope.handoffCookie(from: makeCookie(sameSite: nil), now: now))
        #expect(strict.sameSite == .strict)
        #expect(lax.sameSite == .lax)
        #expect(unset.sameSite == .lax)
    }

    @Test
    func theCookieFlagsAndPathCarryOver() throws {
        let cookie = try #require(
            scope.handoffCookie(
                from: makeCookie(name: "sid", value: "opaque-value", path: "/learn", secure: false,
                                 httpOnly: true),
                now: now
            )
        )
        #expect(cookie.name == "sid")
        #expect(cookie.value == "opaque-value")
        #expect(cookie.path == "/learn")
        #expect(cookie.httpOnly)
        #expect(!cookie.secure)
    }

    @Test
    func cookiesTheServiceWouldRefuseAreNotSent() {
        #expect(scope.handoffCookie(from: makeCookie(name: "__Host-id", secure: false), now: now) == nil)
        #expect(
            scope.handoffCookie(from: makeCookie(name: "__Host-id", domain: ".example.org"), now: now)
                == nil
        )
        #expect(scope.handoffCookie(from: makeCookie(name: "__Secure-id", secure: false), now: now) == nil)
        #expect(scope.handoffCookie(from: makeCookie(name: "__Host-id"), now: now) != nil)
    }

    @Test
    func theEncoderRefusesACookieTheServiceWouldReject() {
        func payload(_ cookie: HandoffCookie) -> DeviceSessionHandoff {
            DeviceSessionHandoff(
                confirmedURL: "https://www.example.org/learn", cookies: [cookie], origins: []
            )
        }
        func cookie(
            name: String = "sid", value: String = "v", domain: String = "www.example.org",
            path: String = "/", expires: Double = -1, secure: Bool = true,
            sameSite: HandoffSameSite = .lax
        ) -> HandoffCookie {
            HandoffCookie(
                name: name, value: value, domain: domain, path: path, expires: expires,
                httpOnly: false, secure: secure, sameSite: sameSite
            )
        }
        let refused: [HandoffCookie] = [
            cookie(name: "ab", value: String(repeating: "v", count: 4_095)),
            cookie(name: "a;b"),
            cookie(name: ""),
            cookie(value: "a;b"),
            cookie(value: "line\nbreak"),
            cookie(domain: "..example.org"),
            cookie(domain: "www.example.org."),
            cookie(domain: "WWW.example.org"),
            cookie(path: "learn"),
            cookie(expires: 0),
            cookie(secure: false, sameSite: .none),
            cookie(name: "__Host-id", domain: ".example.org"),
            cookie(name: "__Host-id", path: "/learn"),
            cookie(name: "__Secure-id", secure: false),
        ]
        for value in refused {
            #expect(throws: WebsiteSessionScopeError.self) { try scope.encode(payload(value)) }
        }
        let duplicate = DeviceSessionHandoff(
            confirmedURL: "https://www.example.org/learn", cookies: [cookie(), cookie()], origins: []
        )
        #expect(throws: WebsiteSessionScopeError.self) { try scope.encode(duplicate) }
        #expect((try? scope.encode(payload(cookie()))) != nil)
    }

    @Test
    func theEncoderRefusesMoreThanOneMebibyte() throws {
        let big = String(repeating: "x", count: WebsiteSessionScope.maximumBodyBytes)
        let handoff = DeviceSessionHandoff(
            confirmedURL: "https://www.example.org/learn",
            cookies: [],
            origins: [
                HandoffOrigin(
                    origin: "https://www.example.org",
                    localStorage: [HandoffStorageItem(name: "blob", value: big)]
                )
            ]
        )
        #expect(throws: WebsiteSessionScopeError.tooLarge) { try scope.encode(handoff) }
    }

    @Test
    func theEncoderRefusesAPageOrStorageOffTheProfilesOrigins() throws {
        let offOrigin = DeviceSessionHandoff(
            confirmedURL: "https://evil.example.net/learn", cookies: [], origins: []
        )
        #expect(throws: WebsiteSessionScopeError.self) { try scope.encode(offOrigin) }
        let plainHTTP = DeviceSessionHandoff(
            confirmedURL: "http://www.example.org/learn", cookies: [], origins: []
        )
        #expect(throws: WebsiteSessionScopeError.self) { try scope.encode(plainHTTP) }
        let foreignStorage = DeviceSessionHandoff(
            confirmedURL: "https://www.example.org/learn",
            cookies: [],
            origins: [HandoffOrigin(origin: "https://evil.example.net", localStorage: [])]
        )
        #expect(throws: WebsiteSessionScopeError.self) { try scope.encode(foreignStorage) }
        let tooMany = DeviceSessionHandoff(
            confirmedURL: "https://www.example.org/learn",
            cookies: (0...300).map { index in
                HandoffCookie(
                    name: "c\(index)", value: "v", domain: "www.example.org", path: "/",
                    expires: -1, httpOnly: false, secure: true, sameSite: .lax
                )
            },
            origins: []
        )
        #expect(throws: WebsiteSessionScopeError.tooLarge) { try scope.encode(tooMany) }
    }

    @Test
    func theEncoderWritesPlaywrightKeysAndAcceptsAValidPayload() throws {
        let learn = try #require(URL(string: "https://www.example.org/learn#top"))
        let foreign = try #require(URL(string: "https://evil.example.net/"))
        let handoff = try #require(
            scope.handoff(
                confirmedURL: learn,
                cookies: [
                    makeCookie(name: "sid", domain: ".example.org"),
                    makeCookie(name: "sid", domain: ".example.org"),
                    makeCookie(name: "tracker", domain: ".ads.example.net"),
                ],
                localStorage: [
                    "https://www.example.org": [
                        HandoffStorageItem(name: "k", value: "v"),
                        HandoffStorageItem(name: "k", value: "later"),
                    ],
                    "https://evil.example.net": [HandoffStorageItem(name: "x", value: "y")],
                ],
                now: now
            )
        )
        #expect(handoff.confirmedURL == "https://www.example.org/learn")
        #expect(handoff.cookies.map(\.name) == ["sid"])
        #expect(handoff.origins == [
            HandoffOrigin(
                origin: "https://www.example.org",
                localStorage: [HandoffStorageItem(name: "k", value: "v")]
            )
        ])
        let body = try scope.encode(handoff)
        let object = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(Set(object.keys) == ["confirmed_url", "cookies", "origins"])
        let cookie = try #require((object["cookies"] as? [[String: Any]])?.first)
        #expect(
            Set(cookie.keys)
                == ["name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"]
        )
        let origin = try #require((object["origins"] as? [[String: Any]])?.first)
        #expect(Set(origin.keys) == ["origin", "localStorage"])

        #expect(
            scope.handoff(
                confirmedURL: foreign, cookies: [], localStorage: [:], now: now
            ) == nil
        )
    }

    /// The service's `max_length` counts code points, as Python's `len` does.
    /// Swift's `count` counts characters, so a name of combining marks can be
    /// short in characters and still too long for the service.
    @Test
    func lengthBoundsCountCodePointsAsTheServiceDoes() throws {
        func marks(_ characters: Int) -> String { String(repeating: "e\u{0301}", count: characters) }
        let tooLong = marks(700)
        #expect(tooLong.count == 700)
        #expect(tooLong.unicodeScalars.count == 1_400)
        let atLimit = marks(512)
        #expect(atLimit.unicodeScalars.count == WebsiteSessionScope.maximumStorageNameCharacters)

        let learn = try #require(URL(string: "https://www.example.org/learn"))
        let filtered = try #require(
            scope.handoff(
                confirmedURL: learn,
                cookies: [],
                localStorage: [
                    "https://www.example.org": [
                        HandoffStorageItem(name: tooLong, value: "v"),
                        HandoffStorageItem(name: atLimit, value: "v"),
                    ]
                ],
                now: now
            )
        )
        #expect(filtered.origins.first?.localStorage.map(\.name.unicodeScalars.count) == [1_024])

        func storage(_ name: String) -> DeviceSessionHandoff {
            DeviceSessionHandoff(
                confirmedURL: "https://www.example.org/learn",
                cookies: [],
                origins: [
                    HandoffOrigin(
                        origin: "https://www.example.org",
                        localStorage: [HandoffStorageItem(name: name, value: "v")]
                    )
                ]
            )
        }
        #expect(throws: WebsiteSessionScopeError.invalid("storage name")) {
            try scope.encode(storage(tooLong))
        }
        #expect((try? scope.encode(storage(atLimit))) != nil)

        func cookie(path: String) -> HandoffCookie {
            HandoffCookie(
                name: "sid", value: "v", domain: "www.example.org", path: path, expires: -1,
                httpOnly: false, secure: true, sameSite: .lax
            )
        }
        #expect(WebsiteSessionScope.cookieProblem(cookie(path: "/" + marks(600))) == "cookie path")
        #expect(WebsiteSessionScope.cookieProblem(cookie(path: "/" + marks(511))) == nil)
    }

    @Test
    func storageJSONMustBeAListOfNameValuePairs() throws {
        #expect(
            try scope.storageItems(fromJSON: #"[["duo.example","1"],["empty",""]]"#)
                == [
                    HandoffStorageItem(name: "duo.example", value: "1"),
                    HandoffStorageItem(name: "empty", value: ""),
                ]
        )
        for malformed in [#"[["only"]]"#, #"[["a","b","c"]]"#, #"[["a",1]]"#, #"{"a":"b"}"#, "not json"] {
            #expect(throws: WebsiteSessionScopeError.notAPair) {
                try scope.storageItems(fromJSON: malformed)
            }
        }
    }

    @Test
    func noDescriptionOrReflectionShowsASessionValue() throws {
        let sentinel = "session-sentinel-7f3a"
        let cookie = HandoffCookie(
            name: "sid", value: sentinel, domain: "www.example.org", path: "/", expires: -1,
            httpOnly: true, secure: true, sameSite: .lax
        )
        let item = HandoffStorageItem(name: "token", value: sentinel)
        let origin = HandoffOrigin(origin: "https://www.example.org", localStorage: [item])
        let handoff = DeviceSessionHandoff(
            confirmedURL: "https://www.example.org/\(sentinel)", cookies: [cookie], origins: [origin]
        )
        var dumped = ""
        dump(handoff, to: &dumped)
        let renderings: [String] = [
            String(describing: handoff), String(reflecting: handoff), "\(cookie)",
            String(reflecting: cookie), String(describing: item), String(reflecting: origin), dumped,
        ]
        for rendering in renderings {
            #expect(!rendering.contains(sentinel))
        }
        #expect(String(describing: handoff) == "<device session>")
    }

    @Test
    func theScopeNormalizesOriginsAndAllowsOnlyTheirPages() throws {
        let mixed = WebsiteSessionScope(allowedOrigins: ["HTTPS://WWW.Example.org", "https://static.example.org"])
        #expect(mixed.allowedOrigins == ["https://www.example.org", "https://static.example.org"])
        #expect(mixed.allowedHosts == ["www.example.org", "static.example.org"])
        #expect(mixed.allows(try #require(URL(string: "https://www.example.org/learn"))))
        #expect(!mixed.allows(try #require(URL(string: "http://www.example.org/learn"))))
        #expect(!mixed.allows(try #require(URL(string: "https://www.example.org:8443/"))))
        #expect(!mixed.allows(try #require(URL(string: "https://example.org/"))))
        let explicitPort = try #require(URL(string: "https://WWW.example.org:443/a?b"))
        #expect(WebsiteSessionScope.origin(of: explicitPort) == "https://www.example.org")
    }

    private func makeCookie(
        name: String = "sid",
        value: String = "value",
        domain: String = "www.example.org",
        path: String = "/",
        expires: Date? = Date(timeIntervalSince1970: 1_800_000_000),
        secure: Bool = true,
        httpOnly: Bool = false,
        sameSite: HTTPCookieStringPolicy? = .sameSiteLax
    ) -> HTTPCookie {
        var properties: [HTTPCookiePropertyKey: Any] = [
            .name: name, .value: value, .domain: domain, .path: path,
        ]
        if let expires { properties[.expires] = expires }
        if secure { properties[.secure] = "TRUE" }
        if httpOnly { properties[HTTPCookiePropertyKey("HttpOnly")] = "TRUE" }
        if let sameSite { properties[.sameSitePolicy] = sameSite }
        return HTTPCookie(properties: properties)!
    }
}
