import Foundation

public enum AuthorizationState: Equatable, Sendable {
    case authenticated
    case requiresReauthentication
}

public enum HTTPMethod: String, Sendable {
    case get = "GET"
    case post = "POST"
    case delete = "DELETE"
}

public struct TransportRequest: Sendable {
    public let method: HTTPMethod
    public let path: String
    public let queryItems: [URLQueryItem]
    public let body: Data?
    public let headers: [String: String]
    public let requiresAuthentication: Bool
    public let retryAttempts: Int

    public init(
        method: HTTPMethod,
        path: String,
        queryItems: [URLQueryItem] = [],
        body: Data? = nil,
        headers: [String: String] = [:],
        requiresAuthentication: Bool = true,
        retryAttempts: Int = 1
    ) {
        self.method = method
        self.path = path
        self.queryItems = queryItems
        self.body = body
        self.headers = headers
        self.requiresAuthentication = requiresAuthentication
        self.retryAttempts = max(1, retryAttempts)
    }
}

public enum HTTPTransportError: Error, LocalizedError {
    case notConfigured
    case missingToken
    case invalidResponse
    case reauthenticationRequired(APIError)
    case authorizationDenied(APIError)
    case api(APIError)
    case connection(URLError)

    public var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "Configure a Veetbot server connection first."
        case .missingToken:
            return "Enter a bearer token to connect."
        case .invalidResponse:
            return "The server returned an invalid HTTP response."
        case .reauthenticationRequired(let error),
            .authorizationDenied(let error),
            .api(let error):
            return error.message
        case .connection(let error):
            return error.localizedDescription
        }
    }
}

public final class RejectRedirectsDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    public func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        completionHandler(nil)
    }
}

public actor HTTPTransport {
    public let configuration: ConnectionConfiguration
    private let tokenStore: any TokenStore
    private let session: URLSession
    private var state: AuthorizationState = .authenticated

    public init(
        configuration: ConnectionConfiguration,
        tokenStore: any TokenStore,
        session: URLSession? = nil
    ) {
        self.configuration = configuration
        self.tokenStore = tokenStore
        if let session {
            self.session = session
        } else {
            let config = URLSessionConfiguration.ephemeral
            config.waitsForConnectivity = true
            config.timeoutIntervalForRequest = 60
            config.timeoutIntervalForResource = 60 * 60
            config.httpShouldSetCookies = false
            config.httpCookieAcceptPolicy = .never
            config.urlCache = nil
            self.session = URLSession(
                configuration: config,
                delegate: RejectRedirectsDelegate(),
                delegateQueue: nil
            )
        }
    }

    public func authorizationState() -> AuthorizationState { state }

    public func send<Response: Decodable>(
        _ request: TransportRequest,
        as type: Response.Type = Response.self
    ) async throws -> Response {
        let (data, _) = try await sendData(request)
        return try JSONDecoder.server.decode(Response.self, from: data)
    }

    public func sendData(
        _ request: TransportRequest,
        accepting additionalStatusCodes: Set<Int> = []
    ) async throws -> (Data, HTTPURLResponse) {
        let urlRequest = try await makeURLRequest(request)
        var lastConnectionError: URLError?
        for attempt in 1...request.retryAttempts {
            do {
                let (data, response) = try await session.data(for: urlRequest)
                guard let http = response as? HTTPURLResponse else {
                    throw HTTPTransportError.invalidResponse
                }
                if (200...299).contains(http.statusCode)
                    || additionalStatusCodes.contains(http.statusCode)
                {
                    state = .authenticated
                    return (data, http)
                }
                if http.statusCode.isRetryableHTTPStatus,
                    attempt < request.retryAttempts
                {
                    try await Task.sleep(
                        nanoseconds: Self.retryDelayNanoseconds(
                            response: http,
                            attempt: attempt
                        )
                    )
                    continue
                }
                try throwAPIError(data: data, response: http)
            } catch is CancellationError {
                throw CancellationError()
            } catch let error as HTTPTransportError {
                throw error
            } catch let error as URLError {
                lastConnectionError = error
                guard attempt < request.retryAttempts, error.isRetryableConnectionFailure else {
                    throw HTTPTransportError.connection(error)
                }
                let delay = UInt64(min(0.25 * pow(2, Double(attempt - 1)), 2) * 1_000_000_000)
                try await Task.sleep(nanoseconds: delay)
            }
        }
        throw HTTPTransportError.connection(
            lastConnectionError ?? URLError(.unknown)
        )
    }

    public func makeSSERequest(path: String, lastEventID: Int?) async throws -> URLRequest {
        var headers = ["Accept": "text/event-stream"]
        if let lastEventID {
            headers["Last-Event-ID"] = String(lastEventID)
        }
        return try await makeURLRequest(
            TransportRequest(method: .get, path: path, headers: headers)
        )
    }

    public func streamingSession() -> URLSession { session }

    public func decodeStreamingError(data: Data, response: HTTPURLResponse) throws -> Never {
        try throwAPIError(data: data, response: response)
    }

    private func makeURLRequest(_ request: TransportRequest) async throws -> URLRequest {
        let url = try configuration.url(path: request.path, queryItems: request.queryItems)
        var urlRequest = URLRequest(url: url)
        urlRequest.httpMethod = request.method.rawValue
        urlRequest.httpBody = request.body
        urlRequest.setValue("application/json", forHTTPHeaderField: "Accept")
        urlRequest.setValue(UUID().uuidString.lowercased(), forHTTPHeaderField: "X-Request-Id")
        if request.body != nil {
            urlRequest.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        if request.requiresAuthentication {
            guard let token = try await tokenStore.readToken(), !token.isEmpty else {
                state = .requiresReauthentication
                throw HTTPTransportError.missingToken
            }
            urlRequest.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        for (name, value) in request.headers {
            urlRequest.setValue(value, forHTTPHeaderField: name)
        }
        return urlRequest
    }

    private func throwAPIError(data: Data, response: HTTPURLResponse) throws -> Never {
        var error =
            (try? JSONDecoder.server.decode(APIError.self, from: data))
            ?? APIError(
                code: .unknown("http_\(response.statusCode)"),
                message: HTTPURLResponse.localizedString(forStatusCode: response.statusCode),
                requestID: response.value(forHTTPHeaderField: "X-Request-Id") ?? "unknown"
            )
        error.statusCode = response.statusCode
        switch response.statusCode {
        case 401:
            state = .requiresReauthentication
            throw HTTPTransportError.reauthenticationRequired(error)
        case 403:
            throw HTTPTransportError.authorizationDenied(error)
        default:
            throw HTTPTransportError.api(error)
        }
    }

    static func retryDelayNanoseconds(
        response: HTTPURLResponse,
        attempt: Int,
        now: Date = Date()
    ) -> UInt64 {
        let fallback = min(0.25 * pow(2, Double(attempt - 1)), 2)
        guard let rawValue = response.value(forHTTPHeaderField: "Retry-After") else {
            return UInt64(fallback * 1_000_000_000)
        }
        let delay: TimeInterval
        if let seconds = TimeInterval(rawValue), seconds >= 0 {
            delay = seconds
        } else if let date = Self.httpDate(from: rawValue) {
            delay = max(0, date.timeIntervalSince(now))
        } else {
            delay = fallback
        }
        return UInt64(min(delay, 60) * 1_000_000_000)
    }

    private static func httpDate(from value: String) -> Date? {
        let formats = [
            "EEE',' dd MMM yyyy HH':'mm':'ss z",
            "EEEE',' dd'-'MMM'-'yy HH':'mm':'ss z",
            "EEE MMM d HH':'mm':'ss yyyy",
        ]
        for format in formats {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.calendar = Calendar(identifier: .gregorian)
            formatter.timeZone = TimeZone(secondsFromGMT: 0)
            formatter.dateFormat = format
            if let date = formatter.date(from: value) {
                return date
            }
        }
        return nil
    }
}

extension URLError {
    fileprivate var isRetryableConnectionFailure: Bool {
        switch code {
        case .timedOut, .cannotFindHost, .cannotConnectToHost, .networkConnectionLost,
            .dnsLookupFailed, .notConnectedToInternet, .internationalRoamingOff,
            .callIsActive, .dataNotAllowed:
            return true
        default:
            return false
        }
    }
}

extension Int {
    fileprivate var isRetryableHTTPStatus: Bool {
        self == 429 || (500...599).contains(self)
    }
}
