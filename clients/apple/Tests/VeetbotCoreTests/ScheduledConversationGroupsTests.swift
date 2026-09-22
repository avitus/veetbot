import Foundation
import Testing

@testable import VeetbotCore

/// The sidebar's Scheduled section (scheduling.md, ADR-0113): a schedule's
/// sessions group by their `schedule_id` once there are two of them, apart
/// from folders and whether or not the server offers folders.
@Suite struct ScheduledConversationGroupsTests {
    private let briefing = UUID(uuidString: "00000000-0000-0000-0000-00000000a001")!
    private let digest = UUID(uuidString: "00000000-0000-0000-0000-00000000a002")!

    private func entry(
        _ number: Int,
        _ title: String,
        scheduleID: UUID? = nil,
        folderID: UUID? = nil
    ) -> SessionHistoryEntry {
        SessionHistoryEntry(
            sessionID: UUID(uuidString: "00000000-0000-0000-0000-0000000000\(String(format: "%02d", number))")!,
            title: title,
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: TimeInterval(number)),
            updatedAt: Date(timeIntervalSince1970: TimeInterval(100 - number)),
            lastRunID: nil,
            folderID: folderID,
            scheduleID: scheduleID
        )
    }

    private func folder(_ name: String) -> FolderView {
        FolderView(
            id: UUID(uuidString: "00000000-0000-0000-0000-00000000f001")!,
            name: name,
            threadCount: 0,
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 1)
        )
    }

    // History is in activity order, newest first, as the store returns it.
    private var history: [SessionHistoryEntry] {
        [
            entry(1, "Loose thread"),
            entry(2, "Weekday technology briefing", scheduleID: briefing),
            entry(3, "Morning digest", scheduleID: digest),
            entry(4, "Weekday tech briefing", scheduleID: briefing),
            entry(5, "Morning digest", scheduleID: digest),
            entry(6, "Weekday tech briefing", scheduleID: briefing),
        ]
    }

    @Test
    func testASchedulesSessionsGroupUnderItsNewestTitleInActivityOrder() throws {
        let grouped = GroupedConversationHistory.make(history: history, folders: [], available: true)
        #expect(grouped.uncategorized.map(\.title) == ["Loose thread"])
        #expect(grouped.schedules.map(\.scheduleID) == [briefing, digest])
        try #require(grouped.schedules.count == 2)
        #expect(grouped.schedules[0].title == "Weekday technology briefing")
        #expect(grouped.schedules[0].entries.map(\.sessionID) == [2, 4, 6].map { entry($0, "").sessionID })
        #expect(grouped.schedules[1].title == "Morning digest")
        #expect(grouped.schedules[1].entries.count == 2)
    }

    @Test
    func testASingleScheduledSessionStaysInTheHistory() {
        let history = [entry(1, "One-off reminder", scheduleID: briefing), entry(2, "Loose thread")]
        let grouped = GroupedConversationHistory.make(history: history, folders: [], available: true)
        #expect(grouped.schedules.isEmpty)
        #expect(grouped.uncategorized == history)
    }

    @Test
    func testSchedulesGroupWhenFoldersAreUnavailable() {
        let grouped = GroupedConversationHistory.make(history: history, folders: [], available: false)
        #expect(grouped.folders.isEmpty)
        #expect(grouped.schedules.map(\.scheduleID) == [briefing, digest])
        #expect(grouped.uncategorized.map(\.title) == ["Loose thread"])
    }

    @Test
    func testAFiledSessionStaysInItsFolder() throws {
        let work = folder("Work")
        let history = [
            entry(1, "Briefing", scheduleID: briefing, folderID: work.id),
            entry(2, "Briefing", scheduleID: briefing),
            entry(3, "Briefing", scheduleID: briefing),
        ]
        let grouped = GroupedConversationHistory.make(history: history, folders: [work], available: true)
        try #require(grouped.folders.count == 1)
        #expect(grouped.folders[0].entries.map(\.sessionID) == [history[0].sessionID])
        try #require(grouped.schedules.count == 1)
        #expect(grouped.schedules[0].entries.map(\.sessionID) == [history[1].sessionID, history[2].sessionID])
    }

    @Test
    func testEqualActivityOrdersGroupsByScheduleIdentifier() {
        let history = [
            entry(1, "Digest", scheduleID: digest),
            entry(2, "Briefing", scheduleID: briefing),
            entry(3, "Digest", scheduleID: digest),
            entry(4, "Briefing", scheduleID: briefing),
        ].map { value -> SessionHistoryEntry in
            var copy = value
            copy.updatedAt = Date(timeIntervalSince1970: 50)
            return copy
        }
        let grouped = GroupedConversationHistory.make(history: history, folders: [], available: true)
        #expect(grouped.schedules.map(\.scheduleID) == [briefing, digest])
    }

    @MainActor
    @Test
    func testGroupsStartCollapsedAndTheOwnersTogglesAreRememberedOnThisDevice() throws {
        let suite = "scheduled-groups-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }

        let preferences = ScheduleGroupSidebarPreferences(defaults: defaults)
        #expect(!preferences.isExpanded(briefing))
        preferences.setExpanded(true, schedule: briefing)
        #expect(preferences.isExpanded(briefing))
        #expect(!preferences.isExpanded(digest))

        let reloaded = ScheduleGroupSidebarPreferences(defaults: defaults)
        #expect(reloaded.isExpanded(briefing))
        reloaded.setExpanded(false, schedule: briefing)
        #expect(!ScheduleGroupSidebarPreferences(defaults: defaults).isExpanded(briefing))
    }

    @MainActor
    @Test
    func testUnreadableStoredGroupExpansionFallsBackToCollapsed() throws {
        let suite = "scheduled-groups-\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set(Data("not json".utf8), forKey: ScheduleGroupSidebarPreferences.expansionKey)
        #expect(!ScheduleGroupSidebarPreferences(defaults: defaults).isExpanded(briefing))
    }

    @Test
    func testTheSessionIndexScheduleIDIsReadFromMetadata() {
        func session(_ metadata: [String: JSONValue]) -> SessionView {
            SessionView(
                id: UUID(),
                status: .active,
                agentID: "general",
                agentVersion: "1",
                title: "Briefing",
                metadata: metadata,
                createdAt: Date(timeIntervalSince1970: 1),
                updatedAt: Date(timeIntervalSince1970: 2),
                activeRunID: nil,
                lastRunID: nil
            )
        }
        #expect(session(["schedule_id": .string(briefing.uuidString.lowercased())]).scheduleID == briefing)
        #expect(session(["schedule_id": .string("not-a-uuid")]).scheduleID == nil)
        #expect(session(["schedule_id": .number(7)]).scheduleID == nil)
        #expect(session([:]).scheduleID == nil)
    }

    @MainActor
    @Test
    func testMergedHistoryEntryTakesTheServerScheduleID() {
        let sessionID = UUID()
        let existing = SessionHistoryEntry(
            sessionID: sessionID,
            title: "Briefing",
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 10),
            lastRunID: nil,
            scheduleID: digest
        )
        func session(_ metadata: [String: JSONValue]) -> SessionView {
            SessionView(
                id: sessionID,
                status: .active,
                agentID: "general",
                agentVersion: "1",
                title: "Briefing",
                metadata: metadata,
                createdAt: existing.createdAt,
                updatedAt: Date(timeIntervalSince1970: 20),
                activeRunID: nil,
                lastRunID: nil
            )
        }
        let scheduled = ChatViewModel.mergedHistoryEntry(
            session: session(["schedule_id": .string(briefing.uuidString)]), existing: existing,
            lastRunID: nil, suggestedTitle: nil, touchedAt: nil
        )
        #expect(scheduled.scheduleID == briefing)
        let plain = ChatViewModel.mergedHistoryEntry(
            session: session([:]), existing: existing, lastRunID: nil,
            suggestedTitle: nil, touchedAt: nil
        )
        #expect(plain.scheduleID == nil)
    }

    @Test
    func testScheduleIDRoundTripsThroughTheFileStoreAndOlderFilesLoad() async throws {
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let file = directory.appendingPathComponent("history.json")
        let scheduled = entry(1, "Briefing", scheduleID: briefing)
        try await FileSessionHistoryStore(fileURL: file).upsert(scheduled)
        #expect(try await FileSessionHistoryStore(fileURL: file).list().first?.scheduleID == briefing)

        try Data(
            #"[{"sessionID":"00000000-0000-0000-0000-000000000123","title":"Legacy","agentID":"general","createdAt":"2026-08-12T12:00:00Z","updatedAt":"2026-08-12T12:00:01Z","lastRunID":null,"folderID":null}]"#
                .utf8
        ).write(to: file)
        let legacy = try await FileSessionHistoryStore(fileURL: file).list()
        #expect(legacy.map(\.title) == ["Legacy"])
        #expect(legacy.first?.scheduleID == nil)
    }

    #if XCODE_BUILD
    @available(macOS 14.0, iOS 17.0, *)
    @Test
    func testSwiftDataRecordPersistsAScheduleID() {
        let plain = entry(1, "Briefing")
        let record = LocalSessionRecord(entry: plain)
        let scheduled = entry(1, "Briefing", scheduleID: briefing)
        #expect(record.update(from: scheduled) == true)
        #expect(record.entry?.scheduleID == briefing)
        #expect(record.update(from: scheduled) == false)
    }
    #endif
}
