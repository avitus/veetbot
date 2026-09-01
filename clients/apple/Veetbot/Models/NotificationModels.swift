import Foundation

public enum DeviceKind: String, Codable, Sendable {
    case mobile
    case laptop
    case desktop
    case web
    case cli
    case surface
}

public enum PushProvider: String, Codable, Sendable {
    case apns
    case telegram
}

public enum PushEnvironment: String, Codable, Sendable {
    case sandbox
    case production
}

public enum DeviceStatus: String, Codable, Sendable {
    case active
    case revoked
}

public enum NotificationKind: String, Codable, CaseIterable, Sendable {
    case approvalRequested = "approval_requested"
    case questionAsked = "question_asked"
    case runFailed = "run_failed"
    case scheduleRunFinished = "schedule_run_finished"
    case scheduleOccurrenceSkipped = "schedule_occurrence_skipped"
    case opsAlert = "ops_alert"
    case opsRecovered = "ops_recovered"
    case test
    case deviceInvocation = "device_invocation"
}

public struct AppleDeviceRegistration: Codable, Equatable, Sendable {
    public let clientDeviceID: String
    public let name: String
    public let kind: DeviceKind
    public let platform: String
    public let appBundleID: String
    public let pushProvider: PushProvider
    public let pushToken: String
    public let pushEnvironment: PushEnvironment
    public let mutedKinds: [NotificationKind]

    public init(
        clientDeviceID: String,
        name: String,
        kind: DeviceKind,
        platform: String,
        appBundleID: String,
        pushProvider: PushProvider = .apns,
        pushToken: String,
        pushEnvironment: PushEnvironment,
        mutedKinds: [NotificationKind] = []
    ) {
        self.clientDeviceID = clientDeviceID
        self.name = name
        self.kind = kind
        self.platform = platform
        self.appBundleID = appBundleID
        self.pushProvider = pushProvider
        self.pushToken = pushToken
        self.pushEnvironment = pushEnvironment
        self.mutedKinds = mutedKinds
    }

    enum CodingKeys: String, CodingKey {
        case name, kind, platform
        case clientDeviceID = "client_device_id"
        case appBundleID = "app_bundle_id"
        case pushProvider = "push_provider"
        case pushToken = "push_token"
        case pushEnvironment = "push_environment"
        case mutedKinds = "muted_kinds"
    }
}

public struct DeviceView: Codable, Equatable, Identifiable, Sendable {
    public let id: UUID
    public let clientDeviceID: String
    public let name: String
    public let kind: DeviceKind
    public let platform: String
    public let appBundleID: String?
    public let pushProvider: PushProvider?
    public let pushEnvironment: PushEnvironment?
    public let pushTokenFingerprint: String?
    public let pushTokenUpdatedAt: Date?
    public let pushTokenInvalidatedAt: Date?
    public let mutedKinds: Set<NotificationKind>
    public let status: DeviceStatus
    public let revokedAt: Date?
    public let lastSeenAt: Date
    public let createdAt: Date
    public let updatedAt: Date

    public init(
        id: UUID,
        clientDeviceID: String,
        name: String,
        kind: DeviceKind,
        platform: String,
        appBundleID: String?,
        pushProvider: PushProvider?,
        pushEnvironment: PushEnvironment?,
        pushTokenFingerprint: String?,
        pushTokenUpdatedAt: Date?,
        pushTokenInvalidatedAt: Date?,
        mutedKinds: Set<NotificationKind>,
        status: DeviceStatus,
        revokedAt: Date?,
        lastSeenAt: Date,
        createdAt: Date,
        updatedAt: Date
    ) {
        self.id = id
        self.clientDeviceID = clientDeviceID
        self.name = name
        self.kind = kind
        self.platform = platform
        self.appBundleID = appBundleID
        self.pushProvider = pushProvider
        self.pushEnvironment = pushEnvironment
        self.pushTokenFingerprint = pushTokenFingerprint
        self.pushTokenUpdatedAt = pushTokenUpdatedAt
        self.pushTokenInvalidatedAt = pushTokenInvalidatedAt
        self.mutedKinds = mutedKinds
        self.status = status
        self.revokedAt = revokedAt
        self.lastSeenAt = lastSeenAt
        self.createdAt = createdAt
        self.updatedAt = updatedAt
    }

    enum CodingKeys: String, CodingKey {
        case id, name, kind, platform, status
        case clientDeviceID = "client_device_id"
        case appBundleID = "app_bundle_id"
        case pushProvider = "push_provider"
        case pushEnvironment = "push_environment"
        case pushTokenFingerprint = "push_token_fingerprint"
        case pushTokenUpdatedAt = "push_token_updated_at"
        case pushTokenInvalidatedAt = "push_token_invalidated_at"
        case mutedKinds = "muted_kinds"
        case revokedAt = "revoked_at"
        case lastSeenAt = "last_seen_at"
        case createdAt = "created_at"
        case updatedAt = "updated_at"
    }
}

public struct NotificationPushPayload: Codable, Equatable, Sendable {
    public let version: Int
    public let kind: NotificationKind
    public let title: String
    public let status: String?
    public let toolName: String?
    public let sessionID: UUID?
    public let runID: UUID?
    public let approvalID: UUID?
    public let questionID: UUID?
    public let scheduleID: UUID?
    public let occurrenceID: UUID?
    public let invocationID: UUID?
    public let deviceID: UUID?
    public let notificationID: UUID
    public let signal: String?
    public let severity: String?
    public let reasonCode: String?
    public let releaseID: String?

    public init?(userInfo: [AnyHashable: Any]) {
        guard let value = userInfo["veetbot"] as? [String: Any] else { return nil }
        let allowedKeys: Set<String> = [
            "version", "kind", "title", "status", "tool_name", "session_id", "run_id",
            "approval_id", "question_id", "schedule_id", "occurrence_id", "invocation_id",
            "device_id", "notification_id", "signal", "severity", "reason_code", "release_id",
        ]
        guard Set(value.keys).isSubset(of: allowedKeys),
            JSONSerialization.isValidJSONObject(value),
            let decoded = try? JSONDecoder.server.decode(
                NotificationPushPayload.self,
                from: JSONSerialization.data(withJSONObject: value)
            ),
            decoded.isStructurallyValid
        else { return nil }
        self = decoded
    }

    private var isStructurallyValid: Bool {
        guard version == 1, title == Self.titles[kind] else { return false }
        let identifiers: [String: UUID?] = [
            "session_id": sessionID,
            "run_id": runID,
            "approval_id": approvalID,
            "question_id": questionID,
            "schedule_id": scheduleID,
            "occurrence_id": occurrenceID,
            "invocation_id": invocationID,
            "device_id": deviceID,
        ]
        let present = Set(identifiers.compactMap { $0.value == nil ? nil : $0.key })
        guard present == Self.requiredIdentifiers[kind] else { return false }
        guard Self.allowedStatuses[kind]?.contains(status) == true else { return false }
        let hasOperations = signal != nil || severity != nil || reasonCode != nil || releaseID != nil
        switch kind {
        case .opsAlert:
            return signal != nil && severity != nil && severity != "recovered" && reasonCode != nil
                && toolName == nil
        case .opsRecovered:
            return signal != nil && severity == "recovered" && reasonCode != nil && toolName == nil
        case .test:
            return !hasOperations && toolName == nil
        default:
            return !hasOperations
        }
    }

    private static let titles: [NotificationKind: String] = [
        .approvalRequested: "Approval needed",
        .questionAsked: "The agent has a question",
        .runFailed: "Run failed",
        .scheduleRunFinished: "Scheduled run finished",
        .scheduleOccurrenceSkipped: "Scheduled run skipped",
        .opsAlert: "Production alert",
        .opsRecovered: "Production recovered",
        .test: "Test notification",
        .deviceInvocation: "Your device has a pending action",
    ]

    private static let requiredIdentifiers: [NotificationKind: Set<String>] = [
        .approvalRequested: ["session_id", "run_id", "approval_id"],
        .questionAsked: ["session_id", "run_id", "question_id"],
        .runFailed: ["session_id", "run_id"],
        .scheduleRunFinished: ["session_id", "run_id", "schedule_id", "occurrence_id"],
        .scheduleOccurrenceSkipped: ["schedule_id", "occurrence_id"],
        .opsAlert: [],
        .opsRecovered: [],
        .test: [],
        .deviceInvocation: ["invocation_id", "device_id"],
    ]

    private static let allowedStatuses: [NotificationKind: Set<String?>] = [
        .approvalRequested: ["WAITING_FOR_APPROVAL"],
        .questionAsked: ["WAITING_FOR_USER"],
        .runFailed: ["FAILED"],
        .scheduleRunFinished: ["COMPLETED", "FAILED", "CANCELLED"],
        .scheduleOccurrenceSkipped: [
            "MISSED", "SKIPPED_OVERLAP", "AUTHORIZATION_FAILED", "CONFIGURATION_FAILED",
        ],
        .opsAlert: [nil],
        .opsRecovered: [nil],
        .test: [nil],
        .deviceInvocation: ["pending"],
    ]

    enum CodingKeys: String, CodingKey {
        case version, kind, title, status, signal, severity
        case toolName = "tool_name"
        case sessionID = "session_id"
        case runID = "run_id"
        case approvalID = "approval_id"
        case questionID = "question_id"
        case scheduleID = "schedule_id"
        case occurrenceID = "occurrence_id"
        case invocationID = "invocation_id"
        case deviceID = "device_id"
        case notificationID = "notification_id"
        case reasonCode = "reason_code"
        case releaseID = "release_id"
    }
}

public enum NotificationFocus: Equatable, Sendable {
    case approval(UUID)
    case question(UUID)

    public var scrollID: String {
        switch self {
        case .approval(let id): return "notification-approval-\(id.uuidString)"
        case .question(let id): return "notification-question-\(id.uuidString)"
        }
    }
}

public struct NotificationDeepLink: Equatable, Sendable {
    public let sessionID: UUID
    public let runID: UUID
    public let focus: NotificationFocus?

    public init(sessionID: UUID, runID: UUID, focus: NotificationFocus?) {
        self.sessionID = sessionID
        self.runID = runID
        self.focus = focus
    }
}

public enum NotificationDeepLinkReducer {
    public static func reduce(_ payload: NotificationPushPayload) -> NotificationDeepLink? {
        guard let sessionID = payload.sessionID, let runID = payload.runID else { return nil }
        let focus: NotificationFocus?
        switch payload.kind {
        case .approvalRequested:
            guard let approvalID = payload.approvalID else { return nil }
            focus = .approval(approvalID)
        case .questionAsked:
            guard let questionID = payload.questionID else { return nil }
            focus = .question(questionID)
        default:
            focus = nil
        }
        return NotificationDeepLink(sessionID: sessionID, runID: runID, focus: focus)
    }
}
