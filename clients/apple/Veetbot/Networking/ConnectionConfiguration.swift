import Foundation

public enum ConnectionConfigurationError: Error, LocalizedError, Equatable {
    case invalidURL
    case httpsRequired
    case credentialsNotAllowed
    case baseURLMustNotContainQueryOrFragment

    public var errorDescription: String? {
        switch self {
        case .invalidURL:
            return "Enter a valid Veetbot server URL."
        case .httpsRequired:
            return "Veetbot connections require HTTPS. Plaintext HTTP is not allowed."
        case .credentialsNotAllowed:
            return "The server URL must not contain a username or password."
        case .baseURLMustNotContainQueryOrFragment:
            return "The server URL must not contain a query or fragment."
        }
    }
}

public struct ConnectionConfiguration: Codable, Hashable, Sendable {
    public let baseURL: URL

    public init(baseURL: URL) throws {
        guard
            let components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false),
            components.host != nil
        else {
            throw ConnectionConfigurationError.invalidURL
        }
        guard components.scheme?.lowercased() == "https" else {
            throw ConnectionConfigurationError.httpsRequired
        }
        guard components.user == nil, components.password == nil else {
            throw ConnectionConfigurationError.credentialsNotAllowed
        }
        guard components.query == nil, components.fragment == nil else {
            throw ConnectionConfigurationError.baseURLMustNotContainQueryOrFragment
        }
        self.baseURL = baseURL
    }

    public init(baseURLString: String) throws {
        guard let url = URL(string: baseURLString.trimmingCharacters(in: .whitespacesAndNewlines))
        else {
            throw ConnectionConfigurationError.invalidURL
        }
        try self.init(baseURL: url)
    }

    public func url(path: String, queryItems: [URLQueryItem] = []) throws -> URL {
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw ConnectionConfigurationError.invalidURL
        }
        let basePath = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        let routePath = path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.path =
            "/"
            + [basePath, routePath]
            .filter { !$0.isEmpty }
            .joined(separator: "/")
        components.queryItems = queryItems.isEmpty ? nil : queryItems
        guard let url = components.url else {
            throw ConnectionConfigurationError.invalidURL
        }
        return url
    }
}

public actor ConnectionConfigurationStore {
    private let defaults: UserDefaults
    private let key = "veetbot.connection.baseURL"

    public init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
    }

    public func load() -> ConnectionConfiguration? {
        guard let value = defaults.string(forKey: key) else { return nil }
        return try? ConnectionConfiguration(baseURLString: value)
    }

    public func save(_ configuration: ConnectionConfiguration) {
        // The bearer token is deliberately not stored here; it lives in Keychain.
        defaults.set(configuration.baseURL.absoluteString, forKey: key)
    }
}
