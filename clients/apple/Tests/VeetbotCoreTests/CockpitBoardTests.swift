import Foundation
import Testing

@testable import VeetbotCore

/// docs/plan/cockpit.md, "The host and the realm" and `CockpitWebView`: what
/// native code gives the host page, which assets the scheme handler may serve,
/// where the web view may navigate, and which host messages it accepts.
@Suite struct CockpitBoardTests {
    static let host = String(repeating: "a", count: 64)
    static let realm = String(repeating: "b", count: 64)
    static let kit = String(repeating: "c", count: 64)
    static let weatherBundle = String(repeating: "d", count: 64)
    static let approvalsBundle = String(repeating: "e", count: 64)
    static let now = Date(timeIntervalSince1970: 1_793_000_000)

    static func widget(
        _ slug: String, bundle: String, sizes: [String], feeds: [(String, Int)] = [],
        changedAt: Date = now
    ) -> CockpitWidget {
        CockpitWidget(
            slug: slug,
            revisionID: UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", slug.count))!,
            bundleSHA256: bundle,
            manifest: [
                "title": .string(slug.capitalized),
                "sizes": .array(sizes.map(JSONValue.string)),
                "feeds": .array(feeds.map { id, history in
                    .object(["id": .string(id), "history": .number(Double(history))])
                }),
            ],
            settings: ["place": .string("Harbor")],
            changedAt: changedAt
        )
    }

    static func snapshot(layouts: [CockpitDeviceClass: CockpitLayout] = [:]) -> CockpitSnapshot {
        CockpitSnapshot(
            cursor: 41,
            serverTime: now,
            runtime: CockpitRuntime(
                kitAPI: 1, entry: host,
                assets: ["host.html": host, "realm.html": realm, "kit.css": kit]
            ),
            widgets: [
                widget("weather", bundle: weatherBundle, sizes: ["sm", "md"], feeds: [("agent.weather", 2)]),
                widget(
                    "approvals", bundle: approvalsBundle, sizes: ["md", "lg"],
                    feeds: [("veetbot.approvals.pending", 0)], changedAt: now.addingTimeInterval(-60)
                ),
            ],
            layouts: layouts,
            feeds: [
                "agent.weather": CockpitFeed(
                    trust: "untrusted",
                    current: CockpitFeedEntry(
                        observedAt: now.addingTimeInterval(-30), expectedIntervalSeconds: 900,
                        data: .object(["temperature": .number(64)])
                    ),
                    history: [
                        CockpitFeedEntry(
                            observedAt: now.addingTimeInterval(-930), expectedIntervalSeconds: 900,
                            data: .object(["temperature": .number(61)])
                        ),
                    ]
                ),
                "veetbot.unused": CockpitFeed(current: nil),
            ]
        )
    }

    @Test
    func withoutALayoutEveryWidgetTakesItsFirstSizeInSnapshotOrder() {
        let placements = CockpitBoard.placements(Self.snapshot(), deviceClass: .wide)
        #expect(placements.map(\.widget.slug) == ["weather", "approvals"])
        #expect(placements.map(\.size) == ["sm", "md"])
    }

    @Test
    func aLayoutOrdersTheBoardAndDropsWhatIsNotLiveOrNotOffered() {
        let layout = CockpitLayout(revision: 3, placements: [
            CockpitPlacement(slug: "approvals", size: "lg"),
            CockpitPlacement(slug: "gone", size: "md"),
            CockpitPlacement(slug: "weather", size: "wide"),
        ])
        let placements = CockpitBoard.placements(
            Self.snapshot(layouts: [.wide: layout]), deviceClass: .wide
        )
        #expect(placements.map(\.widget.slug) == ["approvals"])
        #expect(placements.map(\.size) == ["lg"])
        // Another device class's layout leaves this board in snapshot order.
        let compact = CockpitBoard.placements(
            Self.snapshot(layouts: [.wide: layout]), deviceClass: .compact
        )
        #expect(compact.map(\.widget.slug) == ["weather", "approvals"])
    }

    @Test
    func theHostReceivesEachWidgetsPlacementAndOnlyDeclaredFeeds() throws {
        let message = CockpitBoard.hostSnapshot(
            Self.snapshot(), deviceClass: .wide, theme: "night", platform: "mac",
            offsetMilliseconds: 1500, excluded: [], selected: "weather"
        )
        guard case let .object(body) = message else { Issue.record("not an object"); return }
        #expect(body["type"] == .string("snapshot"))
        #expect(body["theme"] == .string("night"))
        #expect(body["platform"] == .string("mac"))
        #expect(body["offsetMs"] == .number(1500))
        guard case let .array(widgets)? = body["widgets"], case let .object(weather)? = widgets.first
        else { Issue.record("no widgets"); return }
        #expect(widgets.count == 2)
        #expect(weather["slug"] == .string("weather"))
        #expect(weather["bundle"] == .string("\(Self.weatherBundle).js"))
        #expect(weather["title"] == .string("Weather"))
        #expect(weather["size"] == .string("sm"))
        #expect(weather["selected"] == .bool(true))
        #expect(weather["settings"] == .object(["place": .string("Harbor")]))
        #expect(weather["changedAt"] == .number(Self.now.timeIntervalSince1970 * 1000))
        #expect(weather["feeds"] == .array([.object(["id": .string("agent.weather"), "history": .number(2)])]))
        guard case let .object(feeds)? = body["feeds"] else { Issue.record("no feeds"); return }
        // Only declared feeds leave native code; a declared feed without a snapshot is absent.
        #expect(Set(feeds.keys) == ["agent.weather"])
        guard case let .object(envelope)? = feeds["agent.weather"] else { return }
        #expect(envelope["observedAt"] == .number(Self.now.addingTimeInterval(-30).timeIntervalSince1970 * 1000))
        #expect(envelope["expectedIntervalSec"] == .number(900))
        #expect(envelope["error"] == .null)
        #expect(envelope["data"] == .object(["temperature": .number(64)]))
        #expect(envelope["history"] == .array([.object([
            "observedAt": .number(Self.now.addingTimeInterval(-930).timeIntervalSince1970 * 1000),
            "data": .object(["temperature": .number(61)]),
        ])]))
    }

    @Test
    func aQuarantinedWidgetIsLeftOutOfTheNextSnapshot() {
        let message = CockpitBoard.hostSnapshot(
            Self.snapshot(), deviceClass: .wide, theme: "light", platform: "mac",
            offsetMilliseconds: 0, excluded: ["weather"], selected: nil
        )
        guard case let .object(body) = message, case let .array(widgets)? = body["widgets"] else { return }
        #expect(widgets.count == 1)
        #expect(CockpitBoard.newest(Self.snapshot(), excluding: []) == "weather")
        #expect(CockpitBoard.newest(Self.snapshot(), excluding: ["weather"]) == "approvals")
    }

    @Test
    func theSchemeHandlerMayServeOnlyTheSnapshotsOwnHashes() {
        let allowed = CockpitBoard.allowedAssets(Self.snapshot())
        #expect(allowed == [Self.host, Self.realm, Self.kit, Self.weatherBundle, Self.approvalsBundle])
        #expect(CockpitBoard.allowedAssets(CockpitSnapshot(cursor: 0, runtime: nil, widgets: [])).isEmpty)
    }

    @Test
    func onlyAContentHashUnderTheBoardResolves() {
        let digest = Self.weatherBundle
        #expect(CockpitAssetRoute.digest(of: URL(string: "veetbot-cockpit://board/\(digest).js")) == digest)
        #expect(CockpitAssetRoute.digest(of: URL(string: "veetbot-cockpit://board/\(digest).html")) == digest)
        #expect(CockpitAssetRoute.digest(of: URL(string: "veetbot-cockpit://board/\(digest).css")) == digest)
        for raw in [
            "veetbot-cockpit://board/\(digest).png",
            "veetbot-cockpit://board/\(digest.uppercased()).js",
            "veetbot-cockpit://board/../v1/cockpit.js",
            "veetbot-cockpit://board/v1/cockpit/assets/\(digest)",
            "veetbot-cockpit://elsewhere/\(digest).js",
            "veetbot-cockpit://board/\(digest).js?token=1",
            "https://example.test/\(digest).js",
            "veetbot-cockpit://board/abc.js",
        ] {
            #expect(CockpitAssetRoute.digest(of: URL(string: raw)) == nil, "\(raw)")
        }
        #expect(CockpitAssetRoute.url(for: digest, extension: "html")?.absoluteString
            == "veetbot-cockpit://board/\(digest).html")
    }

    @Test
    func navigationAdmitsOnlyTheHostDocumentAndItsRealm() {
        let runtime = Self.snapshot().runtime
        func decide(_ raw: String, mainFrame: Bool) -> CockpitNavigationPolicy.Decision {
            CockpitNavigationPolicy.decide(url: URL(string: raw), isMainFrame: mainFrame, runtime: runtime)
        }
        #expect(decide("veetbot-cockpit://board/\(Self.host).html", mainFrame: true) == .allow)
        #expect(decide("veetbot-cockpit://board/\(Self.realm).html", mainFrame: false) == .allow)
        #expect(decide("about:blank", mainFrame: false) == .allow)
        #expect(decide("veetbot-cockpit://board/\(Self.realm).html", mainFrame: true) == .cancel)
        #expect(decide("veetbot-cockpit://board/\(Self.host).html", mainFrame: false) == .cancelSubframe)
        #expect(decide("https://example.test/", mainFrame: false) == .cancelSubframe)
        #expect(decide("https://example.test/", mainFrame: true) == .cancel)
        #expect(decide("about:blank", mainFrame: true) == .cancel)
        #expect(CockpitNavigationPolicy.decide(url: nil, isMainFrame: true, runtime: runtime) == .cancel)
        #expect(CockpitNavigationPolicy.decide(
            url: URL(string: "veetbot-cockpit://board/\(Self.host).html"), isMainFrame: true, runtime: nil
        ) == .cancel)
    }

    @Test
    func theBridgeAcceptsOnlyTheHostsOwnMessagesFromTheMainFrame() {
        func parse(_ body: Any, mainFrame: Bool = true) -> CockpitHostMessage? {
            CockpitHostMessage(body: body, isMainFrame: mainFrame)
        }
        #expect(parse(["type": "ready"]) == .ready)
        #expect(parse(["type": "loaded", "slug": "weather", "revision": "r1"]) == .loaded(slug: "weather", revision: "r1"))
        #expect(parse(["type": "tile_error", "slug": "weather", "revision": "r1"]) == .tileError(slug: "weather", revision: "r1"))
        #expect(parse(["type": "select", "slug": "weather"]) == .select(slug: "weather"))
        #expect(parse(["type": "quarantined", "slug": "weather", "revision": "r1", "reason": "navigated"])
            == .quarantined(slug: "weather", revision: "r1"))
        #expect(parse(["type": "ready"], mainFrame: false) == nil)
        #expect(parse(["type": "open_url", "url": "https://example.test"]) == nil)
        #expect(parse(["type": "select", "slug": 7]) == nil)
        #expect(parse(["type": "select", "slug": String(repeating: "x", count: 80)]) == nil)
        #expect(parse("ready") == nil)
    }

    @Test
    func theWebContentLosesItsNetworkPrimitivesAndEveryWebLoad() throws {
        let script = CockpitWebContent.removedPrimitivesScript
        for primitive in ["RTCPeerConnection", "WebSocket", "EventSource", "sendBeacon"] {
            #expect(script.contains(primitive), "\(primitive)")
        }
        let rules = try JSONSerialization.jsonObject(with: Data(CockpitWebContent.blockingRules.utf8))
        guard let list = rules as? [[String: [String: String]]] else { Issue.record("rules"); return }
        #expect(list.map { $0["trigger"]?["url-filter"] } == ["^https?://"])
        #expect(list.map { $0["action"]?["type"] } == ["block"])
    }
}
