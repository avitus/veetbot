import Foundation
import Security
import Testing

@testable import VeetbotCore

@Suite(.serialized) struct NotificationCoordinatorTests {
    #if os(macOS)
    @Test @MainActor
    func testDeferredMacRegistrationsKeepTheirSelectionRevisions() async throws {
        // The queued descriptors can reach the coordinator after both changes occurred.
        let suite = UUID().uuidString
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let empty = AppleDeviceDescriptor.current(defaults: defaults)
        let grant = try WritableFolder.select(FileManager.default.temporaryDirectory, account: "owner", defaults: defaults)
        let selected = AppleDeviceDescriptor.current(capabilities: [grant.capability], defaults: defaults)
        WritableFolder.remove(defaults: defaults)
        let removed = AppleDeviceDescriptor.current(defaults: defaults)

        let coordinator = DeviceRegistrationCoordinator(
            identityStore: InMemoryInstallationIdentityStore(installationID: UUID().uuidString)
        )
        let api = FakeDeviceRegistrationAPI(deviceID: UUID())
        for descriptor in [empty, selected, removed, removed] {
            _ = try await coordinator.register(deviceToken: Data([1]), descriptor: descriptor, using: api)
        }
        let registrations = await api.registrations()
        #expect(registrations[0].idempotencyKey != registrations[2].idempotencyKey)
        #expect(registrations[2].idempotencyKey == registrations[3].idempotencyKey)
    }

    @Test(arguments: ["device.folder_disabled", "device.capability_unknown"])
    @MainActor func testMacRegistersForPushWhenFolderAccessIsUnavailable(reason: String) async throws {
        let deviceID = UUID()
        let api = FakeDeviceRegistrationAPI(deviceID: deviceID, rejectedCapabilityReason: reason)
        let coordinator = DeviceRegistrationCoordinator(
            identityStore: InMemoryInstallationIdentityStore(installationID: UUID().uuidString)
        )
        let grant = "device.folder.write_" + String(repeating: "b", count: 32)
        let outcome = try await coordinator.register(
            deviceToken: Data([1]), descriptor: .current(capabilities: [grant]), using: api
        )
        #expect(outcome == .registered(deviceID))
        await api.setRejectedCapabilityReason(nil)
        let restored = try await coordinator.register(
            deviceToken: Data([1]), descriptor: .current(capabilities: [grant]), using: api
        )
        #expect(restored == .registered(deviceID, folderCapability: grant))
        let attempts = await api.registrations()
        #expect(attempts[0].idempotencyKey != attempts[2].idempotencyKey)
        #expect(attempts.map(\.body.capabilities) == [[grant], [], [grant]])
        #expect(attempts.map(\.body.pushToken) == ["01", "01", "01"])
    }
    #endif

    @Test
    func testRegistrationMintsOneInstallationIdentityUploadsEveryTokenAndRevokes() async throws {
        let installationID = "00000000-0000-0000-0000-000000000123"
        let deviceID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000456")
        )
        let identityStore = InMemoryInstallationIdentityStore(installationID: installationID)
        let api = FakeDeviceRegistrationAPI(deviceID: deviceID)
        let coordinator = DeviceRegistrationCoordinator(identityStore: identityStore)
        let descriptor = AppleDeviceDescriptor(
            name: "Owner's iPhone",
            kind: .mobile,
            platform: "ios",
            bundleID: "com.veetbot.apple",
            environment: .sandbox
        )

        let first = try await coordinator.register(
            deviceToken: Data([0x00, 0xab, 0xff]),
            descriptor: descriptor,
            using: api
        )
        let second = try await coordinator.register(
            deviceToken: Data([0x01, 0x02]),
            descriptor: descriptor,
            using: api
        )
        let retry = try await coordinator.register(
            deviceToken: Data([0x01, 0x02]),
            descriptor: descriptor,
            using: api
        )

        #expect(first == .registered(deviceID))
        #expect(second == .registered(deviceID))
        #expect(retry == .registered(deviceID))
        let registrations = await api.registrations()
        #expect(registrations.count == 3)
        #expect(
            registrations.map(\.body.clientDeviceID)
                == [installationID, installationID, installationID]
        )
        #expect(registrations.map(\.body.pushToken) == ["00abff", "0102", "0102"])
        #expect(registrations[0].idempotencyKey != registrations[1].idempotencyKey)
        #expect(registrations[1].idempotencyKey == registrations[2].idempotencyKey)
        #expect(registrations.allSatisfy { $0.idempotencyKey.count <= 255 })
        #expect(registrations.allSatisfy { !$0.idempotencyKey.contains($0.body.pushToken) })
        #expect(await identityStore.creationCount() == 1)

        let revoke = try await coordinator.revoke(using: api)
        #expect(revoke == .revoked(deviceID))
        #expect(await api.revokedDeviceIDs() == [deviceID])
    }

    @Test
    func testMissingDeviceRoutesAreACompatibleOlderServer() async throws {
        let identityStore = InMemoryInstallationIdentityStore(
            installationID: "00000000-0000-0000-0000-000000000123"
        )
        let api = FakeDeviceRegistrationAPI(
            deviceID: UUID(),
            registrationError: HTTPTransportError.api(
                APIError(
                    code: .notFound,
                    message: "not found",
                    requestID: "old-server",
                    statusCode: 404
                )
            )
        )
        let coordinator = DeviceRegistrationCoordinator(identityStore: identityStore)

        let outcome = try await coordinator.register(
            deviceToken: Data([0x01]),
            descriptor: AppleDeviceDescriptor(
                name: "Test Mac",
                kind: .desktop,
                platform: "macos",
                bundleID: "com.veetbot.apple",
                environment: .production
            ),
            using: api
        )

        #expect(outcome == .unsupported)
    }

    @Test
    func testClosedPushPayloadReducesToApprovalAndQuestionDeepLinks() throws {
        let sessionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000001")
        )
        let runID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000002")
        )
        let approvalID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000003")
        )
        let questionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000004")
        )
        let notificationID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000005")
        )
        let approval = try #require(
            NotificationPushPayload(
                userInfo: [
                    "veetbot": [
                        "version": 1,
                        "kind": "approval_requested",
                        "title": "Approval needed",
                        "status": "WAITING_FOR_APPROVAL",
                        "tool_name": "sandbox.run_command",
                        "session_id": sessionID.uuidString,
                        "run_id": runID.uuidString,
                        "approval_id": approvalID.uuidString,
                        "notification_id": notificationID.uuidString,
                    ]
                ]
            )
        )
        let question = try #require(
            NotificationPushPayload(
                userInfo: [
                    "veetbot": [
                        "version": 1,
                        "kind": "question_asked",
                        "title": "The agent has a question",
                        "status": "WAITING_FOR_USER",
                        "session_id": sessionID.uuidString,
                        "run_id": runID.uuidString,
                        "question_id": questionID.uuidString,
                        "notification_id": notificationID.uuidString,
                    ]
                ]
            )
        )

        #expect(
            NotificationDeepLinkReducer.reduce(approval)
                == NotificationDeepLink(
                    sessionID: sessionID,
                    runID: runID,
                    focus: .approval(approvalID)
                )
        )
        #expect(
            NotificationDeepLinkReducer.reduce(question)
                == NotificationDeepLink(
                    sessionID: sessionID,
                    runID: runID,
                    focus: .question(questionID)
                )
        )
    }

    @Test
    func testPushParserRejectsUnknownOrStructurallyInvalidPayloads() {
        let base: [String: Any] = [
            "version": 1,
            "kind": "approval_requested",
            "title": "Approval needed",
            "status": "WAITING_FOR_APPROVAL",
            "session_id": UUID().uuidString,
            "run_id": UUID().uuidString,
            "approval_id": UUID().uuidString,
            "notification_id": UUID().uuidString,
        ]
        var unknown = base
        unknown["message"] = "content must never be accepted"
        var missing = base
        missing.removeValue(forKey: "approval_id")

        #expect(NotificationPushPayload(userInfo: ["veetbot": unknown]) == nil)
        #expect(NotificationPushPayload(userInfo: ["veetbot": missing]) == nil)
        #expect(NotificationPushPayload(userInfo: ["veetbot": ["version": 2]]) == nil)
    }

    @Test
    func testInstallationIdentityUsesTheLocalDataProtectionKeychain() {
        let query = KeychainInstallationIdentityStore.makeBaseQuery(
            service: "com.veetbot.test",
            account: "installation"
        )

        #expect(query[kSecUseDataProtectionKeychain as String] as? Bool == true)
        #expect(query[kSecAttrSynchronizable as String] as? Bool == false)
    }

    @Test
    func testPlatformSpecificEntitlementsAreTrackedAndUITestFixtureSuppressesPermissionPrompt()
        throws
    {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let iosEntitlements = try #require(
            PropertyListSerialization.propertyList(
                from: Data(
                    contentsOf: packageRoot.appendingPathComponent(
                        "Veetbot/Veetbot.entitlements"
                    )
                ),
                format: nil
            ) as? [String: Any]
        )
        let macOSEntitlements = try #require(
            PropertyListSerialization.propertyList(
                from: Data(
                    contentsOf: packageRoot.appendingPathComponent(
                        "Veetbot/Veetbot.macOS.entitlements"
                    )
                ),
                format: nil
            ) as? [String: Any]
        )
        let project = try String(
            contentsOf: packageRoot.appendingPathComponent(
                "Veetbot.xcodeproj/project.pbxproj"
            ),
            encoding: .utf8
        )
        let delegate = try String(
            contentsOf: packageRoot.appendingPathComponent(
                "Veetbot/NotificationApplicationDelegate.swift"
            ),
            encoding: .utf8
        )

        #expect(iosEntitlements["aps-environment"] as? String == "$(APS_ENVIRONMENT)")
        #expect(iosEntitlements["com.apple.developer.aps-environment"] == nil)
        #expect(
            macOSEntitlements["com.apple.developer.aps-environment"] as? String
                == "$(APS_ENVIRONMENT)"
        )
        #expect(macOSEntitlements["aps-environment"] == nil)
        #expect(
            project.components(
                separatedBy: "CODE_SIGN_ENTITLEMENTS = Veetbot/Veetbot.entitlements;"
            ).count - 1 == 2
        )
        #expect(
            project.components(
                separatedBy:
                    #""CODE_SIGN_ENTITLEMENTS[sdk=macosx*]" = Veetbot/Veetbot.macOS.entitlements;"#
            ).count - 1 == 2
        )
        #expect(delegate.contains("--ui-testing-conversation-navigation"))
        #expect(delegate.contains("requestAuthorization"))
    }

    @Test
    func testConversationScrollBehaviorTargetsExistingAndChangedNotificationFocus() throws {
        let approvalID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000031")
        )
        let questionID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000032")
        )

        let existing = ConversationScrollTarget.resolve(.approval(approvalID))
        let changed = ConversationScrollTarget.resolve(.question(questionID))

        #expect(existing == .notification(.approval(approvalID)))
        #expect(existing.scrollID == NotificationFocus.approval(approvalID).scrollID)
        #expect(changed == .notification(.question(questionID)))
        #expect(changed.scrollID == NotificationFocus.question(questionID).scrollID)
    }
}

private actor FakeDeviceRegistrationAPI: DeviceRegistrationAPI {
    nonisolated let notificationServerID = "https://veetbot.test"

    struct Registration: Sendable {
        let body: AppleDeviceRegistration
        let idempotencyKey: String
    }

    private let deviceID: UUID
    private let registrationError: Error?
    private var rejectedCapabilityReason: String?
    private var recordedRegistrations: [Registration] = []
    private var recordedRevocations: [UUID] = []

    init(deviceID: UUID, registrationError: Error? = nil, rejectedCapabilityReason: String? = nil) {
        self.rejectedCapabilityReason = rejectedCapabilityReason
        self.deviceID = deviceID
        self.registrationError = registrationError
    }

    func registerDevice(
        _ body: AppleDeviceRegistration,
        idempotencyKey: String
    ) async throws -> DeviceView {
        if let registrationError { throw registrationError }
        recordedRegistrations.append(
            Registration(body: body, idempotencyKey: idempotencyKey)
        )
        if !body.capabilities.isEmpty, let reason = rejectedCapabilityReason {
            throw HTTPTransportError.api(APIError(
                code: .unknown("device_validation_error"), message: "Capability rejected",
                details: APIErrorDetails(values: ["reason": .string(reason)]),
                requestID: "fixture", statusCode: 422
            ))
        }
        return DeviceView.fixture(
            id: deviceID,
            clientDeviceID: body.clientDeviceID,
            pushEnvironment: body.pushEnvironment,
            capabilities: body.capabilities
        )
    }

    func listDevices(limit: Int, cursor: String?) async throws -> Page<DeviceView> {
        Page(items: [], nextCursor: nil)
    }

    func revokeDevice(_ deviceID: UUID) async throws -> DeviceView {
        recordedRevocations.append(deviceID)
        return DeviceView.fixture(id: deviceID)
    }

    func setRejectedCapabilityReason(_ reason: String?) { rejectedCapabilityReason = reason }

    func registrations() -> [Registration] { recordedRegistrations }
    func revokedDeviceIDs() -> [UUID] { recordedRevocations }
}

extension DeviceView {
    fileprivate static func fixture(
        id: UUID,
        clientDeviceID: String = "installation",
        pushEnvironment: PushEnvironment = .sandbox,
        capabilities: [String] = []
    ) -> DeviceView {
        DeviceView(
            id: id,
            clientDeviceID: clientDeviceID,
            name: "Test device",
            kind: .mobile,
            platform: "ios",
            appBundleID: "com.veetbot.apple",
            pushProvider: .apns,
            pushEnvironment: pushEnvironment,
            pushTokenFingerprint: "abcdef",
            pushTokenUpdatedAt: Date(timeIntervalSince1970: 1),
            pushTokenInvalidatedAt: nil,
            mutedKinds: [],
            capabilities: capabilities,
            status: .active,
            revokedAt: nil,
            lastSeenAt: Date(timeIntervalSince1970: 1),
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 1)
        )
    }
}
