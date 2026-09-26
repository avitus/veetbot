import Foundation

/// One device sign-in window (ADR-0128, 0128-design §5.2). A new website has
/// no profile yet: the profile is created only when the owner taps I'm signed
/// in, from the origins the window ended on.
public struct DeviceSignInRequest: Identifiable, Equatable, Sendable {
    public let id: UUID
    /// What the window loads first. The begin never sends it.
    public let startURL: URL
    public let allowedOrigins: [String]
    /// Set when signing in again to an existing profile.
    public let profileID: UUID?
    /// For a new profile only: the bare or `www` counterpart its first load may
    /// adopt (D11). Existing profiles never change origins.
    public let adoptableOrigin: String?

    public init(
        id: UUID = UUID(),
        startURL: URL,
        allowedOrigins: [String],
        profileID: UUID?,
        adoptableOrigin: String?
    ) {
        self.id = id
        self.startURL = startURL
        self.allowedOrigins = allowedOrigins
        self.profileID = profileID
        self.adoptableOrigin = adoptableOrigin
    }
}

/// Why a device sign-in did not finish, in the owner's words.
public struct DeviceSignInFailure: Equatable, Sendable {
    public let message: String
    public let canRetry: Bool
    /// The remote browser is offered as the alternative for a new website.
    public let offersRemoteBrowser: Bool

    public init(message: String, canRetry: Bool, offersRemoteBrowser: Bool = false) {
        self.message = message
        self.canRetry = canRetry
        self.offersRemoteBrowser = offersRemoteBrowser
    }
}

public enum DeviceSignInResult: Equatable, Sendable {
    case signedIn(profileID: UUID)
    case failed(DeviceSignInFailure)
}

/// How long a device sign-in waits on an unknown handoff outcome: the status
/// is read every 2 seconds for up to 45 (0128-design §2.3).
public struct DeviceSignInTiming: Sendable {
    public var pollInterval: TimeInterval
    public var pollLimit: TimeInterval

    public init(pollInterval: TimeInterval = 2, pollLimit: TimeInterval = 45) {
        self.pollInterval = pollInterval
        self.pollLimit = pollLimit
    }

    public static let standard = DeviceSignInTiming()
}

/// The fixed texts a device sign-in shows (0128-design §2.1, §2.3, §2.5).
public enum DeviceSignInMessage {
    public static let couldNotStart = "Couldn't start the sign-in on this device."
    public static let websiteInUse =
        "Veetbot is using this website in a chat right now. Try again when it finishes."
    public static let tooMuchData = "This website stores more data than Veetbot can save."
    public static let sessionEmpty =
        "This website hasn't saved a sign-in yet. Finish signing in, then tap I'm signed in again."
    public static let sessionSignedOut =
        "Veetbot's browser opened this page signed out. Sign in again, open a page only signed-in members see, then tap I'm signed in."
    public static let sessionUnconfirmed =
        "Open a page only signed-in members see, such as your account or learning page, then tap I'm signed in again."
    public static let couldNotCheck = "Couldn't check the sign-in. Try again."
    public static let couldNotConfirm = "Couldn't confirm the sign-in; try again."
    public static let profileUnavailable =
        "This website login was removed. Close this window and add the website again."
    public static let couldNotFinish = "Couldn't finish the sign-in. Try again."
}
