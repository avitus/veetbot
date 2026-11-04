import Foundation

/// Which Website Access actions a profile row offers (ADR-0128 D12).
enum WebsiteAccessActions {
    /// Any profile that is not revoked can be signed in to again.
    static func offersSignInAgain(_ status: BrowserProfileStatus) -> Bool {
        status != .revoked
    }

    /// Sign in again opens the remote browser for a login last signed in that way,
    /// so the row keeps the device window within reach (ADR-0296).
    static func offersDeviceSignIn(_ profile: BrowserProfileView) -> Bool {
        offersSignInAgain(profile.status) && profile.lastSignInMode == .remote
    }
}
