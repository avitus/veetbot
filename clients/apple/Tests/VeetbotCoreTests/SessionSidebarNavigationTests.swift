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

    /// Keep global navigation actions available through the accessible iOS overflow menu.
    @Test
    func testIOSSidebarUsesAnExplicitAccessibleOverflowMenu() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/RootView.swift"),
            encoding: .utf8
        )
        let sidebarStart = try #require(source.range(of: "private struct SessionSidebar: View"))
        let toolbarStart = try #require(
            source.range(
                of: "#if os(iOS)\n        .toolbar {",
                range: sidebarStart.upperBound ..< source.endIndex
            )
        )
        let firstSheet = try #require(
            source.range(
                of: ".sheet(isPresented: $showingMemoryBrowser)",
                range: toolbarStart.upperBound ..< source.endIndex
            )
        )
        let toolbar = source[toolbarStart.lowerBound ..< firstSheet.lowerBound]
        let iosEnd = try #require(toolbar.range(of: "#endif"))
        let iosToolbar = toolbar[..<iosEnd.lowerBound]

        #expect(iosToolbar.contains("Menu"))
        #expect(iosToolbar.contains("ellipsis.circle"))
        #expect(iosToolbar.contains(".accessibilityLabel(\"More\")"))
        #expect(iosToolbar.contains(".accessibilityHint("))
        #expect(iosToolbar.contains("sidebar.more"))
        #expect(iosToolbar.contains("sidebar.memory"))
        #expect(iosToolbar.contains("sidebar.schedules"))
        #expect(iosToolbar.contains("sidebar.persona"))
        #expect(iosToolbar.contains("sidebar.settings"))
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
        #expect(source.contains(".navigationDestination(isPresented:"))
        #expect(!modernList.contains("isActive:"))
        #expect(source.contains("private var pushingList: some View {\n        navigationList"))

        let directListStart = try #require(
            source.range(of: "private var directlyActivatingList: some View")
        )
        let pushingListStart = try #require(
            source.range(of: "private var pushingList: some View")
        )
        let modernProperty = source[modernListStart.lowerBound ..< directListStart.lowerBound]
        let directList = source[directListStart.lowerBound ..< pushingListStart.lowerBound]
        let pushingList = source[pushingListStart.lowerBound ..< legacyListStart.lowerBound]
        #expect(!modernProperty.contains(".navigationDestination(isPresented:"))
        #expect(!directList.contains(".navigationDestination(isPresented:"))
        #expect(pushingList.contains(".navigationDestination(isPresented:"))
    }

    /// Both sidebar variants render history through one shared section builder,
    /// so folder sections cannot drift between macOS/iPad and compact iPhone.
    @Test
    func testBothSidebarVariantsShareTheFolderAwareHistorySections() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/RootView.swift"),
            encoding: .utf8
        )
        let directListStart = try #require(
            source.range(of: "private var directlyActivatingList: some View")
        )
        let pushingListStart = try #require(
            source.range(of: "private var pushingList: some View")
        )
        let navigationListStart = try #require(
            source.range(of: "private var navigationList: some View")
        )
        let bindingStart = try #require(
            source.range(of: "private var notificationNavigationBinding: Binding<Bool>")
        )
        let directList = source[directListStart.lowerBound ..< pushingListStart.lowerBound]
        let navigationList = source[navigationListStart.lowerBound ..< bindingStart.lowerBound]
        #expect(directList.contains("historySections {"))
        #expect(navigationList.contains("historySections {"))
        #expect(!directList.contains("ForEach(model.history)"))
        #expect(!navigationList.contains("ForEach(model.history)"))
        #expect(source.contains("Section(\"Suggested folders\")"))
        // A folder expands from the device's remembered state, never from a
        // list control's own, so a proposal or refresh cannot reopen it.
        #expect(source.contains("FolderHeaderRow("))
        #expect(source.contains("folderSidebar.expansion.isExpanded(section.id)"))
        #expect(!source.contains("DisclosureGroup(isExpanded:"))
    }

    /// Every folder control carries a stable identifier and is gated on
    /// availability, so an older server shows exactly today's sidebar.
    @Test
    func testFolderControlsCarryStableIdentifiersAndAreGatedOnAvailability() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let root = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/RootView.swift"),
            encoding: .utf8
        )
        let views = try String(
            contentsOf: packageRoot.appendingPathComponent(
                "Veetbot/Views/ConversationFolderViews.swift"
            ),
            encoding: .utf8
        )
        let combined = root + views
        for identifier in [
            "sidebar.new-folder",
            "sidebar.folder.rename",
            "sidebar.folder.delete",
            "sidebar.folder.solo",
            "sidebar.session.move.none",
            "folder.name",
            "folder.save",
            "folder.cancel",
        ] {
            #expect(combined.contains("\"\(identifier)\""), "missing \(identifier)")
        }
        for prefix in [
            "sidebar.folder.\\(",
            "sidebar.session.move.\\(",
            "sidebar.session.move.to.\\(",
            "sidebar.proposal.\\(",
            "sidebar.proposal.accept.\\(",
            "sidebar.proposal.rename.\\(",
            "sidebar.proposal.decline.\\(",
        ] {
            #expect(combined.contains(prefix), "missing \(prefix)")
        }
        #expect(root.contains("model.foldersAvailable"))
        #expect(root.contains("folderDeletionCandidate"))
        #expect(root.contains("FolderNameSheet"))
        #expect(!root.contains(".alert(") || !root.contains("TextField(\"Folder name\""))
        #expect(root.contains("sidebar.new-conversation"))
        #expect(root.contains("sidebar.session.\\(entry.sessionID.uuidString)"))
    }

    /// Every People write carries an audit session, and the client invents a
    /// hidden `people-management` session whenever the browser is opened
    /// without one. Both Memory browser presentations sit alongside a
    /// conversation list that already knows the selection, so both must hand
    /// it down; otherwise the same correction is attributed to the open
    /// conversation on macOS and to a synthetic session on iOS.
    @Test
    func testEveryMemoryBrowserPresentationCarriesTheSelectedSession() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/RootView.swift"),
            encoding: .utf8
        )

        var searchStart = source.startIndex
        var presentations = 0
        while let call = source.range(
            of: "MemoryBrowserView(",
            range: searchStart ..< source.endIndex
        ) {
            let close = try #require(
                source.range(of: ")", range: call.upperBound ..< source.endIndex)
            )
            let arguments = source[call.upperBound ..< close.lowerBound]
            #expect(
                arguments.contains("sessionID: model.selectedSessionID"),
                "MemoryBrowserView(\(arguments)) omits the selected session, so People corrections made through it are attributed to a synthetic people-management session"
            )
            presentations += 1
            searchStart = close.upperBound
        }

        #expect(presentations == 2, "expected the macOS toolbar and iOS overflow presentations")
    }
}
