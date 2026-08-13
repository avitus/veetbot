import Foundation

public struct SessionHistoryEntry: Codable, Identifiable, Hashable, Sendable {
    public let sessionID: UUID
    public var title: String
    public var agentID: String
    public var createdAt: Date
    public var updatedAt: Date
    public var lastRunID: UUID?

    public var id: UUID { sessionID }

    public init(
        sessionID: UUID,
        title: String,
        agentID: String,
        createdAt: Date,
        updatedAt: Date,
        lastRunID: UUID?
    ) {
        self.sessionID = sessionID
        self.title = title
        self.agentID = agentID
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.lastRunID = lastRunID
    }
}

public protocol SessionHistoryStore: Sendable {
    func list() async throws -> [SessionHistoryEntry]
    func upsert(_ entry: SessionHistoryEntry) async throws
    func delete(sessionID: UUID) async throws
}

public enum SessionHistoryStoreFactory {
    public static func makeDefault() -> any SessionHistoryStore {
#if XCODE_BUILD
        if #available(iOS 17.0, macOS 14.0, *) {
            if let store = try? SwiftDataSessionHistoryStore() {
                return store
            }
        }
#endif
        if let store = try? FileSessionHistoryStore() {
            return store
        }
        return VolatileSessionHistoryStore()
    }
}

public actor VolatileSessionHistoryStore: SessionHistoryStore {
    private var entries: [UUID: SessionHistoryEntry] = [:]

    public init() {}

    public func list() -> [SessionHistoryEntry] {
        entries.values.sorted {
            if $0.updatedAt == $1.updatedAt {
                return $0.sessionID.uuidString > $1.sessionID.uuidString
            }
            return $0.updatedAt > $1.updatedAt
        }
    }

    public func upsert(_ entry: SessionHistoryEntry) {
        entries[entry.sessionID] = entry
    }

    public func delete(sessionID: UUID) {
        entries.removeValue(forKey: sessionID)
    }
}
