import Foundation
import Testing

@testable import VeetbotCore

@Suite struct SessionSidebarNavigationTests {
    @Test
    func testCompactNavigationTracksNewAndExistingConversationDestinations() throws {
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000123")
        )
        let firstNewConversation = SessionSidebarDestination.freshConversation()
        let secondNewConversation = SessionSidebarDestination.freshConversation()

        #expect(firstNewConversation != secondNewConversation)
        #expect(SessionSidebarDestination.session(sessionID) == .session(sessionID))
    }
}
