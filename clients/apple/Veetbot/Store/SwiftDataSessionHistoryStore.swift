#if XCODE_BUILD
import Foundation
import SwiftData

@available(iOS 17.0, macOS 14.0, *)
@Model
final class LocalSessionRecord {
    @Attribute(.unique) var sessionID: String
    var title: String
    var agentID: String
    var createdAt: Date
    var updatedAt: Date
    var lastRunID: String?

    init(entry: SessionHistoryEntry) {
        sessionID = entry.sessionID.uuidString
        title = entry.title
        agentID = entry.agentID
        createdAt = entry.createdAt
        updatedAt = entry.updatedAt
        lastRunID = entry.lastRunID?.uuidString
    }

    func update(from entry: SessionHistoryEntry) {
        title = entry.title
        agentID = entry.agentID
        createdAt = entry.createdAt
        updatedAt = entry.updatedAt
        lastRunID = entry.lastRunID?.uuidString
    }

    var entry: SessionHistoryEntry? {
        guard let id = UUID(uuidString: sessionID) else { return nil }
        return SessionHistoryEntry(
            sessionID: id,
            title: title,
            agentID: agentID,
            createdAt: createdAt,
            updatedAt: updatedAt,
            lastRunID: lastRunID.flatMap(UUID.init(uuidString:))
        )
    }
}

@available(iOS 17.0, macOS 14.0, *)
public actor SwiftDataSessionHistoryStore: SessionHistoryStore {
    private let container: ModelContainer

    public init(inMemory: Bool = false) throws {
        let configuration = ModelConfiguration(isStoredInMemoryOnly: inMemory)
        container = try ModelContainer(
            for: LocalSessionRecord.self,
            configurations: configuration
        )
    }

    public func list() throws -> [SessionHistoryEntry] {
        let context = ModelContext(container)
        return try context.fetch(FetchDescriptor<LocalSessionRecord>())
            .compactMap(\.entry)
            .sortedForHistoryList()
    }

    public func upsert(_ entry: SessionHistoryEntry) throws {
        let context = ModelContext(container)
        let identifier = entry.sessionID.uuidString
        let descriptor = FetchDescriptor<LocalSessionRecord>(
            predicate: #Predicate { $0.sessionID == identifier }
        )
        if let existing = try context.fetch(descriptor).first {
            existing.update(from: entry)
        } else {
            context.insert(LocalSessionRecord(entry: entry))
        }
        try context.save()
    }

    public func delete(sessionID: UUID) throws {
        let context = ModelContext(container)
        let identifier = sessionID.uuidString
        let descriptor = FetchDescriptor<LocalSessionRecord>(
            predicate: #Predicate { $0.sessionID == identifier }
        )
        for record in try context.fetch(descriptor) {
            context.delete(record)
        }
        try context.save()
    }
}
#endif
