import Foundation
import Testing

@testable import VeetbotCore

@Suite @MainActor struct SmsIntegrationTests {
    @Test
    func testAppleDeviceRegistrationEncodesCapabilitiesAsAJSONArray() throws {
        let registration = AppleDeviceRegistration(
            clientDeviceID: "installation",
            name: "Owner's iPhone",
            kind: .mobile,
            platform: "ios",
            appBundleID: "com.veetbot.apple",
            pushToken: "00abff",
            pushEnvironment: .sandbox,
            capabilities: ["device.sms.send"]
        )

        let data = try JSONEncoder().encode(registration)

        let decoded = try JSONDecoder().decode(AppleDeviceRegistration.self, from: data)
        #expect(decoded.capabilities == ["device.sms.send"])

        let object = try #require(
            try JSONSerialization.jsonObject(with: data) as? [String: Any]
        )
        #expect(object["capabilities"] as? [String] == ["device.sms.send"])
    }

    @Test
    func testCapabilitiesChangeTheRegistrationIdempotencyKey() async throws {
        let deviceID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000456")
        )
        let identityStore = InMemoryInstallationIdentityStore(
            installationID: "00000000-0000-0000-0000-000000000123"
        )
        let api = CapturingDeviceRegistrationAPI(deviceID: deviceID)
        let coordinator = DeviceRegistrationCoordinator(identityStore: identityStore)
        let withoutCapability = AppleDeviceDescriptor(
            name: "Owner's iPhone",
            kind: .mobile,
            platform: "ios",
            bundleID: "com.veetbot.apple",
            environment: .sandbox
        )
        let withCapability = AppleDeviceDescriptor(
            name: "Owner's iPhone",
            kind: .mobile,
            platform: "ios",
            bundleID: "com.veetbot.apple",
            environment: .sandbox,
            capabilities: ["device.sms.send"]
        )

        _ = try await coordinator.register(
            deviceToken: Data([0x00]),
            descriptor: withoutCapability,
            using: api
        )
        _ = try await coordinator.register(
            deviceToken: Data([0x00]),
            descriptor: withCapability,
            using: api
        )

        let keys = await api.idempotencyKeys()
        #expect(keys.count == 2)
        #expect(keys[0] != keys[1])
    }

    @Test
    func testSmsIntegrationPreferencesDefaultsPersistsAndDerivesCapabilities() throws {
        let suiteName = "SmsIntegrationTests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }

        let preferences = SmsIntegrationPreferences(defaults: defaults)
        #expect(preferences.integrationEnabled == false)
        #expect(preferences.declaredCapabilities == [])

        preferences.integrationEnabled = true
        #expect(preferences.declaredCapabilities == ["device.sms.send"])

        let reloaded = SmsIntegrationPreferences(defaults: defaults)
        #expect(reloaded.integrationEnabled == true)
        #expect(reloaded.declaredCapabilities == ["device.sms.send"])
    }

    @Test
    func testTheLiveRegistrationDeclaresWhicheverCapabilitiesTheOwnerEnabled() throws {
        let preferences = try isolatedPreferences()
        let model = try unconfiguredModel()
        let delegate = NotificationApplicationDelegateBase(remoteRegistrationEnabled: false)

        delegate.attach(to: model, smsPreferences: preferences)

        #expect(delegate.registrationDescriptor().capabilities == [])
        preferences.integrationEnabled = true
        #expect(delegate.registrationDescriptor().capabilities == ["device.sms.send"])
        #expect(model.pushRegistrar === delegate)
    }

    @Test
    func testFlippingTheSettingAsksForAFreshTokenSoRegistrationRepeats() throws {
        let model = try unconfiguredModel()
        let registrar = RecordingPushRegistrar()
        model.pushRegistrar = registrar

        model.requestDeviceCapabilityRegistration()
        model.requestDeviceCapabilityRegistration()

        #expect(registrar.requestCount == 2)
    }

    @Test
    func testTheSettingsToggleAndComposeSheetAreWiredIntoTheOwnersSurfaces() throws {
        let settings = try clientSource("Veetbot/Views/ConnectionSettingsView.swift")
        let root = try clientSource("Veetbot/Views/RootView.swift")

        #expect(settings.contains("Toggle("))
        #expect(settings.contains("isOn: $smsIntegration.integrationEnabled"))
        #expect(settings.contains(#".accessibilityIdentifier("sms-integration.enabled")"#))
        #expect(settings.contains("model.requestDeviceCapabilityRegistration()"))
        #expect(root.contains("SmsComposeSheet(invocation:"))
        #expect(root.contains("await model.completeSmsInvocation("))
        #expect(root.contains("await model.refreshPendingSmsInvocations()"))
    }

    private func isolatedPreferences() throws -> SmsIntegrationPreferences {
        let suiteName = "SmsIntegrationTests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        return SmsIntegrationPreferences(defaults: defaults)
    }

    private func unconfiguredModel() throws -> ChatViewModel {
        let suiteName = "SmsIntegrationTests.model.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        return ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore()
        )
    }

    private func clientSource(_ relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(
            contentsOf: packageRoot.appendingPathComponent(relativePath),
            encoding: .utf8
        )
    }
}

@MainActor
private final class RecordingPushRegistrar: PushRegistrationRequesting {
    private(set) var requestCount = 0

    func requestPushRegistration() {
        requestCount += 1
    }
}

private actor CapturingDeviceRegistrationAPI: DeviceRegistrationAPI {
    nonisolated let notificationServerID = "https://veetbot.test"

    private let deviceID: UUID
    private var capturedKeys: [String] = []

    init(deviceID: UUID) {
        self.deviceID = deviceID
    }

    func registerDevice(
        _ body: AppleDeviceRegistration,
        idempotencyKey: String
    ) async throws -> DeviceView {
        capturedKeys.append(idempotencyKey)
        return DeviceView(
            id: deviceID,
            clientDeviceID: body.clientDeviceID,
            name: "Test device",
            kind: .mobile,
            platform: "ios",
            appBundleID: "com.veetbot.apple",
            pushProvider: .apns,
            pushEnvironment: body.pushEnvironment,
            pushTokenFingerprint: "abcdef",
            pushTokenUpdatedAt: Date(timeIntervalSince1970: 1),
            pushTokenInvalidatedAt: nil,
            mutedKinds: [],
            capabilities: body.capabilities,
            status: .active,
            revokedAt: nil,
            lastSeenAt: Date(timeIntervalSince1970: 1),
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 1)
        )
    }

    func listDevices(limit: Int, cursor: String?) async throws -> Page<DeviceView> {
        Page(items: [], nextCursor: nil)
    }

    func revokeDevice(_ deviceID: UUID) async throws -> DeviceView {
        DeviceView(
            id: deviceID,
            clientDeviceID: "installation",
            name: "Test device",
            kind: .mobile,
            platform: "ios",
            appBundleID: "com.veetbot.apple",
            pushProvider: nil,
            pushEnvironment: nil,
            pushTokenFingerprint: nil,
            pushTokenUpdatedAt: Date(timeIntervalSince1970: 1),
            pushTokenInvalidatedAt: Date(timeIntervalSince1970: 2),
            mutedKinds: [],
            capabilities: [],
            status: .revoked,
            revokedAt: Date(timeIntervalSince1970: 2),
            lastSeenAt: Date(timeIntervalSince1970: 1),
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 2)
        )
    }

    func idempotencyKeys() -> [String] { capturedKeys }
}
