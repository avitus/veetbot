#if os(iOS)
import MessageUI
import SwiftUI
import UIKit

/// The system compose sheet, prefilled from one `device.sms.send` invocation.
/// Nothing here sends: the owner's Send tap inside the system sheet performs
/// the send, which is the non-bypassable human confirmation the policy relies
/// on (docs/plan/device-channel-and-sms.md).
struct SmsComposeSheet: UIViewControllerRepresentable {
    let invocation: SmsInvocation
    let onFinish: (MessageComposeResult) -> Void

    /// Whether this device can send a text at all. A device that cannot must
    /// report `failed` rather than present a sheet the owner cannot use.
    static var canSend: Bool { MFMessageComposeViewController.canSendText() }

    func makeUIViewController(context: Context) -> MFMessageComposeViewController {
        let controller = MFMessageComposeViewController()
        controller.messageComposeDelegate = context.coordinator
        controller.recipients = [invocation.recipient]
        controller.body = invocation.body
        return controller
    }

    func updateUIViewController(
        _ controller: MFMessageComposeViewController,
        context: Context
    ) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(onFinish: onFinish)
    }

    final class Coordinator: NSObject, MFMessageComposeViewControllerDelegate {
        private let onFinish: (MessageComposeResult) -> Void

        init(onFinish: @escaping (MessageComposeResult) -> Void) {
            self.onFinish = onFinish
        }

        /// Reports the outcome and leaves dismissal to the state change that
        /// report causes. Dismissing the controller here as well would race
        /// SwiftUI's own dismissal of the sheet.
        func messageComposeViewController(
            _ controller: MFMessageComposeViewController,
            didFinishWith result: MessageComposeResult
        ) {
            onFinish(result)
        }
    }
}

extension DeviceInvocationResult {
    /// The compose sheet's outcome as the single result the server records.
    /// An outcome this client does not recognize is reported `failed` rather
    /// than assumed sent.
    init(composeResult: MessageComposeResult) {
        switch composeResult {
        case .sent: self = .sent
        case .cancelled: self = .cancelled
        case .failed: self = .failed
        @unknown default: self = .failed
        }
    }
}
#endif
