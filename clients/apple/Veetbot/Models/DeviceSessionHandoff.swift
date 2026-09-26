import Foundation

/// The session a device sign-in hands to the isolated browser service, in
/// Playwright's storage-state key names (ADR-0128, browser-automation.md
/// "Device sign-in ceremony"). Every value here is a website credential: the
/// types never describe, reflect or log their contents, and nothing persists
/// them.
public struct DeviceSessionHandoff: Encodable, Equatable, Sendable {
    public let confirmedURL: String
    public let cookies: [HandoffCookie]
    public let origins: [HandoffOrigin]

    public init(confirmedURL: String, cookies: [HandoffCookie], origins: [HandoffOrigin]) {
        self.confirmedURL = confirmedURL
        self.cookies = cookies
        self.origins = origins
    }

    enum CodingKeys: String, CodingKey {
        case confirmedURL = "confirmed_url"
        case cookies, origins
    }
}

/// One cookie as Playwright's storage state spells it. A domain with a
/// leading dot is a domain cookie; without one it is host-only.
public struct HandoffCookie: Encodable, Equatable, Sendable {
    public let name: String
    public let value: String
    public let domain: String
    public let path: String
    /// `-1` for a session cookie, otherwise Unix seconds, fractions allowed.
    public let expires: Double
    public let httpOnly: Bool
    public let secure: Bool
    public let sameSite: HandoffSameSite

    public init(
        name: String,
        value: String,
        domain: String,
        path: String,
        expires: Double,
        httpOnly: Bool,
        secure: Bool,
        sameSite: HandoffSameSite
    ) {
        self.name = name
        self.value = value
        self.domain = domain
        self.path = path
        self.expires = expires
        self.httpOnly = httpOnly
        self.secure = secure
        self.sameSite = sameSite
    }
}

public enum HandoffSameSite: String, Encodable, Sendable {
    case strict = "Strict"
    case lax = "Lax"
    case none = "None"
}

/// One origin's `localStorage`.
public struct HandoffOrigin: Encodable, Equatable, Sendable {
    public let origin: String
    public let localStorage: [HandoffStorageItem]

    public init(origin: String, localStorage: [HandoffStorageItem]) {
        self.origin = origin
        self.localStorage = localStorage
    }
}

public struct HandoffStorageItem: Encodable, Equatable, Sendable {
    public let name: String
    public let value: String

    public init(name: String, value: String) {
        self.name = name
        self.value = value
    }
}

/// Session material never reaches a log, a crash report or a debugger
/// description: every rendering of these types is one fixed string, and
/// reflection shows no children (0128-design §2.5 item 7).
private let redactedDeviceSession = "<device session>"

extension DeviceSessionHandoff: CustomStringConvertible, CustomDebugStringConvertible,
    CustomReflectable
{
    public var description: String { redactedDeviceSession }
    public var debugDescription: String { redactedDeviceSession }
    public var customMirror: Mirror { Mirror(self, children: [], displayStyle: .struct) }
}

extension HandoffCookie: CustomStringConvertible, CustomDebugStringConvertible,
    CustomReflectable
{
    public var description: String { redactedDeviceSession }
    public var debugDescription: String { redactedDeviceSession }
    public var customMirror: Mirror { Mirror(self, children: [], displayStyle: .struct) }
}

extension HandoffOrigin: CustomStringConvertible, CustomDebugStringConvertible,
    CustomReflectable
{
    public var description: String { redactedDeviceSession }
    public var debugDescription: String { redactedDeviceSession }
    public var customMirror: Mirror { Mirror(self, children: [], displayStyle: .struct) }
}

extension HandoffStorageItem: CustomStringConvertible, CustomDebugStringConvertible,
    CustomReflectable
{
    public var description: String { redactedDeviceSession }
    public var debugDescription: String { redactedDeviceSession }
    public var customMirror: Mirror { Mirror(self, children: [], displayStyle: .struct) }
}
