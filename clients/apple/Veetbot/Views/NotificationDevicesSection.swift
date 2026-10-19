import SwiftUI
import UserNotifications

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

/// Settings → Notifications: whether this device may show notifications,
/// then every device the server notifies, this one first, each with a test
/// and a removal that asks first.
struct NotificationDevicesSection: View {
    @ObservedObject var model: NotificationSettingsViewModel
    @ObservedObject var chat: ChatViewModel
    @State private var permission: UNAuthorizationStatus?
    @State private var confirmingRemoval: UUID?
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            permissionRow
            HairlineDivider()
            if model.unavailable {
                Text("This server does not list devices.")
                    .appFont(.callout)
                    .foregroundColor(AppTheme.inkSoft)
            } else if model.devices.isEmpty {
                Text(model.isLoading ? "Loading devices…" : "No device is registered for notifications yet.")
                    .appFont(.callout)
                    .foregroundColor(AppTheme.inkSoft)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(model.devices.enumerated()), id: \.element.id) { index, device in
                        if index > 0 { HairlineDivider() }
                        deviceRow(device)
                    }
                }
                .a2Card()
            }
            if let error = model.errorMessage {
                Text(error)
                    .appFont(.caption)
                    .foregroundColor(AppTheme.red)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("notifications.error")
            }
        }
        .task { await refresh() }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { await refresh() } }
        }
    }

    private var permissionRow: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text("This \(deviceName)")
                    .appFont(.body, weight: .semibold)
                    .foregroundColor(AppTheme.ink)
                Text(permissionDescription)
                    .appFont(.caption)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            switch permission {
            case .notDetermined:
                Button("Allow notifications") {
                    chat.requestNotificationPermission()
                    Task {
                        try? await Task.sleep(nanoseconds: 1_000_000_000)
                        await refresh()
                    }
                }
                .primaryCapsule()
                .accessibilityIdentifier("notifications.allow")
            case .denied:
                Button("Open System Settings") { openSystemSettings() }
                    .buttonStyle(SecondaryCapsuleButtonStyle())
            default:
                EmptyView()
            }
        }
    }

    private var permissionDescription: String {
        switch permission {
        case .authorized, .provisional, .ephemeral: return "Notifications are on."
        case .denied: return "Notifications are off in System Settings."
        case .notDetermined: return "Veetbot has not asked yet."
        default: return "Checking…"
        }
    }

    private func deviceRow(_ device: DeviceView) -> some View {
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: device.kind == .mobile ? "iphone" : "laptopcomputer")
                .foregroundColor(AppTheme.inkSoft)
                .frame(width: 20)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(model.isThisDevice(device) ? "\(device.name) · this \(deviceName)" : device.name)
                    .appFont(.body)
                    .foregroundColor(AppTheme.ink)
                Text("Last seen \(device.lastSeenAt.formatted(.relative(presentation: .named)))")
                    .appFont(.caption)
                    .foregroundColor(AppTheme.inkSoft)
            }
            Spacer(minLength: 8)
            if confirmingRemoval == device.id {
                Button("Remove") {
                    confirmingRemoval = nil
                    Task { await model.remove(device) }
                }
                .buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red))
                .accessibilityIdentifier("notifications.device.remove.confirm")
                Button("Keep") { confirmingRemoval = nil }
                    .buttonStyle(QuietCapsuleButtonStyle())
            } else {
                Button("Send test") { Task { await model.sendTest(to: device) } }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .disabled(model.pendingDeviceID != nil)
                    .accessibilityIdentifier("notifications.device.test.\(device.id.uuidString)")
                if !model.isThisDevice(device) {
                    Button { confirmingRemoval = device.id } label: { Image(systemName: "trash") }
                        .buttonStyle(QuietIconButtonStyle())
                        .accessibilityLabel("Remove \(device.name)")
                        .disabled(model.pendingDeviceID != nil)
                        .accessibilityIdentifier("notifications.device.remove.\(device.id.uuidString)")
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    private var deviceName: String {
        #if os(macOS)
        return "Mac"
        #else
        return UIDevice.current.userInterfaceIdiom == .pad ? "iPad" : "iPhone"
        #endif
    }

    private func refresh() async {
        permission = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
        await model.load()
    }

    private func openSystemSettings() {
        #if os(iOS)
        if let url = URL(string: UIApplication.openNotificationSettingsURLString) { openURL(url) }
        #elseif os(macOS)
        if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
            openURL(url)
        }
        #endif
    }
}
