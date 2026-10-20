import SwiftUI

#if os(iOS)
import UIKit
#endif

/// Connect's steps after the first (MagicPath kindly-cave-1942): once the
/// server accepts the token, the owner chooses whether this device shows
/// notifications, then sees what is set up before Chat opens. Step 1 is the
/// first-run Connection page in `ConnectionSettingsView`.
enum ConnectOnboardingStep: Int {
    case notifications = 2
    case ready = 3

    var number: Int { rawValue }
}

struct ConnectOnboardingView: View {
    @ObservedObject var model: ChatViewModel
    @Binding var step: ConnectOnboardingStep?
    @State private var notificationsChoice: Bool?

    var body: some View {
        VStack(spacing: 0) {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    HStack(spacing: 10) {
                        VeetbotBrandMark(size: 22)
                        Text("VEETBOT")
                            .veetType(.meta, weight: .semibold)
                            .tracking(0.8)
                            .foregroundColor(AppTheme.inkSoft)
                    }
                    if let step {
                        switch step {
                        case .notifications: notificationsStep
                        case .ready: readyStep
                        }
                    }
                }
                .frame(maxWidth: 460, alignment: .leading)
                .padding(.horizontal, 28)
                .padding(.vertical, 40)
                .frame(maxWidth: .infinity)
            }
            if let step {
                Text("Step \(step.number) of 3")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkFaint)
                    .padding(.bottom, 20)
                    .accessibilityIdentifier("connect.step")
            }
        }
        .background(AppTheme.paper)
        .tint(AppTheme.tealFill)
    }

    private var notificationsStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            Label("Connected to \(model.baseURL?.host ?? "your server")", systemImage: "checkmark")
                .veetType(.ui)
                .foregroundColor(AppTheme.tealText)
                .padding(.top, 24)
            Text("Hear about it when something needs you.")
                .veetType(.display)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 12)
            Text("Veetbot sends a notification when an approval is waiting, a question needs an answer, or a scheduled report is ready. Tapping it opens the conversation it is about.")
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 8)
            HStack(spacing: 8) {
                Button("Allow notifications") {
                    notificationsChoice = true
                    model.requestNotificationPermission()
                    step = .ready
                }
                .primaryCapsule()
                .accessibilityIdentifier("connect.notifications.allow")
                Button("Not now") {
                    notificationsChoice = false
                    step = .ready
                }
                .buttonStyle(QuietCapsuleButtonStyle())
                .accessibilityIdentifier("connect.notifications.later")
            }
            .padding(.top, 24)
        }
    }

    private var readyStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Ready.")
                .veetType(.display)
                .padding(.top, 24)
            Text("Chat opens with your history. Settings holds the connection, models, website access, notifications and appearance.")
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 8)
            VStack(alignment: .leading, spacing: 8) {
                summaryRow("Server", model.baseURL?.host ?? "")
                summaryRow("Token", "Saved in Keychain")
                summaryRow(
                    "Notifications",
                    notificationsChoice == true
                        ? "Asked for this \(deviceName)"
                        : "Off for now. Turn them on in Settings any time."
                )
                summaryRow(
                    "Websites",
                    model.browserProfiles.isEmpty
                        ? "None yet. Add one in Settings when a task needs a sign-in."
                        : "\(model.browserProfiles.count) saved"
                )
            }
            .padding(.top, 24)
            HStack(spacing: 8) {
                Button("Open Chat") { step = nil }
                    .primaryCapsule()
                    .keyboardShortcut(.defaultAction)
                    .accessibilityIdentifier("connect.open-chat")
                Button("Start over") {
                    Task {
                        await model.forgetCredentials()
                        step = nil
                    }
                }
                .buttonStyle(QuietCapsuleButtonStyle())
            }
            .padding(.top, 28)
        }
    }

    private func summaryRow(_ key: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(key).foregroundColor(AppTheme.inkSoft).frame(width: 110, alignment: .leading)
            Text(value).foregroundColor(AppTheme.ink).fixedSize(horizontal: false, vertical: true)
        }
        .veetType(.ui)
    }

    private var deviceName: String {
        #if os(macOS)
        return "Mac"
        #else
        return UIDevice.current.userInterfaceIdiom == .pad ? "iPad" : "iPhone"
        #endif
    }
}
