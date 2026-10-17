import Foundation
import Testing

@testable import VeetbotCore

@Suite struct SessionHistoryStoreTests {
    #if XCODE_BUILD
    @Test
    func testSwiftDataRecordSkipsUnchangedUpdates() {
        let entry = SessionHistoryEntry(
            sessionID: UUID(),
            title: "Stable",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastRunID: UUID()
        )
        let record = LocalSessionRecord(entry: entry)

        #expect(record.update(from: entry) == false)

        var changed = entry
        changed.title = "Changed"
        #expect(record.update(from: changed) == true)
        #expect(record.title == "Changed")
    }
    #endif

    @Test(arguments: HistoryBackend.allCases)
    func testEqualTimestampsUseTheSameDeterministicOrder(
        backend: HistoryBackend
    ) async throws {
        let store = try backend.makeStore()
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

    @Test(arguments: HistoryBackend.allCases)
    func testDeleteRemovesTheEntryFromEveryStore(backend: HistoryBackend) async throws {
        let store = try backend.makeStore()
        let entry = SessionHistoryEntry(
            sessionID: UUID(),
            title: "Remove me",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastRunID: nil
        )
        try await store.upsert(entry)

        try await store.delete(sessionID: entry.sessionID)

        #expect(try await store.list().isEmpty)
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

    @Test(arguments: HistoryBackend.allCases)
    func testFolderIDRoundTripsThroughEveryStore(backend: HistoryBackend) async throws {
        let store = try backend.makeStore()
        let folderID = UUID()
        let entry = SessionHistoryEntry(
            sessionID: UUID(),
            title: "Filed",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastRunID: nil,
            folderID: folderID
        )
        try await store.upsert(entry)
        #expect(try await store.list().first?.folderID == folderID)
        var unfiled = entry
        unfiled.folderID = nil
        try await store.upsert(unfiled)
        #expect(try await store.list().first?.folderID == nil)
    }

    @MainActor
    @Test
    func testMergedHistoryEntryTakesTheServerFolderID() {
        let sessionID = UUID()
        let previous = UUID()
        let next = UUID()
        let existing = SessionHistoryEntry(
            sessionID: sessionID,
            title: "Filed",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 10),
            lastRunID: nil,
            folderID: previous
        )
        func session(folderID: UUID?) -> SessionView {
            SessionView(
                id: sessionID,
                status: .active,
                agentID: "general",
                agentVersion: "1",
                title: "Filed",
                metadata: [:],
                createdAt: existing.createdAt,
                updatedAt: Date(timeIntervalSince1970: 20),
                activeRunID: nil,
                lastRunID: nil,
                folderID: folderID
            )
        }
        let moved = ChatViewModel.mergedHistoryEntry(
            session: session(folderID: next), existing: existing, lastRunID: nil,
            suggestedTitle: nil, touchedAt: nil
        )
        #expect(moved.folderID == next)
        #expect(moved.updatedAt == existing.updatedAt)
        let unfiled = ChatViewModel.mergedHistoryEntry(
            session: session(folderID: nil), existing: existing, lastRunID: nil,
            suggestedTitle: nil, touchedAt: nil
        )
        #expect(unfiled.folderID == nil)
    }

    #if XCODE_BUILD
    @Test
    func testSwiftDataRecordPersistsAFolderChange() {
        let entry = SessionHistoryEntry(
            sessionID: UUID(),
            title: "Stable",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2),
            lastRunID: nil
        )
        let record = LocalSessionRecord(entry: entry)
        var filed = entry
        filed.folderID = UUID()
        #expect(record.update(from: filed) == true)
        #expect(record.entry?.folderID == filed.folderID)
        #expect(record.update(from: filed) == false)
    }
    #endif

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
}

/// The local-history backends: SwiftData in the app, and the in-memory store
/// it falls back to and that package builds without SwiftData use (ADR-0211).
enum HistoryBackend: CaseIterable, Sendable {
    case volatile
    #if XCODE_BUILD
    case swiftData
    #endif

    func makeStore() throws -> any SessionHistoryStore {
        switch self {
        case .volatile:
            VolatileSessionHistoryStore()
        #if XCODE_BUILD
        case .swiftData:
            try SwiftDataSessionHistoryStore(inMemory: true)
        #endif
        }
    }
}
