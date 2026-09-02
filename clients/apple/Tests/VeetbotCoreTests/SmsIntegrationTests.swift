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
