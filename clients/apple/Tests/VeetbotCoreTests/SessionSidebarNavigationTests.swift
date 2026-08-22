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

    #if os(macOS)
    @Test
    func testMacSidebarRowsActivateConversationsDirectly() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/RootView.swift"),
            encoding: .utf8
        )
        let modernListStart = try #require(
            source.range(of: "private var modernList: some View")
        )
        let legacyListStart = try #require(
            source.range(of: "private var legacyList: some View")
        )
        let modernList = source[modernListStart.lowerBound ..< legacyListStart.lowerBound]
        let macStart = try #require(modernList.range(of: "#if os(macOS)"))
        let macEnd = try #require(
            modernList.range(of: "#else", range: macStart.upperBound ..< modernList.endIndex)
        )
        let macImplementation = modernList[macStart.upperBound ..< macEnd.lowerBound]

        #expect(macImplementation.contains("directlyActivatingList"))
        #expect(!macImplementation.contains("NavigationLink"))

        let directListStart = try #require(
            source.range(of: "private var directlyActivatingList: some View")
        )
        let pushingListStart = try #require(
            source.range(of: "private var pushingList: some View")
        )
        let directList = source[directListStart.lowerBound ..< pushingListStart.lowerBound]
        #expect(directList.contains("Button"))
        #expect(directList.contains("activate("))
    }
    #endif

    @Test
    func testModernIOSRowsAdaptToRegularAndCompactWidths() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/RootView.swift"),
            encoding: .utf8
        )
        let modernListStart = try #require(
            source.range(of: "private var modernList: some View")
        )
        let legacyListStart = try #require(
            source.range(of: "private var legacyList: some View")
        )
        let modernList = source[modernListStart.lowerBound ..< legacyListStart.lowerBound]
        let iosStart = try #require(modernList.range(of: "#else"))
        let iosEnd = try #require(
            modernList.range(of: "#endif", range: iosStart.upperBound ..< modernList.endIndex)
        )
        let iosImplementation = modernList[iosStart.upperBound ..< iosEnd.lowerBound]

        #expect(iosImplementation.contains("usesDirectActivation"))
        #expect(iosImplementation.contains("directlyActivatingList"))
        #expect(iosImplementation.contains("pushingList"))
        #expect(!iosImplementation.contains("NavigationLink(value:"))
        #expect(!iosImplementation.contains("value: SessionSidebarDestination"))

        #expect(source.contains("horizontalSizeClass == .regular"))
        #expect(source.contains("usesDirectActivation: usesDirectSidebarActivation"))
    }
}
