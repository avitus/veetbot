import Foundation

/// The two device-scoped invocation routes the compose-sheet flow needs,
/// narrowed to a protocol so the flow is testable without a transport
/// (docs/plan/device-channel-and-sms.md).
public protocol DeviceInvocationAPI: Sendable {
    func pendingInvocations(deviceID: UUID) async throws -> DeviceInvocationList
    func postInvocationResult(
        deviceID: UUID,
        invocationID: UUID,
        result: DeviceInvocationResult
    ) async throws -> DeviceInvocationResultView
}

extension VeetbotAPIClient: DeviceInvocationAPI {}

/// One pending `device.sms.send` call, parsed down to what the compose sheet
/// needs. The recipient and the body are owner-visible content: they are
/// carried in memory to the sheet and never written to a log.
public struct SmsInvocation: Equatable, Identifiable, Sendable {
    public let id: UUID
    public let recipient: String
    public let body: String
    public let expiresAt: Date

    public init(id: UUID, recipient: String, body: String, expiresAt: Date) {
        self.id = id
        self.recipient = recipient
        self.body = body
        self.expiresAt = expiresAt
    }
}

/// Fetches this device's pending invocations and posts exactly one terminal
/// result for each one it takes responsibility for.
public struct DeviceInvocationService: Sendable {
    /// The only device-scoped tool this client understands. Any other pending
    /// invocation is left alone: the server expires it rather than this client
    /// settling a call it cannot perform.
    public static let smsToolName = "device.sms.send"

    private let api: any DeviceInvocationAPI
    private let deviceID: UUID
    private let now: @Sendable () -> Date

    public init(
        api: any DeviceInvocationAPI,
        deviceID: UUID,
        now: @escaping @Sendable () -> Date = { Date() }
    ) {
        self.api = api
        self.deviceID = deviceID
        self.now = now
    }

    /// Every live `device.sms.send` invocation, oldest first. A row whose
    /// deadline has already passed is reported `expired` rather than shown to
    /// the owner, and a row whose arguments do not parse is reported `failed`:
    /// either way the server learns the outcome instead of waiting.
    public func nextSmsInvocations() async throws -> [SmsInvocation] {
        let pending = try await api.pendingInvocations(deviceID: deviceID)
        let moment = now()
        var ready: [SmsInvocation] = []
        for invocation in pending.invocations
        where invocation.toolName == Self.smsToolName {
            guard invocation.expiresAt > moment else {
                await settle(invocation.id, as: .expired)
                continue
            }
            guard
                let recipient = invocation.arguments["recipient"]?.stringValue,
                let body = invocation.arguments["body"]?.stringValue,
                !recipient.isEmpty,
                !body.isEmpty
            else {
                await settle(invocation.id, as: .failed)
                continue
            }
            ready.append(
                SmsInvocation(
                    id: invocation.id,
                    recipient: recipient,
                    body: body,
                    expiresAt: invocation.expiresAt
                )
            )
        }
        return ready
    }

    /// Posts the owner's outcome for one invocation. A conflict means the row
    /// is already terminally settled server-side, so the post is never retried
    /// and never surfaces as an error the owner has to dismiss.
    public func complete(_ invocation: SmsInvocation, with result: DeviceInvocationResult) async {
        await settle(invocation.id, as: result)
    }

    private func settle(_ invocationID: UUID, as result: DeviceInvocationResult) async {
        _ = try? await api.postInvocationResult(
            deviceID: deviceID,
            invocationID: invocationID,
            result: result
        )
    }
}

/// The pending invocations this device still owes the owner an answer for.
/// One is on screen at a time; the rest wait their turn. Merging a freshly
/// fetched queue is idempotent, so the push tap and the foreground recovery
/// fetch can both run without presenting the same compose sheet twice.
public struct SmsInvocationQueue: Equatable, Sendable {
    public private(set) var presented: SmsInvocation?
    private var waiting: [SmsInvocation] = []

    public init() {}

    public var isEmpty: Bool { presented == nil && waiting.isEmpty }

    public mutating func merge(_ invocations: [SmsInvocation]) {
        for invocation in invocations
        where invocation.id != presented?.id && !waiting.contains(where: { $0.id == invocation.id })
        {
            waiting.append(invocation)
        }
        presentNext()
    }

    public mutating func settle(_ invocationID: UUID) {
        if presented?.id == invocationID { presented = nil }
        waiting.removeAll { $0.id == invocationID }
        presentNext()
    }

    public mutating func removeAll() {
        presented = nil
        waiting.removeAll()
    }

    private mutating func presentNext() {
        guard presented == nil, !waiting.isEmpty else { return }
        presented = waiting.removeFirst()
    }
}

/// What this device can do with the invocation currently at the head of the
/// queue. A device without messaging (a Mac, an iPad without an SMS-capable
/// account) reports `failed` instead of showing a sheet that cannot send.
public enum SmsInvocationDisposition: Equatable, Sendable {
    case idle
    case compose(SmsInvocation)
    case unsupported(SmsInvocation)

    public static func resolve(
        _ invocation: SmsInvocation?,
        canSendText: Bool
    ) -> SmsInvocationDisposition {
        guard let invocation else { return .idle }
        return canSendText ? .compose(invocation) : .unsupported(invocation)
    }
}
