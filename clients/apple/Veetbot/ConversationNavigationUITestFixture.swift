#if DEBUG
import Foundation

enum ConversationNavigationUITestFixture {
    static let launchArgument = "--ui-testing-conversation-navigation"
    static let firstSessionID = "00000000-0000-0000-0000-000000000123"
    static let secondSessionID = "00000000-0000-0000-0000-000000000456"
    static let memoryID = "00000000-0000-0000-0000-000000000321"
    static let scheduleID = "00000000-0000-0000-0000-000000000654"
    static let scheduleHistoryID = "00000000-0000-0000-0000-000000000656"

    @MainActor
    static func makeModelIfRequested() -> ChatViewModel? {
        guard ProcessInfo.processInfo.arguments.contains(launchArgument) else { return nil }
        ConversationNavigationUITestURLProtocol.resetEmail()

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
            body = "{\"items\":[\(included ? Self.emailThreadJSON : "")],\"next_cursor\":null}"
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
            body = """
                {"items":[\(Self.firstSessionJSON),\(Self.secondSessionJSON)],"next_cursor":null}
                """
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
            statusCode = 202
            body =
                "{\"run_id\":\"\(Self.runID)\",\"status\":\"QUEUED\"}"
        case ("GET", "/v1/runs/\(Self.runID)/events"):
            statusCode = 200
            body = """
                id: 1
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
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}

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

    private static let emailThreadID = "00000000-0000-0000-0000-000000000801"
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
        {"id":"\(emailThreadID)","account_id":"work","subject":"Board agenda","senders":["alex@example.test"],"updated_at":"2026-09-11T00:00:00Z","revision":1,"summary":"Review the board agenda before Friday.","reason":"A direct request from your board colleague.","needs_reply":true,"draft_id":"\(emailDraftID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","priority":0.95,"complete":true,"messages":[{"id":"message-1","sender":"alex@example.test","to":["owner@work.example"],"cc":[],"subject":"Board agenda","body":"Please review the agenda before Friday.","sent_at":"2026-09-11T00:00:00Z","complete":true,"attachments":[]}],"draft":\(emailDraftJSON)}
        """.replacingOccurrences(of: "\"revision\":1,\"summary\"", with: "\"dismissed_revision\":\(dismissedRevision),\"in_inbox\":\(!archived),\"archive_operation\":\(archiveOperation),\"revision\":1,\"summary\"")
    }
    private static var emailApprovalJSON: String {
        let (body, sent) = emailLock.withLock { (emailBody, emailStatus == "sent") }
        let escapedBody = String(data: try! JSONEncoder().encode(body), encoding: .utf8)!
        return """
            {"id":"\(emailApprovalID)","run_id":"\(emailRunID)","session_id":"\(ConversationNavigationUITestFixture.firstSessionID)","status":"\(sent ? "APPROVED" : "PENDING")","tool_name":"mcp.gmail_work_send.send_message","action_summary":"Send the exact reply","arguments":{"thread_id":"provider-thread","to":"alex@example.test","cc":null,"bcc":null,"subject":"Re: Board agenda","body":\(escapedBody)},"risk":"HIGH","policy_reason":"Approval required","expires_at":null,"created_at":"2026-09-11T00:00:00Z","resolved_at":null,"resolved_by":null,"decision":null}
            """
    }

    private static let firstSessionJSON = """
        {"id":"\(ConversationNavigationUITestFixture.firstSessionID)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Historical chat","metadata":{},"created_at":"2026-08-14T00:00:00Z","updated_at":"2026-08-14T00:04:00Z","active_run_id":null,"last_run_id":null}
        """

    private static let secondSessionJSON = """
        {"id":"\(ConversationNavigationUITestFixture.secondSessionID)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":"Second historical chat","metadata":{},"created_at":"2026-08-13T00:00:00Z","updated_at":"2026-08-13T00:04:00Z","active_run_id":null,"last_run_id":null}
        """

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
