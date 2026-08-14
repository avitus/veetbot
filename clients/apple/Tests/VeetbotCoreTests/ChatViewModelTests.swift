import Foundation
import Testing

@testable import VeetbotCore

@Suite @MainActor struct ChatViewModelTests {
    @Test
    func testConfigureReportsTheCurrentAttemptFailure() async {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suiteName)!
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "existing-token"),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore()
        )

        let configured = await model.configure(
            baseURLString: "not a server URL",
            token: "replacement-token"
        )

        #expect(configured == false)
        #expect(model.errorMessage != nil)
    }

    @Test
    func testHistoryPaginationHasNoArbitraryPageCapAndRejectsLoops() throws {
        var seen: Set<String> = []

        for page in 1 ... 101 {
            let cursor = "cursor-\(page)"
            #expect(try ChatViewModel.nextHistoryCursor(cursor, seen: &seen) == cursor)
        }
        #expect(seen.count == 101)
        #expect(throws: HTTPTransportError.self) {
            try ChatViewModel.nextHistoryCursor("cursor-101", seen: &seen)
        }
        #expect(try ChatViewModel.nextHistoryCursor(nil, seen: &seen) == nil)
    }
}
