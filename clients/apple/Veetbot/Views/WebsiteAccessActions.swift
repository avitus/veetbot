import Foundation

/// Which Website Access actions a profile row offers (ADR-0128 D12).
enum WebsiteAccessActions {
    /// Any profile that is not revoked can be signed in to again.
    static func offersSignInAgain(_ status: BrowserProfileStatus) -> Bool {
        status != .revoked
    }
}
