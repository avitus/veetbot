import Foundation

/// What native code gives the Cockpit host page (docs/plan/cockpit.md, "The
/// host and the realm"). Pure, so every rule is testable without WebKit: the
/// board's placements for one device class, the host's `snapshot` message with
/// only declared feeds, and the content hashes the scheme handler may serve.
public enum CockpitBoard {
    public struct Placed: Equatable, Sendable {
        public let widget: CockpitWidget
        public let size: String
    }

    /// The device class's layout, keeping only live widgets at sizes their
    /// manifests offer; without one, every widget at its first size.
    public static func placements(_ snapshot: CockpitSnapshot, deviceClass: CockpitDeviceClass) -> [Placed] {
        let live = Dictionary(snapshot.widgets.map { ($0.slug, $0) }, uniquingKeysWith: { first, _ in first })
        guard let layout = snapshot.layouts[deviceClass] else {
            return snapshot.widgets.map { Placed(widget: $0, size: $0.sizes.first ?? "md") }
        }
        var placed: [Placed] = []
        var seen: Set<String> = []
        for placement in layout.placements {
            guard let widget = live[placement.slug], widget.sizes.contains(placement.size),
                  seen.insert(placement.slug).inserted
            else { continue }
            placed.append(Placed(widget: widget, size: placement.size))
        }
        return placed
    }

    /// The host's `snapshot` message: each placed widget with only its declared
    /// feeds, instants in epoch milliseconds, and nothing a widget did not declare.
    public static func hostSnapshot(
        _ snapshot: CockpitSnapshot, deviceClass: CockpitDeviceClass, theme: String, platform: String,
        offsetMilliseconds: Double, excluded: Set<String>, selected: String?
    ) -> JSONValue {
        let placed = placements(snapshot, deviceClass: deviceClass).filter { !excluded.contains($0.widget.slug) }
        var declared: Set<String> = []
        let widgets: [JSONValue] = placed.map { item in
            let feeds = item.widget.declaredFeeds
            declared.formUnion(feeds.map(\.id))
            return .object([
                "slug": .string(item.widget.slug),
                "revision": .string(item.widget.revisionID.uuidString.lowercased()),
                "bundle": .string("\(item.widget.bundleSHA256).js"),
                "title": .string(item.widget.title),
                "feeds": .array(feeds.map { .object(["id": .string($0.id), "history": .number(Double($0.history))]) }),
                "size": .string(item.size),
                "settings": .object(item.widget.settings),
                "changedAt": .number(milliseconds(item.widget.changedAt ?? .distantPast)),
                "selected": .bool(item.widget.slug == selected),
            ])
        }
        var feeds: [String: JSONValue] = [:]
        for id in declared.sorted() {
            guard let feed = snapshot.feeds[id], let current = feed.current else { continue }
            feeds[id] = .object([
                "observedAt": .number(milliseconds(current.observedAt)),
                "expectedIntervalSec": .number(Double(current.expectedIntervalSeconds)),
                "error": current.error.map(JSONValue.string) ?? .null,
                "data": current.data,
                "history": .array(feed.history.map {
                    .object(["observedAt": .number(milliseconds($0.observedAt)), "data": $0.data])
                }),
            ])
        }
        return .object([
            "type": .string("snapshot"),
            "theme": .string(theme),
            "platform": .string(platform),
            "offsetMs": .number(offsetMilliseconds),
            "widgets": .array(widgets),
            "feeds": .object(feeds),
        ])
    }

    /// The widget whose live revision changed most recently: the watchdog's quarantine candidate.
    public static func newest(_ snapshot: CockpitSnapshot, excluding: Set<String>) -> String? {
        snapshot.widgets
            .filter { !excluding.contains($0.slug) }
            .max { ($0.changedAt ?? .distantPast) < ($1.changedAt ?? .distantPast) }?
            .slug
    }

    /// Every content hash the current snapshot names; the scheme handler serves nothing else.
    public static func allowedAssets(_ snapshot: CockpitSnapshot) -> Set<String> {
        guard let runtime = snapshot.runtime else { return [] }
        var allowed = Set(runtime.assets.values)
        allowed.insert(runtime.entry)
        allowed.formUnion(snapshot.widgets.map(\.bundleSHA256))
        return allowed.filter(CockpitAssetRoute.isDigest)
    }

    static func milliseconds(_ date: Date) -> Double {
        (date.timeIntervalSince1970 * 1000).rounded()
    }
}

/// `veetbot-cockpit://board/<sha256>.<html|js|css>`: the only URLs the scheme handler resolves.
public enum CockpitAssetRoute {
    public static let scheme = "veetbot-cockpit"
    static let host = "board"
    static let extensions: Set<String> = ["html", "js", "css"]

    public static func digest(of url: URL?) -> String? {
        guard let url, url.scheme == scheme, url.host == host, url.query == nil, url.fragment == nil,
              url.user == nil, url.port == nil
        else { return nil }
        let components = url.path.split(separator: "/", omittingEmptySubsequences: false)
        guard components.count == 2, components[0].isEmpty else { return nil }
        let name = components[1].split(separator: ".")
        guard name.count == 2, extensions.contains(String(name[1])), isDigest(String(name[0])) else { return nil }
        return String(name[0])
    }

    public static func url(for digest: String, extension pathExtension: String) -> URL? {
        guard isDigest(digest), extensions.contains(pathExtension) else { return nil }
        return URL(string: "\(scheme)://\(host)/\(digest).\(pathExtension)")
    }

    static func isDigest(_ value: String) -> Bool {
        value.count == 64 && value.allSatisfy { ("0"..."9").contains($0) || ("a"..."f").contains($0) }
    }
}

/// Where the Cockpit web view may go: the host document in the main frame and
/// the realm document in its frame, and nowhere else (docs/plan/cockpit.md).
public enum CockpitNavigationPolicy {
    public enum Decision: Equatable, Sendable {
        case allow
        case cancel
        /// The realm tried to leave its document: cancel, and quarantine its newest widget.
        case cancelSubframe
    }

    public static func decide(url: URL?, isMainFrame: Bool, runtime: CockpitRuntime?) -> Decision {
        guard let runtime, let url else { return isMainFrame ? .cancel : .cancelSubframe }
        if isMainFrame {
            return CockpitAssetRoute.digest(of: url) == runtime.entry && url.pathExtension == "html" ? .allow : .cancel
        }
        if url.absoluteString == "about:blank" { return .allow }
        if let realm = runtime.assets["realm.html"], CockpitAssetRoute.digest(of: url) == realm,
           url.pathExtension == "html"
        {
            return .allow
        }
        return .cancelSubframe
    }
}

/// The host page's messages to native code; anything else is dropped.
public enum CockpitHostMessage: Equatable, Sendable {
    case ready
    case loaded(slug: String, revision: String)
    case tileError(slug: String, revision: String)
    case select(slug: String)
    case quarantined(slug: String, revision: String)

    private static let maximumIdentifier = 64

    /// Accepts only the main frame's (the host page's) well-formed messages.
    public init?(body: Any, isMainFrame: Bool) {
        guard isMainFrame, let message = body as? [String: Any], let type = message["type"] as? String
        else { return nil }
        func identifier(_ key: String) -> String? {
            guard let value = message[key] as? String, !value.isEmpty, value.count <= Self.maximumIdentifier
            else { return nil }
            return value
        }
        switch type {
        case "ready":
            self = .ready
        case "loaded":
            guard let slug = identifier("slug"), let revision = identifier("revision") else { return nil }
            self = .loaded(slug: slug, revision: revision)
        case "tile_error":
            guard let slug = identifier("slug"), let revision = identifier("revision") else { return nil }
            self = .tileError(slug: slug, revision: revision)
        case "select":
            guard let slug = identifier("slug") else { return nil }
            self = .select(slug: slug)
        case "quarantined":
            guard let slug = identifier("slug"), let revision = identifier("revision") else { return nil }
            self = .quarantined(slug: slug, revision: revision)
        default:
            return nil
        }
    }
}

/// The web view's own defences beside the host's CSP: every web load blocked,
/// and the network primitives removed in every frame before any script runs.
public enum CockpitWebContent {
    public static let messageHandlerName = "cockpit"

    public static let removedPrimitivesScript = """
    (() => {
      for (const name of ['RTCPeerConnection', 'webkitRTCPeerConnection', 'WebSocket', 'EventSource']) {
        try { Object.defineProperty(window, name, { value: undefined, configurable: false, writable: false }); } catch (_) {}
      }
      try { Object.defineProperty(Navigator.prototype, 'sendBeacon', { value: undefined, configurable: false, writable: false }); } catch (_) {}
    })();
    """

    public static let blockingRules = """
    [{"trigger":{"url-filter":"^https?://"},"action":{"type":"block"}}]
    """
}
