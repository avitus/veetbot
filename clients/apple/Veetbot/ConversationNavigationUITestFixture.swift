#if DEBUG
import Foundation
#if os(macOS)
import AppKit
#endif

enum ConversationNavigationUITestFixture {
    static let launchArgument = "--ui-testing-conversation-navigation"
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
        switch (request.httpMethod, url.path) {
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
            body = Self.emailAccountsJSON
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
            body = "{\"items\":[\(items)],\"next_cursor\":null}"
        case ("GET", "/v1/email/threads/00000000-0000-0000-0000-000000000899"):
            statusCode = 200
            body = Self.nextEmailThreadJSON
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
            statusCode = 202
            body = "{\"operation_id\":\"\(Self.emailThreadID)\",\"run_id\":\"\(Self.emailRunID)\",\"status\":\"COMPLETED\",\"replayed\":false}"
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
            body = """
                {"items":[\(Self.firstSessionJSON),\(Self.secondSessionJSON)\(folderSessions)],"next_cursor":null}
                """
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
            body = """
                {"items":[
                  {"sequence":1,"role":"user","content":[{"type":"text","text":"Historical question"}]},
                  {"sequence":2,"role":"assistant","content":[{"type":"text","text":"Historical answer loaded"}]}
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
                if let target { Self.sessionFolders[sessionID] = target } else { Self.sessionFolders.removeValue(forKey: sessionID) }
            }
            statusCode = 200
            body = sessionID == ConversationNavigationUITestFixture.firstSessionID ? Self.firstSessionJSON : Self.secondSessionJSON
        case ("DELETE", let path) where path.hasPrefix("/v1/memories/"):
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
            body = """
                {"items":[\(Self.memoryJSON)],"next_cursor":null}
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
            body = Self.scheduleDetailJSON
        case ("GET", "/v1/settings/models"):
            statusCode = 200
            body = Self.modelSettingsLock.withLock { Self.modelSettingsJSON }
        case ("PUT", "/v1/settings/models"):
            let result = Self.updateModelSettings(requestJSON())
            statusCode = result.0
            body = result.1
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
        let deliver = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: Data(body.utf8))
            self.client?.urlProtocolDidFinishLoading(self)
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
        return """
        {"id":"\(emailThreadID)","account_id":"work","subject":"Board agenda","topics":["Board planning","Hiring"],"senders":["alex@example.test"],"updated_at":"2026-09-11T00:00:00Z","revision":1,"summary":"Review the board agenda before Friday.","reason":"A direct request from your board colleague.","needs_reply":true,"draft_id":"\(emailDraftID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","priority":0.95,"complete":true,"messages":[{"id":"message-1","sender":"alex@example.test","to":["owner@work.example"],"cc":[],"subject":"Board agenda","body":"Please review the agenda before Friday.","sent_at":"2026-09-11T00:00:00Z","complete":true,"attachments":[]}],"draft":\(emailDraftJSON)}
        """.replacingOccurrences(of: "\"revision\":1,\"summary\"", with: "\"dismissed_revision\":\(dismissedRevision),\"in_inbox\":\(!archived),\"archive_operation\":\(archiveOperation),\"revision\":1,\"summary\"")
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
        return """
            {"id":"\(id)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"\(title)","metadata":{},"created_at":"\(createdAt)","updated_at":"\(updatedAt)","active_run_id":null,"last_run_id":null\(folderField)}
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

    private static let browserProfileID = "00000000-0000-0000-0000-000000000789"
    private static let authenticationID = "00000000-0000-0000-0000-000000000790"
    private static let runID = "00000000-0000-0000-0000-000000000791"
    private static let browserProfileJSON = """
        {"id":"\(browserProfileID)","allowed_origins":["https://example.org"],"status":"authentication_required","generation":1,"created_at":"2026-08-23T12:00:00Z","updated_at":"2026-08-23T12:00:00Z","last_used_at":null}
        """
}
#endif
