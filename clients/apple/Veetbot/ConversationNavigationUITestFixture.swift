#if DEBUG
import Foundation
#if os(macOS)
import AppKit
#endif

enum ConversationNavigationUITestFixture {
    static let launchArgument = "--ui-testing-conversation-navigation"
    static let artifactID = "00000000-0000-0000-0000-000000000A01"
    static let artifactSVG = #"<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="green"/></svg>"#
    static let firstSessionID = "00000000-0000-0000-0000-000000000123"
    static let secondSessionID = "00000000-0000-0000-0000-000000000456"
    static let memoryID = "00000000-0000-0000-0000-000000000321"
    static let scheduleID = "00000000-0000-0000-0000-000000000654"
    static let scheduleHistoryID = "00000000-0000-0000-0000-000000000656"
    /// Milestone 29 folders: served only under this argument so every other
    /// journey keeps an older-server index without a `folder_id` key.
    static let foldersLaunchArgument = "--ui-testing-folders"
    static let folderID = "00000000-0000-0000-0000-000000000F01"
    static let proposedFolderID = "00000000-0000-0000-0000-000000000F02"
    static let workFolderID = "00000000-0000-0000-0000-000000000F03"
    static let proposalID = "00000000-0000-0000-0000-000000000E01"
    /// The pass's next proposal, served once the first has been declined.
    static let followUpProposalID = "00000000-0000-0000-0000-000000000E02"
    /// Folder journeys only: a conversation filed in Work and three more that
    /// join the first in the Lisbon proposal, so it names four as a real one would.
    static let planningSessionID = "00000000-0000-0000-0000-0000000004A1"
    static let flightsSessionID = "00000000-0000-0000-0000-0000000004A2"
    static let hotelSessionID = "00000000-0000-0000-0000-0000000004A3"
    static let sintraSessionID = "00000000-0000-0000-0000-0000000004A4"
    static let proposalMemberIDs = [firstSessionID, flightsSessionID, hotelSessionID, sintraSessionID]
    /// ADR-0129: the first conversation is bound to a website profile, and
    /// its run waits on a `browser.act` approval that offers a task permission.
    static let taskGrantLaunchArgument = "--ui-testing-browser-task-grant"
    /// Milestone 31: the work account advertises unsubscribe support and the
    /// census serves four senders. Served only under this argument, so every
    /// other journey keeps an account that offers no Subscriptions entry.
    static let subscriptionsLaunchArgument = "--ui-testing-email-subscriptions"
    /// Each unsubscribe settles as a request the sender did not accept.
    static let subscriptionsFailureLaunchArgument = "--ui-testing-email-subscriptions-failure"
    /// Adds a bulk conversation whose projection carries a subscription block.
    static let bulkThreadLaunchArgument = "--ui-testing-email-bulk-thread"
    /// ADR-0154: one ready x.com sign-in, and a schedule update that binds the
    /// daily review only when the client echoes its definition correctly.
    static let scheduleWebsiteAccessLaunchArgument = "--ui-testing-schedule-website-access"
    /// ADR-0143: a scheduled report this device has not read. Notification
    /// sync reports it unread until a sync acknowledges it as seen.
    static let unreadReportLaunchArgument = "--ui-testing-unread-report"
    static let reportSessionID = "00000000-0000-0000-0000-0000000005A1"
    /// The schedule's earlier occurrence, already read, so the two form a group.
    static let previousReportSessionID = "00000000-0000-0000-0000-0000000005A3"
    static let newsSubscriptionID = "00000000-0000-0000-0000-000000000B01"
    static let dealsSubscriptionID = "00000000-0000-0000-0000-000000000B02"
    static let clubSubscriptionID = "00000000-0000-0000-0000-000000000B03"
    static let promoSubscriptionID = "00000000-0000-0000-0000-000000000B04"
    static let bulkThreadID = "00000000-0000-0000-0000-000000000897"
    /// ADR-0165: the work account advertises spam support and serves a
    /// conversation the server flagged as suspected spam.
    static let suspectedSpamLaunchArgument = "--ui-testing-email-suspected-spam"
    static let spamThreadID = "00000000-0000-0000-0000-000000000898"

    static func makeAppearanceIfRequested() -> AppearancePreferences? {
        guard ProcessInfo.processInfo.arguments.contains(launchArgument),
            let rawSize = ProcessInfo.processInfo.environment["VEETBOT_UI_TEST_TEXT_SIZE"],
            let size = AppTextSize(rawValue: rawSize)
        else { return nil }
        let suiteName = "com.veetbot.apple.ui-tests.appearance"
        guard let defaults = UserDefaults(suiteName: suiteName) else { return nil }
        defaults.removePersistentDomain(forName: suiteName)
        let preferences = AppearancePreferences(defaults: defaults)
        preferences.textSize = size
        return preferences
    }

    /// Folder expansion starts from the default on every launch, in a suite of
    /// its own so a journey never inherits another's, or the owner's, folders.
    @MainActor
    static func makeFolderSidebarIfRequested() -> FolderSidebarPreferences? {
        guard ProcessInfo.processInfo.arguments.contains(launchArgument) else { return nil }
        let suiteName = "com.veetbot.apple.ui-tests.folders"
        guard let defaults = UserDefaults(suiteName: suiteName) else { return nil }
        defaults.removePersistentDomain(forName: suiteName)
        return FolderSidebarPreferences(defaults: defaults)
    }

    @MainActor
    static func makeModelIfRequested() -> ChatViewModel? {
        guard ProcessInfo.processInfo.arguments.contains(launchArgument) else { return nil }
        ConversationNavigationUITestURLProtocol.resetEmail()
        ConversationNavigationUITestURLProtocol.resetFolders()
        ConversationNavigationUITestURLProtocol.resetModelSettings()
        ConversationNavigationUITestURLProtocol.resetTaskGrant()
        ConversationNavigationUITestURLProtocol.resetSubscriptions()
        ConversationNavigationUITestURLProtocol.resetMemory()
        ConversationNavigationUITestURLProtocol.resetReport()
        #if os(macOS)
        if ProcessInfo.processInfo.arguments.contains("--ui-testing-mixed-tools") {
            let availableAfter = Date().addingTimeInterval(
                ProcessInfo.processInfo.arguments.contains("--ui-testing-delayed-window") ? 2 : 0
            )
            Task { @MainActor in
                for _ in 0..<40 {
                    if Date() >= availableAfter,
                        let window = NSApp.windows.first(where: { $0.canBecomeMain })
                    {
                        window.setFrame(
                            NSRect(x: 100, y: 100, width: 1000, height: 700), display: true
                        )
                        return
                    }
                    try? await Task.sleep(nanoseconds: 100_000_000)
                }
            }
        }
        #endif

        let suiteName = "com.veetbot.apple.ui-tests"
        guard let defaults = UserDefaults(suiteName: suiteName) else { return nil }
        defaults.removePersistentDomain(forName: suiteName)
        defaults.set(
            "https://ui-testing.veetbot.invalid",
            forKey: "veetbot.connection.baseURL"
        )

        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ConversationNavigationUITestURLProtocol.self]
        return ChatViewModel(
            tokenStore: InMemoryTokenStore(token: "ui-test-token"),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: URLSession(configuration: configuration)
        )
    }

    @MainActor
    static func makeMemoryAPIClientIfRequested() -> VeetbotAPIClient? {
        guard ProcessInfo.processInfo.arguments.contains(launchArgument) else { return nil }
        guard
            let configuration = try? ConnectionConfiguration(
                baseURLString: "https://ui-testing.veetbot.invalid"
            )
        else { return nil }

        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.protocolClasses = [ConversationNavigationUITestURLProtocol.self]
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: "ui-test-token"),
            session: URLSession(configuration: sessionConfiguration)
        )
        return VeetbotAPIClient(transport: transport)
    }

    @MainActor
    static func makeScheduleAPIClientIfRequested() -> VeetbotAPIClient? {
        makeMemoryAPIClientIfRequested()
    }
}

private final class ConversationNavigationUITestURLProtocol: URLProtocol {
    private var pendingResponse: DispatchWorkItem?
    private static let emailLock = NSLock()
    private static var emailBody = "Thanks, Alex. I'll review the agenda."
    private static var emailRevision = 1
    private static var emailStatus = "ready"
    private static var learningPaused = false
    private static var emailHandled = false
    private static var emailArchiveTarget: Bool?
    private static var emailArchiveReads = 0
    /// Keeps pending visible across XCTest's initial accessibility poll before confirming the fake Gmail result.
    private static let emailArchiveCompletionRead = 4
    private static var emailArchived = false
    private static let folderLock = NSLock()
    private static var folderName = "Travel"
    private static var folderDeleted = false
    private static var createdFolderName: String?
    private static var proposalResolved = false
    private static var proposalDeclined = false
    private static var sessionFolders: [String: String] = [:]
    private static var foldersEnabled: Bool {
        ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.foldersLaunchArgument)
    }
    /// What a server without the folder routes answers for every one of them.
    private static let foldersUnavailableJSON =
        #"{"error":{"code":"not_found","message":"The requested resource was not found.","details":{},"request_id":"ui-test"}}"#
    /// Starts each folder journey with Travel holding the second conversation,
    /// Work holding the planning one, and one open new-folder proposal over the
    /// first conversation and three more.
    static func resetFolders() {
        folderLock.lock()
        defer { folderLock.unlock() }
        folderName = "Travel"
        folderDeleted = false
        createdFolderName = nil
        proposalResolved = false
        proposalDeclined = false
        sessionFolders = foldersEnabled
            ? [
                ConversationNavigationUITestFixture.secondSessionID: ConversationNavigationUITestFixture.folderID,
                ConversationNavigationUITestFixture.planningSessionID: ConversationNavigationUITestFixture.workFolderID,
            ]
            : [:]
    }
    private static let modelSettingsLock = NSLock()
    private static var modelSettingsVersion = 0
    private static var chatModelChoice: (policy: String, effort: String?) = ("astra", "high")
    private static var memoryModelChoice: (policy: String, effort: String?) = ("balanced", nil)
    private static let chatModelOffers: [(policy: String, name: String, efforts: [String])] = [
        ("astra", "GPT-6 Astra", ["low", "medium", "high", "xhigh", "max"]),
        ("fable", "Claude Fable 5.1", ["low", "medium", "high", "xhigh", "max"]),
        ("balanced", "GPT-5.6 Sol", ["low", "medium", "high", "xhigh", "max"]),
    ]
    private static let memoryModelOffers: [(policy: String, name: String, effort: String?)] = [
        ("balanced", "GPT-5.6 Sol", nil),
        ("astra", "GPT-6 Astra", "medium"),
    ]
    /// Starts each native UI test from the never-saved deployment defaults.
    static func resetModelSettings() {
        modelSettingsLock.withLock {
            modelSettingsVersion = 0
            chatModelChoice = ("astra", "high")
            memoryModelChoice = ("balanced", nil)
        }
    }
    private static let taskGrantLock = NSLock()
    private static var taskScopeRevision = 0
    private static var taskScopes: [[String: String]] = []
    private static var taskGrantResolved = false
    private static var taskGrantRevoked = false
    private static var taskGrantJourney: Bool {
        ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.taskGrantLaunchArgument)
    }
    private static var genericCheckpoint: Bool {
        ProcessInfo.processInfo.arguments.contains("--ui-testing-generic-checkpoint")
    }
    private static let scheduleWebsiteLock = NSLock()
    private static var scheduleWebsiteBound = false
    private static var scheduleWebsiteJourney: Bool {
        ProcessInfo.processInfo.arguments.contains(
            ConversationNavigationUITestFixture.scheduleWebsiteAccessLaunchArgument)
    }
    /// The definition fields the server accepts on a full update, by name.
    private static let scheduleDefinitionFields: Set<String> = [
        "title", "instruction", "agent_id", "agent_version", "policy_profile",
        "requested_scopes", "browser_profile_id", "limits", "run_timeout_seconds",
        "cadence", "misfire_grace_seconds", "max_consecutive_failures",
    ]
    /// Starts the task-grant journey with the approval pending and no grant.
    static func resetTaskGrant() {
        taskGrantLock.withLock {
            taskScopeRevision = 0
            taskScopes = []
            taskGrantResolved = ProcessInfo.processInfo.arguments.contains("--ui-testing-approved-checkpoint")
            taskGrantRevoked = false
        }
    }
    /// One census sender. The unverified one has no mechanism sentence yet,
    /// and the protected one is a correspondent the owner writes to.
    private struct SubscriptionSender {
        let id: String
        let name: String
        let address: String
        let mechanism: String
        let destination: String
        let threads: Int
        let verified: Bool
        let protectedReason: String?
        var digest: String? { verified ? "digest-\(id.suffix(3))" : nil }
    }
    /// Volume first and protected senders last, as the server orders the census.
    private static let subscriptionSenders = [
        SubscriptionSender(
            id: ConversationNavigationUITestFixture.newsSubscriptionID, name: "Daily Brief",
            address: "news@daily.example.test", mechanism: "one_click", destination: "daily.example.test",
            threads: 14, verified: true, protectedReason: nil),
        SubscriptionSender(
            id: ConversationNavigationUITestFixture.dealsSubscriptionID, name: "Shop Deals",
            address: "deals@shop.example.test", mechanism: "mailto", destination: "unsubscribe@shop.example.test",
            threads: 9, verified: true, protectedReason: nil),
        SubscriptionSender(
            id: ConversationNavigationUITestFixture.promoSubscriptionID, name: "Promo Weekly",
            address: "promo@weekly.example.test", mechanism: "one_click", destination: "weekly.example.test",
            threads: 3, verified: false, protectedReason: nil),
        SubscriptionSender(
            id: ConversationNavigationUITestFixture.clubSubscriptionID, name: "Running Club",
            address: "club@run.example.test", mechanism: "one_click", destination: "run.example.test",
            threads: 4, verified: true, protectedReason: "correspondent"),
    ]
    private static let subscriptionLock = NSLock()
    /// Each sender's durable state, revision and latest operation, keyed by id.
    private static var subscriptionRecords: [String: (state: String, revision: Int, operation: String?)] = [:]
    /// Operations admitted and not yet read back: the action and its senders.
    private static var subscriptionOperations: [String: (action: String, targets: [String])] = [:]
    private static var subscriptionsEnabled: Bool {
        ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.subscriptionsLaunchArgument)
    }
    private static var bulkThreadEnabled: Bool {
        ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.bulkThreadLaunchArgument)
    }
    private static var suspectedSpamEnabled: Bool {
        ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.suspectedSpamLaunchArgument)
    }
    private static let spamOperationID = "00000000-0000-0000-0000-000000000C01"
    /// Flag clearing, a requested report or restore, its reads, and Spam membership.
    private static var spamCleared = false
    private static var spamTarget: Bool?
    private static var spamReads = 0
    private static var spamInSpam = false
    /// Starts each journey with every sender active and no operation admitted.
    static func resetSubscriptions() {
        subscriptionLock.withLock {
            subscriptionRecords = Dictionary(uniqueKeysWithValues: subscriptionSenders.map {
                ($0.id, (state: "active", revision: 1, operation: nil))
            })
            subscriptionOperations = [:]
        }
    }
    private static let reportLock = NSLock()
    /// Set once a notification sync names the report among its seen runs.
    private static var reportSeen = false
    private static var unreadReportEnabled: Bool {
        ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.unreadReportLaunchArgument)
    }
    static func resetReport() { reportLock.withLock { reportSeen = false } }
    private static let memoryLock = NSLock()
    /// Set by a governed delete, after which the browser's reads omit the belief.
    private static var memoryDeleted = false
    private static var synthesisReviewed = false
    private static var synthesisDeleted = false
    private static var synthesisUndone = false
    static func resetMemory() { memoryLock.withLock {
        memoryDeleted = false; synthesisReviewed = false; synthesisDeleted = false; synthesisUndone = false
    } }
    /// Starts each native UI test with independent draft, learning and attention state.
    static func resetEmail() {
        emailLock.lock()
        defer { emailLock.unlock() }
        emailBody = "Thanks, Alex. I'll review the agenda."
        emailRevision = 1
        emailStatus = "ready"
        learningPaused = false
        emailHandled = false
        emailArchiveTarget = nil
        emailArchiveReads = 0
        emailArchived = false
        spamCleared = false
        spamTarget = nil
        spamReads = 0
        spamInSpam = false
    }
    override static func canInit(with request: URLRequest) -> Bool { true }

    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    /// Serves deterministic native-test routes, retaining handled state across list and detail reads.
    override func startLoading() {
        guard let url = request.url else {
            client?.urlProtocol(self, didFailWithError: URLError(.badURL))
            return
        }

        let body: String
        let statusCode: Int
        var holdOpen = false
        switch (request.httpMethod, url.path) {
        case (_, let path) where path.hasPrefix("/v1/memory-reconsolidations") || path.hasPrefix("/v1/memories/00000000-0000-0000-0000-00000000320"):
            let result = Self.synthesisResponse(request)
            statusCode = result.0
            body = result.1
        case ("GET", "/v1/memories/\(ConversationNavigationUITestFixture.memoryID)"):
            statusCode = 200
            body = Self.memoryJSON
        case ("GET", "/v1/artifacts/\(ConversationNavigationUITestFixture.artifactID)"):
            statusCode = 200
            body = """
                {"id":"\(ConversationNavigationUITestFixture.artifactID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","run_id":"\(Self.runID)","name":"garden.svg","media_type":"image/svg+xml","sha256":"fixture","size_bytes":\(ConversationNavigationUITestFixture.artifactSVG.utf8.count),"metadata":{},"created_at":"2026-09-27T00:00:00Z"}
                """
        case ("GET", "/v1/artifacts/\(ConversationNavigationUITestFixture.artifactID)/content"):
            statusCode = 200
            body = ConversationNavigationUITestFixture.artifactSVG
        case ("GET", "/v1/people") where ProcessInfo.processInfo.arguments.contains(Self.peopleDirectoryArgument):
            statusCode = 200
            let secondPage = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
                .contains(URLQueryItem(name: "cursor", value: "page-2")) == true
            let people = [Self.personJSON, Self.secondPersonJSON] + (3...72).map(Self.directoryPersonJSON)
            let page = secondPage ? people[50...] : people[..<50]
            body = "{\"items\":[\(page.joined(separator: ","))],\"next_cursor\":\(secondPage ? "null" : "\"page-2\"")}"
        case ("GET", let path) where path.hasPrefix("/v1/people/00000000-0000-0000-0000-0000000C"):
            statusCode = 200
            let number = Int(path.suffix(4)) ?? 0
            body = """
            {"person":\(Self.directoryPersonJSON(number)),"aliases":[{"id":"00000000-0000-0000-0000-0000000D\(String(format: "%04d", number))","revision":1,"value":"contact\(number)@example.com","identifier_kind":"email","verification":"channel_observed","context":"","valid_to":null,"support_ids":[]}],"relationships":[],"history":[],"commitments":[],"facts":[],"fact_revisions":{},"related_labels":{},"truncated":false,"coverage":"Email analyzed: recent 90 days."}
            """
        case ("GET", "/v1/people"):
            statusCode = 200
            body = "{\"items\":[\(Self.personJSON),\(Self.secondPersonJSON)],\"next_cursor\":null}"
        case ("GET", "/v1/people/00000000-0000-0000-0000-000000000782"):
            statusCode = 200
            body = Self.secondProfileJSON
        case ("GET", "/v1/people/imports"):
            statusCode = 200
            body = #"{"items":[],"next_cursor":null}"#
        case ("POST", "/v1/people/imports"):
            statusCode = 200
            let values = requestJSON()
            let scope = values["scope"] as? [String: Any] ?? [:]
            let scopeJSON = String(data: try! JSONSerialization.data(withJSONObject: scope), encoding: .utf8)!
            let mailbox = scope["email_source"] as? String == "mailbox"
            body = """
            {"id":"00000000-0000-0000-0000-000000000784","revision":1,"state":"preview","scope":\(scopeJSON),"records_read":0,"mailbox_records_read":0,"mailbox_read_complete":false,"records_processed":0,"records_excluded":0,"failures":0,"spent_usd":"0","reserved_usd":"0","source_read_complete":false,"analysis_complete":false,"coverage":"\(mailbox ? "Selected mailbox history" : "Retained evidence only")"}
            """
        case ("POST", "/v1/people/00000000-0000-0000-0000-000000000777/forget"):
            statusCode = 200
            let applied = requestJSON()["phase"] as? String == "apply"
            body = """
            {"id":"00000000-0000-0000-0000-000000000785","revision":\(applied ? 2 : 1),"state":"\(applied ? "cleanup_pending" : "preview")","counts":{"beliefs":2,"interaction":1},"scope":"Forget derived memories about Maya. Original Chat and Email messages remain at their sources."}
            """
        case ("GET", "/v1/people/operations/00000000-0000-0000-0000-000000000785"):
            statusCode = 200
            body = #"{"id":"00000000-0000-0000-0000-000000000785","revision":3,"state":"completed","counts":{"beliefs":2,"interaction":1}}"#
        case ("POST", "/v1/people/identity-operations"):
            statusCode = 200
            let applied = requestJSON()["operation"] as? String == "apply"
            body = """
            {"id":"00000000-0000-0000-0000-000000000783","revision":\(applied ? 2 : 1),"state":"\(applied ? "completed" : "preview")","operation":"merge","assignments":[{"entity_id":"00000000-0000-0000-0000-000000000780","expected_revision":1}]}
            """
        case ("GET", "/v1/people/00000000-0000-0000-0000-000000000777"):
            statusCode = 200
            body = """
            {"person":\(Self.personJSON),"aliases":[],"relationships":[{"id":"00000000-0000-0000-0000-000000000779","revision":1,"subject":{"kind":"person","id":"00000000-0000-0000-0000-000000000777"},"object":{"kind":"owner"},"predicate":"sibling","qualifier":"","precision":"unknown","support_ids":["00000000-0000-0000-0000-000000000778"]}],"history":[],"commitments":[],"facts":[],"fact_revisions":{},"related_labels":{},"truncated":false,"coverage":"Recorded owner evidence; earlier history may be unavailable."}
            """
        case ("GET", "/v1/people/00000000-0000-0000-0000-000000000777/evidence/00000000-0000-0000-0000-000000000778"):
            statusCode = 200
            body = """
            {"reference":"00000000-0000-0000-0000-000000000778","source_kind":"owner","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","event_sequence":10,"evidence_at":"2026-08-01T00:00:00Z","owner_assertion":"Maya is my sister."}
            """
        case ("GET", "/v1/people/00000000-0000-0000-0000-000000000777/identity-evidence"):
            statusCode = 200
            body = """
            {"items":[{"id":"00000000-0000-0000-0000-000000000780","revision":1,"kind":"mention","label":"Subject mention · characters 0-4","support_ids":["00000000-0000-0000-0000-000000000778"],"unresolved":false},{"id":"00000000-0000-0000-0000-000000000781","revision":1,"kind":"memory_link","label":"Maya likes cycling.","support_ids":["00000000-0000-0000-0000-000000000778"],"unresolved":true}],"next_cursor":null}
            """
        case ("GET", "/v1/email/learning"):
            statusCode = 200
            body = Self.learningJSON
        case ("POST", "/v1/email/drafts/\(Self.emailDraftID)/style-example"):
            let values = requestJSON()
            statusCode = (values["expected_revision"] as? Int) == Self.emailLock.withLock({ Self.emailRevision }) ? 200 : 409
            body = Self.learningJSON
        case ("PUT", "/v1/email/learning"):
            let values = requestJSON()
            Self.emailLock.withLock { Self.learningPaused = values["paused"] as? Bool ?? false }
            statusCode = 200
            body = Self.learningJSON
        case ("GET", "/v1/email/drafts/\(Self.emailDraftID)/revisions"):
            statusCode = 200
            body = "{\"items\":[\(Self.emailDraftJSON)],\"next_cursor\":null}"
        case ("GET", "/v1/email/accounts"):
            statusCode = 200
            let advertised = [
                Self.subscriptionsEnabled ? ",\"unsubscribe_supported\":true" : "",
                Self.suspectedSpamEnabled ? ",\"spam_supported\":true" : "",
            ].joined()
            body = Self.emailAccountsJSON.replacingOccurrences(
                of: "\"archive_supported\":true", with: "\"archive_supported\":true\(advertised)")
        case ("GET", "/v1/email/subscriptions") where Self.subscriptionsEnabled:
            statusCode = 200
            let state = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?
                .first { $0.name == "state" }?.value
            let rows = Self.subscriptionLock.withLock {
                Self.subscriptionSenders
                    .filter { state == nil || Self.subscriptionRecords[$0.id]?.state == state }
                    .map(Self.subscriptionJSON)
            }
            body = "{\"items\":[\(rows.joined(separator: ","))],\"next_cursor\":null}"
        case ("POST", "/v1/email/subscriptions/unsubscribe") where Self.subscriptionsEnabled:
            // The consent names each sender by identity, evidence and revision,
            // once, under the header's idempotency key; the fixture refuses anything else.
            let payload = requestJSON()
            let targets = payload["targets"] as? [[String: Any]] ?? []
            let key = payload["idempotency_key"] as? String
            let ids = targets.compactMap { $0["subscription_id"] as? String }
            let consented = Self.subscriptionLock.withLock {
                !targets.isEmpty && targets.count <= 25 && Set(ids).count == ids.count
                    && payload["archive_existing"] is Bool && key != nil
                    && request.value(forHTTPHeaderField: "Idempotency-Key") == key
                    && targets.allSatisfy { target in
                        guard let id = target["subscription_id"] as? String,
                            let sender = Self.subscriptionSenders.first(where: { $0.id == id }),
                            let record = Self.subscriptionRecords[id]
                        else { return false }
                        return target["evidence_digest"] as? String == sender.digest
                            && target["expected_revision"] as? Int == record.revision
                            && ["active", "failed", "still_sending"].contains(record.state)
                    }
            }
            guard consented else {
                statusCode = 409
                body = #"{"error":{"code":"conflict","message":"The consent does not match the census.","details":{},"request_id":"ui-test"}}"#
                break
            }
            body = Self.admitSubscriptionOperation(action: "unsubscribe", targets: ids)
            statusCode = 202
        case ("POST", let path) where Self.subscriptionsEnabled
            && path.hasPrefix("/v1/email/subscriptions/") && path.hasSuffix("/keep"):
            let id = url.pathComponents[4]
            let payload = requestJSON()
            let row: String? = Self.subscriptionLock.withLock {
                guard let sender = Self.subscriptionSenders.first(where: { $0.id == id }),
                    let record = Self.subscriptionRecords[id],
                    payload["expected_revision"] as? Int == record.revision,
                    let kept = payload["kept"] as? Bool
                else { return nil }
                Self.subscriptionRecords[id] = (kept ? "kept" : "active", record.revision + 1, record.operation)
                return Self.subscriptionJSON(sender)
            }
            statusCode = row == nil ? 409 : 200
            body = row ?? #"{"error":{"code":"conflict","message":"stale revision","details":{},"request_id":"ui-test"}}"#
        case ("POST", let path) where Self.subscriptionsEnabled
            && path.hasPrefix("/v1/email/subscriptions/") && path.hasSuffix("/spam"):
            let id = url.pathComponents[4]
            let spam = requestJSON()["spam"] as? Bool ?? true
            statusCode = 202
            body = Self.admitSubscriptionOperation(action: spam ? "report_spam" : "not_spam", targets: [id])
        case ("GET", let path) where Self.subscriptionsEnabled && path.hasPrefix("/v1/email/operations/"):
            statusCode = 200
            body = Self.readSubscriptionOperation(url.lastPathComponent)
        case ("GET", "/v1/email/threads"):
            statusCode = 200
            let view = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "view" }?.value ?? "priority"
            let handled = Self.emailLock.withLock { Self.emailHandled || Self.emailArchived }
            let included = view == "all" || (view == "other" ? handled : !handled)
            var items = ProcessInfo.processInfo.arguments.contains("--ui-testing-email-full-inbox")
                ? Self.fullInboxJSON : (included ? Self.emailThreadJSON : "")
            if view == "priority", ProcessInfo.processInfo.arguments.contains("--ui-testing-email-archive-next") {
                items += (items.isEmpty ? "" : ",") + Self.nextEmailThreadJSON
            }
            if view == "priority", Self.bulkThreadEnabled {
                items += (items.isEmpty ? "" : ",") + Self.bulkEmailThreadJSON
            }
            // A reported conversation leaves the priority view, as the server's does.
            if Self.suspectedSpamEnabled, view != "priority" || !Self.emailLock.withLock({ Self.spamInSpam }) {
                items += (items.isEmpty ? "" : ",") + Self.spamThreadJSON
            }
            body = "{\"items\":[\(items)],\"next_cursor\":null}"
        case ("GET", "/v1/email/threads/00000000-0000-0000-0000-000000000899"):
            statusCode = 200
            body = Self.nextEmailThreadJSON
        case ("GET", "/v1/email/threads/\(ConversationNavigationUITestFixture.bulkThreadID)") where Self.bulkThreadEnabled:
            statusCode = 200
            body = Self.bulkEmailThreadJSON
        case ("GET", "/v1/email/threads/\(ConversationNavigationUITestFixture.spamThreadID)") where Self.suspectedSpamEnabled:
            // A report settles on the second status read after its admission.
            Self.emailLock.withLock {
                if let target = Self.spamTarget {
                    Self.spamReads += 1
                    if Self.spamReads >= 2 {
                        Self.spamInSpam = target
                        if !target { Self.spamCleared = true }
                    }
                }
            }
            statusCode = 200
            body = Self.spamThreadJSON
        case ("POST", "/v1/email/threads/\(ConversationNavigationUITestFixture.spamThreadID)/spam") where Self.suspectedSpamEnabled:
            let values = requestJSON()
            let key = values["idempotency_key"] as? String
            guard let spam = values["spam"] as? Bool, values["expected_revision"] as? Int == 1,
                key != nil, request.value(forHTTPHeaderField: "Idempotency-Key") == key
            else {
                statusCode = 400
                body = #"{"error":{"code":"malformed_request","message":"Invalid spam request.","details":{},"request_id":"ui-test"}}"#
                break
            }
            Self.emailLock.withLock {
                Self.spamTarget = spam
                Self.spamReads = 0
            }
            statusCode = 202
            body = "{\"operation_id\":\"\(Self.spamOperationID)\",\"run_id\":\"\(Self.emailRunID)\",\"status\":\"RUNNING\",\"replayed\":false}"
        case ("POST", "/v1/email/threads/\(ConversationNavigationUITestFixture.spamThreadID)/spam-flag") where Self.suspectedSpamEnabled:
            guard let cleared = requestJSON()["cleared"] as? Bool else {
                statusCode = 400
                body = #"{"error":{"code":"malformed_request","message":"Invalid flag request.","details":{},"request_id":"ui-test"}}"#
                break
            }
            Self.emailLock.withLock { Self.spamCleared = cleared }
            statusCode = 200
            body = Self.spamThreadJSON
        case ("POST", "/v1/email/threads/\(Self.emailThreadID)/dismiss"):
            let values = requestJSON()
            Self.emailLock.withLock { Self.emailHandled = values["dismissed"] as? Bool ?? true }
            statusCode = 200
            body = Self.emailThreadJSON
        case ("POST", "/v1/email/threads/\(Self.emailThreadID)/archive"):
            let values = requestJSON()
            Self.emailLock.withLock {
                Self.emailArchiveTarget = values["archived"] as? Bool
                Self.emailArchiveReads = 0
            }
            statusCode = 202
            body = "{\"operation_id\":\"\(Self.emailThreadID)\",\"run_id\":\"\(Self.emailRunID)\",\"status\":\"RUNNING\",\"replayed\":false}"
        case ("GET", "/v1/email/threads/\(Self.emailThreadID)"):
            Self.emailLock.withLock {
                if let target = Self.emailArchiveTarget {
                    Self.emailArchiveReads += 1
                    if Self.emailArchiveReads >= Self.emailArchiveCompletionRead && !ProcessInfo.processInfo.arguments.contains("--ui-testing-email-archive-failure") {
                        Self.emailArchived = target
                    }
                }
            }
            statusCode = 200
            body = Self.emailThreadJSON
        case ("POST", "/v1/email/refresh"):
            if ProcessInfo.processInfo.arguments.contains("--ui-testing-email-budget-pause") {
                statusCode = 402
                body = """
                {"error":{"code":"budget_exceeded","message":"Automatic email work is paused. Today: $16.30 spent, $23.00 reserved, $40.00 limit. Rolling 30 days: $347.57 spent, $400.00 limit. The next batch needs $1.00 available. Cached mail and editing remain available.","details":{"daily_spent":"16.2966295","daily_reserved":"23","daily_limit":"40","monthly_spent":"347.5703420","monthly_reserved":"23","monthly_limit":"400","next_reservation":"1","retry_at":"2099-01-01T00:00:00Z"},"request_id":"ui-budget"}}
                """
            } else {
                statusCode = 202
                body = "{\"operation_id\":\"\(Self.emailThreadID)\",\"run_id\":\"\(Self.emailRunID)\",\"status\":\"COMPLETED\",\"replayed\":false}"
            }
        case ("POST", "/v1/email/feedback"):
            statusCode = 200
            body = "{\"feedback_id\":\"\(Self.emailThreadID)\",\"thread\":\(Self.emailThreadJSON)}"
        case ("DELETE", "/v1/email/feedback/\(Self.emailThreadID)"):
            statusCode = 200
            body = Self.emailThreadJSON
        case ("GET", "/v1/email/drafts/\(Self.emailDraftID)"):
            statusCode = 200
            body = Self.emailDraftJSON
        case ("PUT", "/v1/email/drafts/\(Self.emailDraftID)"):
            let values = requestJSON()
            Self.emailLock.lock()
            if let value = values["body"] as? String { Self.emailBody = value }
            Self.emailRevision += 1
            Self.emailLock.unlock()
            statusCode = 200
            body = Self.emailDraftJSON
        case ("POST", "/v1/email/drafts/\(Self.emailDraftID)/send-proposal"):
            Self.emailLock.lock()
            Self.emailStatus = "awaiting_approval"
            Self.emailLock.unlock()
            statusCode = 202
            body = "{\"draft\":\(Self.emailDraftJSON),\"run_id\":\"\(Self.emailRunID)\",\"status\":\"WAITING_FOR_APPROVAL\",\"approval_id\":\"\(Self.emailApprovalID)\"}"
        case ("GET", "/v1/runs/\(Self.emailRunID)"):
            statusCode = 200
            let status = Self.emailLock.withLock { Self.emailStatus == "sent" ? "COMPLETED" : "WAITING_FOR_APPROVAL" }
            body = """
                {"id":"\(Self.emailRunID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","parent_run_id":null,"status":"\(status)","step_count":1,"model_call_count":0,"tool_call_count":1,"usage":{"input_tokens":0,"output_tokens":0,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-09-11T00:00:00Z","updated_at":"2026-09-11T00:00:00Z"}
                """
        case ("GET", "/v1/approvals/\(Self.emailApprovalID)"):
            statusCode = 200
            body = Self.emailApprovalJSON
        case ("POST", "/v1/approvals/\(Self.emailApprovalID)/resolve"):
            let values = requestJSON()
            Self.emailLock.lock()
            Self.emailStatus = values["decision"] as? String == "approve_once" ? "sent" : "ready"
            Self.emailLock.unlock()
            statusCode = 200
            body = Self.emailApprovalJSON
        case ("GET", "/v1/sessions"):
            statusCode = 200
            let folderSessions = Self.foldersEnabled ? "," + Self.folderJourneySessionsJSON : ""
            let reportSession = Self.unreadReportEnabled
                ? "," + Self.reportSessionJSON + "," + Self.previousReportSessionJSON : ""
            body = """
                {"items":[\(Self.firstSessionJSON),\(Self.secondSessionJSON)\(folderSessions)\(reportSession)],"next_cursor":null}
                """
        case ("GET", "/v1/sessions/\(ConversationNavigationUITestFixture.reportSessionID)") where Self.unreadReportEnabled:
            statusCode = 200
            body = Self.reportSessionJSON
        case ("GET", "/v1/sessions/\(ConversationNavigationUITestFixture.reportSessionID)/messages") where Self.unreadReportEnabled:
            statusCode = 200
            body = """
                {"items":[
                  {"sequence":2,"role":"user","content":[{"type":"text","text":"Prepare the weekday briefing."}]},
                  {"sequence":9,"role":"assistant","content":[{"type":"text","text":"Weekday briefing loaded"}]}
                ],"next_cursor":null}
                """
        case ("GET", "/v1/runs/\(Self.reportRunID)") where Self.unreadReportEnabled:
            statusCode = 200
            body = """
                {"id":"\(Self.reportRunID)","session_id":"\(ConversationNavigationUITestFixture.reportSessionID)","parent_run_id":null,"status":"COMPLETED","step_count":2,"model_call_count":2,"tool_call_count":1,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":12,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-10-07T16:00:00Z","updated_at":"2026-10-07T16:01:46Z"}
                """
        case ("GET", "/v1/runs/\(Self.reportRunID)/events") where Self.unreadReportEnabled:
            statusCode = 200
            body = [
                "id: 3\nevent: run.queued\ndata: {\"run_id\":\"\(Self.reportRunID)\"}\n\n",
                "id: 6\nevent: run.started\ndata: {\"run_id\":\"\(Self.reportRunID)\"}\n\n",
                "id: 8\nevent: assistant.message.completed\ndata: {\"run_id\":\"\(Self.reportRunID)\"}\n\n",
                "id: 10\nevent: run.completed\ndata: {\"run_id\":\"\(Self.reportRunID)\"}\n\n",
            ].joined()
        case ("POST", "/v1/notifications/sync") where Self.unreadReportEnabled:
            let values = requestJSON()
            let seen = values["seen_run_ids"] as? [String] ?? []
            let queried = values["query_run_ids"] as? [String] ?? []
            let read = Self.reportLock.withLock {
                if seen.contains(where: { $0.caseInsensitiveCompare(Self.reportRunID) == .orderedSame }) {
                    Self.reportSeen = true
                }
                return Self.reportSeen
            }
            let unread = !read && queried.contains { $0.caseInsensitiveCompare(Self.reportRunID) == .orderedSame }
            statusCode = 200
            body = "{\"obsolete_notification_ids\":[],\"unread_run_ids\":[\(unread ? "\"\(Self.reportRunID)\"" : "")]}"
        case ("POST", "/v1/sessions"):
            statusCode = 201
            body = Self.firstSessionJSON
        case ("GET", "/v1/sessions/\(ConversationNavigationUITestFixture.firstSessionID)"):
            statusCode = 200
            body = Self.firstSessionJSON
        case (
            "GET",
            "/v1/sessions/\(ConversationNavigationUITestFixture.firstSessionID)/messages"
        ):
            statusCode = 200
            let filePart = ProcessInfo.processInfo.arguments.contains("--ui-testing-artifact")
                ? ",{\"type\":\"file\",\"artifact_id\":\"\(ConversationNavigationUITestFixture.artifactID)\",\"media_type\":\"image/svg+xml\",\"filename\":\"garden.svg\"}"
                : ""
            body = """
                {"items":[
                  {"sequence":1,"role":"user","content":[{"type":"text","text":"Historical question"}]},
                  {"sequence":2,"role":"assistant","content":[{"type":"text","text":"Historical answer loaded"}\(filePart)]}
                ],"next_cursor":null}
                """
        case (
            "POST",
            "/v1/sessions/\(ConversationNavigationUITestFixture.firstSessionID)/messages"
        ):
            if ProcessInfo.processInfo.arguments.contains("--ui-testing-chat-send-failure") {
                statusCode = 400
                body = #"{"error":{"code":"invalid_request","message":"Submission rejected","details":{},"request_id":"ui-test"}}"#
            } else {
                statusCode = 202
                body = "{\"run_id\":\"\(Self.runID)\",\"status\":\"QUEUED\"}"
            }
        case ("GET", "/v1/runs/\(Self.runID)/events"):
            statusCode = 200
            body = ProcessInfo.processInfo.arguments.contains("--ui-testing-mixed-tools")
                ? Self.mixedToolEvents : """
                id: 3
                event: run.completed
                data: {"run_id":"\(Self.runID)"}

                """
        case ("GET", "/v1/sessions/\(ConversationNavigationUITestFixture.secondSessionID)"):
            statusCode = 200
            body = Self.secondSessionJSON
        case (
            "GET",
            "/v1/sessions/\(ConversationNavigationUITestFixture.secondSessionID)/messages"
        ):
            statusCode = 200
            body = """
                {"items":[
                  {"sequence":1,"role":"user","content":[{"type":"text","text":"Second historical question"}]},
                  {"sequence":2,"role":"assistant","content":[{"type":"text","text":"Second historical answer loaded"}]}
                ],"next_cursor":null}
                """
        case ("GET", "/v1/folders"):
            guard Self.foldersEnabled else {
                statusCode = 404
                body = Self.foldersUnavailableJSON
                break
            }
            statusCode = 200
            body = "{\"items\":[\(Self.folderItemsJSON)],\"next_cursor\":null}"
        case ("GET", "/v1/folders/proposals"):
            guard Self.foldersEnabled else {
                statusCode = 404
                body = Self.foldersUnavailableJSON
                break
            }
            statusCode = 200
            let (open, declined) = Self.folderLock.withLock { (!Self.proposalResolved, Self.proposalDeclined) }
            let item = open
                ? Self.proposalJSON(state: "proposed", resultingFolderID: nil)
                : declined ? Self.followUpProposalJSON : ""
            body = "{\"items\":[\(item)],\"next_cursor\":null}"
        case ("POST", "/v1/folders"):
            guard Self.foldersEnabled else {
                statusCode = 404
                body = Self.foldersUnavailableJSON
                break
            }
            let name = requestJSON()["name"] as? String ?? "New Folder"
            Self.folderLock.withLock { Self.createdFolderName = name }
            statusCode = 201
            body = Self.folderJSON(id: ConversationNavigationUITestFixture.proposedFolderID, name: name)
        case ("PATCH", "/v1/folders/\(ConversationNavigationUITestFixture.folderID)"):
            guard Self.foldersEnabled else {
                statusCode = 404
                body = Self.foldersUnavailableJSON
                break
            }
            let name = requestJSON()["name"] as? String ?? "Travel"
            Self.folderLock.withLock { Self.folderName = name }
            statusCode = 200
            body = Self.folderJSON(id: ConversationNavigationUITestFixture.folderID, name: name)
        case ("DELETE", "/v1/folders/\(ConversationNavigationUITestFixture.folderID)"):
            guard Self.foldersEnabled else {
                statusCode = 404
                body = Self.foldersUnavailableJSON
                break
            }
            Self.folderLock.withLock {
                Self.folderDeleted = true
                Self.sessionFolders = Self.sessionFolders.filter { $0.value != ConversationNavigationUITestFixture.folderID }
            }
            statusCode = 204
            body = ""
        case ("POST", "/v1/folders/proposals/\(ConversationNavigationUITestFixture.proposalID)/accept"):
            guard Self.foldersEnabled else {
                statusCode = 404
                body = Self.foldersUnavailableJSON
                break
            }
            let override = requestJSON()["name"] as? String
            let name = override ?? "Lisbon Trip"
            let taken = Self.folderLock.withLock {
                [Self.folderName, "Work"].contains { $0.caseInsensitiveCompare(name) == .orderedSame }
            }
            if taken {
                statusCode = 409
                body = #"{"error":{"code":"conflict","message":"A folder with that name already exists.","details":{"reason":"folder_name_taken"},"request_id":"ui-test"}}"#
                break
            }
            Self.folderLock.withLock {
                Self.proposalResolved = true
                Self.createdFolderName = name
                for member in ConversationNavigationUITestFixture.proposalMemberIDs {
                    Self.sessionFolders[member] = ConversationNavigationUITestFixture.proposedFolderID
                }
            }
            statusCode = 200
            body = Self.proposalJSON(state: "accepted", resultingFolderID: ConversationNavigationUITestFixture.proposedFolderID)
        case ("POST", "/v1/folders/proposals/\(ConversationNavigationUITestFixture.proposalID)/decline"):
            guard Self.foldersEnabled else {
                statusCode = 404
                body = Self.foldersUnavailableJSON
                break
            }
            Self.folderLock.withLock {
                Self.proposalResolved = true
                Self.proposalDeclined = true
            }
            statusCode = 200
            body = Self.proposalJSON(state: "declined", resultingFolderID: nil)
        case ("PUT", "/v1/sessions/\(ConversationNavigationUITestFixture.firstSessionID)/folder"),
            ("PUT", "/v1/sessions/\(ConversationNavigationUITestFixture.secondSessionID)/folder"):
            guard Self.foldersEnabled else {
                statusCode = 404
                body = Self.foldersUnavailableJSON
                break
            }
            let sessionID = url.pathComponents[3]
            let target = requestJSON()["folder_id"] as? String
            Self.folderLock.withLock {
                // As on the server, a real move withdraws the open proposal
                // naming the conversation, so no later refresh lists it again.
                if Self.sessionFolders[sessionID] != target,
                    ConversationNavigationUITestFixture.proposalMemberIDs.contains(sessionID)
                {
                    Self.proposalResolved = true
                }
                if let target { Self.sessionFolders[sessionID] = target } else { Self.sessionFolders.removeValue(forKey: sessionID) }
            }
            statusCode = 200
            body = sessionID == ConversationNavigationUITestFixture.firstSessionID ? Self.firstSessionJSON : Self.secondSessionJSON
        case ("DELETE", let path) where path.hasPrefix("/v1/memories/"):
            Self.memoryLock.withLock { Self.memoryDeleted = true }
            statusCode = 204
            body = ""
        case ("POST", let path) where path.hasPrefix("/v1/memories/") && path.hasSuffix("/review"):
            statusCode = 200
            body = Self.memoryJSON
        case ("GET", "/v1/memories"):
            let query = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            guard query.contains(URLQueryItem(name: "ceiling", value: "restricted")) else {
                client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
                return
            }
            statusCode = 200
            let deleted = Self.memoryLock.withLock { Self.memoryDeleted }
            body = """
                {"items":[\(deleted ? "" : Self.memoryJSON)],"next_cursor":null}
                """
        case ("GET", "/v1/schedules"):
            let states = URLComponents(url: url, resolvingAgainstBaseURL: false)?
                .queryItems?
                .filter { $0.name == "state" }
                .compactMap(\.value) ?? []
            statusCode = 200
            if states == ["COMPLETED", "CANCELLED"] {
                body = """
                    {"items":[\(Self.scheduleHistorySummaryJSON)],"next_cursor":null}
                    """
            } else {
                body = """
                    {"items":[\(Self.scheduleSummaryJSON)],"next_cursor":null}
                    """
            }
        case ("GET", "/v1/schedules/\(ConversationNavigationUITestFixture.scheduleID)"):
            statusCode = 200
            body = Self.scheduleWebsiteLock.withLock { Self.scheduleWebsiteBound }
                ? Self.boundScheduleDetailJSON : Self.scheduleDetailJSON
        case ("PATCH", "/v1/schedules/\(ConversationNavigationUITestFixture.scheduleID)")
        where Self.scheduleWebsiteJourney:
            let payload = requestJSON()
            let definition = payload["definition"] as? [String: Any] ?? [:]
            if payload["expected_revision"] as? Int == 1,
                Set(definition.keys) == Self.scheduleDefinitionFields,
                definition["instruction"] as? String
                    == "Full instruction from the schedule point read.",
                definition["requested_scopes"] as? [String] == ["browser.profile.read"],
                definition["browser_profile_id"] as? String == Self.scheduleWebsiteProfileID
            {
                Self.scheduleWebsiteLock.withLock { Self.scheduleWebsiteBound = true }
                statusCode = 200
                body = Self.boundScheduleDetailJSON
            } else {
                statusCode = 422
                body = #"{"error":{"code":"schedule_validation_error","message":"The fixture rejected the schedule definition.","details":{"reason":"schedule.fixture_rejected"},"request_id":"ui-test"}}"#
            }
        case ("GET", "/v1/settings/models"):
            statusCode = 200
            body = Self.modelSettingsLock.withLock { Self.modelSettingsJSON }
        case ("PUT", "/v1/settings/models"):
            let result = Self.updateModelSettings(requestJSON())
            statusCode = result.0
            body = result.1
        case ("GET", "/v1/runs/\(Self.taskRunID)") where Self.taskGrantJourney:
            statusCode = 200
            body = Self.taskRunJSON
        case ("GET", "/v1/runs/\(Self.taskRunID)/events") where Self.taskGrantJourney:
            // The run waits on the approval; the stream stays open, as a
            // suspended run's does, until the test ends.
            statusCode = 200
            holdOpen = true
            body = """
                id: 5
                event: approval.requested
                data: {"run_id":"\(Self.taskRunID)","approval_id":"\(Self.taskApprovalID)"}

                id: 6
                event: run.waiting_for_approval
                data: {"run_id":"\(Self.taskRunID)","approval_id":"\(Self.taskApprovalID)"}


                """
        case ("GET", "/v1/approvals/\(Self.taskApprovalID)") where Self.taskGrantJourney:
            statusCode = 200
            body = Self.taskApprovalJSON
        case ("GET", "/v1/approvals") where Self.taskGrantJourney:
            statusCode = 200
            let pending = Self.taskGrantLock.withLock { !Self.taskGrantResolved }
            body = "{\"items\":[\(pending ? Self.taskApprovalJSON : "")],\"next_cursor\":null}"
        case ("POST", "/v1/approvals/\(Self.taskApprovalID)/resolve") where Self.taskGrantJourney:
            // The client must repeat the offer's scope exactly (ADR-0129 D4).
            let payload = requestJSON()
            let echo = payload["task_grant"] as? [String: String]
            if Self.genericCheckpoint, payload["decision"] as? String == "approve_once" {
                Self.taskGrantLock.withLock { Self.taskGrantResolved = true }
                statusCode = 200
                body = Self.taskApprovalJSON
            } else if payload["decision"] as? String == "approve_for_task",
                echo == ["origin": "https://www.duolingo.com", "path_prefix": "/lesson"]
            {
                Self.taskGrantLock.withLock { Self.taskGrantResolved = true }
                statusCode = 200
                body = Self.taskApprovalJSON
            } else {
                statusCode = 400
                body = #"{"error":{"code":"malformed_request","message":"task_grant must repeat the offer","details":{},"request_id":"ui-test"}}"#
            }
        case ("GET", "/v1/browser-task-grants") where Self.taskGrantJourney:
            statusCode = 200
            let active = Self.taskGrantLock.withLock { !Self.genericCheckpoint && Self.taskGrantResolved && !Self.taskGrantRevoked }
            body = "{\"items\":[\(active ? Self.taskGrantJSON(status: "active") : "")],\"next_cursor\":null}"
        case ("POST", "/v1/browser-task-grants/\(Self.taskGrantID)/revoke") where Self.taskGrantJourney:
            Self.taskGrantLock.withLock { Self.taskGrantRevoked = true }
            statusCode = 200
            body = Self.taskGrantJSON(status: "revoked")
        case ("GET", "/v1/browser-task-scopes"):
            statusCode = 200
            body = Self.taskGrantLock.withLock {
                let data = try! JSONSerialization.data(withJSONObject: ["revision": Self.taskScopeRevision, "scopes": Self.taskScopes])
                return String(decoding: data, as: UTF8.self)
            }
        case ("PUT", "/v1/browser-task-scopes"):
            let payload = requestJSON()
            let response: (Int, String) = Self.taskGrantLock.withLock {
                guard let revision = payload["revision"] as? Int, revision == Self.taskScopeRevision,
                    let scopes = payload["scopes"] as? [[String: String]] else {
                    return (409, #"{"error":{"code":"conflict","message":"changed","details":{},"request_id":"ui"}}"#)
                }
                Self.taskScopeRevision += 1
                Self.taskScopes = scopes
                let data = try! JSONSerialization.data(withJSONObject: ["revision": Self.taskScopeRevision, "scopes": scopes])
                return (200, String(decoding: data, as: UTF8.self))
            }
            statusCode = response.0
            body = response.1
        case ("GET", "/v1/browser-profiles") where Self.scheduleWebsiteJourney:
            statusCode = 200
            body = """
                {"items":[{"id":"\(Self.scheduleWebsiteProfileID)","allowed_origins":["https://x.com"],"status":"ready","generation":1,"created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z","last_used_at":null}],"next_cursor":null}
                """
        case ("GET", "/v1/browser-profiles"):
            statusCode = 200
            body = #"{"items":[],"next_cursor":null}"#
        case ("POST", "/v1/browser-profiles"):
            statusCode = 201
            body = Self.browserProfileJSON
        case (
            "POST",
            "/v1/browser-profiles/\(Self.browserProfileID)/authentication-ceremonies"
        ):
            statusCode = 201
            body = """
                {"id":"\(Self.authenticationID)","profile_id":"\(Self.browserProfileID)","status":"authentication_required","expires_at":"2026-08-23T12:05:00Z","launch_url":"https://browser.example/authentication/\(Self.authenticationID)#capability=opaque"}
                """
        case ("GET", "/v1/browser-authentication-ceremonies/\(Self.authenticationID)"):
            // Returning to the app re-reads an open remote ceremony (ADR-0128 D16).
            statusCode = 200
            body = """
                {"id":"\(Self.authenticationID)","profile_id":"\(Self.browserProfileID)","status":"authentication_required","expires_at":"2026-08-23T12:05:00Z","launch_url":null}
                """
        default:
            client?.urlProtocol(self, didFailWithError: URLError(.unsupportedURL))
            return
        }

        guard let response = HTTPURLResponse(
            url: url,
            statusCode: statusCode,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        ) else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        let keepOpen = holdOpen
        let deliver = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: Data(body.utf8))
            if !keepOpen {
                self.client?.urlProtocolDidFinishLoading(self)
            }
        }
        pendingResponse = deliver
        let slowChat = ProcessInfo.processInfo.arguments.contains("--ui-testing-chat-slow-send")
        let isSubmission = request.httpMethod == "POST" && url.path.hasSuffix("/messages")
        let isRunStream = url.path == "/v1/runs/\(Self.runID)/events"
        if slowChat && isSubmission && ProcessInfo.processInfo.arguments.contains("--ui-testing-chat-hold-submission") {
            // The keyboard test observes a pending request, independent of how
            // long XCTest takes to query accessibility. Teardown cancels it.
            return
        }
        if slowChat && (isSubmission || isRunStream) {
            DispatchQueue.main.asyncAfter(deadline: .now() + 8, execute: deliver)
        } else {
            deliver.perform()
        }
    }

    override func stopLoading() {
        pendingResponse?.cancel()
        pendingResponse = nil
    }

    private func requestJSON() -> [String: Any] {
        var data = request.httpBody ?? Data()
        if let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
        }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
    }

    /// Applies the same rules as the server: the version guards the write,
    /// only offered choices are stored, and restating the stored values does
    /// not advance the version.
    private static func updateModelSettings(_ payload: [String: Any]) -> (Int, String) {
        func choice(_ key: String) -> (policy: String, effort: String?)? {
            guard let object = payload[key] as? [String: Any],
                let policy = object["model_policy"] as? String,
                object.keys.contains("reasoning_effort")
            else { return nil }
            return (policy, object["reasoning_effort"] as? String)
        }
        return modelSettingsLock.withLock {
            guard payload["expected_version"] as? Int == modelSettingsVersion else {
                return (409, #"{"error":{"code":"conflict","message":"model settings version is stale","details":{},"request_id":"ui-test"}}"#)
            }
            guard let chat = choice("chat"), let memory = choice("memory"),
                chatModelOffers.contains(where: { offer in
                    offer.policy == chat.policy
                        && (chat.effort.map { offer.efforts.contains($0) } ?? offer.efforts.isEmpty)
                }),
                memoryModelOffers.contains(where: { $0.policy == memory.policy && $0.effort == memory.effort })
            else {
                return (400, #"{"error":{"code":"malformed_request","message":"That model choice is not offered.","details":{},"request_id":"ui-test"}}"#)
            }
            if chat != chatModelChoice || memory != memoryModelChoice {
                chatModelChoice = chat
                memoryModelChoice = memory
                modelSettingsVersion += 1
            }
            return (200, modelSettingsJSON)
        }
    }

    /// Callers hold `modelSettingsLock`.
    private static var modelSettingsJSON: String {
        func effort(_ value: String?) -> String { value.map { "\"\($0)\"" } ?? "null" }
        let chatOptions = chatModelOffers.map { offer in
            let efforts = offer.efforts.map { "\"\($0)\"" }.joined(separator: ",")
            return #"{"model_policy":"\#(offer.policy)","display_name":"\#(offer.name)","provider":"openai","model":"\#(offer.policy)","reasoning_efforts":[\#(efforts)],"default_reasoning_effort":"high"}"#
        }.joined(separator: ",")
        let memoryOptions = memoryModelOffers.map { offer in
            #"{"model_policy":"\#(offer.policy)","display_name":"\#(offer.name)","provider":"openai","model":"\#(offer.policy)","reasoning_effort":\#(effort(offer.effort))}"#
        }.joined(separator: ",")
        return #"{"version":\#(modelSettingsVersion),"chat":{"model_policy":"\#(chatModelChoice.policy)","reasoning_effort":\#(effort(chatModelChoice.effort))},"memory":{"model_policy":"\#(memoryModelChoice.policy)","reasoning_effort":\#(effort(memoryModelChoice.effort))},"chat_options":[\#(chatOptions)],"memory_options":[\#(memoryOptions)]}"#
    }

    private static let emailThreadID = "00000000-0000-0000-0000-000000000801"
    /// A distinct conversation proves that detail archiving loads the next message's own content.
    private static let nextEmailThreadJSON = """
        {"id":"00000000-0000-0000-0000-000000000899","account_id":"work","subject":"Next conversation","senders":["sam@example.test"],"updated_at":"2026-09-11T00:00:00Z","revision":1,"in_inbox":true,"summary":"Review the next conversation.","reason":"A separate request.","needs_reply":false,"draft_id":null,"session_id":null,"priority":0.9,"complete":true,"messages":[{"id":"next-message","account_id":"work","provider_message_id":"next-message","provider_thread_id":"next-thread","sender":"sam@example.test","to":["owner@work.example"],"cc":[],"subject":"Next conversation","body":"Here is the next email to read.","sent_at":"2026-09-11T00:00:00Z","label_ids":["INBOX"],"attachments":[],"direction":"received","complete":true}],"draft":null}
        """
    /// Bulk mail from the Shop Deals sender. Its subscription block follows
    /// the census, as the server's thread projection does.
    private static var bulkEmailThreadJSON: String {
        let (state, revision) = subscriptionLock.withLock {
            let record = subscriptionRecords[ConversationNavigationUITestFixture.dealsSubscriptionID]
            return (record?.state ?? "active", record?.revision ?? 1)
        }
        let deals = subscriptionSenders.first { $0.id == ConversationNavigationUITestFixture.dealsSubscriptionID }!
        return """
            {"id":"\(ConversationNavigationUITestFixture.bulkThreadID)","account_id":"work","subject":"This week's deals","senders":["\(deals.address)"],"updated_at":"2026-09-10T00:00:00Z","revision":1,"in_inbox":true,"summary":"A weekly store promotion.","reason":"Bulk mail from a store you rarely open.","needs_reply":false,"draft_id":null,"session_id":null,"priority":0.4,"complete":true,"messages":[{"id":"deals-message","account_id":"work","provider_message_id":"deals-message","provider_thread_id":"deals-thread","sender":"\(deals.address)","to":["owner@work.example"],"cc":[],"subject":"This week's deals","body":"Everything in the store is on sale this week.","sent_at":"2026-09-10T00:00:00Z","label_ids":["INBOX"],"attachments":[],"direction":"received","complete":true}],"draft":null,"subscription":{"id":"\(deals.id)","state":"\(state)","mechanism":"\(deals.mechanism)","destination":"\(deals.destination)","evidence_digest":"\(deals.digest!)","revision":\(revision)}}
            """
    }
    /// The owner-facing census row; callers hold `subscriptionLock`.
    private static func subscriptionJSON(_ sender: SubscriptionSender) -> String {
        let record = subscriptionRecords[sender.id] ?? (state: "active", revision: 1, operation: nil)
        let destination = sender.verified ? sender.destination : ""
        let mailto = sender.verified && sender.mechanism == "mailto"
            ? #"{"to":"\#(sender.destination)","subject":"unsubscribe","body":""}"# : "null"
        let digest = sender.digest.map { "\"\($0)\"" } ?? "null"
        let reason = sender.protectedReason.map { "\"\($0)\"" } ?? "null"
        return """
            {"id":"\(sender.id)","account_id":"work","display_name":"\(sender.name)","address":"\(sender.address)","list_id":"","thread_count":\(sender.threads),"thread_count_overflow":false,"first_seen_at":"2026-07-01T00:00:00Z","last_received_at":"2026-09-10T00:00:00Z","mechanism":"\(sender.mechanism)","verified":\(sender.verified),"link_only":false,"destination":"\(destination)","mailto":\(mailto),"evidence_digest":\(digest),"state":"\(record.state)","protected":\(sender.protectedReason != nil),"protected_reason":\(reason),"requested_at":null,"operation":\(record.operation ?? "null"),"revision":\(record.revision)}
            """
    }
    /// Marks each sender pending behind one durable operation and returns its admission.
    private static func admitSubscriptionOperation(action: String, targets: [String]) -> String {
        let operationID = UUID().uuidString
        subscriptionLock.withLock {
            subscriptionOperations[operationID] = (action, targets)
            for id in targets {
                guard let record = subscriptionRecords[id] else { continue }
                subscriptionRecords[id] = (
                    "pending", record.revision + 1,
                    subscriptionOperationJSON(operationID, action: action, status: "pending", code: nil, prior: record.state))
            }
        }
        return "{\"operation_id\":\"\(operationID)\",\"run_id\":\"\(emailRunID)\",\"status\":\"RUNNING\",\"replayed\":false}"
    }
    /// The run finishes on its first read. Under the failure argument the
    /// sender refused an unsubscribe, which the row then records honestly.
    /// A phishing-shaped conversation from an unknown sender, flagged until the owner says otherwise.
    private static var spamThreadJSON: String {
        let (cleared, target, reads, inSpam) = emailLock.withLock { (spamCleared, spamTarget, spamReads, spamInSpam) }
        let operation = target.map {
            "{\"operation_id\":\"\(spamOperationID)\",\"run_id\":\"\(emailRunID)\",\"target_archived\":\($0),\"target_spam\":true,\"status\":\"\(reads >= 2 ? "completed" : "pending")\",\"error\":null}"
        } ?? "null"
        return """
        {"id":"\(ConversationNavigationUITestFixture.spamThreadID)","account_id":"work","subject":"Your account is locked","senders":["security@account-alerts.example.test"],"updated_at":"2026-09-10T12:00:00Z","revision":1,"in_inbox":\(!inSpam),"in_spam":\(inSpam),"suspected_spam":\(!cleared && !inSpam),"spam_cleared":\(cleared),"archive_operation":\(operation),"summary":"Claims your account is locked and asks you to sign in.","reason":"An unknown sender asks you to sign in through a link.","needs_reply":false,"draft_id":null,"session_id":null,"priority":0.9,"complete":true,"messages":[{"id":"spam-message","sender":"security@account-alerts.example.test","to":["owner@work.example"],"cc":[],"subject":"Your account is locked","body":"Your account is locked. Sign in within 24 hours to keep it.","sent_at":"2026-09-10T12:00:00Z","complete":true,"attachments":[]}],"draft":null}
        """
    }

    private static func readSubscriptionOperation(_ operationID: String) -> String {
        let failing = ProcessInfo.processInfo.arguments.contains(
            ConversationNavigationUITestFixture.subscriptionsFailureLaunchArgument)
        subscriptionLock.withLock {
            guard let operation = subscriptionOperations.removeValue(forKey: operationID) else { return }
            let action = operation.action
            for id in operation.targets {
                guard let record = subscriptionRecords[id] else { continue }
                let refused = failing && action == "unsubscribe"
                let state = switch action {
                case "report_spam": "reported_spam"
                case "not_spam": "active"
                default: refused ? "failed" : "unsubscribed"
                }
                subscriptionRecords[id] = (
                    state, record.revision + 1,
                    subscriptionOperationJSON(
                        operationID, action: action, status: refused ? "failed" : "completed",
                        code: refused ? "http_status" : nil, prior: "active"))
            }
        }
        return "{\"operation_id\":\"\(operationID)\",\"run_id\":\"\(emailRunID)\",\"status\":\"COMPLETED\",\"replayed\":false}"
    }
    private static func subscriptionOperationJSON(
        _ id: String, action: String, status: String, code: String?, prior: String
    ) -> String {
        let code = code.map { "\"\($0)\"" } ?? "null"
        return """
            {"operation_id":"\(id)","run_id":"\(emailRunID)","action":"\(action)","status":"\(status)","code":\(code),"requested_at":"2026-09-28T00:00:00Z","prior_state":"\(prior)"}
            """
    }
    /// Five synthetic priorities exercise real list geometry without using private mail.
    private static var fullInboxJSON: String {
        let subjects = ["Board agenda", "Design review for the autumn release", "Friday planning notes",
                        "Updated project timeline", "Travel details for next week"]
        return subjects.enumerated().map { index, subject in
            emailThreadJSON
                .replacingOccurrences(of: "\"id\":\"\(emailThreadID)\"",
                                      with: "\"id\":\"00000000-0000-0000-0000-00000000080\(index + 1)\"")
                .replacingOccurrences(of: "\"subject\":\"Board agenda\"", with: "\"subject\":\"\(subject)\"")
        }.joined(separator: ",")
    }
    private static let emailDraftID = "00000000-0000-0000-0000-000000000802"
    private static let emailRunID = "00000000-0000-0000-0000-000000000803"
    private static let emailApprovalID = "00000000-0000-0000-0000-000000000804"
    private static var learningJSON: String {
        "{\"paused\":\(emailLock.withLock { learningPaused }),\"profile_revision\":1,\"excluded_sources\":0,\"style_examples\":8,\"history_processed\":42,\"history_complete\":false}"
    }
    private static let emailAccountsJSON = """
        {"items":[{"id":"work","label":"Work","email_address":"owner@work.example","status":"ready","last_synced_at":"2026-09-11T00:00:00Z","history_complete":false,"history_processed":42,"archive_supported":true,"write_server_id":"gmail_work_write","read_server_id":"gmail_work_read","send_server_id":"gmail_work_send"}],"next_cursor":null}
        """
    private static var emailDraftJSON: String {
        let (body, revision, status) = emailLock.withLock { (emailBody, emailRevision, emailStatus) }
        let escapedBody = String(data: try! JSONEncoder().encode(body), encoding: .utf8)!
        return """
            {"id":"\(emailDraftID)","thread_id":"\(emailThreadID)","account_id":"work","revision":\(revision),"source_revision":1,"provider_thread_id":"provider-thread","send_tool_name":"mcp.gmail_work_send.send_message","to":["alex@example.test"],"cc":[],"bcc":[],"subject":"Re: Board agenda","body":\(escapedBody),"status":"\(status)","stale":false,"run_id":"\(emailRunID)","approval_id":"\(emailApprovalID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","updated_at":"2026-09-11T00:00:00Z"}
            """
    }
    /// Projects confirmed mailbox state separately from a deliberately observable pending archive operation.
    private static var emailThreadJSON: String {
        let (dismissedRevision, archived, target, reads) = emailLock.withLock {
            (emailHandled ? "1" : "null", emailArchived, emailArchiveTarget, emailArchiveReads)
        }
        let archiveOperation: String
        if let target {
            let failed = ProcessInfo.processInfo.arguments.contains("--ui-testing-email-archive-failure")
            let status = reads < emailArchiveCompletionRead ? "pending" : failed ? "failed" : "completed"
            archiveOperation = "{\"operation_id\":\"\(emailThreadID)\",\"run_id\":\"\(emailRunID)\",\"target_archived\":\(target),\"status\":\"\(status)\",\"error\":null}"
        } else { archiveOperation = "null" }
        let readerFields: String
        if ProcessInfo.processInfo.arguments.contains("--ui-testing-email-reader") {
            let original = "Research and discovery\n<https://example.com/article>\n\nBy Jane Writer\n\nA careful reading starts with clear evidence.\n\nUnsubscribe\n<https://example.com/leave>"
            let reader = "## Research and discovery\n\nBy Jane Writer\n\nA careful reading starts with [clear evidence](https://example.com/article)."
            let sourceJSON = String(data: try! JSONEncoder().encode(original), encoding: .utf8)!
            let readerJSON = String(data: try! JSONEncoder().encode(reader), encoding: .utf8)!
            readerFields = "\"body\":\(sourceJSON),\"reader_body\":\(readerJSON),"
        } else {
            readerFields = "\"body\":\"Please review the agenda before Friday.\","
        }
        return """
        {"id":"\(emailThreadID)","account_id":"work","subject":"Board agenda","topics":["Board planning","Hiring"],"senders":["alex@example.test"],"updated_at":"2026-09-11T00:00:00Z","revision":1,"summary":"Review the board agenda before Friday.","reason":"A direct request from your board colleague.","needs_reply":true,"draft_id":"\(emailDraftID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","priority":0.95,"complete":true,"messages":[{"id":"message-1","sender":"alex@example.test","to":["owner@work.example"],"cc":[],"subject":"Board agenda","body":"Please review the agenda before Friday.","sent_at":"2026-09-11T00:00:00Z","complete":true,"attachments":[]}],"draft":\(emailDraftJSON)}
        """.replacingOccurrences(of: "\"revision\":1,\"summary\"", with: "\"dismissed_revision\":\(dismissedRevision),\"in_inbox\":\(!archived),\"archive_operation\":\(archiveOperation),\"revision\":1,\"summary\"")
            .replacingOccurrences(of: "\"body\":\"Please review the agenda before Friday.\",", with: readerFields)
    }
    private static var emailApprovalJSON: String {
        let (body, sent) = emailLock.withLock { (emailBody, emailStatus == "sent") }
        let escapedBody = String(data: try! JSONEncoder().encode(body), encoding: .utf8)!
        return """
            {"id":"\(emailApprovalID)","run_id":"\(emailRunID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","status":"\(sent ? "APPROVED" : "PENDING")","tool_name":"mcp.gmail_work_send.send_message","action_summary":"Send the exact reply","arguments":{"thread_id":"provider-thread","to":"alex@example.test","cc":null,"bcc":null,"subject":"Re: Board agenda","body":\(escapedBody)},"risk":"HIGH","policy_reason":"Approval required","expires_at":null,"created_at":"2026-09-11T00:00:00Z","resolved_at":null,"resolved_by":null,"decision":null}
            """
    }

    /// Synthetic mixed Gmail activity exercises expansion without mailbox contents or credentials.
    private static var mixedToolEvents: String {
        var frames = (1...20).map { index in
            let name = index.isMultiple(of: 2)
                ? "mcp.gmail_read.get_thread" : "mcp.gmail_work_read.search_threads"
            let event = index > 14 ? "tool.call.failed" : "tool.call.completed"
            return "id: \(index + 2)\nevent: \(event)\ndata: {\"call_id\":\"gmail-\(index)\",\"name\":\"\(name)\",\"arguments\":{\"query\":\"example \(index)\"},\"result_item\":{\"content\":[{\"type\":\"text\",\"text\":\"Example result \(index)\"}],\"is_error\":false,\"trust\":\"external_untrusted\"}}\n\n"
        }
        frames.append("id: 23\nevent: assistant.message.completed\ndata: {\"message\":{\"kind\":\"assistant\",\"content\":[{\"type\":\"text\",\"text\":\"Your answer is visible below the tool summary.\"}]}}\n\n")
        frames.append("id: 24\nevent: run.completed\ndata: {\"run_id\":\"\(runID)\"}\n\n")
        return frames.joined()
    }

    private static let reportRunID = "00000000-0000-0000-0000-0000000005A2"
    private static var reportSessionJSON: String {
        """
            {"id":"\(ConversationNavigationUITestFixture.reportSessionID)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Weekday briefing","metadata":{"schedule_id":"\(ConversationNavigationUITestFixture.scheduleID)"},"created_at":"2026-10-07T16:00:00Z","updated_at":"2026-10-07T16:01:46Z","active_run_id":null,"last_run_id":"\(reportRunID)"}
            """
    }

    private static var previousReportSessionJSON: String {
        """
            {"id":"\(ConversationNavigationUITestFixture.previousReportSessionID)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Weekday briefing","metadata":{"schedule_id":"\(ConversationNavigationUITestFixture.scheduleID)"},"created_at":"2026-10-06T16:00:00Z","updated_at":"2026-10-06T16:01:07Z","active_run_id":null,"last_run_id":"00000000-0000-0000-0000-0000000005A4"}
            """
    }

    private static var firstSessionJSON: String {
        sessionJSON(
            id: ConversationNavigationUITestFixture.firstSessionID,
            title: "Historical chat",
            createdAt: "2026-08-14T00:00:00Z",
            updatedAt: "2026-08-14T00:04:00Z"
        )
    }

    private static var secondSessionJSON: String {
        sessionJSON(
            id: ConversationNavigationUITestFixture.secondSessionID,
            title: "Second historical chat",
            createdAt: "2026-08-13T00:00:00Z",
            updatedAt: "2026-08-13T00:04:00Z"
        )
    }

    /// A Milestone 29 index always carries `folder_id`, null included; the
    /// older-server index of every other journey omits the key.
    private static func sessionJSON(id: String, title: String, createdAt: String, updatedAt: String) -> String {
        var folderField = ""
        if foldersEnabled {
            let folder = folderLock.withLock { sessionFolders[id] }
            folderField = ",\"folder_id\":" + (folder.map { "\"\($0)\"" } ?? "null")
        }
        let bound = taskGrantJourney && id == ConversationNavigationUITestFixture.firstSessionID
        let metadata = bound ? "{\"browser_profile_id\":\"\(browserProfileID)\"}" : "{}"
        let activeRun = bound ? "\"\(taskRunID)\"" : "null"
        return """
            {"id":"\(id)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"\(title)","metadata":\(metadata),"created_at":"\(createdAt)","updated_at":"\(updatedAt)","active_run_id":\(activeRun),"last_run_id":\(activeRun)\(folderField)}
            """
    }

    private static func folderJSON(id: String, name: String) -> String {
        let count = folderLock.withLock { sessionFolders.values.filter { $0 == id }.count }
        return """
            {"id":"\(id)","name":"\(name)","thread_count":\(count),"created_at":"2026-09-16T12:00:00Z","updated_at":"2026-09-16T12:00:00Z"}
            """
    }

    private static var folderItemsJSON: String {
        let (deleted, name, created) = folderLock.withLock { (folderDeleted, folderName, createdFolderName) }
        var items: [String] = []
        if !deleted { items.append(folderJSON(id: ConversationNavigationUITestFixture.folderID, name: name)) }
        items.append(folderJSON(id: ConversationNavigationUITestFixture.workFolderID, name: "Work"))
        if let created { items.append(folderJSON(id: ConversationNavigationUITestFixture.proposedFolderID, name: created)) }
        return items.joined(separator: ",")
    }

    private static func proposalJSON(state: String, resultingFolderID: String?) -> String {
        let resulting = resultingFolderID.map { "\"\($0)\"" } ?? "null"
        let members = ConversationNavigationUITestFixture.proposalMemberIDs.map { "\"\($0)\"" }.joined(separator: ",")
        return """
            {"id":"\(ConversationNavigationUITestFixture.proposalID)","kind":"new_folder","proposed_name":"Lisbon Trip","target_folder_id":null,"member_session_ids":[\(members)],"rationale":null,"derivation":"lexical","state":"\(state)","withdrawal_reason":null,"resulting_folder_id":\(resulting),"created_at":"2026-09-16T12:00:00Z","resolved_at":null}
            """
    }

    /// After the Lisbon folder is declined, the next pass proposes filing its
    /// travel conversations in Travel instead.
    private static let followUpProposalJSON = """
        {"id":"\(ConversationNavigationUITestFixture.followUpProposalID)","kind":"add_to_folder","proposed_name":null,"target_folder_id":"\(ConversationNavigationUITestFixture.folderID)","member_session_ids":["\(ConversationNavigationUITestFixture.flightsSessionID)","\(ConversationNavigationUITestFixture.hotelSessionID)"],"rationale":null,"derivation":"lexical","state":"proposed","withdrawal_reason":null,"resulting_folder_id":null,"created_at":"2026-09-16T12:15:00Z","resolved_at":null}
        """

    private static var folderJourneySessionsJSON: String {
        [
            (ConversationNavigationUITestFixture.planningSessionID, "Quarterly planning notes", "2026-08-12"),
            (ConversationNavigationUITestFixture.flightsSessionID, "Flights to Lisbon in October", "2026-08-11"),
            (ConversationNavigationUITestFixture.hotelSessionID, "Alfama hotel shortlist", "2026-08-10"),
            (ConversationNavigationUITestFixture.sintraSessionID, "Day trip to Sintra and Cascais", "2026-08-09"),
        ]
        .map { id, title, day in
            sessionJSON(id: id, title: title, createdAt: "\(day)T00:00:00Z", updatedAt: "\(day)T00:04:00Z")
        }
        .joined(separator: ",")
    }

    private static let personJSON = """
        {"id":"00000000-0000-0000-0000-000000000777","revision":1,"display_name":"Maya","state":"active","pinned":false,"sensitivity":"sensitive","support_ids":[]}
        """

    /// A directory as long as a real one, paged at the client's limit of 50.
    static let peopleDirectoryArgument = "--ui-testing-people-directory"

    private static func directoryPersonJSON(_ number: Int) -> String {
        """
        {"id":"00000000-0000-0000-0000-0000000C\(String(format: "%04d", number))","revision":1,"display_name":"Contact \(String(format: "%02d", number))","state":"provisional","pinned":false,"sensitivity":"sensitive","support_ids":[]}
        """
    }

    private static let secondPersonJSON = personJSON
        .replacingOccurrences(of: "000777", with: "000782")
        .replacingOccurrences(of: "Maya", with: "Maya Chen")

    /// A second, distinct profile, so choosing another person has visible
    /// content to replace. It fills every section of the profile.
    private static let secondProfileJSON = """
        {"person":\(secondPersonJSON),\
        "aliases":[\(secondAliasesJSON)],\
        "relationships":[\(secondRelationshipsJSON)],\
        "history":[\(secondHistoryJSON)],\
        "commitments":[\(secondCommitmentsJSON)],\
        "facts":[\(secondFactJSON)],"fact_revisions":{"00000000-0000-0000-0000-0000000007a5":1},\
        "related_labels":{"00000000-0000-0000-0000-000000000777":"Maya"},\
        "merge_suggestions":[\(secondSuggestionJSON)],\
        "automatic_merges":[{"operation_id":"00000000-0000-0000-0000-0000000007B3","revision":1,"merged":\(mergedPersonJSON),"merged_at":"2026-09-20T12:00:00Z"}],\
        "truncated":false,"coverage":"Email analyzed: recent 90 days; earlier Chat history not imported."}
        """

    private static let secondAliasesJSON = """
        {"id":"00000000-0000-0000-0000-0000000007A1","revision":1,"value":"maya.chen@example.com","identifier_kind":"email","verification":"channel_observed","context":"work","valid_to":null,"support_ids":[]},\
        {"id":"00000000-0000-0000-0000-0000000007A7","revision":1,"value":"+1 415 555 0142","identifier_kind":"phone","verification":"owner_confirmed","context":"mobile","valid_to":null,"support_ids":[]}
        """

    private static let secondRelationshipsJSON = """
        {"id":"00000000-0000-0000-0000-0000000007A2","revision":1,"subject":{"kind":"person","id":"00000000-0000-0000-0000-000000000782"},"object":{"kind":"owner"},"predicate":"colleague","qualifier":"Design review team","valid_from":"2025-03-01T00:00:00Z","precision":"month","source_timezone":"America/Los_Angeles","support_ids":[]},\
        {"id":"00000000-0000-0000-0000-0000000007A6","revision":1,"subject":{"kind":"person","id":"00000000-0000-0000-0000-000000000782"},"object":{"kind":"person","id":"00000000-0000-0000-0000-000000000777"},"predicate":"friend","qualifier":"","precision":"unknown","support_ids":[]}
        """

    private static let secondHistoryJSON = """
        {"id":"00000000-0000-0000-0000-0000000007A3","revision":1,"channel":"email","interaction_kind":"exchange","attribution":"observed","direction":"incoming","summary":"Shared the Q3 roadmap draft and asked for comments by Friday.","occurred_at":"2026-09-18T16:30:00Z","precision":"instant","source_timezone":"America/Los_Angeles","support_ids":[],"participants":[]},\
        {"id":"00000000-0000-0000-0000-0000000007A8","revision":1,"channel":"email","interaction_kind":"exchange","attribution":"observed","direction":"outgoing","summary":"Sent email","occurred_at":"2026-09-12T23:02:00Z","precision":"instant","source_timezone":"America/Los_Angeles","support_ids":[],"participants":[]},\
        {"id":"00000000-0000-0000-0000-0000000007A9","revision":1,"channel":"chat","interaction_kind":"meeting","attribution":"owner_reported","direction":"reported","summary":"Met for coffee to plan the offsite.","occurred_at":"2026-08-28T00:00:00Z","precision":"day","source_timezone":"America/Los_Angeles","support_ids":[],"participants":[]}
        """

    private static let secondCommitmentsJSON = """
        {"id":"00000000-0000-0000-0000-0000000007A4","revision":1,"debtor":{"kind":"owner"},"beneficiary":{"kind":"person","id":"00000000-0000-0000-0000-000000000782"},"description":"Send feedback on the Q3 roadmap draft","state":"open","due_at":"2026-09-26T00:00:00Z","due_precision":"day","source_timezone":"America/Los_Angeles","support_ids":[]},\
        {"id":"00000000-0000-0000-0000-0000000007B0","revision":1,"debtor":{"kind":"person","id":"00000000-0000-0000-0000-000000000782"},"beneficiary":{"kind":"owner"},"description":"Share the offsite budget","state":"uncertain","due_at":null,"due_precision":null,"support_ids":[]}
        """

    private static let mergedPersonJSON = personJSON
        .replacingOccurrences(of: "000777", with: "0007B1")
        .replacingOccurrences(of: "Maya", with: "Maya C.")
        .replacingOccurrences(of: "\"active\"", with: "\"merged\"")

    private static let secondSuggestionJSON = """
        {"id":"00000000-0000-0000-0000-0000000007B2","revision":1,"source":\(secondPersonJSON),"target":\(personJSON.replacingOccurrences(of: "000777", with: "0007B4").replacingOccurrences(of: "Maya", with: "M. Chen")),"reason":"nickname","family_name":false,"state":"open"}
        """

    private static let secondFactJSON = memoryJSON
        .replacingOccurrences(of: ConversationNavigationUITestFixture.memoryID, with: "00000000-0000-0000-0000-0000000007A5")
        .replacingOccurrences(of: "\"subject\":\"the user\"", with: "\"subject\":\"Maya Chen\"")
        .replacingOccurrences(of: "The user prefers dark mode.", with: "Maya Chen leads the design review team.")

    private static func synthesisResponse(_ request: URLRequest) -> (Int, String) {
        memoryLock.withLock {
            let args = ProcessInfo.processInfo.arguments
            let missing = (404, #"{"error":{"code":"not_found","message":"Not found","details":{},"request_id":"fixture"}}"#)
            guard args.contains("--ui-testing-synthesis") else { return missing }
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            guard query.contains(URLQueryItem(name: "ceiling", value: "restricted")) else { return (400, "") }
            var operation = try! JSONSerialization.jsonObject(with: Data(synthesisOperationJSON.utf8)) as! [String: Any]
            var summary = try! JSONSerialization.jsonObject(with: Data(synthesisSummaryJSON.utf8)) as! [String: Any]
            let merge = args.contains("--ui-testing-synthesis-merge")
            let hidden = args.contains("--ui-testing-synthesis-hidden")
            operation["kind"] = merge ? "merge" : "summary"
            operation["reason"] = merge ? "equivalent" : "summarized"
            if args.contains("--ui-testing-synthesis-connection") {
                operation["kind"] = "hypothesis"; operation["reason"] = "inferred"
                summary["record_kind"] = "hypothesis"
                var content = summary["content"] as! [String: Any]
                content["statement"] = "User may prefer calm environments."
                content["subject"] = "Tentative connection"
                content["authority"] = "inferred"; content["confidence"] = 0.35
                let sources = (summary["sources"] as! [[String: Any]]).map { source in
                    var value = source; value["omitted"] = false; return value
                }
                content["clauses"] = [["text": "User may prefer calm environments.", "source_ids": sources.map { $0["belief_id"] as! String }]]
                summary["sources"] = sources; operation["sources"] = sources
                summary["content"] = content
                var operationContent = operation["content"] as! [String: Any]
                operationContent["statement"] = content["statement"]
                operationContent["subject"] = content["subject"]
                operation["content"] = operationContent
            }
            if request.httpMethod == "POST" && request.url!.path.hasSuffix("/undo") {
                guard request.value(forHTTPHeaderField: "Idempotency-Key") != nil else { return (400, "") }
                synthesisUndone = true
            } else if request.httpMethod == "POST" { synthesisReviewed = true }
            else if request.httpMethod == "DELETE" { synthesisDeleted = true; return (204, "") }
            if hidden || synthesisDeleted {
                operation["content"] = NSNull(); operation["sources"] = []; operation["state"] = "invalidated"
            }
            if synthesisUndone { operation["state"] = "undone"; operation["revision"] = 2 }
            summary["flagged_for_review"] = !synthesisReviewed
            func encoded(_ object: Any) -> String { String(decoding: try! JSONSerialization.data(withJSONObject: object), as: UTF8.self) }
            if request.url!.path == "/v1/memory-reconsolidations" {
                let kind = query.first { $0.name == "kind" }?.value
                let state = query.first { $0.name == "state" }?.value
                let matches = (kind == nil || kind == operation["kind"] as? String) && (state == nil || state == operation["state"] as? String)
                return (200, encoded(["items": matches ? [operation] : [], "next_cursor": NSNull()]))
            }
            if request.url!.path.hasPrefix("/v1/memory-reconsolidations/") { return (200, encoded(operation)) }
            if request.url!.path.contains("00000000-0000-0000-0000-000000003202") { return synthesisDeleted ? missing : (200, encoded(summary)) }
            return missing
        }
    }
    private static let synthesisOperationJSON = #"{"id":"00000000-0000-0000-0000-000000003201","kind":"summary","state":"committed","revision":1,"reason":"summarized","policy":"reconsolidation@1","model_identity":"extractive@1","created_at":"2026-10-06T01:00:00Z","committed_at":"2026-10-06T01:00:00Z","content":{"memory_id":"00000000-0000-0000-0000-000000003202","subject":"the user","statement":"The user prefers dark mode.","clauses":[{"text":"The user prefers dark mode.","source_ids":["00000000-0000-0000-0000-000000000321"]}]},"sources":[{"belief_id":"00000000-0000-0000-0000-000000000321","content_revision":1,"subject":"the user","statement":"The user prefers dark mode.","session_id":"00000000-0000-0000-0000-000000000123","event_ids":[10,11],"omitted":false},{"belief_id":"00000000-0000-0000-0000-000000003203","content_revision":1,"subject":"the user","statement":"The user prefers quiet mornings.","session_id":"00000000-0000-0000-0000-000000000124","event_ids":[10,11],"omitted":true}]}"#
    private static let synthesisSummaryJSON = #"{"id":"00000000-0000-0000-0000-000000003202","record_kind":"summary","operation_id":"00000000-0000-0000-0000-000000003201","revision":1,"status":"active","flagged_for_review":true,"created_at":"2026-10-06T01:00:00Z","updated_at":"2026-10-06T01:00:00Z","content":{"subject":"the user","statement":"The user prefers dark mode.","clauses":[{"text":"The user prefers dark mode.","source_ids":["00000000-0000-0000-0000-000000000321"]}],"belief_types":["preference"],"scope":"user","portability":"portable","sensitivity":"restricted","authority":"user","confidence":0.8,"last_evidence_at":"2026-09-01T00:00:00Z","valid_from":"2026-09-01T00:00:00Z","expires_at":null},"sources":[{"belief_id":"00000000-0000-0000-0000-000000000321","content_revision":1,"subject":"the user","statement":"The user prefers dark mode.","session_id":"00000000-0000-0000-0000-000000000123","event_ids":[10,11],"omitted":false},{"belief_id":"00000000-0000-0000-0000-000000003203","content_revision":1,"subject":"the user","statement":"The user prefers quiet mornings.","session_id":"00000000-0000-0000-0000-000000000124","event_ids":[10,11],"omitted":true}]}"#

    private static let memoryJSON = """
        {"id":"\(ConversationNavigationUITestFixture.memoryID)","subject":"the user","statement":"The user prefers dark mode.","belief_type":"preference","claim_kind":"preference","derivation":"direct","longevity":"durable","status":"active","polarity":"assert","scope":"session","portability":"portable","authority":"user","sensitivity":"restricted","confidence":0.87,"corroboration_count":3,"flagged_for_review":false,"conflicts_with":[],"superseded_by":null,"source_session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","source_event_ids":[10,11],"formation_run_id":"00000000-0000-0000-0000-000000000900","consolidation_policy_version":"formation@1","origin_scopes":["session"],"valid_from":"2026-08-01T00:00:00Z","valid_to":null,"expires_at":null,"last_evidence_at":"2026-08-15T00:00:00Z","last_used_at":null,"last_reinforced_at":"2026-08-15T00:00:00Z","created_at":"2026-07-01T00:00:00Z","updated_at":"2026-08-20T00:00:00Z"}
        """

    private static let scheduleSummaryJSON = """
        {"id":"\(ConversationNavigationUITestFixture.scheduleID)","state":"ACTIVE","pause_reason":null,"current_revision":1,"next_fire_at":"2026-08-30T16:00:00Z","title":"Daily review","instruction_preview":"Preview from the schedule index.","cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"}
        """

    private static let scheduleHistorySummaryJSON = """
        {"id":"\(ConversationNavigationUITestFixture.scheduleHistoryID)","state":"COMPLETED","pause_reason":null,"current_revision":1,"next_fire_at":null,"title":"Finished review","instruction_preview":"Recent completed schedule.","cadence":{"kind":"ONCE","at":"2026-08-28T16:00:00Z"},"created_at":"2026-08-28T00:00:00Z","updated_at":"2026-08-28T16:00:00Z"}
        """

    private static let scheduleDetailJSON = """
        {"schedule":{"id":"\(ConversationNavigationUITestFixture.scheduleID)","tenant_id":"local","principal_id":"principal","state":"ACTIVE","pause_reason":null,"current_revision":1,"next_fire_at":"2026-08-30T16:00:00Z","consecutive_failures":0,"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"},"revision":{"schedule_id":"\(ConversationNavigationUITestFixture.scheduleID)","revision":1,"title":"Daily review","instruction":"Full instruction from the schedule point read.","agent_id":"00000000-0000-0000-0000-000000000655","agent_version":"1","policy_profile":"default","requested_scopes":[],"limits":{"max_steps":12,"max_model_calls":12,"max_tool_calls":24,"max_input_tokens":null,"max_output_tokens":null,"max_cost":"1","deadline_at":null,"synthesis_reserve_steps":0,"synthesis_reserve_model_calls":0,"synthesis_reserve_cost":"0"},"run_timeout_seconds":300,"cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"timezone":"America/Los_Angeles","misfire_grace_seconds":3600,"max_consecutive_failures":1,"created_by_principal_id":"principal","created_at":"2026-08-29T00:00:00Z"},"replayed":false}
        """

    private static let scheduleWebsiteProfileID = "00000000-0000-0000-0000-0000000007aa"

    private static let boundScheduleDetailJSON = """
        {"schedule":{"id":"\(ConversationNavigationUITestFixture.scheduleID)","tenant_id":"local","principal_id":"principal","state":"ACTIVE","pause_reason":null,"current_revision":2,"next_fire_at":"2026-08-30T16:00:00Z","consecutive_failures":0,"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T01:00:00Z"},"revision":{"schedule_id":"\(ConversationNavigationUITestFixture.scheduleID)","revision":2,"title":"Daily review","instruction":"Full instruction from the schedule point read.","agent_id":"00000000-0000-0000-0000-000000000655","agent_version":"1","policy_profile":"default","requested_scopes":["browser.profile.read"],"browser_profile_id":"\(scheduleWebsiteProfileID)","limits":{"max_steps":12,"max_model_calls":12,"max_tool_calls":24,"max_input_tokens":null,"max_output_tokens":null,"max_cost":"1","deadline_at":null,"synthesis_reserve_steps":0,"synthesis_reserve_model_calls":0,"synthesis_reserve_cost":"0"},"run_timeout_seconds":300,"cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"timezone":"America/Los_Angeles","misfire_grace_seconds":3600,"max_consecutive_failures":1,"created_by_principal_id":"principal","created_at":"2026-08-29T01:00:00Z"},"replayed":false}
        """

    private static let browserProfileID = "00000000-0000-0000-0000-000000000789"
    private static let taskRunID = "00000000-0000-0000-0000-0000000007A1"
    private static let taskApprovalID = "00000000-0000-0000-0000-0000000007A2"
    private static let taskGrantID = "00000000-0000-0000-0000-0000000007A3"
    private static var taskRunJSON: String {
        """
        {"id":"\(taskRunID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","parent_run_id":null,"status":"WAITING_FOR_APPROVAL","step_count":2,"model_call_count":2,"tool_call_count":1,"usage":{"input_tokens":0,"output_tokens":0,"cost_usd":"0"},"limits":{"max_steps":40,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-09-25T18:00:00Z","updated_at":"2026-09-25T18:00:00Z"}
        """
    }
    /// The contract fixture's offered approval (T0-3), with this journey's ids.
    private static var taskApprovalJSON: String {
        let resolved = taskGrantLock.withLock { taskGrantResolved }
        let status = resolved ? "APPROVED" : "PENDING"
        if genericCheckpoint {
            let decision = resolved ? "\"approve_once\"" : "null"
            return """
                {"id":"\(taskApprovalID)","run_id":"\(taskRunID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","status":"\(status)","tool_name":"workspace.write_text","action_summary":"Write the approved note","arguments":{"path":"note.txt","text":"Keep these details available"},"risk":"HIGH","policy_reason":"Approval required","expires_at":null,"created_at":"2026-09-25T18:00:00Z","resolved_at":null,"resolved_by":null,"decision":\(decision)}
                """
        }
        let decision = resolved ? "\"approve_for_task\"" : "null"
        let grant = resolved ? "\"\(taskGrantID)\"" : "null"
        let resolvedAt = resolved ? "\"2026-09-25T18:00:30Z\"" : "null"
        let resolvedBy = resolved ? "\"owner\"" : "null"
        return """
            {"id":"\(taskApprovalID)","run_id":"\(taskRunID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","status":"\(status)","tool_name":"browser.act","action_summary":"Click a button on www.duolingo.com/lesson/unit-3","arguments":{"view":"browser.act.v1","described":true,"kind":"click","page_origin":"https://www.duolingo.com","page_path":"/lesson/unit-3","page_title":"Duolingo","element_role":"button","element_name":"el gato","field":"none","consequence":"unknown","refused":false},"argument_digests":{},"risk":"HIGH","policy_reason":"policy.matrix.external_write","expires_at":"2099-09-25T18:15:00Z","created_at":"2026-09-25T18:00:00Z","resolved_at":\(resolvedAt),"resolved_by":\(resolvedBy),"decision":\(decision),"task_grant_offer":{"origin":"https://www.duolingo.com","path_prefix":"/lesson","duration_seconds":1800,"max_actions":200,"max_typed_characters":4096,"action_kinds":["click","type","select","check","press","scroll"],"summary":"Clicks and typing on www.duolingo.com/lesson for 30 minutes, up to 200 actions. Passwords and one-time codes are never typed. Veetbot recognises payments, purchases, subscriptions and trials, account and settings changes, messages and posts, deletions, and signing out by the website's labels, and asks again for those. Stop this at any time."},"task_grant_id":\(grant),"task_grant_not_covered":null}
            """
    }
    private static func taskGrantJSON(status: String) -> String {
        let ended = status == "active" ? "null" : "\"2026-09-25T18:05:00Z\""
        let reason = status == "active" ? "null" : "\"\(status)\""
        return """
            {"id":"\(taskGrantID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","profile_id":"\(browserProfileID)","origin":"https://www.duolingo.com","path_prefix":"/lesson","action_kinds":["click","type","select","check","press","scroll"],"status":"\(status)","end_reason":\(reason),"max_actions":200,"actions_used":0,"max_typed_characters":4096,"typed_characters":0,"created_at":"2099-09-25T18:00:30Z","expires_at":"2099-09-25T18:30:30Z","last_used_at":null,"ended_at":\(ended),"approval_id":"\(taskApprovalID)","approved_by":"owner"}
            """
    }
    private static let authenticationID = "00000000-0000-0000-0000-000000000790"
    private static let runID = "00000000-0000-0000-0000-000000000791"
    private static let browserProfileJSON = """
        {"id":"\(browserProfileID)","allowed_origins":["https://example.org"],"status":"authentication_required","generation":1,"created_at":"2026-08-23T12:00:00Z","updated_at":"2026-08-23T12:00:00Z","last_used_at":null}
        """
}
#endif
