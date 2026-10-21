import Foundation
import Testing

@testable import VeetbotCore

/// The A2 Settings design (MagicPath breezy-shore-6741, young-hill-4621,
/// clever-star-2783) and Connect (kindly-cave-1942): one group per page, a
/// sidebar of groups on the Mac and iPad and a list of groups on the iPhone, a
/// Notifications group with the device list, a theme picker, Clear local
/// cache, the token's show/hide control, when the connection was last
/// verified, "Use for new chats" on a website row, and Connect's steps 2 and 3.
@Suite struct SettingsGroupsStructureTests {
    @Test
    func testTheGroupsFollowTheDesignOrder() {
        #expect(
            ConnectionSettingsSection.allCases == [
                .connection, .models, .websiteAccess, .notifications, .appearance,
                .smsIntegration, .dataAndPrivacy,
            ]
        )
        #expect(ConnectionSettingsSection.allCases.map(\.title) == [
            "Connection", "Models", "Website access", "Notifications", "Appearance",
            "SMS integration", "Data & privacy",
        ])
    }

    @Test
    func testEachGroupIsAPageReachedFromASidebarOrAList() throws {
        let source = try source(at: "Veetbot/Views/ConnectionSettingsView.swift")

        #expect(source.contains("private var groupSidebar: some View"))
        #expect(source.contains("private var groupList: some View"))
        #expect(source.contains(".accessibilityIdentifier(\"settings.group.\\(section.rawValue)\")"))
        #expect(source.contains(".navigationDestination(for: ConnectionSettingsSection.self)"))
        #expect(source.contains("usesGroupSidebar"))
        // The website group carries an amber dot while a login needs the owner.
        #expect(source.contains("websiteAccessNeedsAttention"))
    }

    @Test
    func testNotificationsListDevicesAndOfferTestAndRemove() throws {
        let devices = try source(at: "Veetbot/Views/NotificationDevicesSection.swift")
        let settings = try source(at: "Veetbot/Views/ConnectionSettingsView.swift")

        #expect(settings.contains("case .notifications:"))
        #expect(settings.contains("NotificationSettingsViewModel("))
        #expect(settings.contains("NotificationDevicesSection(model: notificationSettings, chat: model)"))
        #expect(devices.contains(".accessibilityIdentifier(\"notifications.device.test.\\(device.id.uuidString)\")"))
        #expect(devices.contains(".accessibilityIdentifier(\"notifications.device.remove.\\(device.id.uuidString)\")"))
        #expect(devices.contains("Allow notifications"))
    }

    @Test
    func testAppearanceOffersTheThemeAndDataOffersClearingTheCache() throws {
        let source = try source(at: "Veetbot/Views/ConnectionSettingsView.swift")

        #expect(source.contains("Picker(\"Theme\", selection: $appearance.theme)"))
        #expect(source.contains(".accessibilityIdentifier(\"settings.appearance.theme\")"))
        #expect(source.contains("Clear the local cache? Chats stay on the server and reload."))
        #expect(source.contains(".accessibilityIdentifier(\"settings.clear-cache\")"))
        #expect(source.contains("await model.clearLocalCache()"))
    }

    @Test
    func testTheTokenCanBeShownAndTheConnectionSaysWhenItWasVerified() throws {
        let source = try source(at: "Veetbot/Views/ConnectionSettingsView.swift")

        #expect(source.contains("showsToken ? \"Hide token\" : \"Show token\""))
        #expect(source.contains(".accessibilityIdentifier(\"settings.token.visibility\")"))
        #expect(source.contains("ConnectionVerification.phrase(for: model.lastVerifiedAt"))
        #expect(source.contains("Button(\"Use for new chats\")"))
    }

    @Test
    func testTheThemeReachesEveryWindow() throws {
        let theme = try source(at: "Veetbot/Views/AppTheme.swift")
        let root = try source(at: "Veetbot/Views/RootView.swift")

        #expect(theme.contains("enum AppThemeApplier"))
        #expect(root.contains("AppThemeApplier.apply(appearance.theme)"))
    }

    @Test
    func testConnectContinuesWithNotificationsAndReady() throws {
        let root = try source(at: "Veetbot/Views/RootView.swift")
        let connect = try source(at: "Veetbot/Views/ConnectOnboardingView.swift")

        #expect(root.contains("ConnectOnboardingView("))
        #expect(connect.contains("Hear about it when something needs you."))
        #expect(connect.contains("Allow notifications"))
        #expect(connect.contains("Not now"))
        #expect(connect.contains("Ready."))
        #expect(connect.contains("Open Chat"))
        #expect(connect.contains("Step \\(step.number) of 3"))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
