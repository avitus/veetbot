import Foundation
import Testing

@testable import VeetbotCore

@Suite struct ConversationFoldersTests {
    private func entry(_ number: Int, _ title: String, folderID: UUID? = nil) -> SessionHistoryEntry {
        SessionHistoryEntry(
            sessionID: UUID(uuidString: "00000000-0000-0000-0000-0000000000\(String(format: "%02d", number))")!,
            title: title,
            agentID: "general",
            createdAt: Date(timeIntervalSince1970: TimeInterval(number)),
            updatedAt: Date(timeIntervalSince1970: TimeInterval(100 - number)),
            lastRunID: nil,
            folderID: folderID
        )
    }

    private func folder(_ number: Int, _ name: String) -> FolderView {
        FolderView(
            id: UUID(uuidString: "00000000-0000-0000-0000-00000000f0\(String(format: "%02d", number))")!,
            name: name,
            threadCount: 0,
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 1)
        )
    }

    @Test
    func testUncategorizedComeFirstAndFoldersSortCaseInsensitively() throws {
        let work = folder(1, "Work")
        let travel = folder(2, "travel")
        let history = [
            entry(1, "Flights", folderID: travel.id),
            entry(2, "Loose thread"),
            entry(3, "Quarterly plan", folderID: work.id),
            entry(4, "Another loose thread"),
        ]
        let grouped = GroupedConversationHistory.make(history: history, folders: [work, travel], available: true)
        #expect(grouped.uncategorized.map(\.title) == ["Loose thread", "Another loose thread"])
        #expect(grouped.folders.map(\.folder.name) == ["travel", "Work"])
        try #require(grouped.folders.count == 2)
        #expect(grouped.folders[0].entries.map(\.title) == ["Flights"])
        #expect(grouped.folders[1].entries.map(\.title) == ["Quarterly plan"])
    }

    @Test
    func testUnknownFolderIDsFallBackToUncategorizedAndEmptyFoldersAreListed() throws {
        let work = folder(1, "Work")
        let history = [entry(1, "Orphan", folderID: UUID())]
        let grouped = GroupedConversationHistory.make(history: history, folders: [work], available: true)
        #expect(grouped.uncategorized.map(\.title) == ["Orphan"])
        #expect(grouped.folders.map(\.id) == [work.id])
        try #require(grouped.folders.count == 1)
        #expect(grouped.folders[0].entries.isEmpty)
    }

    @Test
    func testUnavailableFoldersFlattenToTodaysHistory() {
        let work = folder(1, "Work")
        let history = [entry(1, "Filed", folderID: work.id), entry(2, "Loose")]
        let grouped = GroupedConversationHistory.make(history: history, folders: [work], available: false)
        #expect(grouped.uncategorized == history)
        #expect(grouped.folders.isEmpty)
    }

    @Test
    func testProposalHeadlinesResolveMembersAndHideUnknownKinds() throws {
        let work = folder(1, "Work")
        let first = entry(1, "Lisbon flights")
        let second = entry(2, "Lisbon hotel")
        let unknownMember = UUID()
        let newFolder = try JSONDecoder.server.decode(
            FolderProposalView.self,
            from: Data(
                #"{"id":"00000000-0000-0000-0000-0000000000e1","kind":"new_folder","proposed_name":"Lisbon Trip","target_folder_id":null,"member_session_ids":["\#(first.sessionID.uuidString)","\#(second.sessionID.uuidString)","\#(unknownMember.uuidString)"],"rationale":null,"derivation":"lexical","state":"proposed","withdrawal_reason":null,"resulting_folder_id":null,"created_at":"2026-09-16T12:00:00Z","resolved_at":null}"#
                    .utf8
            )
        )
        let addition = try JSONDecoder.server.decode(
            FolderProposalView.self,
            from: Data(
                #"{"id":"00000000-0000-0000-0000-0000000000e2","kind":"add_to_folder","proposed_name":null,"target_folder_id":"\#(work.id.uuidString)","member_session_ids":["\#(first.sessionID.uuidString)"],"rationale":"Same project","derivation":"model","state":"proposed","withdrawal_reason":null,"resulting_folder_id":null,"created_at":"2026-09-16T12:00:00Z","resolved_at":null}"#
                    .utf8
            )
        )
        let orphanAddition = try JSONDecoder.server.decode(
            FolderProposalView.self,
            from: Data(
                #"{"id":"00000000-0000-0000-0000-0000000000e3","kind":"add_to_folder","proposed_name":null,"target_folder_id":"\#(UUID().uuidString)","member_session_ids":["\#(second.sessionID.uuidString)"],"rationale":null,"derivation":"model","state":"proposed","withdrawal_reason":null,"resulting_folder_id":null,"created_at":"2026-09-16T12:00:00Z","resolved_at":null}"#
                    .utf8
            )
        )
        let unknownKind = try JSONDecoder.server.decode(
            FolderProposalView.self,
            from: Data(
                #"{"id":"00000000-0000-0000-0000-0000000000e4","kind":"merge_folders","proposed_name":null,"target_folder_id":null,"member_session_ids":["\#(first.sessionID.uuidString)"],"rationale":null,"derivation":"model","state":"proposed","withdrawal_reason":null,"resulting_folder_id":null,"created_at":"2026-09-16T12:00:00Z","resolved_at":null}"#
                    .utf8
            )
        )
        let presented = FolderProposalPresentation.make(
            proposals: [newFolder, addition, orphanAddition, unknownKind],
            folders: [work],
            history: [first, second]
        )
        #expect(presented.map(\.id) == [newFolder.id, addition.id, orphanAddition.id])
        try #require(presented.count == 3)
        #expect(presented[0].kind == .newFolder)
        #expect(presented[0].headline == "New folder “Lisbon Trip”")
        #expect(presented[0].memberTitles == ["Lisbon flights", "Lisbon hotel"])
        #expect(presented[0].unresolvedMemberCount == 1)
        #expect(presented[1].headline == "Add to “Work”")
        #expect(presented[1].memberTitles == ["Lisbon flights"])
        #expect(presented[2].headline == "Add to a folder")
        // Only a new-folder proposal carries a name the owner can change.
        #expect(presented[0].proposedName == "Lisbon Trip")
        #expect(presented[1].proposedName == nil)
    }

    // MARK: - Folder expansion and solo mode

    @Test
    func testSoloIsTheDefaultAndEveryFolderStartsCollapsed() {
        let state = FolderExpansionState()
        #expect(state.solo)
        #expect(!state.isExpanded(folder(1, "Work").id))
    }

    @Test
    func testInSoloExpandingOneFolderCollapsesTheOthers() {
        let work = folder(1, "Work").id
        let travel = folder(2, "Travel").id
        var state = FolderExpansionState()
        state.setExpanded(true, folder: work)
        #expect(state.isExpanded(work))
        state.setExpanded(true, folder: travel)
        #expect(state.isExpanded(travel))
        #expect(!state.isExpanded(work))
        // Collapsing a folder that is already closed leaves the open one open.
        state.setExpanded(false, folder: work)
        #expect(state.isExpanded(travel))
        state.setExpanded(false, folder: travel)
        #expect(!state.isExpanded(travel))
        #expect(!state.isExpanded(work))
    }

    @Test
    func testWithoutSoloFoldersOpenIndependentlyAndStayOpenUntilClosed() {
        let work = folder(1, "Work").id
        let travel = folder(2, "Travel").id
        var state = FolderExpansionState(solo: false)
        #expect(state.isExpanded(work))
        #expect(state.isExpanded(travel))
        state.setExpanded(false, folder: work)
        #expect(!state.isExpanded(work))
        #expect(state.isExpanded(travel))
        state.setExpanded(true, folder: work)
        #expect(state.isExpanded(work))
        #expect(state.isExpanded(travel))
    }

    @Test
    func testSwitchingSoloKeepsWhatIsOnScreenAsFarAsTheRuleAllows() {
        let work = folder(1, "Work").id
        let travel = folder(2, "Travel").id
        let home = folder(3, "Home").id
        let order = [work, travel, home]
        var state = FolderExpansionState(solo: false)
        state.setExpanded(false, folder: work)
        // Turning solo on keeps the first open folder open and closes the rest.
        state.setSolo(true, order: order)
        #expect(state.solo)
        #expect(state.isExpanded(travel))
        #expect(!state.isExpanded(work))
        #expect(!state.isExpanded(home))
        // Turning it off keeps that folder open and every other one closed.
        state.setSolo(false, order: order)
        #expect(!state.solo)
        #expect(state.isExpanded(travel))
        #expect(!state.isExpanded(work))
        #expect(!state.isExpanded(home))
        // A folder that arrives later opens, as it always has without solo.
        #expect(state.isExpanded(folder(4, "New").id))
    }

    @Test
    func testRetainForgetsFoldersTheServerNoLongerLists() {
        let work = folder(1, "Work").id
        let travel = folder(2, "Travel").id
        var solo = FolderExpansionState()
        solo.setExpanded(true, folder: work)
        solo.retain(only: [travel])
        #expect(!solo.isExpanded(work))
        #expect(solo == FolderExpansionState())

        var free = FolderExpansionState(solo: false)
        free.setExpanded(false, folder: work)
        free.setExpanded(false, folder: travel)
        free.retain(only: [travel])
        #expect(free.isExpanded(work))
        #expect(!free.isExpanded(travel))
    }

    @MainActor
    @Test
    func testSidebarPreferencesRememberExpansionOnThisDevice() throws {
        let suite = "com.veetbot.tests.folders.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let work = folder(1, "Work").id
        let travel = folder(2, "Travel").id

        let first = FolderSidebarPreferences(defaults: defaults)
        #expect(first.expansion == FolderExpansionState())
        first.expansion.setExpanded(true, folder: travel)
        #expect(FolderSidebarPreferences(defaults: defaults).expansion.isExpanded(travel))
        first.expansion.setSolo(false, order: [work, travel])

        let second = FolderSidebarPreferences(defaults: defaults)
        #expect(second.expansion == first.expansion)
        #expect(!second.expansion.solo)
        #expect(second.expansion.isExpanded(travel))
        #expect(!second.expansion.isExpanded(work))
    }

    @MainActor
    @Test
    func testUnreadableStoredExpansionFallsBackToTheDefault() throws {
        let suite = "com.veetbot.tests.folders.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set(Data("not json".utf8), forKey: FolderSidebarPreferences.expansionKey)
        #expect(FolderSidebarPreferences(defaults: defaults).expansion == FolderExpansionState())
    }
}
