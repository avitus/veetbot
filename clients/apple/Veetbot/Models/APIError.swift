import Foundation

public enum APIErrorCode: Hashable, Sendable {
    case authenticationError
    case authorizationError
    case notFound
    case conflict
    case invalidStateTransition
    case toolNotFound
    case toolValidationError
    case toolPolicyDenied
    case approvalRequired
    case approvalDenied
    case approvalExpired
    case budgetExceeded
    case deadlineExceeded
    case runDeadlineExceeded
    case runCancelled
    case contextOverflow
    case toolLoopDetected
    case modelTransientError
    case modelPermanentError
    case modelProtocolError
    case toolTimeout
    case toolExecutionError
    case toolResultInvalid
    case sandboxProvisionError
    case sandboxExecutionError
    case artifactStorageError
    case concurrencyConflict
    case malformedRequest
    case unsupportedMediaType
    case payloadTooLarge
    case rateLimited
    case internalError
    case unknown(String)

    public init(rawValue: String) {
        switch rawValue {
        case "authentication_error": self = .authenticationError
        case "authorization_error": self = .authorizationError
        case "not_found": self = .notFound
        case "conflict": self = .conflict
        case "invalid_state_transition": self = .invalidStateTransition
        case "tool_not_found": self = .toolNotFound
        case "tool_validation_error": self = .toolValidationError
        case "tool_policy_denied": self = .toolPolicyDenied
        case "approval_required": self = .approvalRequired
        case "approval_denied": self = .approvalDenied
        case "approval_expired": self = .approvalExpired
        case "budget_exceeded": self = .budgetExceeded
        case "deadline_exceeded": self = .deadlineExceeded
        case "run_deadline_exceeded": self = .runDeadlineExceeded
        case "run_cancelled": self = .runCancelled
        case "context_overflow": self = .contextOverflow
        case "tool_loop_detected": self = .toolLoopDetected
        case "model_transient_error": self = .modelTransientError
        case "model_permanent_error": self = .modelPermanentError
        case "model_protocol_error": self = .modelProtocolError
        case "tool_timeout": self = .toolTimeout
        case "tool_execution_error": self = .toolExecutionError
        case "tool_result_invalid": self = .toolResultInvalid
        case "sandbox_provision_error": self = .sandboxProvisionError
        case "sandbox_execution_error": self = .sandboxExecutionError
        case "artifact_storage_error": self = .artifactStorageError
        case "concurrency_conflict": self = .concurrencyConflict
        case "malformed_request": self = .malformedRequest
        case "unsupported_media_type": self = .unsupportedMediaType
        case "payload_too_large": self = .payloadTooLarge
        case "rate_limited": self = .rateLimited
        case "internal_error": self = .internalError
        default: self = .unknown(rawValue)
        }
    }

    public var rawValue: String {
        switch self {
        case .authenticationError: return "authentication_error"
        case .authorizationError: return "authorization_error"
        case .notFound: return "not_found"
        case .conflict: return "conflict"
        case .invalidStateTransition: return "invalid_state_transition"
        case .toolNotFound: return "tool_not_found"
        case .toolValidationError: return "tool_validation_error"
        case .toolPolicyDenied: return "tool_policy_denied"
        case .approvalRequired: return "approval_required"
        case .approvalDenied: return "approval_denied"
        case .approvalExpired: return "approval_expired"
        case .budgetExceeded: return "budget_exceeded"
        case .deadlineExceeded: return "deadline_exceeded"
        case .runDeadlineExceeded: return "run_deadline_exceeded"
        case .runCancelled: return "run_cancelled"
        case .contextOverflow: return "context_overflow"
        case .toolLoopDetected: return "tool_loop_detected"
        case .modelTransientError: return "model_transient_error"
        case .modelPermanentError: return "model_permanent_error"
        case .modelProtocolError: return "model_protocol_error"
        case .toolTimeout: return "tool_timeout"
        case .toolExecutionError: return "tool_execution_error"
        case .toolResultInvalid: return "tool_result_invalid"
        case .sandboxProvisionError: return "sandbox_provision_error"
        case .sandboxExecutionError: return "sandbox_execution_error"
        case .artifactStorageError: return "artifact_storage_error"
        case .concurrencyConflict: return "concurrency_conflict"
        case .malformedRequest: return "malformed_request"
        case .unsupportedMediaType: return "unsupported_media_type"
        case .payloadTooLarge: return "payload_too_large"
        case .rateLimited: return "rate_limited"
        case .internalError: return "internal_error"
        case .unknown(let value): return value
        }
    }
}

extension APIErrorCode: Codable {
    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        self.init(rawValue: try container.decode(String.self))
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(rawValue)
    }
}

public struct APIErrorDetails: Codable, Hashable, Sendable {
    public let values: [String: JSONValue]

    public init(values: [String: JSONValue] = [:]) {
        self.values = values
    }

    public init(from decoder: Decoder) throws {
        values = try [String: JSONValue](from: decoder)
    }

    public func encode(to encoder: Encoder) throws {
        try values.encode(to: encoder)
    }

    public var reason: String? { values["reason"]?.stringValue }
    public var runID: UUID? { values["run_id"]?.stringValue.flatMap(UUID.init(uuidString:)) }
    public var runStatus: RunStatus? {
        values["run_status"]?.stringValue.flatMap(RunStatus.init(rawValue:))
    }
    public var approvalID: UUID? {
        values["approval_id"]?.stringValue.flatMap(UUID.init(uuidString:))
    }
    public var decision: ApprovalDecision? {
        values["decision"]?.stringValue.flatMap(ApprovalDecision.init(rawValue:))
    }
}

public struct APIError: Error, Codable, Sendable, LocalizedError {
    public let code: APIErrorCode
    public let message: String
    public let details: APIErrorDetails
    public let requestID: String
    public var statusCode: Int?

    private enum RootKeys: String, CodingKey { case error }
    private enum ErrorKeys: String, CodingKey {
        case code, message, details
        case requestID = "request_id"
    }

    public init(
        code: APIErrorCode,
        message: String,
        details: APIErrorDetails = APIErrorDetails(),
        requestID: String,
        statusCode: Int? = nil
    ) {
        self.code = code
        self.message = message
        self.details = details
        self.requestID = requestID
        self.statusCode = statusCode
    }

    public init(from decoder: Decoder) throws {
        let root = try decoder.container(keyedBy: RootKeys.self)
        let nested = try root.nestedContainer(keyedBy: ErrorKeys.self, forKey: .error)
        code = try nested.decode(APIErrorCode.self, forKey: .code)
        message = try nested.decode(String.self, forKey: .message)
        details =
            try nested.decodeIfPresent(APIErrorDetails.self, forKey: .details)
            ?? APIErrorDetails()
        requestID = try nested.decodeIfPresent(String.self, forKey: .requestID) ?? "unknown"
        statusCode = nil
    }

    public func encode(to encoder: Encoder) throws {
        var root = encoder.container(keyedBy: RootKeys.self)
        var nested = root.nestedContainer(keyedBy: ErrorKeys.self, forKey: .error)
        try nested.encode(code, forKey: .code)
        try nested.encode(message, forKey: .message)
        try nested.encode(details, forKey: .details)
        try nested.encode(requestID, forKey: .requestID)
    }

    public var errorDescription: String? { message }
}
