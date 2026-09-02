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
/// result for each one it takes responsibility for. It is an actor because it
/// remembers the results it still owes the server: an outcome the owner
/// already gave must never be replaced by a second compose sheet.
public actor DeviceInvocationService {
    /// The only device-scoped tool this client understands. Any other pending
    /// invocation is left alone: the server expires it rather than this client
    /// settling a call it cannot perform.
    public static let smsToolName = "device.sms.send"

    private let api: any DeviceInvocationAPI
    private let deviceID: UUID
    private let now: @Sendable () -> Date
    private let resultPostAttempts: Int
    private let retryBackoff: @Sendable (Int) async -> Void
    /// Results the owner has already given that the server has not yet
    /// acknowledged. Keyed by invocation id, replayed before the next fetch.
    private var owedResults: [UUID: DeviceInvocationResult] = [:]
    /// Every invocation this service has already answered. A pending list is a
    /// snapshot, and a row that outlives its own result — because the post is
    /// still owed, or because the fetch raced the post — must never come back
    /// as a second compose sheet for a message the owner already sent.
    private var answeredInvocationIDs: Set<UUID> = []

    public init(
        api: any DeviceInvocationAPI,
        deviceID: UUID,
        now: @escaping @Sendable () -> Date = { Date() },
        resultPostAttempts: Int = 3,
        retryBackoff: @escaping @Sendable (Int) async -> Void = { attempt in
            try? await Task.sleep(nanoseconds: UInt64(attempt) * 250_000_000)
        }
    ) {
        self.api = api
        self.deviceID = deviceID
        self.now = now
        self.resultPostAttempts = max(1, resultPostAttempts)
        self.retryBackoff = retryBackoff
    }

    /// Every live `device.sms.send` invocation, oldest first. A row whose
    /// deadline has already passed is reported `expired` rather than shown to
    /// the owner, and a row whose arguments do not parse is reported `failed`:
    /// either way the server learns the outcome instead of waiting.
    public func nextSmsInvocations() async throws -> [SmsInvocation] {
        await replayOwedResults()
        let pending = try await api.pendingInvocations(deviceID: deviceID)
        let moment = now()
        var ready: [SmsInvocation] = []
        for invocation in pending.invocations
        where invocation.toolName == Self.smsToolName {
            // A row this service has already answered stays answered. Showing
            // it again would ask the owner to send the same message twice.
            guard !answeredInvocationIDs.contains(invocation.id) else { continue }
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

    /// Posts the owner's outcome for one invocation.
    public func complete(_ invocation: SmsInvocation, with result: DeviceInvocationResult) async {
        await settle(invocation.id, as: result)
    }

    /// Posts one result, retrying only what a retry can fix. Recording a
    /// result is idempotent server-side for a settled row that has not
    /// expired, so a replay is safe; a conflict is raised only for a row the
    /// server has already expired, which no retry can change.
    private func settle(_ invocationID: UUID, as result: DeviceInvocationResult) async {
        answeredInvocationIDs.insert(invocationID)
        for attempt in 1...resultPostAttempts {
            do {
                _ = try await api.postInvocationResult(
                    deviceID: deviceID,
                    invocationID: invocationID,
                    result: result
                )
                owedResults[invocationID] = nil
                return
            } catch {
                if Self.isTerminal(error) {
                    owedResults[invocationID] = nil
                    return
                }
                guard attempt < resultPostAttempts else { break }
                await retryBackoff(attempt)
            }
        }
        // Every attempt failed for a reason that says nothing about whether
        // the row is settled. Remember the outcome so the next fetch replays
        // it rather than dropping the owner's answer on the floor.
        owedResults[invocationID] = result
    }

    private func replayOwedResults() async {
        for (invocationID, result) in owedResults {
            await settle(invocationID, as: result)
        }
    }

    /// A response the server will give again no matter how often it is asked:
    /// the request itself is the problem, not the moment it was made.
    private static func isTerminal(_ error: Error) -> Bool {
        guard
            case HTTPTransportError.api(let apiError) = error,
            let statusCode = apiError.statusCode
        else { return false }
        switch statusCode {
        case 408, 425, 429: return false
        case 400..<500: return true
        default: return false
        }
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

    /// Retires the invocation currently on screen and brings the next one
    /// forward. Anything else — a sheet dismissal that arrives after the head
    /// has already advanced — is ignored and reported as not settled, so an
    /// answer given to one invocation can never settle another.
    @discardableResult
    public mutating func settle(_ invocationID: UUID) -> Bool {
        guard presented?.id == invocationID else { return false }
        presented = nil
        presentNext()
        return true
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
