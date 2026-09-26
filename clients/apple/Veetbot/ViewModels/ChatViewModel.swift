import Combine
import Foundation
import UserNotifications

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

/// One file in the composer: staged, uploading, uploaded, or failed (ADR-0120).
public struct ComposerAttachment: Identifiable, Equatable, Sendable {
    public enum State: Equatable, Sendable {
        case uploading(progress: Double)
        case uploaded(artifactID: UUID, mediaType: String)
        case failed(message: String)
    }

    public let id: UUID
    public let filename: String
    public let mediaType: String
    public internal(set) var state: State

    public var isImage: Bool { mediaType.hasPrefix("image/") }

    public var artifactID: UUID? {
        guard case .uploaded(let artifactID, _) = state else { return nil }
        return artifactID
    }

    /// The block a sent message carries; the server decides from the stored bytes
    /// which uploads are images, so an image block names only a server image.
    var contentBlock: ContentBlock? {
        guard case .uploaded(let artifactID, let storedType) = state else { return nil }
        if ComposerAttachment.imageMediaTypes.contains(storedType) {
            return .image(artifactID: artifactID, mediaType: storedType, detail: "auto")
        }
        return .file(artifactID: artifactID, mediaType: storedType, filename: filename)
    }

    static let imageMediaTypes: Set<String> = ["image/png", "image/jpeg", "image/gif", "image/webp"]
    public static let maximumPerMessage = 10
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
    @Published public var composerText = ""
    /// Files waiting to be sent with the next message (ADR-0120).
    @Published public private(set) var attachments: [ComposerAttachment] = []
    @Published public private(set) var connectionGeneration = UUID()
    @Published public private(set) var isReconfiguring = false
    public var currentAPIClient: VeetbotAPIClient? { api }
    public var callNotificationHandler: (() -> Void)?
    @Published public private(set) var callResult: CallResultViewData?
    public var emailNotificationHandler: ((UUID, UUID?) async -> Void)?
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
    /// The device sign-in window on screen, if any (ADR-0128). It carries no
    /// launch URL, capability or session value: those live only in the locals
    /// of `completeDeviceSignIn`.
    @Published public private(set) var deviceSignInRequest: DeviceSignInRequest?
    /// The `device.sms.send` invocation whose compose sheet the owner should
    /// see now, if any. Its recipient and body live only here and in the sheet.
    @Published public private(set) var pendingSmsInvocation: SmsInvocation?
    /// This installation's server-side device id, learned from its own
    /// registration. Nil until the push token has been registered this launch.
    @Published public private(set) var registeredDeviceID: UUID?
    /// The server's folders and open grouping proposals, refreshed with the
    /// history; memory only, because the server index is the authority.
    @Published public private(set) var folders: [FolderView] = []
    @Published public private(set) var folderProposals: [FolderProposalView] = []
    /// False until a server answers the folder list; a 404 or 405 keeps the
    /// flat history and hides every folder control without an error.
    @Published public private(set) var foldersAvailable = false
    /// The name sheet's inline error — create, rename, or accepting a proposal
    /// under a new name; never the global banner.
    @Published public private(set) var folderEditorError: String?
    @Published public private(set) var pendingFolderProposalIDs: Set<UUID> = []

    public var groupedHistory: GroupedConversationHistory {
        .make(history: history, folders: folders, available: foldersAvailable)
    }

    public var suggestedFolders: [FolderProposalPresentation] {
        FolderProposalPresentation.make(proposals: folderProposals, folders: folders, history: history)
    }

    /// Set by the application delegate when it attaches.
    public weak var pushRegistrar: (any PushRegistrationRequesting)?

    public let runState: RunStateReducer

    private let tokenStore: any TokenStore
    private let configurationStore: ConnectionConfigurationStore
    private let historyStore: any SessionHistoryStore
    private let artifactCache: ArtifactCache
    private let deviceRegistrationCoordinator: DeviceRegistrationCoordinator
    private let urlSession: URLSession?
    private let deviceHandoffClient: DeviceSignInHandoffClient
    private let deviceSignInTiming: DeviceSignInTiming
    /// The profile a device sign-in window created and that has not become
    /// ready, so closing the window can remove it.
    private var deviceSignInCreatedProfile: (requestID: UUID, profileID: UUID)?
    private var api: VeetbotAPIClient?
    private var eventStream: ReconnectingEventStream?
    private let watchTasks = WatchTaskBox()
    private var loadedApprovalIDs: Set<UUID> = []
    private var selectionRequestID: UUID?
    private var callResultRequestID: UUID?
    private var removedHistorySessionIDs: Set<UUID> = []
    private var deletingHistorySessionIDs: Set<UUID> = []
    private var historyReconciliationID: UUID?
    private var pendingSubmission: (signature: String, key: String)?
    private var stagedAttachments: [UUID: StagedAttachment] = [:]
    private var attachmentUploads: [UUID: Task<Void, Never>] = [:]
    /// Changes whenever the conversation does, so a late upload result is dropped.
    private var attachmentGeneration = UUID()
    private var sessionCreation: Task<SessionView, Error>?
    private var pendingNotificationPayload: NotificationPushPayload?
    private var smsInvocations = SmsInvocationQueue()
    private var smsInvocationDeviceID: UUID?
    /// Held rather than rebuilt per call: the service remembers the results it
    /// still owes the server, and a fresh instance would forget them.
    private var smsService: DeviceInvocationService?
    private var smsServiceBaseURL: URL?

    public init(
        tokenStore: any TokenStore = KeychainTokenStore(),
        configurationStore: ConnectionConfigurationStore = ConnectionConfigurationStore(),
        historyStore: (any SessionHistoryStore)? = nil,
        artifactCache: ArtifactCache = ArtifactCache(),
        deviceRegistrationCoordinator: DeviceRegistrationCoordinator =
            DeviceRegistrationCoordinator(),
        runState: RunStateReducer? = nil,
        urlSession: URLSession? = nil,
        deviceHandoffClient: DeviceSignInHandoffClient? = nil,
        deviceSignInTiming: DeviceSignInTiming = .standard
    ) {
        self.tokenStore = SessionTokenStore(durable: tokenStore)
        self.configurationStore = configurationStore
        self.historyStore = historyStore ?? SessionHistoryStoreFactory.makeDefault()
        self.artifactCache = artifactCache
        self.deviceRegistrationCoordinator = deviceRegistrationCoordinator
        self.runState = runState ?? RunStateReducer()
        self.urlSession = urlSession
        self.deviceHandoffClient = deviceHandoffClient ?? DeviceSignInHandoffClient()
        self.deviceSignInTiming = deviceSignInTiming
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
        dismissCallResult()
        resetFolderState()
        isConfigured = false
        connectionGeneration = UUID()
        composerText = ""
        clearAttachments()
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

    public func dismissCallResult() {
        callResultRequestID = nil
        callResult = nil
    }

    public func deleteCallResult() async {
        guard let api, let selected = callResult else { return }
        let generation = connectionGeneration
        do {
            let erased = try await api.deleteCallResult(selected.callID)
            guard generation == connectionGeneration, callResult?.callID == selected.callID else { return }
            callResult = erased
            resetSelectedSession()
        } catch {
            guard generation == connectionGeneration else { return }
            present(error)
        }
    }

    public func openNotification(_ payload: NotificationPushPayload) async {
        if payload.kind == .callFinished {
            guard let callID = payload.callID else { return }
            guard let api else { pendingNotificationPayload = payload; return }
            let generation = connectionGeneration
            let requestID = UUID()
            callResultRequestID = requestID
            do {
                let result = try await api.callResult(callID)
                guard generation == connectionGeneration, callResultRequestID == requestID else { return }
                callNotificationHandler?()
                callResult = result
            } catch {
                guard generation == connectionGeneration, callResultRequestID == requestID else { return }
                present(error)
            }
            return
        }
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
            if let emailNotificationHandler {
                let session = try await api.getSession(link.sessionID)
                if let value = session.metadata["email_thread_id"]?.stringValue,
                    let threadID = UUID(uuidString: value) {
                    let approvalID: UUID?
                    if case .approval(let id) = link.focus { approvalID = id } else { approvalID = nil }
                    await emailNotificationHandler(threadID, approvalID)
                    return
                }
            }
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

    /// One service per (connection, device), kept alive so the results it owes
    /// the server survive between a failed post and the next recovery fetch.
    private func smsInvocationService(for deviceID: UUID) -> DeviceInvocationService? {
        guard let api else { return nil }
        if let smsService, smsInvocationDeviceID == deviceID, smsServiceBaseURL == baseURL {
            return smsService
        }
        let service = DeviceInvocationService(api: api, deviceID: deviceID)
        smsService = service
        smsInvocationDeviceID = deviceID
        smsServiceBaseURL = baseURL
        return service
    }

    private func loadSmsInvocations(deviceID: UUID, reportingFailures: Bool) async {
        guard let service = smsInvocationService(for: deviceID) else { return }
        do {
            let invocations = try await service.nextSmsInvocations()
            smsInvocations.merge(invocations)
            pendingSmsInvocation = smsInvocations.presented
        } catch {
            // The recovery fetch is best-effort: an invocation this client
            // never sees expires server-side and surfaces as `tool.device_offline`
            // in the conversation, which beats an alert on every app launch.
            if reportingFailures { present(error) }
        }
    }

    /// Posts the owner's outcome for the invocation that was on screen and
    /// brings forward whatever is waiting behind it. An outcome for anything
    /// other than the presented invocation — a sheet dismissal that arrives
    /// after the head advanced — settles nothing and posts nothing.
    public func completeSmsInvocation(
        _ invocation: SmsInvocation,
        with result: DeviceInvocationResult
    ) async {
        guard smsInvocations.settle(invocation.id) else { return }
        pendingSmsInvocation = smsInvocations.presented
        guard
            let deviceID = smsInvocationDeviceID ?? registeredDeviceID,
            let service = smsInvocationService(for: deviceID)
        else { return }
        await service.complete(invocation, with: result)
    }

    /// Drops every invocation this client was holding without posting a
    /// result: the credential that authorized the fetch is gone, so the server
    /// expires whatever is outstanding.
    private func clearPendingSmsInvocations() {
        smsInvocations.removeAll()
        smsInvocationDeviceID = nil
        smsService = nil
        smsServiceBaseURL = nil
        registeredDeviceID = nil
        pendingSmsInvocation = nil
    }

    public func acknowledgeNotificationNavigation() {
        notificationNavigationID = nil
    }

    public func reportNotificationRegistrationFailure(_ error: Error) {
        let systemError = error as NSError
        // A saved denial is a preference, not a failed conversation. In
        // particular, macOS may return this error when permission is requested
        // again; leave any unrelated application error intact.
        if systemError.domain == UNErrorDomain,
            systemError.code == UNError.notificationsNotAllowed.rawValue
        {
            return
        }
        present(error)
    }

    public func newSession() {
        resetSelectedSession()
    }

    // MARK: - Conversation folders (Milestone 29)

    /// Create a folder and, when asked, file one conversation in it. A refused
    /// or duplicate name lands in `folderEditorError` for the sheet.
    @discardableResult
    public func createFolder(named name: String, moving sessionID: UUID? = nil) async -> FolderView? {
        guard let api else { return nil }
        let generation = connectionGeneration
        folderEditorError = nil
        do {
            let folder = try await api.createFolder(
                name: name.trimmingCharacters(in: .whitespacesAndNewlines)
            )
            guard generation == connectionGeneration else { return nil }
            upsertFolder(folder)
            if let sessionID {
                await moveSession(sessionID, toFolder: folder.id)
            }
            return folder
        } catch {
            guard generation == connectionGeneration else { return nil }
            folderEditorError = Self.folderErrorMessage(error)
            return nil
        }
    }

    @discardableResult
    public func renameFolder(_ folderID: UUID, to name: String) async -> Bool {
        guard let api else { return false }
        let generation = connectionGeneration
        folderEditorError = nil
        do {
            let folder = try await api.renameFolder(
                folderID, name: name.trimmingCharacters(in: .whitespacesAndNewlines)
            )
            guard generation == connectionGeneration else { return false }
            upsertFolder(folder)
            return true
        } catch {
            guard generation == connectionGeneration else { return false }
            folderEditorError = Self.folderErrorMessage(error)
            return false
        }
    }

    /// Delete a folder; its conversations return to History and nothing is deleted.
    public func deleteFolder(_ folderID: UUID) async {
        guard let api else { return }
        let generation = connectionGeneration
        historyReconciliationID = nil
        do {
            try await api.deleteFolder(folderID)
            guard generation == connectionGeneration else { return }
            folders.removeAll { $0.id == folderID }
            folderProposals.removeAll { $0.targetFolderID == folderID }
            let members = history.filter { $0.folderID == folderID }.map(\.sessionID)
            try await applyFolderMembership(sessionIDs: members, folderID: nil)
        } catch {
            guard generation == connectionGeneration else { return }
            present(error)
        }
    }

    /// File a conversation in a folder, or unfile it with an explicit null.
    public func moveSession(_ sessionID: UUID, toFolder folderID: UUID?) async {
        guard let api else { return }
        let generation = connectionGeneration
        historyReconciliationID = nil
        do {
            let session = try await api.setSessionFolder(sessionID, folderID: folderID)
            guard generation == connectionGeneration else { return }
            let existing = history.first { $0.sessionID == sessionID }
            try await store(session: session, lastRunID: session.lastRunID ?? existing?.lastRunID)
            folderProposals.removeAll { $0.memberSessionIDs.contains(sessionID) }
        } catch {
            guard generation == connectionGeneration else { return }
            present(error)
        }
    }

    /// Accept a proposal, under a name the owner typed when one is given. From
    /// the name sheet, a taken or refused name — or any other failure — stays in
    /// `folderEditorError` with the proposal still open; a proposal resolved
    /// elsewhere is a refresh either way. True once the proposal is no longer
    /// open here, which is when the sheet may close.
    @discardableResult
    public func acceptFolderProposal(_ proposalID: UUID, name: String? = nil) async -> Bool {
        guard let api else { return false }
        guard let proposal = folderProposals.first(where: { $0.id == proposalID }) else { return true }
        let generation = connectionGeneration
        let override = name?.trimmingCharacters(in: .whitespacesAndNewlines)
        if override != nil { folderEditorError = nil }
        pendingFolderProposalIDs.insert(proposalID)
        defer { pendingFolderProposalIDs.remove(proposalID) }
        historyReconciliationID = nil
        do {
            let resolved = try await api.acceptFolderProposal(proposalID, name: override)
            guard generation == connectionGeneration else { return false }
            folderProposals.removeAll { $0.id == proposalID }
            if let folderID = resolved.resultingFolderID {
                try await applyFolderMembership(
                    sessionIDs: proposal.memberSessionIDs, folderID: folderID
                )
            }
            try? await reconcileHistory()
            return true
        } catch {
            guard generation == connectionGeneration else { return false }
            if override != nil, !Self.isResolvedElsewhere(error) {
                folderEditorError = Self.folderErrorMessage(error)
                return false
            }
            await handleProposalResolutionFailure(error, proposalID: proposalID)
            return !folderProposals.contains { $0.id == proposalID }
        }
    }

    public func declineFolderProposal(_ proposalID: UUID) async {
        guard let api else { return }
        let generation = connectionGeneration
        pendingFolderProposalIDs.insert(proposalID)
        defer { pendingFolderProposalIDs.remove(proposalID) }
        do {
            _ = try await api.declineFolderProposal(proposalID)
            guard generation == connectionGeneration else { return }
            folderProposals.removeAll { $0.id == proposalID }
            try? await reconcileHistory()
        } catch {
            guard generation == connectionGeneration else { return }
            await handleProposalResolutionFailure(error, proposalID: proposalID)
        }
    }

    public func clearFolderEditorError() { folderEditorError = nil }

    /// A proposal resolved elsewhere is a refresh, not an error.
    private func handleProposalResolutionFailure(_ error: Error, proposalID: UUID) async {
        if Self.isResolvedElsewhere(error) {
            folderProposals.removeAll { $0.id == proposalID }
            try? await reconcileHistory()
            return
        }
        present(error)
    }

    /// A 404, or a 409 other than a taken name: the proposal was resolved or
    /// withdrawn elsewhere. A taken name leaves it open for another name.
    private static func isResolvedElsewhere(_ error: Error) -> Bool {
        guard case HTTPTransportError.api(let apiError) = error else { return false }
        switch apiError.statusCode {
        case 404: return true
        case 409: return apiError.details.reason != "folder_name_taken"
        default: return false
        }
    }

    private func upsertFolder(_ folder: FolderView) {
        var next = folders.filter { $0.id != folder.id }
        next.append(folder)
        folders = next.sorted(by: GroupedConversationHistory.folderOrder)
    }

    private func applyFolderMembership(sessionIDs: [UUID], folderID: UUID?) async throws {
        for sessionID in sessionIDs {
            guard var entry = history.first(where: { $0.sessionID == sessionID }) else { continue }
            entry.folderID = folderID
            try await historyStore.upsert(entry)
        }
        history = try await historyStore.list()
    }

    /// Never throws: the folder surface is optional, and its absence or a
    /// transient failure must not turn history reconciliation into a banner.
    private func reconcileFolders(
        using api: VeetbotAPIClient, reconciliationID: UUID, supported: Bool
    ) async {
        guard supported else {
            // An older server, or an empty index: nothing to ask for.
            publishFolderSurface(folders: [], proposals: [], available: false)
            return
        }
        do {
            var loaded: [FolderView] = []
            var cursor: String?
            var seenCursors: Set<String> = []
            repeat {
                let page = try await api.listFolders(cursor: cursor)
                guard historyReconciliationID == reconciliationID else { return }
                loaded.append(contentsOf: page.items)
                cursor = try nextPageCursor(page.nextCursor, seen: &seenCursors)
            } while cursor != nil
            let proposals = try await api.listFolderProposals().items
            guard historyReconciliationID == reconciliationID else { return }
            publishFolderSurface(
                folders: loaded.sorted(by: GroupedConversationHistory.folderOrder),
                proposals: proposals,
                available: true
            )
        } catch let error as VeetbotAPIClientError {
            guard case .foldersUnavailable = error,
                historyReconciliationID == reconciliationID
            else { return }
            publishFolderSurface(folders: [], proposals: [], available: false)
        } catch {
            // Any other failure keeps the previous folder state.
        }
    }

    /// The sidebar sync runs every 30 seconds. Publishing unchanged values would
    /// redraw the sidebar and the open conversation each time, so only changes
    /// are published. The folders are in place before the surface appears and
    /// are cleared only after it has gone.
    private func publishFolderSurface(
        folders newFolders: [FolderView], proposals: [FolderProposalView], available: Bool
    ) {
        if !available, foldersAvailable { foldersAvailable = false }
        if folders != newFolders { folders = newFolders }
        if folderProposals != proposals { folderProposals = proposals }
        if available, !foldersAvailable { foldersAvailable = true }
    }

    private func resetFolderState() {
        folders = []
        folderProposals = []
        foldersAvailable = false
        folderEditorError = nil
        pendingFolderProposalIDs = []
    }

    private static func folderErrorMessage(_ error: Error) -> String {
        if case HTTPTransportError.api(let apiError) = error {
            return apiError.message
        }
        return error.localizedDescription
    }

    public func reportConnectionError(_ error: Error) { present(error) }

    public func openSharedSession(_ id: UUID) async {
        guard let api else { return }
        do {
            let session = try await api.getSession(id)
            try await store(session: session, lastRunID: session.activeRunID ?? session.lastRunID)
            if let entry = history.first(where: { $0.sessionID == id }) { await selectSession(entry) }
        } catch { present(error) }
    }

    private func resetSelectedSession() {
        selectionRequestID = nil
        watchTasks.cancel()
        selectedSessionID = nil
        sessionBusy = false
        loadedApprovalIDs.removeAll()
        pendingSubmission = nil
        clearAttachments()
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
        if selectedSessionID != entry.sessionID { clearAttachments() }
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
        var serverSpeaksFolders = false
        repeat {
            let page = try await api.listSessions(cursor: cursor)
            guard historyReconciliationID == reconciliationID else { return }
            for session in page.items {
                // A cached People session left unlisted is pruned below.
                guard !session.isPeopleOperational else { continue }
                serverIDs.insert(session.id)
                serverSpeaksFolders = serverSpeaksFolders || session.folderSupported
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
            case .found(let session) where session.isPeopleOperational:
                // The session backs People data, so it is hidden locally, never deleted.
                try await historyStore.delete(sessionID: session.id)
                guard historyReconciliationID == reconciliationID else { return }
                if selectedSessionID == session.id {
                    resetSelectedSession()
                }
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
        if history != reconciledHistory {
            history = reconciledHistory
        }
        if prunedHistory {
            await artifactCache.removeAll()
        }
        await reconcileFolders(
            using: api, reconciliationID: reconciliationID, supported: serverSpeaksFolders
        )
    }

    private func discardSuccessfullyDeletedHistory() async {
        for sessionID in removedHistorySessionIDs {
            try? await historyStore.delete(sessionID: sessionID)
        }
    }

    @discardableResult
    public func send(_ rawText: String) async -> Bool {
        let text = rawText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty || !attachments.isEmpty, let api else { return false }
        guard !isSending else { return false }

        if runState.isRunActive {
            if runState.runStatus == .waitingForUser,
                let prompt = runState.clarifyingQuestion
            {
                // An answer is text; staged attachments wait for the next message.
                guard !text.isEmpty else { return false }
                return await answerQuestion(prompt, answer: text)
            } else {
                sessionBusy = true
                errorMessage = "This session already has an active run."
                return false
            }
        }

        guard attachmentsReady else {
            errorMessage = "Wait for the attachments to finish uploading, or remove them."
            return false
        }
        isSending = true
        sessionBusy = false
        defer { isSending = false }
        let sentAttachments = attachments
        let content: [ContentBlock] =
            sentAttachments.compactMap(\.contentBlock) + (text.isEmpty ? [] : [.text(text)])
        let suggestedTitle = text.isEmpty ? (sentAttachments.first?.filename ?? "") : text
        // The key covers the attachments too: the same words with other files
        // are a different message, never a replay of the first.
        let signature = ([text] + sentAttachments.compactMap { $0.artifactID?.uuidString })
            .joined(separator: "\u{1F}")
        let idempotencyKey: String
        if let pendingSubmission, pendingSubmission.signature == signature {
            idempotencyKey = pendingSubmission.key
        } else {
            idempotencyKey = UUID().uuidString.lowercased()
            pendingSubmission = (signature, idempotencyKey)
        }
        runState.beginPendingUserMessage(
            content: content,
            submissionID: idempotencyKey
        )
        do {
            let session: SessionView
            if let selectedSessionID {
                session = try await api.getSession(selectedSessionID)
            } else {
                session = try await createSelectedSession(suggestedTitle: suggestedTitle)
            }
            let submit = try await api.submitMessage(
                sessionID: session.id,
                content: content,
                idempotencyKey: idempotencyKey
            )
            runState.begin(runID: submit.runID, status: submit.status)
            removeSentAttachments(sentAttachments)
            do {
                try await store(
                    session: session,
                    lastRunID: submit.runID,
                    suggestedTitle: suggestedTitle,
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
            runState.discardPendingUserMessage(submissionID: idempotencyKey)
            // The owner moved to another conversation while this one was created.
            if error is CancellationError { return false }
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
            } else if let apiError = apiError(from: error),
                apiError.statusCode == 404,
                apiError.message == "artifact not found"
            {
                // An upload that expired or was removed cannot be sent; the owner
                // attaches the file again.
                pendingSubmission = nil
                markAttachmentsFailed(
                    sentAttachments,
                    message: "This file is no longer available. Remove it and attach it again."
                )
                present(error)
            } else {
                present(error)
            }
            return false
        }
    }

    // MARK: - Attachments (ADR-0120)

    /// Every staged file has finished uploading; a message may carry them now.
    public var attachmentsReady: Bool {
        attachments.allSatisfy { $0.artifactID != nil }
    }

    /// Stage files picked or dropped by URL and start uploading each.
    public func attach(fileURLs: [URL]) async {
        for url in fileURLs {
            guard reserveAttachmentSlot() else { return }
            do {
                let staged = try await Task.detached(priority: .userInitiated) {
                    try AttachmentStaging.stage(fileURL: url)
                }.value
                enqueue(staged)
            } catch {
                present(error)
            }
        }
    }

    /// Stage what a drag or the photo picker carries and start uploading each.
    public func attach(itemProviders: [NSItemProvider]) async {
        for provider in itemProviders {
            guard reserveAttachmentSlot() else { return }
            do {
                enqueue(try await AttachmentStaging.stage(itemProvider: provider))
            } catch {
                present(error)
            }
        }
    }

    public func removeAttachment(_ id: UUID) {
        attachmentUploads.removeValue(forKey: id)?.cancel()
        stagedAttachments.removeValue(forKey: id)
        attachments.removeAll { $0.id == id }
    }

    public func retryAttachment(_ id: UUID) {
        guard stagedAttachments[id] != nil,
            let index = attachments.firstIndex(where: { $0.id == id })
        else { return }
        attachments[index].state = .uploading(progress: 0)
        startUpload(id)
    }

    private func reserveAttachmentSlot() -> Bool {
        guard attachments.count < ComposerAttachment.maximumPerMessage else {
            errorMessage =
                "A message can carry at most \(ComposerAttachment.maximumPerMessage) attachments."
            return false
        }
        return true
    }

    private func enqueue(_ staged: StagedAttachment) {
        guard reserveAttachmentSlot() else { return }
        let id = UUID()
        stagedAttachments[id] = staged
        attachments.append(
            ComposerAttachment(
                id: id,
                filename: staged.filename,
                mediaType: staged.mediaType,
                state: .uploading(progress: 0)
            )
        )
        startUpload(id)
    }

    private func startUpload(_ id: UUID) {
        guard let api, let staged = stagedAttachments[id] else { return }
        let generation = attachmentGeneration
        let reportProgress: @Sendable (Double) -> Void = { [weak self] fraction in
            Task { @MainActor in
                self?.updateUploadProgress(id, fraction, generation: generation)
            }
        }
        attachmentUploads[id]?.cancel()
        attachmentUploads[id] = Task { [weak self] in
            guard let self else { return }
            do {
                let sessionID = try await self.uploadSessionID(suggestedTitle: staged.filename)
                guard self.attachmentGeneration == generation else { return }
                let artifact = try await api.uploadArtifact(
                    sessionID: sessionID,
                    data: staged.data,
                    filename: staged.filename,
                    mediaType: staged.mediaType,
                    // One key per staged file: a retry replays the stored upload.
                    idempotencyKey: id.uuidString.lowercased(),
                    progress: reportProgress
                )
                guard self.attachmentGeneration == generation else { return }
                self.setAttachmentState(
                    id,
                    .uploaded(artifactID: artifact.id, mediaType: artifact.mediaType)
                )
            } catch is CancellationError {
                return
            } catch {
                guard self.attachmentGeneration == generation else { return }
                self.setAttachmentState(id, .failed(message: Self.uploadFailureMessage(error)))
            }
            self.attachmentUploads[id] = nil
        }
    }

    private func updateUploadProgress(_ id: UUID, _ fraction: Double, generation: UUID) {
        guard attachmentGeneration == generation,
            let index = attachments.firstIndex(where: { $0.id == id }),
            case .uploading = attachments[index].state
        else { return }
        attachments[index].state = .uploading(progress: fraction)
    }

    private func setAttachmentState(_ id: UUID, _ state: ComposerAttachment.State) {
        guard let index = attachments.firstIndex(where: { $0.id == id }) else { return }
        attachments[index].state = state
    }

    private func removeSentAttachments(_ sent: [ComposerAttachment]) {
        for attachment in sent {
            stagedAttachments.removeValue(forKey: attachment.id)
            attachmentUploads.removeValue(forKey: attachment.id)?.cancel()
        }
        let sentIDs = Set(sent.map(\.id))
        attachments.removeAll { sentIDs.contains($0.id) }
    }

    private func markAttachmentsFailed(_ failed: [ComposerAttachment], message: String) {
        for attachment in failed {
            stagedAttachments.removeValue(forKey: attachment.id)
            setAttachmentState(attachment.id, .failed(message: message))
        }
    }

    private func clearAttachments() {
        attachmentGeneration = UUID()
        for task in attachmentUploads.values { task.cancel() }
        attachmentUploads.removeAll()
        stagedAttachments.removeAll()
        attachments.removeAll()
        sessionCreation?.cancel()
        sessionCreation = nil
    }

    /// The conversation an upload belongs to, creating it at most once for
    /// several files dropped together.
    private func uploadSessionID(suggestedTitle: String) async throws -> UUID {
        if let selectedSessionID { return selectedSessionID }
        return try await createSelectedSession(suggestedTitle: suggestedTitle).id
    }

    private func createSelectedSession(suggestedTitle: String) async throws -> SessionView {
        if let sessionCreation { return try await sessionCreation.value }
        guard let api else { throw HTTPTransportError.notConfigured }
        let generation = attachmentGeneration
        let browserProfileID = selectedBrowserProfileID
        let creation = Task { try await api.createSession(browserProfileID: browserProfileID) }
        sessionCreation = creation
        let session: SessionView
        do {
            session = try await creation.value
        } catch {
            if sessionCreation == creation { sessionCreation = nil }
            throw error
        }
        guard attachmentGeneration == generation else { throw CancellationError() }
        if sessionCreation == creation {
            sessionCreation = nil
            selectedSessionID = session.id
            try await store(session: session, lastRunID: nil, suggestedTitle: suggestedTitle)
        }
        return session
    }

    private static func uploadFailureMessage(_ error: Error) -> String {
        if let attachmentError = error as? VeetbotAPIClientError {
            return attachmentError.localizedDescription
        }
        if case HTTPTransportError.api(let apiError) = error, apiError.statusCode == 413 {
            return "This file is larger than the server accepts."
        }
        return (error as? LocalizedError)?.errorDescription ?? "The upload failed."
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
            return
        }
        await reconcileOpenCeremonies(using: api)
    }

    /// A sign-in the owner finished without checking its status would stay
    /// unknown: for each profile that is not ready, read the status of its
    /// newest open, unexpired ceremony, which records a ready outcome (D16).
    /// Best effort and silent; there is no timer.
    private func reconcileOpenCeremonies(using api: VeetbotAPIClient) async {
        let generation = connectionGeneration
        let now = Date()
        var becameReady = false
        for profile in browserProfiles where profile.status != .ready && profile.status != .revoked {
            guard
                let ceremonies = try? await api.listBrowserAuthentications(profileID: profile.id),
                let open = ceremonies
                    .filter({ !$0.status.isTerminal && $0.expiresAt > now })
                    .max(by: { $0.expiresAt < $1.expiresAt }),
                let refreshed = try? await api.getBrowserAuthentication(open.id),
                generation == connectionGeneration
            else { continue }
            if browserAuthentication?.id == refreshed.id {
                browserAuthentication = refreshed
                if refreshed.status.isTerminal { websiteAuthenticationLaunchURL = nil }
            }
            becameReady = becameReady || refreshed.status == .ready
        }
        if becameReady, generation == connectionGeneration {
            try? await reloadBrowserProfiles(using: api)
        }
    }

    @discardableResult
    public func createWebsiteAccess(
        websiteURL: String,
        additionalOrigins: String = ""
    ) async -> URL? {
        guard let api else { return nil }
        guard let target = Self.websiteLoginTarget(websiteURL) else {
            errorMessage = "Enter a valid HTTPS website URL without a username or password."
            return nil
        }
        let normalizedLoginURL = target.loginURL
        let primaryOrigin = target.origin
        let extraOrigins = additionalOrigins
            .components(separatedBy: CharacterSet(charactersIn: ",\n\r"))
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
        var normalizedOrigins = [primaryOrigin]
        for origin in extraOrigins where !normalizedOrigins.contains(origin) {
            normalizedOrigins.append(origin)
        }
        isManagingWebsiteAccess = true
        defer { isManagingWebsiteAccess = false }
        var createdProfileID: UUID?
        do {
            let profile = try await api.createBrowserProfile(
                allowedOrigins: normalizedOrigins
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

    // MARK: - Device sign-in (ADR-0128)

    /// Opens the sign-in window for a new website; same URL rules as the
    /// remote path. The window first loads the address as typed.
    @discardableResult
    public func beginDeviceSignIn(websiteURL: String) -> DeviceSignInRequest? {
        guard api != nil else { return nil }
        guard let target = Self.websiteLoginTarget(websiteURL),
            let startURL = URL(string: target.loginURL)
        else {
            errorMessage = "Enter a valid HTTPS website URL without a username or password."
            return nil
        }
        let request = DeviceSignInRequest(
            startURL: startURL,
            allowedOrigins: [target.origin],
            profileID: nil,
            adoptableOrigin: DeviceSignInNavigationPolicy.adoptableOrigin(for: target.origin)
        )
        openDeviceSignIn(request)
        return request
    }

    /// Opens the sign-in window to sign in again to an existing, unrevoked
    /// profile, at the root of its first origin.
    @discardableResult
    public func beginDeviceSignIn(profile: BrowserProfileView) -> DeviceSignInRequest? {
        guard api != nil, profile.status != .revoked,
            let origin = profile.allowedOrigins.first,
            let startURL = URL(string: origin + "/")
        else { return nil }
        let request = DeviceSignInRequest(
            startURL: startURL,
            allowedOrigins: profile.allowedOrigins,
            profileID: profile.id,
            adoptableOrigin: nil
        )
        openDeviceSignIn(request)
        return request
    }

    /// The owner tapped I'm signed in: create the profile if it is new, begin
    /// a device ceremony, hand the session over once and learn the outcome
    /// (0128-design §5.2). The launch URL and its capability exist only in
    /// this function's locals.
    public func completeDeviceSignIn(
        _ request: DeviceSignInRequest,
        handoff: DeviceSessionHandoff,
        adoptedOrigin: String?
    ) async -> DeviceSignInResult {
        let offersRemote = request.profileID == nil
        // The remote browser is the alternative when this device cannot start
        // or carry the sign-in of a new website (B5).
        func failure(_ message: String, canRetry: Bool = true) -> DeviceSignInResult {
            let remoteMayHelp =
                message == DeviceSignInMessage.couldNotStart
                || message == DeviceSignInMessage.tooMuchData
            return .failed(
                DeviceSignInFailure(
                    message: message,
                    canRetry: canRetry,
                    offersRemoteBrowser: offersRemote && remoteMayHelp
                )
            )
        }
        guard let api else { return failure(DeviceSignInMessage.couldNotStart) }
        let generation = connectionGeneration
        let origins = request.profileID == nil
            ? (adoptedOrigin.map { [$0] } ?? request.allowedOrigins)
            : request.allowedOrigins
        let body: Data
        do {
            body = try WebsiteSessionScope(allowedOrigins: origins).encode(handoff)
        } catch WebsiteSessionScopeError.tooLarge {
            return failure(DeviceSignInMessage.tooMuchData, canRetry: false)
        } catch {
            return failure(DeviceSignInMessage.couldNotStart)
        }
        guard let confirmed = URL(string: handoff.confirmedURL),
            let confirmedOrigin = WebsiteSessionScope.origin(of: confirmed)
        else { return failure(DeviceSignInMessage.couldNotStart) }

        isManagingWebsiteAccess = true
        defer { isManagingWebsiteAccess = false }

        let profileID: UUID
        if let existing = request.profileID {
            profileID = existing
        } else if let created = deviceSignInCreatedProfile, created.requestID == request.id {
            profileID = created.profileID
        } else {
            do {
                let profile = try await api.createBrowserProfile(allowedOrigins: origins)
                guard generation == connectionGeneration else {
                    return failure(DeviceSignInMessage.couldNotStart)
                }
                deviceSignInCreatedProfile = (request.id, profile.id)
                profileID = profile.id
            } catch {
                return failure(DeviceSignInMessage.couldNotStart)
            }
        }

        // D19: the begin names the root of the confirmed page's origin, which
        // is allowed by construction and keeps the page's path and query off
        // the API.
        let ceremony: BrowserAuthenticationView
        switch await beginDeviceCeremony(
            using: api, profileID: profileID, loginURL: confirmedOrigin + "/"
        ) {
        case .success(let begun):
            ceremony = begun
        case .failure(let error):
            return failure(error.message)
        }
        guard let target = DeviceSignInHandoffTarget(launchURL: ceremony.launchURL) else {
            _ = try? await api.cancelBrowserAuthentication(ceremony.id)
            return failure(DeviceSignInMessage.couldNotStart)
        }

        let status: BrowserAuthenticationStatus?
        switch await deviceHandoffClient.send(body, to: target) {
        case .sealed, .transportFailed:
            status = await deviceCeremonyStatus(using: api, id: ceremony.id, polling: true)
        case .capabilityRejected:
            status = await deviceCeremonyStatus(using: api, id: ceremony.id, polling: false)
        case .tooLarge:
            _ = try? await api.cancelBrowserAuthentication(ceremony.id)
            return failure(DeviceSignInMessage.tooMuchData, canRetry: false)
        case .rejected(let code) where code == "internal_error" || code.hasPrefix("http_5"):
            status = await deviceCeremonyStatus(using: api, id: ceremony.id, polling: false)
        case .rejected(let code):
            _ = try? await api.cancelBrowserAuthentication(ceremony.id)
            if code == "tool.browser.profile_unavailable" {
                try? await reloadBrowserProfiles(using: api)
                return failure(DeviceSignInMessage.profileUnavailable, canRetry: false)
            }
            return failure(Self.deviceSignInMessage(for: code))
        }

        guard status == .ready else {
            if !(status?.isTerminal ?? false) {
                _ = try? await api.cancelBrowserAuthentication(ceremony.id)
            }
            return failure(DeviceSignInMessage.couldNotConfirm)
        }
        guard generation == connectionGeneration else {
            return failure(DeviceSignInMessage.couldNotConfirm)
        }
        if deviceSignInCreatedProfile?.profileID == profileID {
            deviceSignInCreatedProfile = nil
        }
        try? await reloadBrowserProfiles(using: api)
        if browserProfiles.contains(where: { $0.id == profileID && $0.status == .ready }) {
            selectedBrowserProfileID = profileID
            await configurationStore.saveBrowserProfileID(profileID)
        }
        errorMessage = nil
        return .signedIn(profileID: profileID)
    }

    /// The window closed after a successful sign-in.
    public func finishDeviceSignIn() {
        deviceSignInRequest = nil
        deviceSignInCreatedProfile = nil
    }

    /// The window closed without a sign-in: a profile it created and that never
    /// became ready is removed.
    public func abandonDeviceSignIn() async {
        let created = deviceSignInCreatedProfile?.profileID
        deviceSignInRequest = nil
        deviceSignInCreatedProfile = nil
        guard let api, let created else { return }
        isManagingWebsiteAccess = true
        defer { isManagingWebsiteAccess = false }
        let cleanupError = await discardUnusedBrowserProfile(
            using: api, profileID: created, authenticationID: nil
        )
        browserProfiles.removeAll { $0.id == created }
        try? await reloadBrowserProfiles(using: api)
        if let cleanupError {
            errorMessage =
                "The unused website login could not be fully removed: \(displayMessage(for: cleanupError)). Refresh Website Access and use the trash button to try again."
        }
    }

    /// The owner chose the remote browser from the sign-in window of a new
    /// website: the device attempt is abandoned and today's remote path begins.
    @discardableResult
    public func switchDeviceSignInToRemoteBrowser(_ request: DeviceSignInRequest) async -> URL? {
        await abandonDeviceSignIn()
        guard request.profileID == nil else { return nil }
        return await createWebsiteAccess(websiteURL: request.startURL.absoluteString)
    }

    /// The app became active or Website Access appeared: re-read an open remote
    /// ceremony, whose outcome the owner may have finished elsewhere (D16).
    public func refreshOpenRemoteAuthentication() async {
        guard let browserAuthentication, !browserAuthentication.status.isTerminal else { return }
        await refreshBrowserAuthentication()
    }

    private func openDeviceSignIn(_ request: DeviceSignInRequest) {
        deviceSignInCreatedProfile = nil
        deviceSignInRequest = request
        errorMessage = nil
    }

    /// Begin, recovering once from a lost answer or an open ceremony by
    /// cancelling the newest open one (D20, 0128-design §2.5 item 9).
    private func beginDeviceCeremony(
        using api: VeetbotAPIClient,
        profileID: UUID,
        loginURL: String
    ) async -> Result<BrowserAuthenticationView, DeviceSignInError> {
        func begin() async throws -> BrowserAuthenticationView {
            try await api.beginBrowserAuthentication(
                profileID: profileID, loginURL: loginURL, mode: .device
            )
        }
        let conflict: Bool
        do {
            return .success(try await begin())
        } catch HTTPTransportError.connection {
            conflict = false
        } catch {
            guard apiError(from: error)?.statusCode == 409 else {
                return .failure(DeviceSignInError(DeviceSignInMessage.couldNotStart))
            }
            conflict = true
        }
        let ceremonies: [BrowserAuthenticationView]
        do {
            ceremonies = try await api.listBrowserAuthentications(profileID: profileID)
        } catch {
            return .failure(DeviceSignInError(DeviceSignInMessage.couldNotStart))
        }
        let now = Date()
        let open = ceremonies
            .filter {
                ($0.status == .authenticationRequired || $0.status == .needsUser) && $0.expiresAt > now
            }
            .max { $0.expiresAt < $1.expiresAt }
        if let open {
            do {
                _ = try await api.cancelBrowserAuthentication(open.id)
            } catch {
                return .failure(DeviceSignInError(DeviceSignInMessage.couldNotStart))
            }
        } else if conflict {
            return .failure(DeviceSignInError(DeviceSignInMessage.websiteInUse))
        }
        do {
            return .success(try await begin())
        } catch {
            return .failure(DeviceSignInError(DeviceSignInMessage.couldNotStart))
        }
    }

    /// The ceremony's status after a handoff whose answer is missing or says
    /// nothing final: polled every interval up to the limit, or read once.
    /// Nil when it never answered.
    private func deviceCeremonyStatus(
        using api: VeetbotAPIClient,
        id: UUID,
        polling: Bool
    ) async -> BrowserAuthenticationStatus? {
        let deadline = Date().addingTimeInterval(deviceSignInTiming.pollLimit)
        var last: BrowserAuthenticationStatus?
        while true {
            if let view = try? await api.getBrowserAuthentication(id) {
                last = view.status
                if view.status.isTerminal { return view.status }
            }
            guard polling, Date() < deadline, !Task.isCancelled else { return last }
            try? await Task.sleep(nanoseconds: UInt64(deviceSignInTiming.pollInterval * 1_000_000_000))
        }
    }

    private static func deviceSignInMessage(for code: String) -> String {
        switch code {
        case "session_empty": return DeviceSignInMessage.sessionEmpty
        case "session_signed_out": return DeviceSignInMessage.sessionSignedOut
        case "session_unconfirmed": return DeviceSignInMessage.sessionUnconfirmed
        case "tool.browser.provider_unavailable": return DeviceSignInMessage.couldNotCheck
        default: return DeviceSignInMessage.couldNotFinish
        }
    }

    /// The login URL and primary origin a typed website names: HTTPS is
    /// added when omitted; credentials and ports other than 443 are refused.
    static func websiteLoginTarget(_ websiteURL: String) -> (loginURL: String, origin: String)? {
        let input = websiteURL.trimmingCharacters(in: .whitespacesAndNewlines)
        let hasScheme = input.range(of: "^[A-Za-z][A-Za-z0-9+.-]*://", options: .regularExpression) != nil
        let candidate = hasScheme ? input : "https://" + input
        guard
            var components = URLComponents(string: candidate),
            components.scheme?.lowercased() == "https",
            let host = components.host, !host.isEmpty,
            components.user == nil, components.password == nil,
            components.port == nil || components.port == 443
        else { return nil }
        components.scheme = "https"
        components.host = host.lowercased()
        components.port = nil
        guard let loginURL = components.url?.absoluteString else { return nil }
        components.path = ""
        components.query = nil
        components.fragment = nil
        guard let origin = components.url?.absoluteString else { return nil }
        return (loginURL, origin)
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
        let createdByDeviceSignIn = deviceSignInCreatedProfile?.profileID
        clearWebsiteAuthenticationState()
        deviceSignInRequest = nil
        deviceSignInCreatedProfile = nil
        guard let api else { return }
        if let createdByDeviceSignIn {
            _ = await discardUnusedBrowserProfile(
                using: api, profileID: createdByDeviceSignIn, authenticationID: nil
            )
        }
        guard let ceremony, !ceremony.status.isTerminal else { return }
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
        dismissCallResult()
        isReconfiguring = true
        defer { isReconfiguring = false }
        connectionGeneration = UUID()
        await artifactCache.removeAll()
        pendingSubmission = nil
        clearAttachments()
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
        dismissCallResult()
        resetFolderState()
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
            lastRunID: lastRunID ?? session.lastRunID ?? existing?.lastRunID,
            // The server index is the authority: its value wins, nil included.
            folderID: session.folderID,
            scheduleID: session.scheduleID
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

/// A device sign-in failure message, carried through begin recovery.
private struct DeviceSignInError: Error {
    let message: String

    init(_ message: String) { self.message = message }
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
