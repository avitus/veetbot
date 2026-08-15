import Foundation
import Testing

@testable import VeetbotCore

@Suite struct SessionHistoryStoreTests {
    @Test(arguments: HistoryBackend.allCases)
    func testEqualTimestampsUseTheSameDeterministicOrder(
        backend: HistoryBackend
    ) async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let store: any SessionHistoryStore =
            switch backend {
            case .file:
                try FileSessionHistoryStore(
                    fileURL: directory.appendingPathComponent("history.json")
                )
            case .volatile:
                VolatileSessionHistoryStore()
            #if XCODE_BUILD
            case .swiftData:
                if #available(macOS 14.0, iOS 17.0, *) {
                    try SwiftDataSessionHistoryStore(inMemory: true)
                } else {
                    VolatileSessionHistoryStore()
                }
            #endif
            }
        let timestamp = Date(timeIntervalSince1970: 10)
        let lowerID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000001"))
        let higherID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000002"))
        for id in [lowerID, higherID] {
            try await store.upsert(
                SessionHistoryEntry(
                    sessionID: id,
                    title: id.uuidString,
                    agentID: "general",
                    createdAt: timestamp,
                    updatedAt: timestamp,
                    lastRunID: nil
                )
            )
        }

        #expect(try await store.list().map(\.sessionID) == [higherID, lowerID])
    }

    @Test
    func testFileStorePersistsAndSortsLocalHistory() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("history.json")
        let older = SessionHistoryEntry(
            sessionID: UUID(),
            title: "Older",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastRunID: nil
        )
        let newer = SessionHistoryEntry(
            sessionID: UUID(),
            title: "Newer",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 3),
            updatedAt: Date(timeIntervalSince1970: 4),
            lastRunID: UUID()
        )
        let store = try FileSessionHistoryStore(fileURL: file)
        try await store.upsert(older)
        try await store.upsert(newer)
        let reloaded = try FileSessionHistoryStore(fileURL: file)
        let entries = await reloaded.list()
        #expect(entries.map(\.title) == ["Newer", "Older"])
        #expect(entries.first?.lastRunID == newer.lastRunID)
    }

    @Test
    func testCorruptHistoryRecoversOnNextUpsert() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("history.json")
        try Data("not-json".utf8).write(to: file)

        let store = try FileSessionHistoryStore(fileURL: file)
        #expect(await store.list().isEmpty)
        let entry = SessionHistoryEntry(
            sessionID: UUID(),
            title: "Recovered",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastRunID: nil
        )
        try await store.upsert(entry)
        let reloaded = try FileSessionHistoryStore(fileURL: file)
        #expect(await reloaded.list().map(\.title) == ["Recovered"])
    }

    @Test
    func testDuplicateHistoryIDsKeepMostRecentlyUpdatedEntry() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("history.json")
        let sessionID = UUID()
        let older = SessionHistoryEntry(
            sessionID: sessionID,
            title: "Older",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastRunID: nil
        )
        let newer = SessionHistoryEntry(
            sessionID: sessionID,
            title: "Newer",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 3),
            lastRunID: UUID()
        )
        try JSONEncoder.server.encode([older, newer]).write(to: file)

        let store = try FileSessionHistoryStore(fileURL: file)
        let entries = await store.list()
        #expect(entries.count == 1)
        #expect(entries.first?.title == "Newer")
    }

    @Test
    func testFileStoreDeletePersists() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("history.json")
        let entry = SessionHistoryEntry(
            sessionID: UUID(),
            title: "Remove me",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastRunID: nil
        )
        let store = try FileSessionHistoryStore(fileURL: file)
        try await store.upsert(entry)

        try await store.delete(sessionID: entry.sessionID)

        let reloaded = try FileSessionHistoryStore(fileURL: file)
        #expect(await reloaded.list().isEmpty)
    }

    @MainActor
    @Test
    func testRefreshingSelectedHistoryPreservesItsSortTimestamp() {
        let sessionID = UUID()
        let previousRunID = UUID()
        let refreshedRunID = UUID()
        let originalTimestamp = Date(timeIntervalSince1970: 10)
        let existing = SessionHistoryEntry(
            sessionID: sessionID,
            title: "Stable",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: originalTimestamp,
            lastRunID: previousRunID
        )
        let session = SessionView(
            id: sessionID,
            status: .active,
            agentID: "general",
            agentVersion: "1",
            title: nil,
            metadata: [:],
            createdAt: existing.createdAt,
            updatedAt: Date(timeIntervalSince1970: 20),
            activeRunID: refreshedRunID,
            lastRunID: refreshedRunID
        )

        let refreshed = ChatViewModel.mergedHistoryEntry(
            session: session,
            existing: existing,
            lastRunID: refreshedRunID,
            suggestedTitle: nil,
            touchedAt: nil
        )

        #expect(refreshed.updatedAt == originalTimestamp)
        #expect(refreshed.lastRunID == refreshedRunID)
        #expect(refreshed.title == existing.title)
    }

    @Test
    func testArtifactCacheEvictsLeastRecentlyUsedBytes() async {
        let firstID = UUID()
        let secondID = UUID()
        let cache = ArtifactCache(maximumBytes: 5)
        await cache.insert(CachedArtifactContent(data: Data([1, 2, 3]), etag: "one"), for: firstID)
        await cache.insert(
            CachedArtifactContent(data: Data([4, 5, 6]), etag: "two"), for: secondID)
        #expect(await cache.value(for: firstID) == nil)
        #expect(await cache.value(for: secondID)?.etag == "two")

        await cache.removeAll()
        #expect(await cache.value(for: secondID) == nil)
    }

    @Test
    func testFailedHistoryWriteDoesNotMutateMemory() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let unwritableTarget = directory.appendingPathComponent("directory", isDirectory: true)
        try FileManager.default.createDirectory(
            at: unwritableTarget, withIntermediateDirectories: true)
        let store = try FileSessionHistoryStore(fileURL: unwritableTarget)
        let entry = SessionHistoryEntry(
            sessionID: UUID(),
            title: "Must not stick",
            agentID: "general",
            createdAt: Date(),
            updatedAt: Date(),
            lastRunID: nil
        )
        do {
            try await store.upsert(entry)
            Issue.record("expected directory target to reject atomic file write")
        } catch {
            #expect(await store.list().isEmpty)
        }
    }
}

enum HistoryBackend: CaseIterable, Sendable {
    case file
    case volatile
    #if XCODE_BUILD
    case swiftData
    #endif
}
