import Combine
import Foundation

public struct LoadedArtifact: Sendable {
    public let metadata: ArtifactView
    public let data: Data
}

private enum MissingSessionResolution: Sendable {
    case found(SessionView)
    case deleted(UUID)
}

private func resolveMissingSession(
    _ sessionID: UUID,
    using api: VeetbotAPIClient
) async throws -> MissingSessionResolution {
    do {
        return .found(try await api.getSession(sessionID))
    } catch let error as HTTPTransportError {
        if case .api(let apiError) = error, apiError.statusCode == 404 {
            return .deleted(sessionID)
        }
        throw error
    }
}

private func resolveMissingSessions(
    _ sessionIDs: Set<UUID>,
    using api: VeetbotAPIClient,
    maximumConcurrency: Int = 8
) async throws -> [MissingSessionResolution] {
    let orderedIDs = sessionIDs.sorted { $0.uuidString < $1.uuidString }
    let concurrency = min(max(1, maximumConcurrency), orderedIDs.count)
    return try await withThrowingTaskGroup(of: MissingSessionResolution.self) { group in
        for sessionID in orderedIDs.prefix(concurrency) {
            group.addTask { try await resolveMissingSession(sessionID, using: api) }
        }
        var nextIndex = concurrency
        var resolutions: [MissingSessionResolution] = []
        resolutions.reserveCapacity(orderedIDs.count)
        while let resolution = try await group.next() {
            resolutions.append(resolution)
            if nextIndex < orderedIDs.count {
                let sessionID = orderedIDs[nextIndex]
                nextIndex += 1
                group.addTask { try await resolveMissingSession(sessionID, using: api) }
            }
        }
        return resolutions
    }
}

/// Guards a keyset paginator against a server that returns the cursor it was
/// given, which would otherwise spin the client in a fetch loop forever.
/// Shared by every view model that walks a `Page` by cursor.
func nextPageCursor(
    _ cursor: String?,
    seen: inout Set<String>
) throws -> String? {
    guard let cursor else { return nil }
    guard seen.insert(cursor).inserted else {
        throw HTTPTransportError.invalidResponse
    }
    return cursor
}

/// Whatever owns the platform's push registration — the application delegate
/// in the shipping app. A capability the owner switches on has to reach the
/// server through a fresh registration, and only the delegate may ask the
/// operating system for the token that carries it.
@MainActor
public protocol PushRegistrationRequesting: AnyObject {
    func requestPushRegistration()
}

@MainActor
public final class ChatViewModel: ObservableObject {
    @Published public private(set) var history: [SessionHistoryEntry] = []
    @Published public private(set) var selectedSessionID: UUID?
    @Published public private(set) var baseURL: URL?
    @Published public private(set) var isConfigured = false
    @Published public private(set) var requiresReauthentication = false
    @Published public private(set) var isSending = false
    @Published public private(set) var sessionBusy = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var notificationFeatureAvailable: Bool?
    @Published public private(set) var notificationFocus: NotificationFocus?
    @Published public private(set) var notificationNavigationID: UUID?
    @Published public private(set) var browserProfiles: [BrowserProfileView] = []
    @Published public private(set) var selectedBrowserProfileID: UUID?
    @Published public private(set) var browserAuthentication: BrowserAuthenticationView?
    /// The one-time launch capability of the in-flight ceremony. It is held only
    /// in memory, only while its ceremony is live, and is surrendered with the
    /// ceremony so no view can offer it after the ceremony ends.
    @Published public private(set) var websiteAuthenticationLaunchURL: URL?
    @Published public private(set) var isManagingWebsiteAccess = false
    /// The `device.sms.send` invocation whose compose sheet the owner should
    /// see now, if any. Its recipient and body live only here and in the sheet.
    @Published public private(set) var pendingSmsInvocation: SmsInvocation?
    /// This installation's server-side device id, learned from its own
    /// registration. Nil until the push token has been registered this launch.
    @Published public private(set) var registeredDeviceID: UUID?

    /// Set by the application delegate when it attaches.
    public weak var pushRegistrar: (any PushRegistrationRequesting)?

    public let runState: RunStateReducer

    private let tokenStore: any TokenStore
    private let configurationStore: ConnectionConfigurationStore
    private let historyStore: any SessionHistoryStore
    private let artifactCache: ArtifactCache
    private let deviceRegistrationCoordinator: DeviceRegistrationCoordinator
    private let urlSession: URLSession?
    private var api: VeetbotAPIClient?
    private var eventStream: ReconnectingEventStream?
    private let watchTasks = WatchTaskBox()
    private var loadedApprovalIDs: Set<UUID> = []
    private var selectionRequestID: UUID?
    private var removedHistorySessionIDs: Set<UUID> = []
    private var deletingHistorySessionIDs: Set<UUID> = []
    private var historyReconciliationID: UUID?
    private var pendingSubmission: (text: String, key: String)?
    private var pendingNotificationPayload: NotificationPushPayload?
    private var smsInvocations = SmsInvocationQueue()
    private var smsInvocationDeviceID: UUID?

    public init(
        tokenStore: any TokenStore = KeychainTokenStore(),
        configurationStore: ConnectionConfigurationStore = ConnectionConfigurationStore(),
        historyStore: (any SessionHistoryStore)? = nil,
        artifactCache: ArtifactCache = ArtifactCache(),
        deviceRegistrationCoordinator: DeviceRegistrationCoordinator =
            DeviceRegistrationCoordinator(),
        runState: RunStateReducer? = nil,
        urlSession: URLSession? = nil
    ) {
        self.tokenStore = SessionTokenStore(durable: tokenStore)
        self.configurationStore = configurationStore
        self.historyStore = historyStore ?? SessionHistoryStoreFactory.makeDefault()
        self.artifactCache = artifactCache
        self.deviceRegistrationCoordinator = deviceRegistrationCoordinator
        self.runState = runState ?? RunStateReducer()
        self.urlSession = urlSession
        Task { await bootstrap() }
    }

    @discardableResult
    public func configure(baseURLString: String, token: String) async -> Bool {
        do {
            let configuration = try ConnectionConfiguration(baseURLString: baseURLString)
            let trimmedToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
            let previousToken = try await tokenStore.readToken()
            let credentialsChanged = !trimmedToken.isEmpty && trimmedToken != previousToken
            let previousConfiguration = await configurationStore.load()
            if credentialsChanged || previousConfiguration?.baseURL != configuration.baseURL {
                await abandonWebsiteAuthenticationCeremony()
            }
            if !trimmedToken.isEmpty {
                try await tokenStore.saveToken(trimmedToken)
            }
            guard try await tokenStore.readToken() != nil else {
                throw HTTPTransportError.missingToken
            }
            try await install(configuration)
            if previousConfiguration?.baseURL != configuration.baseURL {
                selectedBrowserProfileID = nil
                browserProfiles = []
                clearWebsiteAuthenticationState()
                await configurationStore.saveBrowserProfileID(nil)
            } else if credentialsChanged {
                browserProfiles = []
                clearWebsiteAuthenticationState()
                if selectedBrowserProfileID != nil, let api {
                    do {
                        try await reloadBrowserProfiles(using: api)
                    } catch {
                        selectedBrowserProfileID = nil
                        await configurationStore.saveBrowserProfileID(nil)
                        clearInstalledConnection()
                        throw error
                    }
                }
            }
            await configurationStore.save(configuration)
            requiresReauthentication = false
            errorMessage = nil
            return true
        } catch {
            present(error)
            return false
        }
    }

    public func forgetCredentials() async {
        await abandonWebsiteAuthenticationCeremony()
        var revokeError: Error?
        if let api {
            do {
                _ = try await deviceRegistrationCoordinator.revoke(using: api)
            } catch {
                revokeError = error
            }
        }
        selectionRequestID = nil
        historyReconciliationID = nil
        watchTasks.cancel()
        var tokenDeletionError: Error?
        do {
            try await tokenStore.deleteToken()
        } catch {
            tokenDeletionError = error
        }
        api = nil
        eventStream = nil
        browserProfiles = []
        selectedBrowserProfileID = nil
        clearWebsiteAuthenticationState()
        clearPendingSmsInvocations()
        await configurationStore.saveBrowserProfileID(nil)
        await artifactCache.removeAll()
        isConfigured = false
        requiresReauthentication = true
        if let revokeError {
            present(revokeError)
        } else if let tokenDeletionError {
            present(tokenDeletionError)
        }
    }

    public func registerRemoteNotifications(
        deviceToken: Data,
        descriptor: AppleDeviceDescriptor
    ) async {
        guard let api else { return }
        do {
            let outcome = try await deviceRegistrationCoordinator.register(
                deviceToken: deviceToken,
                descriptor: descriptor,
                using: api
            )
            notificationFeatureAvailable = outcome != .unsupported
            if case .registered(let deviceID) = outcome {
                registeredDeviceID = deviceID
            }
        } catch {
            present(error)
        }
    }

    /// Asks the operating system for the push token again so the delegate
    /// re-registers this device with whatever capabilities are now declared.
    /// The registration digest covers the capability set, so the repost is a
    /// real update rather than an idempotent replay.
    public func requestDeviceCapabilityRegistration() {
        pushRegistrar?.requestPushRegistration()
    }

    public func openNotification(_ payload: NotificationPushPayload) async {
        if payload.kind == .deviceInvocation {
            await openDeviceInvocation(payload)
            return
        }
        guard let link = NotificationDeepLinkReducer.reduce(payload) else { return }
        guard let api else {
            pendingNotificationPayload = payload
            return
        }
        do {
            let entry: SessionHistoryEntry
            if let existing = history.first(where: { $0.sessionID == link.sessionID }) {
                entry = existing
            } else {
                let session = try await api.getSession(link.sessionID)
                try await store(
                    session: session,
                    lastRunID: link.runID
                )
                guard let stored = history.first(where: { $0.sessionID == link.sessionID }) else {
                    throw HTTPTransportError.invalidResponse
                }
                entry = stored
            }
            await selectSession(entry, preferredRunID: link.runID)
            guard selectedSessionID == link.sessionID, runState.activeRunID == link.runID else {
                return
            }
            if case .approval(let approvalID) = link.focus {
                let approval = try await api.getApproval(approvalID)
                guard approval.sessionID == link.sessionID, approval.runID == link.runID else {
                    throw HTTPTransportError.invalidResponse
                }
                loadedApprovalIDs.insert(approval.id)
                runState.mergeApproval(approval)
            }
            notificationFocus = link.focus
            notificationNavigationID = UUID()
        } catch {
            present(error)
        }
    }

    /// The owner tapped a device-invocation push. The payload names only the
    /// invocation and the device, so the arguments come from the authenticated
    /// fetch rather than from the notification.
    private func openDeviceInvocation(_ payload: NotificationPushPayload) async {
        guard let deviceID = payload.deviceID else { return }
        guard api != nil else {
            pendingNotificationPayload = payload
            return
        }
        await loadSmsInvocations(deviceID: deviceID, reportingFailures: true)
    }

    /// Re-reads the pending queue when the app comes forward, so an invocation
    /// whose push was missed still reaches the owner before it expires.
    public func refreshPendingSmsInvocations() async {
        guard let deviceID = registeredDeviceID else { return }
        await loadSmsInvocations(deviceID: deviceID, reportingFailures: false)
    }

    private func loadSmsInvocations(deviceID: UUID, reportingFailures: Bool) async {
        guard let api else { return }
        let service = DeviceInvocationService(api: api, deviceID: deviceID)
        do {
            let invocations = try await service.nextSmsInvocations()
            smsInvocationDeviceID = deviceID
            smsInvocations.merge(invocations)
            pendingSmsInvocation = smsInvocations.presented
        } catch {
            // The recovery fetch is best-effort: an invocation this client
            // never sees expires server-side and surfaces as `tool.device_offline`
            // in the conversation, which beats an alert on every app launch.
            if reportingFailures { present(error) }
        }
    }

    /// Posts the owner's outcome for the presented invocation exactly once and
    /// brings forward whatever is waiting behind it.
    public func completeSmsInvocation(
        _ invocation: SmsInvocation,
        with result: DeviceInvocationResult
    ) async {
        guard smsInvocations.presented?.id == invocation.id else { return }
        let deviceID = smsInvocationDeviceID ?? registeredDeviceID
        smsInvocations.settle(invocation.id)
        pendingSmsInvocation = smsInvocations.presented
        guard let api, let deviceID else { return }
        await DeviceInvocationService(api: api, deviceID: deviceID)
            .complete(invocation, with: result)
    }

    /// Drops every invocation this client was holding without posting a
    /// result: the credential that authorized the fetch is gone, so the server
    /// expires whatever is outstanding.
    private func clearPendingSmsInvocations() {
        smsInvocations.removeAll()
        smsInvocationDeviceID = nil
        registeredDeviceID = nil
        pendingSmsInvocation = nil
    }

    public func acknowledgeNotificationNavigation() {
        notificationNavigationID = nil
    }

    public func reportNotificationRegistrationFailure(_ error: Error) {
        present(error)
    }

    public func newSession() {
        resetSelectedSession()
    }

    private func resetSelectedSession() {
        selectionRequestID = nil
        watchTasks.cancel()
        selectedSessionID = nil
        sessionBusy = false
        loadedApprovalIDs.removeAll()
        pendingSubmission = nil
        notificationFocus = nil
        notificationNavigationID = nil
        runState.reset()
    }

    public func selectSession(
        _ entry: SessionHistoryEntry,
        preferredRunID: UUID? = nil
    ) async {
        guard let api, !removedHistorySessionIDs.contains(entry.sessionID) else { return }
        let requestID = UUID()
        selectionRequestID = requestID
        watchTasks.cancel()
        selectedSessionID = entry.sessionID
        sessionBusy = false
        loadedApprovalIDs.removeAll()
        pendingSubmission = nil
        notificationFocus = nil
        notificationNavigationID = nil
        runState.reset()
        do {
            let session = try await api.getSession(entry.sessionID)
            guard selectionRequestID == requestID,
                !removedHistorySessionIDs.contains(entry.sessionID)
            else { return }
            try await store(
                session: session,
                lastRunID: session.activeRunID ?? session.lastRunID ?? entry.lastRunID
            )
            guard selectionRequestID == requestID else {
                if removedHistorySessionIDs.contains(entry.sessionID) {
                    try? await historyStore.delete(sessionID: entry.sessionID)
                    history =
                        (try? await historyStore.list())
                        ?? history.filter {
                            $0.sessionID != entry.sessionID
                        }
                }
                return
            }
            let messages = try await loadSessionMessages(
                api: api,
                sessionID: session.id,
                requestID: requestID
            )
            guard selectionRequestID == requestID else { return }
            runState.restore(messages: messages)
            if let runID = preferredRunID
                ?? session.activeRunID
                ?? session.lastRunID
                ?? entry.lastRunID
            {
                let run = try await api.getRun(runID)
                guard selectionRequestID == requestID else { return }
                guard run.sessionID == session.id else {
                    throw HTTPTransportError.invalidResponse
                }
                runState.seed(run: run)
                watch(runID: run.id, touchHistoryOnCompletion: run.status.isActive)
            }
        } catch {
            if selectionRequestID == requestID {
                present(error)
            }
        }
    }

    private func loadSessionMessages(
        api: VeetbotAPIClient,
        sessionID: UUID,
        requestID: UUID
    ) async throws -> [SessionMessageView] {
        var messages: [SessionMessageView] = []
        var cursor: String?
        var seenCursors: Set<String> = []
        repeat {
            let page = try await api.listSessionMessages(
                sessionID: sessionID,
                cursor: cursor
            )
            guard selectionRequestID == requestID else { return [] }
            messages.append(contentsOf: page.items)
            cursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
        } while cursor != nil
        return messages
    }

    public func deleteSessionEverywhere(_ entry: SessionHistoryEntry) async {
        guard let api else { return }
        historyReconciliationID = nil
        deletingHistorySessionIDs.insert(entry.sessionID)
        defer { deletingHistorySessionIDs.remove(entry.sessionID) }
        do {
            try await api.deleteSession(entry.sessionID)
            removedHistorySessionIDs.insert(entry.sessionID)
            historyReconciliationID = nil
            if selectedSessionID == entry.sessionID { newSession() }
            history.removeAll { $0.sessionID == entry.sessionID }
            try await historyStore.delete(sessionID: entry.sessionID)
            history = try await historyStore.list()
            await artifactCache.removeAll()
        } catch {
            present(error)
        }
    }

    public func synchronizeHistory() async {
        do {
            try await reconcileHistory()
        } catch let error as VeetbotAPIClientError {
            present(error)
        } catch let error as HTTPTransportError {
            if case .reauthenticationRequired = error {
                present(error)
            }
        } catch {
            // Background reconciliation is best effort; direct user actions
            // still surface their own failures.
        }
    }

    private func reconcileHistory() async throws {
        guard let api else { return }
        let reconciliationID = UUID()
        historyReconciliationID = reconciliationID
        defer {
            if historyReconciliationID == reconciliationID {
                historyReconciliationID = nil
            }
        }
        let locallyKnownAtStart = Set(history.map(\.sessionID))
        var cursor: String?
        var seenCursors: Set<String> = []
        var serverIDs: Set<UUID> = []
        repeat {
            let page = try await api.listSessions(cursor: cursor)
            guard historyReconciliationID == reconciliationID else { return }
            for session in page.items {
                serverIDs.insert(session.id)
                guard !removedHistorySessionIDs.contains(session.id),
                    !deletingHistorySessionIDs.contains(session.id)
                else { continue }
                let existing = history.first { $0.sessionID == session.id }
                let entry = Self.mergedHistoryEntry(
                    session: session,
                    existing: existing,
                    lastRunID: session.lastRunID,
                    suggestedTitle: nil,
                    touchedAt: session.updatedAt
                )
                try await historyStore.upsert(entry)
                guard historyReconciliationID == reconciliationID else {
                    await discardSuccessfullyDeletedHistory()
                    return
                }
            }
            cursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
        } while cursor != nil

        let missingResolutions = try await resolveMissingSessions(
            locallyKnownAtStart.subtracting(serverIDs),
            using: api
        )
        guard historyReconciliationID == reconciliationID else { return }
        var prunedHistory = false
        for resolution in missingResolutions {
            switch resolution {
            case .found(let session):
                guard !removedHistorySessionIDs.contains(session.id),
                    !deletingHistorySessionIDs.contains(session.id)
                else { continue }
                let existing = history.first { $0.sessionID == session.id }
                let entry = Self.mergedHistoryEntry(
                    session: session,
                    existing: existing,
                    lastRunID: session.lastRunID,
                    suggestedTitle: nil,
                    touchedAt: session.updatedAt
                )
                try await historyStore.upsert(entry)
                guard historyReconciliationID == reconciliationID else {
                    await discardSuccessfullyDeletedHistory()
                    return
                }
            case .deleted(let sessionID):
                try await historyStore.delete(sessionID: sessionID)
                guard historyReconciliationID == reconciliationID else { return }
                prunedHistory = true
                if selectedSessionID == sessionID {
                    resetSelectedSession()
                }
            }
        }
        let reconciledHistory = try await historyStore.list()
        guard historyReconciliationID == reconciliationID else { return }
        history = reconciledHistory
        if prunedHistory {
            await artifactCache.removeAll()
        }
    }

    private func discardSuccessfullyDeletedHistory() async {
        for sessionID in removedHistorySessionIDs {
            try? await historyStore.delete(sessionID: sessionID)
        }
    }

    @discardableResult
    public func send(_ rawText: String) async -> Bool {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let api else { return false }
        guard !isSending else { return false }

        if runState.isRunActive {
            if runState.runStatus == .waitingForUser,
                let prompt = runState.clarifyingQuestion
            {
                return await answerQuestion(prompt, answer: text)
            } else {
                sessionBusy = true
                errorMessage = "This session already has an active run."
                return false
            }
        }

        isSending = true
        sessionBusy = false
        defer { isSending = false }
        let idempotencyKey: String
        if let pendingSubmission, pendingSubmission.text == text {
            idempotencyKey = pendingSubmission.key
        } else {
            idempotencyKey = UUID().uuidString.lowercased()
            pendingSubmission = (text, idempotencyKey)
        }
        do {
            let session: SessionView
            if let selectedSessionID {
                session = try await api.getSession(selectedSessionID)
            } else {
                session = try await api.createSession(
                    browserProfileID: selectedBrowserProfileID
                )
                selectedSessionID = session.id
                try await store(session: session, lastRunID: nil, suggestedTitle: text)
            }
            let submit = try await api.submitMessage(
                sessionID: session.id,
                content: [.text(text)],
                idempotencyKey: idempotencyKey
            )
            runState.begin(runID: submit.runID, status: submit.status)
            do {
                try await store(
                    session: session,
                    lastRunID: submit.runID,
                    suggestedTitle: text,
                    touchedNow: true
                )
            } catch {
                // The server accepted the message; a local cache failure must
                // not invite a second submission with a new idempotency key.
                present(error)
            }
            watch(runID: submit.runID)
            pendingSubmission = nil
            return true
        } catch {
            if let apiError = apiError(from: error),
                apiError.code == .conflict,
                apiError.details.reason == "active_run_exists",
                let runID = apiError.details.runID
            {
                pendingSubmission = nil
                sessionBusy = true
                errorMessage = "This session is busy; attached to its active run."
                do {
                    let run = try await api.getRun(runID)
                    runState.seed(run: run)
                    watch(runID: runID, touchHistoryOnCompletion: run.status.isActive)
                } catch {
                    present(error)
                }
            } else {
                present(error)
            }
            return false
        }
    }

    @discardableResult
    public func answerQuestion(
        _ prompt: ClarifyingQuestionPrompt,
        answer rawAnswer: String
    ) async -> Bool {
        guard let api, runState.runStatus == .waitingForUser else { return false }
        let answer = rawAnswer.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !answer.isEmpty else { return false }
        isSending = true
        defer { isSending = false }
        do {
            let result = try await api.deliverInput(
                runID: prompt.runID,
                content: [.text(answer)],
                questionID: prompt.questionID
            )
            runState.begin(runID: result.runID, status: result.status)
            try await touchSelectedHistory(lastRunID: result.runID)
            // The existing SSE connection remains open while the run is suspended.
            return true
        } catch {
            present(error)
            return false
        }
    }

    public func cancelActiveRun() async {
        guard let api, let runID = runState.activeRunID, runState.isRunActive else { return }
        do {
            let run = try await api.cancelRun(runID)
            runState.seed(run: run)
        } catch {
            present(error)
        }
    }

    public func resolveApproval(
        _ approval: ApprovalView,
        decision: ApprovalDecision,
        reason: String? = nil
    ) async {
        guard let api else { return }
        do {
            let stored = try await api.resolveApproval(
                approval.id,
                decision: decision,
                reason: reason
            )
            runState.mergeApproval(stored)
        } catch {
            if let apiError = apiError(from: error),
                apiError.code == .conflict,
                apiError.details.reason == "approval_already_resolved"
            {
                do {
                    runState.mergeApproval(try await api.getApproval(approval.id))
                    errorMessage =
                        "This approval was already resolved; the first decision is shown."
                } catch {
                    present(error)
                }
            } else {
                present(error)
            }
        }
    }

    public func refreshPendingApprovals() async {
        guard let api else { return }
        do {
            var cursor: String?
            var seenCursors: Set<String> = []
            repeat {
                let page = try await api.listPendingApprovals(
                    runID: runState.activeRunID,
                    sessionID: selectedSessionID,
                    cursor: cursor
                )
                for approval in page.items {
                    loadedApprovalIDs.insert(approval.id)
                    runState.mergeApproval(approval)
                }
                cursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
            } while cursor != nil
        } catch {
            present(error)
        }
    }

    public func loadArtifact(_ artifactID: UUID) async throws -> LoadedArtifact {
        guard let api else { throw HTTPTransportError.notConfigured }
        let metadata = try await api.getArtifact(artifactID)
        let cached = await artifactCache.value(for: artifactID)
        let response = try await api.getArtifactContent(artifactID, etag: cached?.etag)
        switch response {
        case .content(let data, let etag):
            let value = CachedArtifactContent(data: data, etag: etag)
            await artifactCache.insert(value, for: artifactID)
            return LoadedArtifact(metadata: metadata, data: data)
        case .notModified:
            if let cached { return LoadedArtifact(metadata: metadata, data: cached.data) }
            let retry = try await api.getArtifactContent(artifactID)
            guard case .content(let data, let etag) = retry else {
                throw HTTPTransportError.invalidResponse
            }
            await artifactCache.insert(
                CachedArtifactContent(data: data, etag: etag),
                for: artifactID
            )
            return LoadedArtifact(metadata: metadata, data: data)
        }
    }

    public func clearError() { errorMessage = nil }

    public func clearCachedArtifacts() async {
        await artifactCache.removeAll()
    }

    public func refreshBrowserProfiles() async {
        guard let api else { return }
        isManagingWebsiteAccess = true
        defer { isManagingWebsiteAccess = false }
        do {
            try await reloadBrowserProfiles(using: api)
            errorMessage = nil
        } catch {
            present(error)
        }
    }

    @discardableResult
    public func createWebsiteAccess(
        origin: String,
        loginURL: String
    ) async -> URL? {
        guard let api else { return nil }
        let normalizedOrigin = origin.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalizedLoginURL = loginURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalizedOrigin.isEmpty, !normalizedLoginURL.isEmpty else {
            errorMessage = "Enter both the website origin and its login page."
            return nil
        }
        isManagingWebsiteAccess = true
        defer { isManagingWebsiteAccess = false }
        var createdProfileID: UUID?
        do {
            let profile = try await api.createBrowserProfile(
                allowedOrigins: [normalizedOrigin]
            )
            createdProfileID = profile.id
            let ceremony = try await api.beginBrowserAuthentication(
                profileID: profile.id,
                loginURL: normalizedLoginURL
            )
            browserAuthentication = ceremony
            websiteAuthenticationLaunchURL = ceremony.launchURL
            browserProfiles.removeAll { $0.id == profile.id }
            browserProfiles.append(profile)
            errorMessage = nil
            do {
                try await reloadBrowserProfiles(using: api)
            } catch {
                present(error)
            }
            return ceremony.launchURL
        } catch {
            let creationError = error
            if let createdProfileID {
                let cleanupError = await discardUnusedBrowserProfile(
                    using: api,
                    profileID: createdProfileID,
                    authenticationID: nil
                )
                browserProfiles.removeAll { $0.id == createdProfileID }
                if browserAuthentication?.profileID == createdProfileID {
                    clearWebsiteAuthenticationState()
                }
                if let cleanupError {
                    errorMessage =
                        "\(displayMessage(for: creationError)) The unused browser profile could not be fully removed: \(displayMessage(for: cleanupError))"
                } else {
                    present(creationError)
                }
            } else {
                present(creationError)
            }
            return nil
        }
    }

    /// The handoff succeeded, so the client stops offering the one-time launch
    /// capability. The ceremony itself stays live until the runtime reports it.
    public func websiteAuthenticationLaunchOpened() {
        websiteAuthenticationLaunchURL = nil
    }

    public func websiteAuthenticationLaunchFailed() async {
        await discardCurrentWebsiteAccess(
            successMessage:
                "Veetbot couldn’t open the secure login page. The unused login was removed; try again."
        )
    }

    public func cancelWebsiteAccessSetup() async {
        await discardCurrentWebsiteAccess(successMessage: nil)
    }

    public func refreshBrowserAuthentication() async {
        guard let api, let browserAuthentication else { return }
        isManagingWebsiteAccess = true
        defer { isManagingWebsiteAccess = false }
        do {
            let updated = try await api.getBrowserAuthentication(browserAuthentication.id)
            self.browserAuthentication = updated
            if updated.status.isTerminal {
                websiteAuthenticationLaunchURL = nil
            }
            try await reloadBrowserProfiles(using: api)
            if updated.status == .ready,
                browserProfiles.contains(where: {
                    $0.id == updated.profileID && $0.status == .ready
                })
            {
                selectedBrowserProfileID = updated.profileID
                await configurationStore.saveBrowserProfileID(updated.profileID)
            }
            errorMessage = nil
        } catch {
            present(error)
        }
    }

    public func selectBrowserProfile(_ profileID: UUID?) async {
        guard profileID == nil
            || browserProfiles.contains(where: { $0.id == profileID && $0.status == .ready })
        else { return }
        selectedBrowserProfileID = profileID
        await configurationStore.saveBrowserProfileID(profileID)
    }

    public func removeBrowserProfile(_ profileID: UUID) async {
        guard let api else { return }
        isManagingWebsiteAccess = true
        defer { isManagingWebsiteAccess = false }
        do {
            if let authentication = browserAuthentication,
                authentication.profileID == profileID,
                !authentication.status.isTerminal
            {
                _ = try await api.cancelBrowserAuthentication(authentication.id)
            }
            _ = try await api.revokeBrowserProfile(profileID)
            try await api.deleteBrowserProfile(profileID)
            if selectedBrowserProfileID == profileID {
                selectedBrowserProfileID = nil
                await configurationStore.saveBrowserProfileID(nil)
            }
            if browserAuthentication?.profileID == profileID {
                clearWebsiteAuthenticationState()
            }
            try await reloadBrowserProfiles(using: api)
            errorMessage = nil
        } catch {
            present(error)
        }
    }

    private func discardCurrentWebsiteAccess(successMessage: String?) async {
        guard let api, let authentication = browserAuthentication else {
            if let successMessage { errorMessage = successMessage }
            return
        }
        isManagingWebsiteAccess = true
        defer { isManagingWebsiteAccess = false }
        let profileID = authentication.profileID
        let cleanupError = await discardUnusedBrowserProfile(
            using: api,
            profileID: profileID,
            authenticationID: authentication.status.isTerminal ? nil : authentication.id
        )
        clearWebsiteAuthenticationState()
        browserProfiles.removeAll { $0.id == profileID }
        if selectedBrowserProfileID == profileID {
            selectedBrowserProfileID = nil
            await configurationStore.saveBrowserProfileID(nil)
        }
        do {
            try await reloadBrowserProfiles(using: api)
        } catch {
            if cleanupError == nil {
                present(error)
                return
            }
        }
        if let cleanupError {
            errorMessage =
                "The login setup could not be fully removed: \(displayMessage(for: cleanupError)). Refresh Website Access and use the trash button to try again."
        } else {
            errorMessage = successMessage
        }
    }

    /// Surrenders an in-flight website-login ceremony before the connection it
    /// was begun under is replaced or deleted. The launch capability stays live
    /// until the ceremony is cancelled or expires, so the cancellation is sent
    /// through the transport and credential that began it, before either
    /// changes. It is best effort: the old credential may already be rejected,
    /// and a failure here must not block the connection change. The client
    /// forgets the ceremony either way, so nothing can offer the capability.
    private func abandonWebsiteAuthenticationCeremony() async {
        let ceremony = browserAuthentication
        clearWebsiteAuthenticationState()
        guard let api, let ceremony, !ceremony.status.isTerminal else { return }
        _ = try? await api.cancelBrowserAuthentication(ceremony.id)
    }

    /// Forgets the in-flight ceremony and its one-time launch capability.
    private func clearWebsiteAuthenticationState() {
        browserAuthentication = nil
        websiteAuthenticationLaunchURL = nil
    }

    private func discardUnusedBrowserProfile(
        using api: VeetbotAPIClient,
        profileID: UUID,
        authenticationID: UUID?
    ) async -> Error? {
        var firstError: Error?
        if let authenticationID {
            do {
                _ = try await api.cancelBrowserAuthentication(authenticationID)
            } catch {
                firstError = error
            }
        }
        do {
            _ = try await api.revokeBrowserProfile(profileID)
        } catch {
            if firstError == nil { firstError = error }
        }
        do {
            try await api.deleteBrowserProfile(profileID)
            return nil
        } catch {
            if firstError == nil { firstError = error }
        }
        return firstError
    }

    private func reloadBrowserProfiles(using api: VeetbotAPIClient) async throws {
        var profiles: [BrowserProfileView] = []
        var cursor: String?
        var seen: Set<String> = []
        repeat {
            let page = try await api.listBrowserProfiles(cursor: cursor)
            profiles.append(contentsOf: page.items)
            cursor = try nextPageCursor(page.nextCursor, seen: &seen)
        } while cursor != nil
        browserProfiles = profiles
        if let selectedBrowserProfileID,
            !profiles.contains(where: {
                $0.id == selectedBrowserProfileID && $0.status == .ready
            })
        {
            self.selectedBrowserProfileID = nil
            await configurationStore.saveBrowserProfileID(nil)
        }
    }

    private func bootstrap() async {
        do {
            history = try await historyStore.list()
        } catch {
            present(error)
        }
        do {
            if let configuration = await configurationStore.load(),
                try await tokenStore.readToken() != nil
            {
                selectedBrowserProfileID = await configurationStore.loadBrowserProfileID()
                try await install(configuration)
                if selectedBrowserProfileID != nil, let api {
                    do {
                        try await reloadBrowserProfiles(using: api)
                    } catch {
                        selectedBrowserProfileID = nil
                        browserProfiles = []
                        clearWebsiteAuthenticationState()
                        await configurationStore.saveBrowserProfileID(nil)
                        clearInstalledConnection()
                        throw error
                    }
                }
            }
        } catch {
            present(error)
        }
    }

    private func install(_ configuration: ConnectionConfiguration) async throws {
        await artifactCache.removeAll()
        pendingSubmission = nil
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: tokenStore,
            session: urlSession
        )
        api = VeetbotAPIClient(transport: transport)
        eventStream = ReconnectingEventStream(reader: SSEReader(transport: transport))
        baseURL = configuration.baseURL
        do {
            try await reconcileHistory()
        } catch {
            clearInstalledConnection()
            throw error
        }
        isConfigured = true
        if let pendingNotificationPayload {
            self.pendingNotificationPayload = nil
            await openNotification(pendingNotificationPayload)
        }
    }

    private func clearInstalledConnection() {
        api = nil
        eventStream = nil
        baseURL = nil
        isConfigured = false
    }

    private func watch(runID: UUID, touchHistoryOnCompletion: Bool = true) {
        guard let eventStream else { return }
        watchTasks.cancel()
        watchTasks.task = Task { [weak self] in
            do {
                for try await frame in eventStream.frames(runID: runID) {
                    guard let self else { return }
                    self.runState.reduce(frame)
                    if frame.event == "approval.requested"
                        || frame.event == "run.waiting_for_approval"
                    {
                        await self.loadApprovalsRequiredByReducer()
                        await self.refreshPendingApprovals()
                    }
                }
                guard let self else { return }
                if touchHistoryOnCompletion {
                    await self.refreshHistoryAfterRun(runID)
                }
            } catch is CancellationError {
                return
            } catch {
                self?.present(error)
            }
        }
    }

    private func loadApprovalsRequiredByReducer() async {
        guard let api else { return }
        for approvalID in runState.needsApprovalIDs where !loadedApprovalIDs.contains(approvalID) {
            do {
                let approval = try await api.getApproval(approvalID)
                loadedApprovalIDs.insert(approvalID)
                runState.mergeApproval(approval)
            } catch {
                present(error)
            }
        }
    }

    private func refreshHistoryAfterRun(_ runID: UUID) async {
        do {
            try await updateSelectedHistory(lastRunID: runID)
        } catch {
            present(error)
        }
    }

    private func store(
        session: SessionView,
        lastRunID: UUID?,
        suggestedTitle: String? = nil,
        touchedNow: Bool = false
    ) async throws {
        let existing = history.first { $0.sessionID == session.id }
        let entry = Self.mergedHistoryEntry(
            session: session,
            existing: existing,
            lastRunID: lastRunID,
            suggestedTitle: suggestedTitle,
            touchedAt: touchedNow ? Date() : nil
        )
        try await historyStore.upsert(entry)
        history = try await historyStore.list()
    }

    static func mergedHistoryEntry(
        session: SessionView,
        existing: SessionHistoryEntry?,
        lastRunID: UUID?,
        suggestedTitle: String?,
        touchedAt: Date?
    ) -> SessionHistoryEntry {
        let title =
            session.title
            ?? existing?.title
            ?? suggestedTitle.map(Self.title(from:))
            ?? "New conversation"
        return SessionHistoryEntry(
            sessionID: session.id,
            title: title,
            agentID: session.agentID,
            createdAt: session.createdAt,
            updatedAt: touchedAt ?? existing?.updatedAt ?? session.updatedAt,
            lastRunID: lastRunID ?? session.lastRunID ?? existing?.lastRunID
        )
    }

    private func touchSelectedHistory(lastRunID: UUID) async throws {
        try await updateSelectedHistory(lastRunID: lastRunID)
    }

    private func updateSelectedHistory(lastRunID: UUID) async throws {
        guard let selectedSessionID,
            var entry = history.first(where: { $0.sessionID == selectedSessionID })
        else { return }
        entry.updatedAt = Date()
        entry.lastRunID = lastRunID
        try await historyStore.upsert(entry)
        history = try await historyStore.list()
    }

    private static func title(from text: String) -> String {
        let collapsed = text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        return String(collapsed.prefix(64))
    }

    private func apiError(from error: Error) -> APIError? {
        switch error {
        case HTTPTransportError.api(let value),
            HTTPTransportError.reauthenticationRequired(let value),
            HTTPTransportError.authorizationDenied(let value):
            return value
        default:
            return nil
        }
    }

    private func present(_ error: Error) {
        if case HTTPTransportError.reauthenticationRequired = error {
            requiresReauthentication = true
        }
        errorMessage = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }

    private func displayMessage(for error: Error) -> String {
        (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
    }
}

/// Main-actor callers own all task reads and writes; `deinit` may only cancel off-actor.
private final class WatchTaskBox: @unchecked Sendable {
    var task: Task<Void, Never>?

    func cancel() {
        task?.cancel()
        task = nil
    }

    deinit { task?.cancel() }
}
