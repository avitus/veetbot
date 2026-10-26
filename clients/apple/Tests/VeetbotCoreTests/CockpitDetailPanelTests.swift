import Foundation
import Testing

@testable import VeetbotCore

/// A widget's details panel follows "Mac — Cockpit · A2" (MagicPath
/// merrily-city-4157): Feed with its last value, expected interval and when it
/// goes stale, Private settings, History with the live revision and Revert,
/// then Discuss in Chat and Remove from this screen.
@MainActor
@Suite struct CockpitDetailPanelTests {
    @Test
    func testTheFeedFactsReadAsTheDesignWritesThem() {
        #expect(CockpitDetailText.lastValue(.live, age: 2) == "Updated just now")
        #expect(CockpitDetailText.lastValue(.live, age: 120) == "Updated 2 min ago")
        #expect(CockpitDetailText.lastValue(.aging, age: 1_800) == "Updated 30 min ago")
        #expect(CockpitDetailText.lastValue(.stale, age: 47 * 60) == "Stale · last update 47 min ago")
        #expect(CockpitDetailText.lastValue(.error, age: 3 * 3600) == "Feed failed · last good 3 h ago")
        #expect(CockpitDetailText.lastValue(.pending, age: nil) == "Waiting for the first update")
        #expect(CockpitDetailText.expected(900) == "Every 15 min")
        #expect(CockpitDetailText.expected(7_200) == "Every 2 h")
        #expect(CockpitDetailText.expected(30) == "Every 30 s")
        #expect(CockpitDetailText.staleAfter(900) == "45 min without an update")
        #expect(CockpitDetailText.staleAfter(3_600) == "3 h without an update")
        #expect(CockpitDetailText.source(trust: "platform") == "Veetbot")
        #expect(CockpitDetailText.source(trust: "untrusted") == "Written by the assistant")
    }

    @Test
    func testAHistoryRowSaysWhatWhoWhenAndWhichCommit() {
        #expect(CockpitDetailText.revisionTitle("pending_commit") == "Pending commit")
        #expect(CockpitDetailText.revisionSource("owner_import") == "You")
        #expect(CockpitDetailText.revisionSource("assistant") == "Veetbot")
        #expect(CockpitDetailText.shortSHA("e5d8b02c1a") == "e5d8b02")
    }

    /// Discuss in Chat opens a new conversation with an editable opener about
    /// the widget; nothing is sent until the owner sends it.
    @Test
    func testDiscussInChatStagesAnEditableConversationAboutTheWidget() {
        let suite = "CockpitDetailPanelTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let chat = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: nil),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore()
        )
        let coordinator = AppCoordinator(chat: chat)
        coordinator.mode = .cockpit
        coordinator.discussWidget(title: "Weather")
        #expect(coordinator.mode == .chat)
        #expect(chat.composerText == "About my Cockpit widget “Weather”: ")
    }

    @Test
    func testThePanelCarriesTheDesignsSectionsAndActions() throws {
        let detail = try source(at: "Veetbot/Views/CockpitDetailView.swift")
        #expect(detail.contains("section(\"Feed\")"))
        #expect(!detail.contains("section(\"Feeds\")"))
        #expect(detail.contains("\"Private settings\""))
        #expect(detail.contains("Kept on your server, never in the public repo."))
        #expect(detail.contains("section(\"History\")"))
        #expect(detail.contains(".accessibilityIdentifier(\"cockpit.detail.discuss\")"))
        #expect(detail.contains(".accessibilityIdentifier(\"cockpit.detail.remove\")"))
        #expect(detail.contains("Remove from this screen"))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
