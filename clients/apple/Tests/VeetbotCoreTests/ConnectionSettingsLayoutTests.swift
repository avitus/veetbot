import Testing

@testable import VeetbotCore

@Suite struct ConnectionSettingsLayoutTests {
    @Test
    func testSettingsAreGroupedByUserIntent() {
        #expect(
            ConnectionSettingsSection.allCases == [
                .connection,
                .websiteAccess,
                .appearance,
                .dataAndPrivacy,
            ]
        )
    }

    @Test
    func testBuildIdentityMakesInstalledClientVersionVisible() {
        let identity = ClientBuildIdentity(
            infoDictionary: [
                "CFBundleShortVersionString": "0.1.1",
                "CFBundleVersion": "2",
            ]
        )

        #expect(identity.displayName == "Version 0.1.1 (2)")
    }
}

#if os(macOS)
import AppKit

@Suite struct SettingsWindowConfigurationTests {
    @Test
    func testAppWindowUsesAStableNamedSceneRoot() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/VeetbotApp.swift"),
            encoding: .utf8
        )
        let normalizedSource = source
            .split(whereSeparator: { $0.isWhitespace })
            .joined(separator: " ")

        #expect(normalizedSource.contains("WindowGroup { VeetbotSceneRoot("))
        #expect(!normalizedSource.contains("WindowGroup { RootView("))
        let rootTypeName = String(reflecting: VeetbotSceneRoot.self)
        #expect(rootTypeName.hasSuffix(".VeetbotSceneRoot"))
        #expect(!rootTypeName.contains("unknown context"))
    }

    @Test
    func testMainAndSettingsWindowsHaveDistinctPersistentFrames() {
        #expect(MainWindowConfiguration.frameName == "VeetbotMainWindow")
        #expect(SettingsWindowConfiguration.frameName == "VeetbotSettingsWindow")
        #expect(MainWindowConfiguration.frameName != SettingsWindowConfiguration.frameName)
    }

    @Test @MainActor
    func testMainWindowInstallsItsFrameAutosaveName() {
        let window = NSWindow(
            contentRect: NSRect(x: 80, y: 80, width: 900, height: 640),
            styleMask: [.titled, .closable, .resizable],
            backing: .buffered,
            defer: false
        )

        MainWindowConfiguration.apply(to: window)

        #expect(window.frameAutosaveName == MainWindowConfiguration.frameName)
    }

    @Test @MainActor
    func testWindowCanGrowHorizontallyAndVertically() {
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: SettingsWindowConfiguration.initialSize),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )

        SettingsWindowConfiguration.apply(to: window)

        #expect(window.styleMask.contains(.resizable))
        #expect(window.contentMinSize.width < SettingsWindowConfiguration.initialSize.width)
        #expect(window.contentMinSize.height < SettingsWindowConfiguration.initialSize.height)
        #expect(window.contentMaxSize.width > SettingsWindowConfiguration.initialSize.width)
        #expect(window.contentMaxSize.height > SettingsWindowConfiguration.initialSize.height)
    }
}
#endif
