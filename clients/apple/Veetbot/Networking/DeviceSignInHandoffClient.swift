import Foundation

/// Where a device sign-in sends the session: the begin response's
/// `launch_url` without its fragment, and the fragment's single-use
/// capability (ADR-0128, 0128-design §2.5 item 6). It lives only in the locals
/// of one sign-in attempt and never describes its capability.
public struct DeviceSignInHandoffTarget: Sendable {
    public let endpoint: URL
    public let capability: String

    public init?(launchURL: URL?) {
        guard
            let launchURL,
            var components = URLComponents(url: launchURL, resolvingAgainstBaseURL: false),
            components.scheme?.lowercased() == "https",
            let host = components.host, !host.isEmpty,
            components.user == nil, components.password == nil,
            components.query == nil,
            components.path.range(
                of: "^/authentication/[0-9a-fA-F-]{36}/handoff$", options: .regularExpression
            ) != nil,
            let fragment = components.fragment,
            fragment.range(of: "^capability=[A-Za-z0-9_-]{32,128}$", options: .regularExpression)
                != nil
        else { return nil }
        components.fragment = nil
        guard let endpoint = components.url else { return nil }
        self.endpoint = endpoint
        self.capability = String(fragment.dropFirst("capability=".count))
    }
}

extension DeviceSignInHandoffTarget: CustomStringConvertible, CustomDebugStringConvertible,
    CustomReflectable
{
    public var description: String { "<device handoff target>" }
    public var debugDescription: String { "<device handoff target>" }
    public var customMirror: Mirror { Mirror(self, children: [], displayStyle: .struct) }
}

/// What the isolated service answered (0128-design §2.3).
public enum DeviceHandoffOutcome: Equatable, Sendable {
    /// `200`: verified and sealed.
    case sealed
    /// A fixed error code the service returned, such as `session_unconfirmed`.
    case rejected(code: String)
    /// `401`: the capability was missing, wrong, used, expired or not a device one.
    case capabilityRejected
    /// `413`: the body was over 1 MiB.
    case tooLarge
    /// No HTTP answer: the outcome is unknown and only the ceremony status tells.
    case transportFailed
}

/// Sends a device session to the isolated service's handoff address with its
/// own ephemeral session: no cookies, no cache, no credential store, no
/// redirects, and never the Veetbot API credential (§2.5 item 5).
public struct DeviceSignInHandoffClient: Sendable {
    public static let capabilityHeader = "X-Browser-Ceremony-Capability"
    public static let requestTimeout: TimeInterval = 75

    private let session: URLSession

    public init(session: URLSession = DeviceSignInHandoffClient.makeDefaultSession()) {
        self.session = session
    }

    public static func makeSessionConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieAcceptPolicy = .never
        configuration.httpCookieStorage = nil
        configuration.urlCache = nil
        configuration.urlCredentialStorage = nil
        configuration.timeoutIntervalForRequest = requestTimeout
        return configuration
    }

    public static func makeDefaultSession() -> URLSession {
        URLSession(
            configuration: makeSessionConfiguration(),
            delegate: RejectRedirectsDelegate(),
            delegateQueue: nil
        )
    }

    /// One POST of an already validated body. It is never retried: a lost
    /// answer is resolved by reading the ceremony's status (§2.3).
    public func send(_ body: Data, to target: DeviceSignInHandoffTarget) async -> DeviceHandoffOutcome {
        var request = URLRequest(url: target.endpoint)
        request.httpMethod = "POST"
        request.httpBody = body
        request.httpShouldHandleCookies = false
        request.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        request.timeoutInterval = Self.requestTimeout
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue(target.capability, forHTTPHeaderField: Self.capabilityHeader)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            return .transportFailed
        }
        guard let http = response as? HTTPURLResponse else { return .transportFailed }
        switch http.statusCode {
        case 200:
            // Anything but the documented answer leaves the outcome to the
            // ceremony status, which is authoritative.
            let status = (try? JSONDecoder().decode(HandoffAnswer.self, from: data))?.status
            return status == "ready" ? .sealed : .transportFailed
        case 401:
            return .capabilityRejected
        case 413:
            return .tooLarge
        default:
            let code = (try? JSONDecoder().decode(HandoffError.self, from: data))?.error.code
            return .rejected(code: code ?? "http_\(http.statusCode)")
        }
    }
}

private struct HandoffAnswer: Decodable {
    let status: String
}

private struct HandoffError: Decodable {
    struct Body: Decodable {
        let code: String
    }

    let error: Body
}
