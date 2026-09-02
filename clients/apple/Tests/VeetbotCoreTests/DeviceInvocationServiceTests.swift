import Foundation
import Testing

@testable import VeetbotCore

@Suite struct DeviceInvocationServiceTests {
    private static let now = Date(timeIntervalSince1970: 1_000_000)
    private static let deviceID = UUID(uuidString: "00000000-0000-0000-0000-0000000000d1")!

    @Test
    func testOnlyLiveSmsInvocationsSurviveAndLapsedRowsAreExpired() async throws {
        let live = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000001"))
        let lapsed = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000002"))
        let foreign = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000003"))
        let api = FakeDeviceInvocationAPI(
            pending: [
                invocation(
                    id: live,
                    arguments: ["recipient": .string("+15550001111"), "body": .string("running late")],
                    expiresAt: Self.now.addingTimeInterval(300)
                ),
                invocation(
                    id: lapsed,
                    arguments: ["recipient": .string("+15550002222"), "body": .string("stale")],
                    expiresAt: Self.now.addingTimeInterval(-1)
                ),
                invocation(
                    id: foreign,
                    toolName: "sandbox.run_command",
                    arguments: ["command": .string("ls")],
                    expiresAt: Self.now.addingTimeInterval(300)
                ),
            ]
        )
        let service = DeviceInvocationService(
            api: api,
            deviceID: Self.deviceID,
            now: { Self.now }
        )

        let ready = try await service.nextSmsInvocations()

        #expect(
            ready == [
                SmsInvocation(
                    id: live,
                    recipient: "+15550001111",
                    body: "running late",
                    expiresAt: Self.now.addingTimeInterval(300)
                )
            ]
        )
        #expect(
            await api.postedResults() == [
                FakeDeviceInvocationAPI.PostedResult(invocationID: lapsed, result: .expired)
            ]
        )
        #expect(await api.postedDeviceIDs() == [Self.deviceID])
    }

    @Test
    func testCompletingASendPostsExactlyOneResult() async throws {
        let invocationID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000011"))
        let api = FakeDeviceInvocationAPI(pending: [])
        let service = DeviceInvocationService(
            api: api,
            deviceID: Self.deviceID,
            now: { Self.now }
        )

        await service.complete(
            SmsInvocation(
                id: invocationID,
                recipient: "+15550001111",
                body: "on my way",
                expiresAt: Self.now.addingTimeInterval(300)
            ),
            with: .sent
        )

        #expect(
            await api.postedResults() == [
                FakeDeviceInvocationAPI.PostedResult(invocationID: invocationID, result: .sent)
            ]
        )
    }

    @Test
    func testAConflictOnTheResultPostIsTerminalRatherThanRetried() async throws {
        let invocationID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000021"))
        let api = FakeDeviceInvocationAPI(
            pending: [],
            postError: HTTPTransportError.api(
                APIError(
                    code: .conflict,
                    message: "already resolved",
                    requestID: "conflict-1",
                    statusCode: 409
                )
            )
        )
        let service = DeviceInvocationService(
            api: api,
            deviceID: Self.deviceID,
            now: { Self.now }
        )

        await service.complete(
            SmsInvocation(
                id: invocationID,
                recipient: "+15550001111",
                body: "on my way",
                expiresAt: Self.now.addingTimeInterval(300)
            ),
            with: .cancelled
        )

        #expect(
            await api.postedResults() == [
                FakeDeviceInvocationAPI.PostedResult(invocationID: invocationID, result: .cancelled)
            ]
        )
    }

    @Test
    func testMalformedArgumentsAreFailedRatherThanPresented() async throws {
        let malformed = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000031"))
        let api = FakeDeviceInvocationAPI(
            pending: [
                invocation(
                    id: malformed,
                    arguments: ["body": .string("no recipient")],
                    expiresAt: Self.now.addingTimeInterval(300)
                )
            ]
        )
        let service = DeviceInvocationService(
            api: api,
            deviceID: Self.deviceID,
            now: { Self.now }
        )

        let ready = try await service.nextSmsInvocations()

        #expect(ready.isEmpty)
        #expect(
            await api.postedResults() == [
                FakeDeviceInvocationAPI.PostedResult(invocationID: malformed, result: .failed)
            ]
        )
    }

    @Test
    func testTheQueuePresentsOneInvocationAtATimeWithoutRepeatingItself() throws {
        let first = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000041"))
        let second = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000042"))
        let firstInvocation = SmsInvocation(
            id: first,
            recipient: "+15550001111",
            body: "first",
            expiresAt: Self.now.addingTimeInterval(300)
        )
        let secondInvocation = SmsInvocation(
            id: second,
            recipient: "+15550002222",
            body: "second",
            expiresAt: Self.now.addingTimeInterval(300)
        )
        var queue = SmsInvocationQueue()

        queue.merge([firstInvocation, secondInvocation])
        #expect(queue.presented == firstInvocation)

        // A re-fetch (foreground recovery over the same pending queue) must not
        // re-present what is already on screen or duplicate what is waiting.
        queue.merge([firstInvocation, secondInvocation])
        #expect(queue.presented == firstInvocation)

        queue.settle(first)
        #expect(queue.presented == secondInvocation)

        queue.settle(second)
        #expect(queue.presented == nil)
        #expect(queue.isEmpty)
    }

    @Test
    func testADeviceThatCannotSendTextsFailsTheInvocationInsteadOfComposing() throws {
        let invocationID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000051"))
        let invocation = SmsInvocation(
            id: invocationID,
            recipient: "+15550001111",
            body: "on my way",
            expiresAt: Self.now.addingTimeInterval(300)
        )

        #expect(
            SmsInvocationDisposition.resolve(invocation, canSendText: true)
                == .compose(invocation)
        )
        #expect(
            SmsInvocationDisposition.resolve(invocation, canSendText: false)
                == .unsupported(invocation)
        )
        #expect(SmsInvocationDisposition.resolve(nil, canSendText: true) == .idle)
    }

    private func invocation(
        id: UUID,
        toolName: String = "device.sms.send",
        arguments: [String: JSONValue],
        expiresAt: Date
    ) -> DeviceInvocationView {
        DeviceInvocationView(
            id: id,
            toolName: toolName,
            arguments: arguments,
            createdAt: Self.now.addingTimeInterval(-10),
            expiresAt: expiresAt
        )
    }
}

private actor FakeDeviceInvocationAPI: DeviceInvocationAPI {
    struct PostedResult: Equatable, Sendable {
        let invocationID: UUID
        let result: DeviceInvocationResult
    }

    private let pending: [DeviceInvocationView]
    private let postError: Error?
    private var posted: [PostedResult] = []
    private var deviceIDs: [UUID] = []

    init(pending: [DeviceInvocationView], postError: Error? = nil) {
        self.pending = pending
        self.postError = postError
    }

    func pendingInvocations(deviceID: UUID) async throws -> DeviceInvocationList {
        DeviceInvocationList(invocations: pending)
    }

    func postInvocationResult(
        deviceID: UUID,
        invocationID: UUID,
        result: DeviceInvocationResult
    ) async throws -> DeviceInvocationResultView {
        posted.append(PostedResult(invocationID: invocationID, result: result))
        deviceIDs.append(deviceID)
        if let postError { throw postError }
        return DeviceInvocationResultView(
            id: invocationID,
            status: result.rawValue,
            resolvedAt: Date(timeIntervalSince1970: 1_000_001)
        )
    }

    func postedResults() -> [PostedResult] { posted }
    func postedDeviceIDs() -> [UUID] { deviceIDs }
}
