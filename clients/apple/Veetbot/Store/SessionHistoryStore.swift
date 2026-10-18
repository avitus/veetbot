import Foundation
import OSLog

public struct SessionHistoryEntry: Codable, Identifiable, Hashable, Sendable {
    public let sessionID: UUID
    public var title: String
    public var agentID: String
    public var createdAt: Date
    public var updatedAt: Date
    public var lastRunID: UUID?
    /// The server-assigned folder; the server's value wins on every reconcile.
    public var folderID: UUID?
    /// The `schedule_id` metadata of a scheduled session; the server's value wins.
    public var scheduleID: UUID?

    public var id: UUID { sessionID }

    public init(
        sessionID: UUID,
        title: String,
        agentID: String,
        createdAt: Date,
        updatedAt: Date,
        lastRunID: UUID?,
        folderID: UUID? = nil,
        scheduleID: UUID? = nil
    ) {
        self.sessionID = sessionID
        self.title = title
        self.agentID = agentID
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.lastRunID = lastRunID
        self.folderID = folderID
        self.scheduleID = scheduleID
    }
}

public protocol SessionHistoryStore: Sendable {
    func list() async throws -> [SessionHistoryEntry]
    func upsert(_ entry: SessionHistoryEntry) async throws
    func delete(sessionID: UUID) async throws
}

extension Sequence where Element == SessionHistoryEntry {
    func sortedForHistoryList() -> [SessionHistoryEntry] {
        sorted {
            if $0.updatedAt == $1.updatedAt {
                return $0.sessionID.uuidString > $1.sessionID.uuidString
            }
            return $0.updatedAt > $1.updatedAt
        }
    }
}

public enum SessionHistoryStoreFactory {
    private static let logger = Logger(
        subsystem: "com.veetbot.apple",
        category: "session-history"
    )

    /// SwiftData keeps the cache. If its container cannot be opened, the
    /// failure is logged and this launch keeps history in memory; the
    /// authoritative session index refills it on connect (ADR-0211). Command
    /// Line Tools builds cannot link SwiftData and use the in-memory store.
    public static func makeDefault() -> any SessionHistoryStore {
        removeLegacyHistoryFile()
        #if XCODE_BUILD
        do {
            return try SwiftDataSessionHistoryStore()
        } catch {
            logger.error(
                "SwiftData history initialization failed: \(error.localizedDescription, privacy: .public)"
            )
        }
        #endif
        return VolatileSessionHistoryStore()
    }

    /// The retired file store's cache (ADR-0211) could still list conversations
    /// the owner deleted later, so it is removed rather than left orphaned.
    static func removeLegacyHistoryFile(applicationSupport: URL? = nil) {
        guard
            let support = applicationSupport
                ?? FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)
                .first
        else { return }
        let legacy = support.appendingPathComponent("Veetbot", isDirectory: true)
            .appendingPathComponent("session-history.json")
        guard FileManager.default.fileExists(atPath: legacy.path) else { return }
        do {
            try FileManager.default.removeItem(at: legacy)
        } catch {
            logger.error(
                "Retired history file removal failed: \(error.localizedDescription, privacy: .public)"
            )
        }
    }
}

public actor VolatileSessionHistoryStore: SessionHistoryStore {
    private var entries: [UUID: SessionHistoryEntry] = [:]

    public init() {}

    public func list() -> [SessionHistoryEntry] {
        entries.values.sortedForHistoryList()
    }

    public func upsert(_ entry: SessionHistoryEntry) {
        entries[entry.sessionID] = entry
    }

    public func delete(sessionID: UUID) {
        entries.removeValue(forKey: sessionID)
    }
}
