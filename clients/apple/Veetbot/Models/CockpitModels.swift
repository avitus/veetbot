import Foundation

/// The Cockpit board as `GET /v1/cockpit` returns it (docs/plan/cockpit.md,
/// the routes). Held in presentation memory only; feed data and private
/// settings never reach disk.
public struct CockpitSnapshot: Decodable, Equatable, Sendable {
    public let cursor: Int
    /// The server's clock when it took the snapshot; widgets render with server-corrected time.
    public let serverTime: Date?
    public let runtime: CockpitRuntime?
    public let widgets: [CockpitWidget]
    public let layouts: [CockpitDeviceClass: CockpitLayout]
    public let feeds: [String: CockpitFeed]

    enum CodingKeys: String, CodingKey {
        case cursor, runtime, widgets, layouts, feeds
        case serverTime = "server_time"
    }

    public init(
        cursor: Int, serverTime: Date? = nil, runtime: CockpitRuntime?, widgets: [CockpitWidget],
        layouts: [CockpitDeviceClass: CockpitLayout] = [:], feeds: [String: CockpitFeed] = [:]
    ) {
        self.cursor = cursor
        self.serverTime = serverTime
        self.runtime = runtime
        self.widgets = widgets
        self.layouts = layouts
        self.feeds = feeds
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        cursor = try container.decode(Int.self, forKey: .cursor)
        serverTime = try container.decodeIfPresent(Date.self, forKey: .serverTime)
        runtime = try container.decodeIfPresent(CockpitRuntime.self, forKey: .runtime)
        widgets = try container.decode([CockpitWidget].self, forKey: .widgets)
        let keyed = try container.decode([String: CockpitLayout].self, forKey: .layouts)
        // A device class this client does not know is another client's board.
        layouts = Dictionary(uniqueKeysWithValues: keyed.compactMap { key, layout in
            CockpitDeviceClass(rawValue: key).map { ($0, layout) }
        })
        feeds = try container.decode([String: CockpitFeed].self, forKey: .feeds)
    }
}

/// The three boards a principal arranges: one per device class.
public enum CockpitDeviceClass: String, Codable, CaseIterable, Sendable {
    case compact, regular, wide
}

/// The host page and realm assets a release exported, by content hash.
public struct CockpitRuntime: Decodable, Equatable, Sendable {
    public let kitAPI: Int
    /// The host page's content hash.
    public let entry: String
    /// Logical asset name to content hash.
    public let assets: [String: String]

    enum CodingKeys: String, CodingKey {
        case entry, assets
        case kitAPI = "kit_api"
    }

    public init(kitAPI: Int, entry: String, assets: [String: String]) {
        self.kitAPI = kitAPI
        self.entry = entry
        self.assets = assets
    }
}

/// One live widget: its revision, bundle and manifest, and the owner's private settings.
public struct CockpitWidget: Decodable, Equatable, Identifiable, Sendable {
    public let slug: String
    public let revisionID: UUID
    public let bundleSHA256: String
    public let kitAPI: Int
    public let manifest: [String: JSONValue]
    public let settings: [String: JSONValue]
    /// When the live revision last changed; the watchdog quarantines the newest first.
    public let changedAt: Date?

    public var id: String { slug }

    enum CodingKeys: String, CodingKey {
        case slug, manifest, settings
        case revisionID = "revision_id"
        case bundleSHA256 = "bundle_sha256"
        case kitAPI = "kit_api"
        case changedAt = "changed_at"
    }

    public init(
        slug: String, revisionID: UUID, bundleSHA256: String, kitAPI: Int = 1,
        manifest: [String: JSONValue], settings: [String: JSONValue] = [:], changedAt: Date? = nil
    ) {
        self.slug = slug
        self.revisionID = revisionID
        self.bundleSHA256 = bundleSHA256
        self.kitAPI = kitAPI
        self.manifest = manifest
        self.settings = settings
        self.changedAt = changedAt
    }

    public var title: String {
        if case let .string(title)? = manifest["title"], !title.isEmpty { return title }
        return slug
    }

    public var purpose: String? {
        if case let .string(purpose)? = manifest["purpose"], !purpose.isEmpty { return purpose }
        return nil
    }

    /// The tile sizes the manifest offers, in its order.
    public var sizes: [String] {
        guard case let .array(values)? = manifest["sizes"] else { return [] }
        return values.compactMap { if case let .string(size) = $0 { size } else { nil } }
    }

    /// The manifest schema's ceiling on a declared feed's history depth.
    static let maximumHistory = 288

    /// The feeds the manifest declares, with the history depth each asks for.
    public var declaredFeeds: [CockpitDeclaredFeed] {
        guard case let .array(values)? = manifest["feeds"] else { return [] }
        return values.compactMap { value in
            guard case let .object(feed) = value, case let .string(id)? = feed["id"] else { return nil }
            var history = 0
            if case let .number(depth)? = feed["history"], depth > 0, depth.isFinite {
                history = Int(min(depth, Double(Self.maximumHistory)))
            }
            return CockpitDeclaredFeed(id: id, history: history)
        }
    }
}

public struct CockpitDeclaredFeed: Equatable, Sendable {
    public let id: String
    public let history: Int
}

public struct CockpitLayout: Codable, Equatable, Sendable {
    public let revision: Int
    public let placements: [CockpitPlacement]

    public init(revision: Int, placements: [CockpitPlacement]) {
        self.revision = revision
        self.placements = placements
    }
}

public struct CockpitPlacement: Codable, Equatable, Hashable, Sendable {
    public let slug: String
    public let size: String

    public init(slug: String, size: String) {
        self.slug = slug
        self.size = size
    }
}

/// A declared feed's current snapshot and, oldest first, the history before it.
public struct CockpitFeed: Decodable, Equatable, Sendable {
    public let trust: String
    public let current: CockpitFeedEntry?
    public let history: [CockpitFeedEntry]

    public init(trust: String = "platform", current: CockpitFeedEntry?, history: [CockpitFeedEntry] = []) {
        self.trust = trust
        self.current = current
        self.history = history
    }
}

public struct CockpitFeedEntry: Decodable, Equatable, Sendable {
    public let observedAt: Date
    public let expectedIntervalSeconds: Int
    public let error: String?
    public let data: JSONValue
    public let schemaVersion: Int

    enum CodingKeys: String, CodingKey {
        case error, data
        case observedAt = "observed_at"
        case expectedIntervalSeconds = "expected_interval_seconds"
        case schemaVersion = "schema_version"
    }

    public init(
        observedAt: Date, expectedIntervalSeconds: Int, error: String? = nil, data: JSONValue,
        schemaVersion: Int = 1
    ) {
        self.observedAt = observedAt
        self.expectedIntervalSeconds = expectedIntervalSeconds
        self.error = error
        self.data = data
        self.schemaVersion = schemaVersion
    }

    /// The freshness level the kit computes, with the approved thresholds:
    /// live to 1.5 intervals, aging to three, stale beyond; an error is an error.
    public func freshness(at now: Date) -> CockpitFreshness {
        if error != nil { return .error }
        let age = now.timeIntervalSince(observedAt)
        let interval = Double(max(expectedIntervalSeconds, 1))
        if age <= interval * 1.5 { return .live }
        if age <= interval * 3 { return .aging }
        return .stale
    }
}

public enum CockpitFreshness: String, Sendable {
    case pending, live, aging, stale, error
}

public struct CockpitDiagnostic: Decodable, Equatable, Sendable {
    public let rule: String
    public let file: String
    public let message: String
    public let line: Int?
}

public struct CockpitRevision: Decodable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let state: String
    public let source: String
    public let commitSHA: String?
    public let commitURL: URL?
    public let diagnostics: [CockpitDiagnostic]
    public let createdAt: Date
    public let updatedAt: Date

    enum CodingKeys: String, CodingKey {
        case id, state, source, diagnostics
        case commitSHA = "commit_sha"
        case commitURL = "commit_url"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

public struct CockpitImportFailure: Decodable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let commitSHA: String
    public let commitURL: URL?
    public let diagnostics: [CockpitDiagnostic]
    public let seen: Bool
    public let createdAt: Date

    enum CodingKeys: String, CodingKey {
        case id, diagnostics, seen
        case commitSHA = "commit_sha"
        case commitURL = "commit_url"
        case createdAt = "created_at"
    }
}

/// `GET /v1/cockpit/widgets/{slug}`: revisions with commit links, import failures, the settings schema.
public struct CockpitWidgetDetail: Decodable, Equatable, Sendable {
    public let slug: String
    public let state: String
    public let liveRevisionID: UUID?
    public let settingsSchema: [String: JSONValue]?
    public let settings: [String: JSONValue]
    public let revisions: [CockpitRevision]
    public let importFailures: [CockpitImportFailure]

    enum CodingKeys: String, CodingKey {
        case slug, state, settings, revisions
        case liveRevisionID = "live_revision_id"
        case settingsSchema = "settings_schema"
        case importFailures = "import_failures"
    }
}

/// An owner change the outbox will commit: a revision's state, or `queued` for a removal.
public struct CockpitChange: Decodable, Equatable, Sendable {
    public let slug: String
    public let operation: String
    public let state: String
    public let revisionID: UUID?
    public let diagnostics: [CockpitDiagnostic]

    enum CodingKeys: String, CodingKey {
        case slug, operation, state, diagnostics
        case revisionID = "revision_id"
    }
}
