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
    var folderID: String?
    var scheduleID: String?

    init(entry: SessionHistoryEntry) {
        sessionID = entry.sessionID.uuidString
        title = entry.title
        agentID = entry.agentID
        createdAt = entry.createdAt
        updatedAt = entry.updatedAt
        lastRunID = entry.lastRunID?.uuidString
        folderID = entry.folderID?.uuidString
        scheduleID = entry.scheduleID?.uuidString
    }

    @discardableResult
    func update(from entry: SessionHistoryEntry) -> Bool {
        let nextLastRunID = entry.lastRunID?.uuidString
        let nextFolderID = entry.folderID?.uuidString
        let nextScheduleID = entry.scheduleID?.uuidString
        guard title != entry.title
            || agentID != entry.agentID
            || createdAt != entry.createdAt
            || updatedAt != entry.updatedAt
            || lastRunID != nextLastRunID
            || folderID != nextFolderID
            || scheduleID != nextScheduleID
        else { return false }

        title = entry.title
        agentID = entry.agentID
        createdAt = entry.createdAt
        updatedAt = entry.updatedAt
        lastRunID = nextLastRunID
        folderID = nextFolderID
        scheduleID = nextScheduleID
        return true
    }

    var entry: SessionHistoryEntry? {
        guard let id = UUID(uuidString: sessionID) else { return nil }
        return SessionHistoryEntry(
            sessionID: id,
            title: title,
            agentID: agentID,
            createdAt: createdAt,
            updatedAt: updatedAt,
            lastRunID: lastRunID.flatMap(UUID.init(uuidString:)),
            folderID: folderID.flatMap(UUID.init(uuidString:)),
            scheduleID: scheduleID.flatMap(UUID.init(uuidString:))
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
        let changed: Bool
        if let existing = try context.fetch(descriptor).first {
            changed = existing.update(from: entry)
        } else {
            context.insert(LocalSessionRecord(entry: entry))
            changed = true
        }
        if changed { try context.save() }
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
