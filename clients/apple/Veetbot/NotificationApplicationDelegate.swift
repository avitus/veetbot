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
                let granted = try await center.requestAuthorization(
                    options: [.alert, .badge, .sound]
                )
                guard granted else { return }
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
        guard remoteRegistrationEnabled else { return }
        registerWithOperatingSystem()
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
        completionHandler([.banner, .list, .sound])
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
