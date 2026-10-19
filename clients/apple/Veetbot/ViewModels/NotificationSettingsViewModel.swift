import Foundation

/// Settings → Notifications: the devices the server will notify, from
/// `GET /v1/devices` (notifications-and-devices.md), with this one first. A
/// device can get a content-free test notification or be removed, which
/// revokes it so it gets no more notifications. Both writes need
/// `device.write`. Muting kinds per device has no update route, so the page
/// offers none. A server without the routes reads as unavailable.
@MainActor
public final class NotificationSettingsViewModel: ObservableObject {
    @Published public private(set) var devices: [DeviceView] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var unavailable = false
    @Published public private(set) var errorMessage: String?
    /// The device a write is acting on, so its row's buttons wait for it.
    @Published public private(set) var pendingDeviceID: UUID?

    private let makeAPIClient: () async -> VeetbotAPIClient?
    private let installationID: () async -> String?
    private let toasts: ToastCenter
    private var thisInstallationID: String?

    public init(
        makeAPIClient: @escaping () async -> VeetbotAPIClient?,
        installationID: @escaping () async -> String?,
        toasts: ToastCenter? = nil
    ) {
        self.makeAPIClient = makeAPIClient
        self.installationID = installationID
        self.toasts = toasts ?? .shared
    }

    public func load() async {
        guard let api = await makeAPIClient() else { return }
        isLoading = true
        defer { isLoading = false }
        errorMessage = nil
        thisInstallationID = await installationID()
        do {
            var loaded: [DeviceView] = []
            var cursor: String?
            var seen: Set<String> = []
            repeat {
                let page = try await api.listDevices(limit: 200, cursor: cursor)
                loaded.append(contentsOf: page.items)
                cursor = page.nextCursor
                if let cursor, !seen.insert(cursor).inserted { break }
            } while cursor != nil
            unavailable = false
            devices = loaded
                .filter { $0.status == .active }
                .sorted { lhs, rhs in
                    let lhsThis = isThisDevice(lhs), rhsThis = isThisDevice(rhs)
                    if lhsThis != rhsThis { return lhsThis }
                    return lhs.lastSeenAt > rhs.lastSeenAt
                }
        } catch {
            if Self.isMissingRoute(error) {
                unavailable = true
                devices = []
            } else {
                errorMessage = Self.describe(error)
            }
        }
    }

    public func sendTest(to device: DeviceView) async {
        guard pendingDeviceID == nil, let api = await makeAPIClient() else { return }
        pendingDeviceID = device.id
        defer { pendingDeviceID = nil }
        errorMessage = nil
        do {
            try await api.sendTestNotification(to: device.id, idempotencyKey: UUID().uuidString)
            toasts.show("Test notification sent to \(device.name).")
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    public func remove(_ device: DeviceView) async {
        guard pendingDeviceID == nil, let api = await makeAPIClient() else { return }
        pendingDeviceID = device.id
        defer { pendingDeviceID = nil }
        errorMessage = nil
        do {
            _ = try await api.revokeDevice(device.id)
            devices.removeAll { $0.id == device.id }
            toasts.show("Removed \(device.name). It gets no more notifications.")
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    public func isThisDevice(_ device: DeviceView) -> Bool {
        thisInstallationID != nil && device.clientDeviceID == thisInstallationID
    }

    private static func isMissingRoute(_ error: Error) -> Bool {
        guard case HTTPTransportError.api(let apiError) = error else { return false }
        return apiError.statusCode == 404 || apiError.statusCode == 405
    }

    private static func describe(_ error: Error) -> String {
        if case HTTPTransportError.api(let apiError) = error { return apiError.message }
        return error.localizedDescription
    }
}

/// How long ago the connection last answered an authenticated request, as
/// the Connection group's token line says it.
enum ConnectionVerification {
    static func phrase(for date: Date?, now: Date = Date()) -> String? {
        guard let date else { return nil }
        let seconds = max(0, now.timeIntervalSince(date))
        if seconds < 60 { return "verified just now" }
        let minutes = Int(seconds / 60)
        if minutes < 60 { return "verified \(minutes) min ago" }
        return "verified \(minutes / 60) h ago"
    }
}
