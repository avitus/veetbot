import Combine
import Foundation

/// Owner-controlled, device-local preference for the SMS-capture integration
/// (docs/plan/device-channel-and-sms.md). Off by default: the device declares
/// the `device.sms.send` capability during registration only once the owner
/// opts in.
@MainActor
public final class SmsIntegrationPreferences: ObservableObject {
    private enum Key {
        static let integrationEnabled = "veetbot.sms.integrationEnabled"
    }

    @Published public var integrationEnabled: Bool {
        didSet { defaults.set(integrationEnabled, forKey: Key.integrationEnabled) }
    }

    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.integrationEnabled = defaults.bool(forKey: Key.integrationEnabled)
    }

    /// The capability tokens the device should declare on the next
    /// registration. Empty when the integration is off.
    public var declaredCapabilities: [String] {
        integrationEnabled ? ["device.sms.send"] : []
    }
}
