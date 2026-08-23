import Foundation
import Security
import Testing

@testable import VeetbotCore

@Suite(.serialized) struct NotificationCoordinatorTests {
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
    func testEntitlementIsTrackedAndUITestFixtureSuppressesPermissionPrompt() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let entitlement = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Veetbot.entitlements"),
            encoding: .utf8
        )
        let delegate = try String(
            contentsOf: packageRoot.appendingPathComponent(
                "Veetbot/NotificationApplicationDelegate.swift"
            ),
            encoding: .utf8
        )

        #expect(entitlement.contains("<key>aps-environment</key>"))
        #expect(entitlement.contains("$(APS_ENVIRONMENT)"))
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
    private var recordedRegistrations: [Registration] = []
    private var recordedRevocations: [UUID] = []

    init(deviceID: UUID, registrationError: Error? = nil) {
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
        return DeviceView.fixture(
            id: deviceID,
            clientDeviceID: body.clientDeviceID,
            pushEnvironment: body.pushEnvironment
        )
    }

    func listDevices(limit: Int, cursor: String?) async throws -> Page<DeviceView> {
        Page(items: [], nextCursor: nil)
    }

    func revokeDevice(_ deviceID: UUID) async throws -> DeviceView {
        recordedRevocations.append(deviceID)
        return DeviceView.fixture(id: deviceID)
    }

    func registrations() -> [Registration] { recordedRegistrations }
    func revokedDeviceIDs() -> [UUID] { recordedRevocations }
}

extension DeviceView {
    fileprivate static func fixture(
        id: UUID,
        clientDeviceID: String = "installation",
        pushEnvironment: PushEnvironment = .sandbox
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
            status: .active,
            revokedAt: nil,
            lastSeenAt: Date(timeIntervalSince1970: 1),
            createdAt: Date(timeIntervalSince1970: 1),
            updatedAt: Date(timeIntervalSince1970: 1)
        )
    }
}
