import Foundation

public actor FileSessionHistoryStore: SessionHistoryStore {
    private let fileURL: URL
    private var entries: [UUID: SessionHistoryEntry]

    public init(fileURL: URL? = nil) throws {
        if let fileURL {
            self.fileURL = fileURL
        } else {
            let root = try FileManager.default.url(
                for: .applicationSupportDirectory,
                in: .userDomainMask,
                appropriateFor: nil,
                create: true
            )
            let directory = root.appendingPathComponent("Veetbot", isDirectory: true)
            try FileManager.default.createDirectory(
                at: directory,
                withIntermediateDirectories: true
            )
            self.fileURL = directory.appendingPathComponent("session-history.json")
        }
        if let data = try? Data(contentsOf: self.fileURL),
           let decoded = try? JSONDecoder.server.decode([SessionHistoryEntry].self, from: data)
        {
            entries = Dictionary(
                decoded.map { ($0.sessionID, $0) },
                uniquingKeysWith: { first, second in
                    first.updatedAt >= second.updatedAt ? first : second
                }
            )
        } else {
            // A corrupt local cache must not disable future persistence. The next
            // upsert atomically replaces it with valid JSON.
            entries = [:]
        }
    }

    public func list() -> [SessionHistoryEntry] {
        entries.values.sorted {
            if $0.updatedAt == $1.updatedAt { return $0.sessionID.uuidString > $1.sessionID.uuidString }
            return $0.updatedAt > $1.updatedAt
        }
    }

    public func upsert(_ entry: SessionHistoryEntry) throws {
        var updated = entries
        updated[entry.sessionID] = entry
        try persist(updated)
        entries = updated
    }

    public func delete(sessionID: UUID) throws {
        var updated = entries
        updated.removeValue(forKey: sessionID)
        try persist(updated)
        entries = updated
    }

    private func persist(_ updated: [UUID: SessionHistoryEntry]) throws {
        let data = try JSONEncoder.pretty.encode(Array(updated.values))
        try data.write(to: fileURL, options: .atomic)
    }
}
