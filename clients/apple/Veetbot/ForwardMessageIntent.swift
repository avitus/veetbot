import Foundation

#if os(iOS)
import AppIntents
#endif

/// The narrow surface the "Forward Message to Veetbot" intent needs to post
/// one captured message to this device's ingest route
/// (docs/plan/device-channel-and-sms.md). Narrowed to a protocol so the
/// forwarder is testable without a transport.
public protocol DeviceMessageAPI: Sendable {
    func postDeviceMessage(
        deviceID: UUID,
        channel: String,
        sender: String,
        body: String,
        receivedAt: Date
    ) async throws -> DeviceIngestResult
}

extension VeetbotAPIClient: DeviceMessageAPI {}

/// Thrown when the owner has not turned the SMS-capture integration on.
/// The intent shell swallows this — and every other forwarding failure —
/// into a benign result; the type exists so the forwarder's own tests can
/// assert on it directly.
public struct ForwardingDisabled: Error, Equatable, Sendable {
    public init() {}
}

/// Posts one captured message to this device's ingest route. Platform-
/// neutral and stateless: the intent shell below is the only caller, and an
/// AppIntent's `perform()` is hard to exercise directly, so the behavior
/// that matters is pulled out here where it is plainly testable.
public struct DeviceMessageForwarder: Sendable {
    private let api: any DeviceMessageAPI
    private let deviceID: UUID
    private let enabled: @Sendable () -> Bool
    private let now: @Sendable () -> Date

    public init(
        api: any DeviceMessageAPI,
        deviceID: UUID,
        enabled: @escaping @Sendable () -> Bool,
        now: @escaping @Sendable () -> Date = Date.init
    ) {
        self.api = api
        self.deviceID = deviceID
        self.enabled = enabled
        self.now = now
    }

    /// Posts `{channel: "sms", sender, body, received_at: now()}`. Throws
    /// `ForwardingDisabled` — without calling the API at all — when the
    /// owner's setting is off; a network error rethrows unchanged (the
    /// intent shell converts everything to a benign result, since capture is
    /// best-effort). A duplicate ingest — the route is digest-idempotent —
    /// passes straight through; it is the caller's call what a repeat means.
    public func forward(sender: String, body: String) async throws -> DeviceIngestResult {
        guard enabled() else { throw ForwardingDisabled() }
        return try await api.postDeviceMessage(
            deviceID: deviceID,
            channel: "sms",
            sender: sender,
            body: body,
            receivedAt: now()
        )
    }
}

/// `perform()`'s full body, pulled out from AppIntents specifics and kept
/// platform-neutral, so the one thing that actually matters about the
/// ordering — nothing below runs at all when the owner's SMS-capture
/// setting is off — is directly testable without the AppIntents framework.
/// `attemptForward` is everything after the enabled check: resolving this
/// installation's device id (a Keychain read, then a `/v1/devices` round
/// trip) and posting the forward. With the setting off, `attemptForward` is
/// never invoked, so none of that Keychain or network work happens.
struct ForwardMessageRunner: Sendable {
    let attemptForward: @Sendable () async throws -> Void

    func run(integrationEnabled: Bool) async {
        guard integrationEnabled else { return }
        // Capture is best-effort glue: any failure here — the app never
        // having been configured, the device not (yet) registered, a
        // network error — is swallowed the same way `perform()` swallows it.
        try? await attemptForward()
    }
}

#if os(iOS)
/// The Shortcuts-facing half of SMS capture
/// (docs/plan/device-channel-and-sms.md, "The iOS client and the owner
/// ceremony"): an in-app App Intent an owner's Messages-forwarding
/// automation calls with the sender and body of one incoming text. No
/// extension target, no app group — the keychain access this needs is the
/// app's own, readable once the device has been unlocked after boot (see
/// the client seam map §7).
@available(iOS 16.0, *)
struct ForwardMessageToVeetbotIntent: AppIntent {
    static let title: LocalizedStringResource = "Forward Message to Veetbot"
    static let openAppWhenRun = false

    @Parameter(title: "Sender") var sender: String
    @Parameter(title: "Message") var body: String

    /// Never throws: a Shortcuts automation must never surface an error
    /// dialog on the owner's phone over what is deliberately a best-effort
    /// capture. The owner's setting is read once and checked first, before
    /// any configuration, Keychain, or network work — see
    /// `ForwardMessageRunner`, which owns and tests that ordering.
    func perform() async -> some IntentResult {
        let integrationEnabled = await MainActor.run {
            SmsIntegrationPreferences().integrationEnabled
        }
        let runner = ForwardMessageRunner {
            guard let configuration = await ConnectionConfigurationStore().load() else {
                return
            }
            let api = VeetbotAPIClient(
                transport: HTTPTransport(
                    configuration: configuration,
                    tokenStore: KeychainTokenStore()
                )
            )
            let installationID = try await KeychainInstallationIdentityStore()
                .readOrCreateInstallationID()
            guard
                let deviceID = try await Self.resolveDeviceID(
                    installationID: installationID,
                    using: api
                )
            else {
                return
            }
            let forwarder = DeviceMessageForwarder(
                api: api,
                deviceID: deviceID,
                enabled: { integrationEnabled }
            )
            _ = try await forwarder.forward(sender: sender, body: body)
        }
        await runner.run(integrationEnabled: integrationEnabled)
        return .result()
    }

    /// This installation's server-assigned device id. A background intent
    /// invocation has no access to a running `ChatViewModel`'s in-memory
    /// `registeredDeviceID` (map §3), so it looks the same fact up the
    /// persisted way instead: matching this installation's client device id
    /// — the same field `DeviceRegistrationCoordinator.revoke` matches on
    /// when its own cache is empty — against the owner's registered devices.
    private static func resolveDeviceID(
        installationID: String,
        using api: VeetbotAPIClient
    ) async throws -> UUID? {
        var cursor: String?
        var seenCursors: Set<String> = []
        repeat {
            let page = try await api.listDevices(limit: 200, cursor: cursor)
            if let match = page.items.first(where: { $0.clientDeviceID == installationID }) {
                return match.id
            }
            cursor = page.nextCursor
            if let cursor, !seenCursors.insert(cursor).inserted {
                return nil
            }
        } while cursor != nil
        return nil
    }
}
#endif
