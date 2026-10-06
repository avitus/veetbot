import Foundation
import Testing

#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

@testable import VeetbotCore

@Suite(.serialized) @MainActor struct ScheduleViewModelTests {
    @Test
    func testReloadAndLoadMoreKeepUniqueRowsAndStopARepeatedCursor() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000721"))
        let secondID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000722"))
        let lock = NSLock()
        var requestedCursors: [String?] = []
        let model = try makeModel { request in
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "cursor" })?.value
            lock.withLock { requestedCursors.append(cursor) }
            if cursor == nil {
                return Self.response(
                    request,
                    body: #"{"items":[\#(Self.summaryJSON(id: firstID))],"next_cursor":"page-2"}"#
                )
            }
            return Self.response(
                request,
                body: #"{"items":[\#(Self.summaryJSON(id: firstID)),\#(Self.summaryJSON(id: secondID)),\#(Self.summaryJSON(id: secondID))],"next_cursor":"page-2"}"#
            )
        }

        await model.reload()
        await model.loadMore()
        await model.loadMore()

        #expect(model.items.map(\.id) == [firstID, secondID])
        #expect(lock.withLock { requestedCursors.count } == 2)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testCurrentAndRecentHistoryUseDisjointServerSideStateFilters() async throws {
        let lock = NSLock()
        var requestedStates: [[String]] = []
        let model = try makeModel { request in
            let states = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?
                .filter { $0.name == "state" }
                .compactMap(\.value) ?? []
            lock.withLock { requestedStates.append(states) }
            return Self.response(request, body: #"{"items":[],"next_cursor":null}"#)
        }

        await model.reload(.current)
        await model.reload(.recentHistory)

        #expect(
            lock.withLock { requestedStates }
                == [
                    [ScheduleStateKind.active.rawValue, ScheduleStateKind.paused.rawValue],
                    [ScheduleStateKind.completed.rawValue, ScheduleStateKind.cancelled.rawValue],
                ]
        )
        #expect(model.section == .recentHistory)
    }

    @Test
    func testLaterPageFailureRetainsRowsAndRetryContinuesThatPage() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000731"))
        let secondID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000732"))
        let lock = NSLock()
        var pageTwoAttempts = 0
        let model = try makeModel { request in
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "cursor" })?.value
            if cursor == nil {
                return Self.response(
                    request,
                    body: #"{"items":[\#(Self.summaryJSON(id: firstID))],"next_cursor":"page-2"}"#
                )
            }
            let attempt = lock.withLock {
                pageTwoAttempts += 1
                return pageTwoAttempts
            }
            if attempt == 1 {
                return Self.response(
                    request,
                    statusCode: 503,
                    body: #"{"error":{"code":"service_unavailable","message":"Try again.","details":{},"request_id":"page-2"}}"#
                )
            }
            return Self.response(
                request,
                body: #"{"items":[\#(Self.summaryJSON(id: secondID))],"next_cursor":null}"#
            )
        }

        await model.reload()
        await model.loadMore()

        #expect(model.items.map(\.id) == [firstID])
        #expect(model.errorMessage == "Try again.")

        await model.retry()

        #expect(model.items.map(\.id) == [firstID, secondID])
        #expect(model.errorMessage == nil)
    }

    @Test
    func testDetailUsesPointReadAndCanRetryWithoutReplacingTheList() async throws {
        let scheduleID = try #require(
            UUID(uuidString: "00000000-0000-0000-0000-000000000741")
        )
        let lock = NSLock()
        var detailAttempts = 0
        let model = try makeModel { request in
            if request.url?.path == "/v1/schedules" {
                return Self.response(
                    request,
                    body: #"{"items":[\#(Self.summaryJSON(id: scheduleID))],"next_cursor":null}"#
                )
            }
            let attempt = lock.withLock {
                detailAttempts += 1
                return detailAttempts
            }
            if attempt == 1 {
                return Self.response(
                    request,
                    statusCode: 503,
                    body: #"{"error":{"code":"service_unavailable","message":"Detail unavailable.","details":{},"request_id":"detail"}}"#
                )
            }
            return Self.response(request, body: Self.detailJSON(id: scheduleID))
        }

        await model.reload()
        await model.loadDetail(scheduleID)

        #expect(model.items.map(\.id) == [scheduleID])
        #expect(model.detailRecords[scheduleID] == nil)
        #expect(model.detailError(for: scheduleID) == "Detail unavailable.")

        await model.retryDetail(scheduleID)

        #expect(model.detailRecords[scheduleID]?.revision.instruction == "Full instruction")
        #expect(model.detailError(for: scheduleID) == nil)
        #expect(model.items.map(\.id) == [scheduleID])
    }

    @Test
    func testMissingConnectionAndOldServerProduceUnavailableState() async throws {
        let missingConnection = ScheduleViewModel(makeAPIClient: { nil })
        await missingConnection.reload()
        #expect(missingConnection.unavailable)

        let oldServer = try makeModel { request in
            Self.response(
                request,
                statusCode: 404,
                body: #"{"error":{"code":"not_found","message":"Not found.","details":{},"request_id":"old"}}"#
            )
        }
        await oldServer.reload()
        #expect(oldServer.unavailable)
        #expect(oldServer.items.isEmpty)
    }

    @Test
    func testChoosingASignInSavesItAndShowsTheServerResult() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000751"))
        let profileID = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000007AA"))
        let lock = NSLock()
        var patched = false
        let model = try makeModel { request in
            if request.httpMethod == "PATCH" {
                lock.withLock { patched = true }
                return Self.response(
                    request,
                    body: Self.detailJSON(id: scheduleID, revision: 2, profile: profileID)
                )
            }
            return Self.response(request, body: Self.detailJSON(id: scheduleID))
        }
        await model.loadDetail(scheduleID)
        #expect(model.detailRecords[scheduleID]?.revision.browserProfileID == nil)

        await model.setWebsiteAccess(scheduleID, browserProfileID: profileID)

        #expect(lock.withLock { patched })
        #expect(model.detailRecords[scheduleID]?.revision.browserProfileID == profileID)
        #expect(model.detailRecords[scheduleID]?.schedule.currentRevision == 2)
        #expect(model.websiteAccessError(for: scheduleID) == nil)
        #expect(!model.isSavingWebsiteAccess(scheduleID))
    }

    @Test
    func testARejectedSignInKeepsTheServerBindingAndSaysWhatToDo() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000752"))
        let model = try makeModel { request in
            if request.httpMethod == "PATCH" {
                return Self.response(
                    request,
                    statusCode: 422,
                    body: #"{"error":{"code":"schedule_validation_error","message":"browser profile is not ready","details":{"reason":"schedule.browser_profile_unavailable"},"request_id":"rejected"}}"#
                )
            }
            return Self.response(request, body: Self.detailJSON(id: scheduleID))
        }
        await model.loadDetail(scheduleID)

        await model.setWebsiteAccess(scheduleID, browserProfileID: UUID())

        #expect(model.detailRecords[scheduleID]?.revision.browserProfileID == nil)
        #expect(model.detailRecords[scheduleID]?.schedule.currentRevision == 1)
        #expect(
            model.websiteAccessError(for: scheduleID)
                == "That sign-in isn't ready. Sign in again in Website Access, then choose it here."
        )
        #expect(!model.isSavingWebsiteAccess(scheduleID))
    }

    @Test
    func testAFailedSaveShowsTheServerMessageUntilALaterSaveSucceeds() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000753"))
        let profileID = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000007AA"))
        let lock = NSLock()
        var patches = 0
        let model = try makeModel { request in
            if request.httpMethod == "PATCH" {
                let attempt = lock.withLock {
                    patches += 1
                    return patches
                }
                if attempt == 1 {
                    return Self.response(
                        request,
                        statusCode: 403,
                        body: #"{"error":{"code":"authorization_error","message":"This connection may not change schedules.","details":{},"request_id":"denied"}}"#
                    )
                }
                return Self.response(
                    request,
                    body: Self.detailJSON(id: scheduleID, revision: 2, profile: profileID)
                )
            }
            return Self.response(request, body: Self.detailJSON(id: scheduleID))
        }
        await model.loadDetail(scheduleID)

        await model.setWebsiteAccess(scheduleID, browserProfileID: profileID)
        #expect(
            model.websiteAccessError(for: scheduleID)
                == "This connection may not change schedules."
        )

        await model.setWebsiteAccess(scheduleID, browserProfileID: profileID)
        #expect(model.websiteAccessError(for: scheduleID) == nil)
        #expect(model.detailRecords[scheduleID]?.revision.browserProfileID == profileID)
    }

    @Test
    func testWithoutAConnectionTheChangeExplainsWhy() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000754"))
        let model = ScheduleViewModel(makeAPIClient: { nil })

        await model.setWebsiteAccess(scheduleID, browserProfileID: UUID())

        #expect(
            model.websiteAccessError(for: scheduleID)
                == "Connect to a Veetbot server to change website access."
        )
    }

    @Test
    func testWebsiteAccessProfilesLoadEveryPage() async throws {
        let firstID = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000007A1"))
        let secondID = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000007A2"))
        let model = try makeModel { request in
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first(where: { $0.name == "cursor" })?.value
            if cursor == nil {
                return Self.response(
                    request,
                    body: #"{"items":[\#(Self.profileJSON(id: firstID, origin: "https://x.com", status: "ready"))],"next_cursor":"profiles-2"}"#
                )
            }
            return Self.response(
                request,
                body: #"{"items":[\#(Self.profileJSON(id: secondID, origin: "https://www.duolingo.com", status: "authentication_required"))],"next_cursor":null}"#
            )
        }

        await model.loadWebsiteAccessProfiles()

        #expect(model.websiteAccessProfiles?.map(\.id) == [firstID, secondID])
        #expect(model.websiteAccessProfilesError == nil)
    }

    @Test
    func testSignInsThatCannotLoadAreReportedInsteadOfShownAsNone() async throws {
        let model = try makeModel { request in
            Self.response(
                request,
                statusCode: 403,
                body: #"{"error":{"code":"authorization_error","message":"Missing browser.profile.read.","details":{},"request_id":"profiles"}}"#
            )
        }

        await model.loadWebsiteAccessProfiles()

        #expect(model.websiteAccessProfiles == nil)
        #expect(model.websiteAccessProfilesError == "Missing browser.profile.read.")
    }

    @Test
    func testOptionsOfferReadySignInsAndKeepTheCurrentOneVisible() throws {
        let ready = try Self.profile("00000000-0000-0000-0000-0000000007B1", ["https://x.com"], .ready)
        let loggedOut = try Self.profile(
            "00000000-0000-0000-0000-0000000007B2", ["https://www.duolingo.com"],
            .authenticationRequired
        )
        let twoSites = try Self.profile(
            "00000000-0000-0000-0000-0000000007B3",
            ["https://mail.example.com", "https://accounts.example.com"], .ready
        )
        let revoked = try Self.profile(
            "00000000-0000-0000-0000-0000000007B4", ["https://old.example.com"], .revoked
        )
        let missingID = try #require(UUID(uuidString: "00000000-0000-0000-0000-0000000007B5"))
        let profiles = [ready, loggedOut, twoSites, revoked]

        #expect(
            scheduleWebsiteAccessOptions(profiles: profiles, boundProfileID: nil) == [
                ScheduleWebsiteAccessOption(id: ready.id, label: "x.com"),
                ScheduleWebsiteAccessOption(
                    id: twoSites.id, label: "mail.example.com, accounts.example.com"
                ),
            ]
        )
        #expect(
            scheduleWebsiteAccessOptions(profiles: profiles, boundProfileID: loggedOut.id) == [
                ScheduleWebsiteAccessOption(id: ready.id, label: "x.com"),
                ScheduleWebsiteAccessOption(
                    id: loggedOut.id, label: "www.duolingo.com · Login required"
                ),
                ScheduleWebsiteAccessOption(
                    id: twoSites.id, label: "mail.example.com, accounts.example.com"
                ),
            ]
        )
        #expect(
            scheduleWebsiteAccessOptions(profiles: profiles, boundProfileID: missingID) == [
                ScheduleWebsiteAccessOption(id: ready.id, label: "x.com"),
                ScheduleWebsiteAccessOption(
                    id: twoSites.id, label: "mail.example.com, accounts.example.com"
                ),
                ScheduleWebsiteAccessOption(id: missingID, label: "Unavailable sign-in"),
            ]
        )
    }

    @Test
    func testOnlyActiveAndPausedSchedulesOfferTheChange() {
        #expect(scheduleAllowsWebsiteAccessChange(.active))
        #expect(scheduleAllowsWebsiteAccessChange(.paused))
        #expect(!scheduleAllowsWebsiteAccessChange(.completed))
        #expect(!scheduleAllowsWebsiteAccessChange(.cancelled))
        #expect(!scheduleAllowsWebsiteAccessChange(nil))
    }

    private func makeModel(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> ScheduleViewModel {
        let configuration = try ConnectionConfiguration(
            baseURLString: "https://schedule-view-model.invalid"
        )
        let handlerID = ScheduleViewModelURLProtocol.register(handler)
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        sessionConfiguration.httpAdditionalHeaders = [
            ScheduleViewModelURLProtocol.handlerHeader: handlerID
        ]
        sessionConfiguration.protocolClasses = [ScheduleViewModelURLProtocol.self]
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: "valid"),
            session: URLSession(configuration: sessionConfiguration)
        )
        let client = VeetbotAPIClient(transport: transport)
        return ScheduleViewModel(makeAPIClient: { client })
    }

    nonisolated private static func response(
        _ request: URLRequest,
        statusCode: Int = 200,
        body: String
    ) -> (HTTPURLResponse, Data) {
        let response = HTTPURLResponse(
            url: request.url!,
            statusCode: statusCode,
            httpVersion: nil,
            headerFields: ["Content-Type": "application/json"]
        )!
        return (response, Data(body.utf8))
    }

    nonisolated private static func summaryJSON(id: UUID) -> String {
        #"{"id":"\#(id.uuidString)","state":"ACTIVE","pause_reason":null,"current_revision":1,"next_fire_at":"2026-08-30T16:00:00Z","title":"Daily review","instruction_preview":"Preview","cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"}"#
    }

    nonisolated private static func detailJSON(
        id: UUID,
        revision: Int = 1,
        profile: UUID? = nil
    ) -> String {
        let binding = profile.map { #""\#($0.uuidString.lowercased())""# } ?? "null"
        let scopes = profile == nil ? "[]" : #"["browser.profile.read"]"#
        return #"{"schedule":{"id":"\#(id.uuidString)","tenant_id":"local","principal_id":"principal","state":"ACTIVE","pause_reason":null,"current_revision":\#(revision),"next_fire_at":"2026-08-30T16:00:00Z","consecutive_failures":0,"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"},"revision":{"schedule_id":"\#(id.uuidString)","revision":\#(revision),"title":"Daily review","instruction":"Full instruction","agent_id":"00000000-0000-0000-0000-000000000742","agent_version":"1","policy_profile":"default","requested_scopes":\#(scopes),"browser_profile_id":\#(binding),"limits":{"max_steps":12,"max_model_calls":12,"max_tool_calls":24,"max_input_tokens":null,"max_output_tokens":null,"max_cost":"1","deadline_at":null,"synthesis_reserve_steps":0,"synthesis_reserve_model_calls":0,"synthesis_reserve_cost":"0"},"run_timeout_seconds":300,"cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"timezone":"America/Los_Angeles","misfire_grace_seconds":3600,"max_consecutive_failures":1,"created_by_principal_id":"principal","created_at":"2026-08-29T00:00:00Z"},"replayed":false}"#
    }

    nonisolated private static func profileJSON(id: UUID, origin: String, status: String) -> String {
        #"{"id":"\#(id.uuidString.lowercased())","allowed_origins":["\#(origin)"],"status":"\#(status)","generation":1,"created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-01T00:00:00Z","last_used_at":null}"#
    }

    private static func profile(
        _ id: String,
        _ origins: [String],
        _ status: BrowserProfileStatus
    ) throws -> BrowserProfileView {
        let date = Date(timeIntervalSince1970: 1_790_000_000)
        return BrowserProfileView(
            id: try #require(UUID(uuidString: id)),
            allowedOrigins: origins,
            status: status,
            generation: 1,
            createdAt: date,
            updatedAt: date,
            lastUsedAt: nil
        )
    }
}

private final class ScheduleViewModelURLProtocol: URLProtocol {
    static let handlerHeader = "X-Veetbot-Schedule-Test-Handler-ID"
    private static let store = ScheduleViewModelURLProtocolHandlerStore()

    static func register(
        _ handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) -> String {
        store.register(handler)
    }

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard
            let handlerID = request.value(forHTTPHeaderField: Self.handlerHeader),
            let handler = Self.store.handler(for: handlerID)
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

private final class ScheduleViewModelURLProtocolHandlerStore: @unchecked Sendable {
    typealias Handler = (URLRequest) throws -> (HTTPURLResponse, Data)
    private let lock = NSLock()
    private var handlers: [String: Handler] = [:]

    func register(_ handler: @escaping Handler) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }

    func handler(for id: String) -> Handler? {
        lock.withLock { handlers[id] }
    }
}
