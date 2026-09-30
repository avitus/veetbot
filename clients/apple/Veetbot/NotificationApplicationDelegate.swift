import Combine
import Foundation
import UserNotifications

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

@MainActor
class NotificationApplicationDelegateBase: NSObject, @preconcurrency UNUserNotificationCenterDelegate,
    PushRegistrationRequesting
{
    /// The category a `device_invocation` push carries. It registers no
    /// actions: the owner opens the app and the compose sheet, and the send
    /// itself is the system's own confirmation.
    static let deviceInvocationCategoryIdentifier = "DEVICE_INVOCATION"

    private weak var model: ChatViewModel?
    private var smsPreferences: SmsIntegrationPreferences?
    private var configuredSubscription: AnyCancellable?
    private var requestedServer: URL?
    private var pendingResponsePayloads: [NotificationPushPayload] = []
    private let remoteRegistrationEnabled: Bool
    var pendingResponseCount: Int { pendingResponsePayloads.count }

    override convenience init() {
        self.init(
            remoteRegistrationEnabled: !ProcessInfo.processInfo.arguments.contains(
                "--ui-testing-conversation-navigation"
            )
        )
    }

    init(remoteRegistrationEnabled: Bool) {
        self.remoteRegistrationEnabled = remoteRegistrationEnabled
        super.init()
    }

    func attach(
        to model: ChatViewModel,
        smsPreferences: SmsIntegrationPreferences? = nil
    ) {
        if let smsPreferences { self.smsPreferences = smsPreferences }
        model.pushRegistrar = self
        guard self.model !== model else { return }
        self.model = model
        let pending = pendingResponsePayloads
        pendingResponsePayloads.removeAll()
        for payload in pending {
            open(payload, on: model)
        }
        configuredSubscription = model.$baseURL
            .combineLatest(model.$isConfigured)
            .sink { [weak self] baseURL, configured in
                guard let self else { return }
                guard configured, let baseURL else {
                    self.requestedServer = nil
                    return
                }
                guard self.requestedServer != baseURL else { return }
                self.requestedServer = baseURL
                self.requestRemoteNotificationsAfterConnection()
            }
    }

    private func requestRemoteNotificationsAfterConnection() {
        guard remoteRegistrationEnabled else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        #if os(iOS)
        center.setNotificationCategories([
            UNNotificationCategory(
                identifier: Self.deviceInvocationCategoryIdentifier,
                actions: [],
                intentIdentifiers: [],
                options: []
            )
        ])
        #endif
        Task { [weak self] in
            do {
                let settings = await center.notificationSettings()
                switch settings.authorizationStatus {
                case .notDetermined:
                    let granted = try await center.requestAuthorization(
                        options: [.alert, .badge, .sound]
                    )
                    guard granted else { return }
                case .authorized, .provisional, .ephemeral:
                    break
                case .denied:
                    return
                @unknown default:
                    return
                }
                self?.registerWithOperatingSystem()
            } catch {
                self?.model?.reportNotificationRegistrationFailure(error)
            }
        }
    }

    private func registerWithOperatingSystem() {
        #if os(iOS)
        UIApplication.shared.registerForRemoteNotifications()
        #elseif os(macOS)
        NSApplication.shared.registerForRemoteNotifications()
        #endif
    }

    /// Asks the operating system for the push token again. The owner flipping
    /// a capability lands here, and the redelivered token re-registers the
    /// device with the capability set it now declares.
    func requestPushRegistration() {
        requestRemoteNotificationsAfterConnection()
    }

    /// This device as the next registration will describe it, including the
    /// capabilities the owner has switched on.
    func registrationDescriptor() -> AppleDeviceDescriptor {
        AppleDeviceDescriptor.current(capabilities: smsPreferences?.declaredCapabilities ?? [])
    }

    func received(deviceToken: Data) {
        guard let model else { return }
        let descriptor = registrationDescriptor()
        Task {
            await model.registerRemoteNotifications(
                deviceToken: deviceToken,
                descriptor: descriptor
            )
        }
    }

    func registrationFailed(_ error: Error) {
        model?.reportNotificationRegistrationFailure(error)
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let payload = NotificationPushPayload(
            userInfo: response.notification.request.content.userInfo
        )
        completionHandler()
        guard let payload else { return }
        received(payload: payload)
    }

    func received(payload: NotificationPushPayload) {
        guard let model else {
            pendingResponsePayloads.append(payload)
            return
        }
        open(payload, on: model)
    }

    private func open(_ payload: NotificationPushPayload, on model: ChatViewModel) {
        Task { await model.openNotification(payload) }
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        let payload = NotificationPushPayload(userInfo: notification.request.content.userInfo)
        let suppress = NotificationPresentation.suppress(payload, visibleSessionID: model?.visibleNotificationSessionID)
        completionHandler(suppress ? [] : [.banner, .list, .sound])
        if let model { Task { await model.synchronizeNotifications() } }
    }
}

#if os(iOS)
@MainActor
final class NotificationApplicationDelegate: NotificationApplicationDelegateBase,
    UIApplicationDelegate
{
    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        received(deviceToken: deviceToken)
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        registrationFailed(error)
    }
}
#elseif os(macOS)
@MainActor
final class NotificationApplicationDelegate: NotificationApplicationDelegateBase,
    NSApplicationDelegate
{
    func applicationDidFinishLaunching(_ notification: Notification) {
        UNUserNotificationCenter.current().delegate = self
    }

    func application(
        _ application: NSApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        received(deviceToken: deviceToken)
    }

    func application(
        _ application: NSApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        registrationFailed(error)
    }
}
#endif

struct DeliveredNotification: Sendable {
    let requestIdentifier: String
    let notificationID: UUID
}

@MainActor
protocol DeliveredNotificationStore {
    func delivered() async -> [DeliveredNotification]
    func remove(identifiers: [String])
}

protocol NotificationSyncAPI: Sendable {
    func syncNotifications(_ request: NotificationSyncRequest) async throws -> NotificationSyncResult
}

@MainActor
final class NotificationAttentionCoordinator {
    private let store: any DeliveredNotificationStore
    init(store: any DeliveredNotificationStore) { self.store = store }

    func synchronize(
        using api: any NotificationSyncAPI,
        seenRunIDs: [UUID],
        isCurrent: @escaping @MainActor () -> Bool
    ) async {
        guard isCurrent(), !Task.isCancelled else { return }
        let delivered = await store.delivered()
        guard isCurrent(), !Task.isCancelled else { return }
        let ids = Array(Set(delivered.map(\.notificationID))).sorted { $0.uuidString < $1.uuidString }
        // APNs can leave a large backlog; reconcile all entries in bounded batches.
        let batches = max(1, (ids.count + 199) / 200)
        for batch in 0..<batches {
            guard isCurrent(), !Task.isCancelled else { return }
            let lower = min(batch * 200, ids.count)
            let upper = min(lower + 200, ids.count)
            do {
                let result = try await api.syncNotifications(NotificationSyncRequest(
                    deliveredNotificationIDs: Array(ids[lower..<upper]),
                    seenRunIDs: batch == 0 ? Array(Set(seenRunIDs).prefix(100)) : []
                ))
                guard isCurrent(), !Task.isCancelled else { return }
                let obsolete = Set(result.obsoleteNotificationIDs).intersection(ids[lower..<upper])
                let requests = delivered.filter { obsolete.contains($0.notificationID) }.map(\.requestIdentifier)
                if !requests.isEmpty { store.remove(identifiers: requests) }
            } catch {
                // Unsupported servers, denied scopes and offline devices retain
                // their notifications; the next foreground sync retries.
                return
            }
        }
    }
}

@MainActor
final class SystemDeliveredNotificationStore: DeliveredNotificationStore {
    func delivered() async -> [DeliveredNotification] {
        await UNUserNotificationCenter.current().deliveredNotifications().compactMap { notification in
            guard let payload = NotificationPushPayload(userInfo: notification.request.content.userInfo) else { return nil }
            return DeliveredNotification(requestIdentifier: notification.request.identifier, notificationID: payload.notificationID)
        }
    }
    func remove(identifiers: [String]) {
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: identifiers)
    }
}
