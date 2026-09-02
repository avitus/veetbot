import Foundation
import Testing

@testable import VeetbotCore

@Suite struct DeviceMessageForwarderTests {
    private static let deviceID = UUID(uuidString: "00000000-0000-0000-0000-0000000000f1")!
    private static let now = Date(timeIntervalSince1970: 1_700_000_000)

    @Test
    func testForwardPostsToTheSmsChannelWithTheInjectedReceivedAt() async throws {
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000000f2"))
        let runID = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000000f3"))
        let api = FakeDeviceMessageAPI(
            result: DeviceIngestResult(duplicate: false, sessionID: sessionID, runID: runID)
        )
        let forwarder = DeviceMessageForwarder(
            api: api,
            deviceID: Self.deviceID,
            enabled: { true },
            now: { Self.now }
        )

        let result = try await forwarder.forward(sender: "+15550001111", body: "running late")

        #expect(result.duplicate == false)
        #expect(result.sessionID == sessionID)
        #expect(result.runID == runID)
        #expect(
            await api.posted() == [
                FakeDeviceMessageAPI.PostedMessage(
                    deviceID: Self.deviceID,
                    channel: "sms",
                    sender: "+15550001111",
                    body: "running late",
                    receivedAt: Self.now
                )
            ]
        )
    }

    @Test
    func testDisabledIntegrationThrowsForwardingDisabledWithoutCallingTheAPI() async throws {
        let api = FakeDeviceMessageAPI(
            result: DeviceIngestResult(duplicate: false, sessionID: UUID(), runID: UUID())
        )
        let forwarder = DeviceMessageForwarder(
            api: api,
            deviceID: Self.deviceID,
            enabled: { false },
            now: { Self.now }
        )

        await #expect(throws: ForwardingDisabled.self) {
            try await forwarder.forward(sender: "+15550001111", body: "running late")
        }
        #expect(await api.posted().isEmpty)
    }

    @Test
    func testADuplicateIngestResultPassesThroughUnchanged() async throws {
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000000f4"))
        let runID = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000000f5"))
        let api = FakeDeviceMessageAPI(
            result: DeviceIngestResult(duplicate: true, sessionID: sessionID, runID: runID)
        )
        let forwarder = DeviceMessageForwarder(
            api: api,
            deviceID: Self.deviceID,
            enabled: { true },
            now: { Self.now }
        )

        let result = try await forwarder.forward(sender: "+15550001111", body: "running late")

        #expect(result.duplicate == true)
        #expect(result.sessionID == sessionID)
        #expect(result.runID == runID)
        #expect(await api.posted().count == 1)
    }
}

/// The intent shell's ordering fix: the owner's setting must be checked
/// before any of the Keychain/network work `attemptForward` stands in for.
@Suite struct ForwardMessageRunnerTests {
    @Test
    func testDisabledIntegrationNeverInvokesAttemptForward() async throws {
        let spy = AttemptForwardSpy()
        let runner = ForwardMessageRunner {
            await spy.recordCall()
        }

        await runner.run(integrationEnabled: false)

        #expect(await spy.callCount == 0)
    }

    @Test
    func testEnabledIntegrationInvokesAttemptForwardExactlyOnce() async throws {
        let spy = AttemptForwardSpy()
        let runner = ForwardMessageRunner {
            await spy.recordCall()
        }

        await runner.run(integrationEnabled: true)

        #expect(await spy.callCount == 1)
    }

    @Test
    func testAFailureInAttemptForwardIsSwallowedRatherThanPropagated() async throws {
        struct BoomError: Error {}
        let runner = ForwardMessageRunner {
            throw BoomError()
        }

        // Must not throw: the run() call itself has no `try`.
        await runner.run(integrationEnabled: true)
    }
}

private actor AttemptForwardSpy {
    private(set) var callCount = 0

    func recordCall() {
        callCount += 1
    }
}

private actor FakeDeviceMessageAPI: DeviceMessageAPI {
    struct PostedMessage: Equatable, Sendable {
        let deviceID: UUID
        let channel: String
        let sender: String
        let body: String
        let receivedAt: Date
    }

    private let result: DeviceIngestResult
    private var calls: [PostedMessage] = []

    init(result: DeviceIngestResult) {
        self.result = result
    }

    func postDeviceMessage(
        deviceID: UUID,
        channel: String,
        sender: String,
        body: String,
        receivedAt: Date
    ) async throws -> DeviceIngestResult {
        calls.append(
            PostedMessage(
                deviceID: deviceID,
                channel: channel,
                sender: sender,
                body: body,
                receivedAt: receivedAt
            )
        )
        return result
    }

    func posted() -> [PostedMessage] { calls }
}
