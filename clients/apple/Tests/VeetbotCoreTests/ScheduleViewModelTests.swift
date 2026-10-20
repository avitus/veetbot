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

    /// ADR-0225 part A: Pause sends the revision the owner saw and confirms
    /// with a toast whose Undo resumes; Resume and Stop answer the same way.
    @Test
    func testPauseResumeAndStopSendTheRevisionAndConfirmWithAToast() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000761"))
        let recorder = ScheduleRequestRecorder()
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
            recorder.append(request)
            let path = request.url!.path
            if path.hasSuffix("/pause") {
                return Self.response(request, body: Self.detailJSON(id: scheduleID, revision: 2, state: "PAUSED"))
            }
            if path.hasSuffix("/resume") {
                return Self.response(request, body: Self.detailJSON(id: scheduleID, revision: 3))
            }
            if request.httpMethod == "DELETE" {
                return Self.response(request, body: Self.detailJSON(id: scheduleID, revision: 4, state: "CANCELLED"))
            }
            return Self.response(request, body: Self.detailJSON(id: scheduleID))
        }
        await model.loadDetail(scheduleID)

        await model.pause(scheduleID)
        let pause = try #require(recorder.snapshot.last)
        #expect(pause.url?.path == "/v1/schedules/\(scheduleID.uuidString)/pause")
        #expect(try Self.body(pause)["expected_revision"] as? Int == 1)
        #expect(model.detailRecords[scheduleID]?.schedule.stateKind == .paused)
        #expect(toasts.current?.text == "Paused. Nothing runs until you resume it.")
        #expect(toasts.current?.undo != nil)

        await toasts.performUndo()
        let resume = try #require(recorder.snapshot.last)
        #expect(resume.url?.path == "/v1/schedules/\(scheduleID.uuidString)/resume")
        #expect(try Self.body(resume)["expected_revision"] as? Int == 2)
        #expect(model.detailRecords[scheduleID]?.schedule.stateKind == .active)
        #expect(toasts.current?.text == "Resumed.")

        await model.stop(scheduleID)
        let stop = try #require(recorder.snapshot.last)
        #expect(stop.httpMethod == "DELETE")
        #expect(URLComponents(url: stop.url!, resolvingAgainstBaseURL: false)?.queryItems
            == [URLQueryItem(name: "expected_revision", value: "3")])
        #expect(model.detailRecords[scheduleID]?.schedule.stateKind == .cancelled)
        #expect(toasts.current?.text == "Stopped. It stays in Recent History for 30 days.")
    }

    /// ADR-0225 part D: the switch sends the whole definition back with only
    /// `notify` changed, so fields this client does not model survive.
    @Test
    func testOnlyNotifyWhenNeededSavesTheWholeDefinitionWithTheNewPreference() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000768"))
        let recorder = ScheduleRequestRecorder()
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
            recorder.append(request)
            if request.httpMethod == "PATCH" {
                return Self.response(request, body: Self.detailJSON(id: scheduleID, revision: 2, notify: "attention_only"))
            }
            return Self.response(request, body: Self.detailJSON(id: scheduleID, notify: "every_report"))
        }
        await model.loadDetail(scheduleID)
        #expect(model.detailRecords[scheduleID]?.revision.notify == "every_report")

        await model.setNotifyOnlyWhenNeeded(scheduleID, true)

        let patch = try #require(recorder.snapshot.last)
        #expect(patch.httpMethod == "PATCH")
        let body = try Self.body(patch)
        #expect(body["expected_revision"] as? Int == 1)
        let definition = try #require(body["definition"] as? [String: Any])
        #expect(definition["notify"] as? String == "attention_only")
        #expect(definition["instruction"] as? String == "Full instruction")
        #expect(definition["revision"] == nil && definition["schedule_id"] == nil)
        #expect(model.detailRecords[scheduleID]?.revision.notify == "attention_only")
        #expect(toasts.current?.text == "Saved. Applies from the next run.")
    }

    /// ADR-0225 part B: the sentence is read into a draft the owner sees
    /// before anything is saved; creating posts the server's own definition.
    @Test
    func testANewScheduleIsReadFirstAndCreatedFromTheServersDefinition() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000771"))
        let recorder = ScheduleRequestRecorder()
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
            recorder.append(request)
            switch (request.httpMethod, request.url!.path) {
            case ("POST", "/v1/schedules/drafts"):
                return Self.response(request, body: Self.draftJSON)
            case ("POST", "/v1/schedules"):
                return Self.response(request, statusCode: 201, body: Self.detailJSON(id: scheduleID))
            default:
                return Self.response(request, body: #"{"items":[\#(Self.summaryJSON(id: scheduleID))],"next_cursor":null}"#)
            }
        }

        await model.readDraft("Every weekday at 7 am, tell me what needs me today.", timezone: "America/Los_Angeles")

        let read = try #require(recorder.snapshot.last)
        #expect(read.url?.path == "/v1/schedules/drafts")
        let body = try Self.body(read)
        #expect(body["text"] as? String == "Every weekday at 7 am, tell me what needs me today.")
        #expect(body["timezone"] as? String == "America/Los_Angeles")
        guard case .ready(let draft) = model.draftState else {
            Issue.record("expected a ready draft, got \(model.draftState)")
            return
        }
        #expect(draft.title == "Weekday briefing")
        #expect(draft.cadence?.kind == "WEEKLY")
        #expect(draft.nextFireAt.count == 3)

        #expect(await model.createFromDraft())
        let create = try #require(recorder.snapshot.first { $0.url?.path == "/v1/schedules" && $0.httpMethod == "POST" })
        #expect(!(create.value(forHTTPHeaderField: "Idempotency-Key") ?? "").isEmpty)
        let posted = try Self.body(create)
        #expect(posted["instruction"] as? String == "Every weekday at 7 am, tell me what needs me today.")
        #expect(posted["agent_id"] as? String == "00000000-0000-0000-0000-000000000742")
        #expect(model.draftState == .idle)
        #expect(toasts.current?.text.hasPrefix("Scheduled. The first run is") == true)
        #expect(model.items.map(\.id) == [scheduleID])
    }

    @Test
    func testAnUnclearOrUnavailableDraftSaysWhyAndCreatesNothing() async throws {
        var answer = (200, #"{"status":"needs_clarification","reason":"no_timing","definition":null,"next_fire_at":[]}"#)
        let lock = NSLock()
        let model = try makeModel { request in
            let (status, body) = lock.withLock { answer }
            return Self.response(request, statusCode: status, body: body)
        }

        await model.readDraft("Check the snow report", timezone: "America/Los_Angeles")
        #expect(model.draftState == .unclear("Say when it should run, for example every morning at 7."))
        #expect(await model.createFromDraft() == false)

        lock.withLock { answer = (422, #"{"error":{"code":"schedule_validation_error","message":"x","details":{"reason":"schedule.drafts_unavailable"},"request_id":"r"}}"#) }
        await model.readDraft("Every day at 7 check the snow", timezone: "America/Los_Angeles")
        #expect(model.draftState == .unavailable)

        lock.withLock { answer = (429, #"{"error":{"code":"rate_limited","message":"x","details":{"reason":"schedule.drafts_rate_limited"},"request_id":"r"}}"#) }
        await model.readDraft("Every day at 8 check the snow", timezone: "America/Los_Angeles")
        #expect(model.draftState == .failed("You have read many schedules in the last hour. Try again later."))
    }

    /// ADR-0225 part C: Run now sends the revision on screen with a key, says
    /// so with a toast, and refreshes Recent runs; a busy schedule says why.
    @Test
    func testRunNowStartsARunAndExplainsARefusal() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000781"))
        let recorder = ScheduleRequestRecorder()
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let lock = NSLock()
        var busy = false
        let model = try makeModel(toasts: toasts) { request in
            recorder.append(request)
            let path = request.url!.path
            if path.hasSuffix("/run") {
                if lock.withLock({ busy }) {
                    return Self.response(request, statusCode: 409, body: #"{"error":{"code":"conflict","message":"x","details":{"reason":"schedule.run_active"},"request_id":"r"}}"#)
                }
                return Self.response(request, statusCode: 201, body: Self.occurrenceJSON(
                    id: UUID(), schedule: scheduleID, at: "2026-10-20T15:00:00Z", disposition: "MATERIALIZED",
                    session: UUID(), run: UUID()))
            }
            if path.hasSuffix("/occurrences") {
                return Self.response(request, body: #"{"items":[],"next_cursor":null}"#)
            }
            return Self.response(request, body: Self.detailJSON(id: scheduleID))
        }
        await model.loadDetail(scheduleID)

        await model.runNow(scheduleID)

        let run = try #require(recorder.snapshot.first { $0.url!.path.hasSuffix("/run") })
        #expect(run.httpMethod == "POST")
        #expect(try Self.body(run)["expected_revision"] as? Int == 1)
        #expect(!(run.value(forHTTPHeaderField: "Idempotency-Key") ?? "").isEmpty)
        #expect(toasts.current?.text == "Running now. The report arrives in Chat.")
        #expect(recorder.snapshot.contains { $0.url!.path.hasSuffix("/occurrences") })

        lock.withLock { busy = true }
        await model.runNow(scheduleID)
        #expect(model.lifecycleError(for: scheduleID) == "A run of this schedule is still going.")
    }

    /// A revision that moved on is read again and the change retried once.
    @Test
    func testAStaleRevisionIsReadAgainAndRetriedOnce() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000762"))
        let recorder = ScheduleRequestRecorder()
        let model = try makeModel { request in
            recorder.append(request)
            if request.url!.path.hasSuffix("/pause") {
                let sent = try Self.body(request)["expected_revision"] as? Int
                if sent == 1 {
                    return Self.response(request, statusCode: 409, body: #"{"error":{"code":"conflict","message":"Schedule changed.","details":{"reason":"schedule.revision_conflict"},"request_id":"r"}}"#)
                }
                return Self.response(request, body: Self.detailJSON(id: scheduleID, revision: 3, state: "PAUSED"))
            }
            let reads = recorder.snapshot.filter { $0.httpMethod == "GET" }.count
            return Self.response(request, body: Self.detailJSON(id: scheduleID, revision: reads > 1 ? 2 : 1))
        }
        await model.loadDetail(scheduleID)

        await model.pause(scheduleID)

        let pauses = recorder.snapshot.filter { $0.url!.path.hasSuffix("/pause") }
        try #require(pauses.count == 2)
        #expect(try Self.body(pauses[1])["expected_revision"] as? Int == 2)
        #expect(model.detailRecords[scheduleID]?.schedule.stateKind == .paused)
        #expect(model.lifecycleError(for: scheduleID) == nil)
    }

    /// Recent runs read the occurrence ledger; a run's report opens only
    /// where its conversation still exists.
    @Test
    func testRecentRunsReadTheOccurrencesAndTheirRuns() async throws {
        let scheduleID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000763"))
        let sessionID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000764"))
        let runID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000765"))
        let ran = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000766"))
        let skipped = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000767"))
        let model = try makeModel { request in
            let path = request.url!.path
            if path.hasSuffix("/occurrences") {
                let items = [
                    Self.occurrenceJSON(id: ran, schedule: scheduleID, at: "2026-10-19T14:00:00Z", disposition: "MATERIALIZED", session: sessionID, run: runID),
                    Self.occurrenceJSON(id: skipped, schedule: scheduleID, at: "2026-10-18T14:00:00Z", disposition: "SKIPPED_OVERLAP"),
                ]
                return Self.response(request, body: #"{"items":[\#(items.joined(separator: ","))],"next_cursor":null}"#)
            }
            if path == "/v1/runs/\(runID.uuidString)" {
                return Self.response(request, body: Self.runJSON(id: runID, session: sessionID, status: "COMPLETED"))
            }
            return Self.response(request, body: Self.detailJSON(id: scheduleID))
        }

        await model.loadRecentRuns(scheduleID)

        let runs = model.recentRuns(for: scheduleID)
        #expect(runs.map(\.occurrence.id) == [ran, skipped])
        #expect(runs.first?.runStatus == .completed)
        #expect(runs.first?.reportSessionID == sessionID)
        #expect(runs.last?.reportSessionID == nil)
        #expect(runs.last?.title == "Skipped: the last run was still going")
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

    @Test(arguments: ["noClient", "error", "success"])
    func testOlderWebsiteProfileLoadCannotOverwriteNewerLoad(outcome: String) async throws {
        let newID = UUID()
        let oldClient = try makeClient { request in
            Self.response(request, statusCode: outcome == "error" ? 403 : 200,
                body: outcome == "error"
                    ? #"{"error":{"code":"authorization_error","message":"Old failure","details":{},"request_id":"old"}}"#
                    : #"{"items":[],"next_cursor":null}"#)
        }
        let newClient = try makeClient { request in
            Self.response(request,
                body: #"{"items":[\#(Self.profileJSON(id: newID, origin: "https://x.com", status: "ready"))],"next_cursor":null}"#)
        }
        let clients = SuspendedProfileClients(old: outcome == "noClient" ? nil : oldClient, new: newClient)
        let model = ScheduleViewModel(makeAPIClient: { await clients.load() })
        let older = Task { await model.loadWebsiteAccessProfiles() }
        await clients.waitUntilSuspended()
        await model.loadWebsiteAccessProfiles()
        await clients.release()
        await older.value
        #expect(model.websiteAccessProfiles?.map(\.id) == [newID])
        #expect(model.websiteAccessProfilesError == nil)
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
        toasts: ToastCenter? = nil,
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> ScheduleViewModel {
        let client = try makeClient(handler: handler)
        return ScheduleViewModel(
            makeAPIClient: { client },
            toasts: toasts ?? ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        )
    }

    nonisolated private static func occurrenceJSON(
        id: UUID, schedule: UUID, at: String, disposition: String, session: UUID? = nil, run: UUID? = nil
    ) -> String {
        let materialized = disposition == "MATERIALIZED"
        let sessionValue = session.map { #""\#($0.uuidString)""# } ?? "null"
        let runValue = run.map { #""\#($0.uuidString)""# } ?? "null"
        let reason = materialized ? "null" : #""schedule.overlap""#
        let authority = materialized ? #""v1""# : "null"
        let materializedAt = materialized ? #""\#(at)""# : "null"
        return #"{"id":"\#(id.uuidString)","schedule_id":"\#(schedule.uuidString)","schedule_revision":1,"nominal_fire_at":"\#(at)","disposition":"\#(disposition)","session_id":\#(sessionValue),"run_id":\#(runValue),"reason_code":\#(reason),"authority_version":\#(authority),"materialized_at":\#(materializedAt),"links_erased_at":null,"created_at":"\#(at)"}"#
    }

    private func makeClient(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> VeetbotAPIClient {
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
        return VeetbotAPIClient(transport: transport)
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

    nonisolated private static func body(_ request: URLRequest) throws -> [String: Any] {
        var data = request.httpBody ?? Data()
        if data.isEmpty, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var buffer = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(buffer, count: count)
            }
        }
        return try JSONSerialization.jsonObject(with: data) as? [String: Any] ?? [:]
    }

    nonisolated private static func runJSON(id: UUID, session: UUID, status: String) -> String {
        #"{"id":"\#(id.uuidString)","session_id":"\#(session.uuidString)","parent_run_id":null,"status":"\#(status)","step_count":3,"model_call_count":2,"tool_call_count":1,"usage":{"input_tokens":10,"output_tokens":5,"cost_usd":"0.01"},"limits":{"max_steps":12,"deadline_at":null,"max_cost_usd":"1"},"failure":null,"cancel_requested_at":null,"created_at":"2026-10-19T14:00:00Z","updated_at":"2026-10-19T14:01:00Z"}"#
    }

    nonisolated private static let draftJSON = #"{"status":"ready","reason":null,"definition":{"title":"Weekday briefing","instruction":"Every weekday at 7 am, tell me what needs me today.","agent_id":"00000000-0000-0000-0000-000000000742","agent_version":"1","policy_profile":"default","requested_scopes":[],"browser_profile_id":null,"limits":{"max_steps":12,"max_model_calls":12,"max_tool_calls":24,"max_input_tokens":null,"max_output_tokens":null,"max_cost":"5","deadline_at":null,"synthesis_reserve_steps":2,"synthesis_reserve_model_calls":2,"synthesis_reserve_cost":"1"},"run_timeout_seconds":300,"cadence":{"kind":"WEEKLY","local_time":"07:00:00","weekdays":[1,2,3,4,5],"timezone":"America/Los_Angeles"},"misfire_grace_seconds":3600,"max_consecutive_failures":2,"notify":"every_report"},"next_fire_at":["2026-10-20T14:00:00Z","2026-10-21T14:00:00Z","2026-10-22T14:00:00Z"]}"#

    nonisolated private static func summaryJSON(id: UUID) -> String {
        #"{"id":"\#(id.uuidString)","state":"ACTIVE","pause_reason":null,"current_revision":1,"next_fire_at":"2026-08-30T16:00:00Z","title":"Daily review","instruction_preview":"Preview","cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"}"#
    }

    nonisolated private static func detailJSON(
        id: UUID,
        revision: Int = 1,
        profile: UUID? = nil,
        state: String = "ACTIVE",
        notify: String? = nil
    ) -> String {
        let notifyField = notify.map { #","notify":"\#($0)""# } ?? ""
        let binding = profile.map { #""\#($0.uuidString.lowercased())""# } ?? "null"
        let scopes = profile == nil ? "[]" : #"["browser.profile.read"]"#
        return #"{"schedule":{"id":"\#(id.uuidString)","tenant_id":"local","principal_id":"principal","state":"\#(state)","pause_reason":null,"current_revision":\#(revision),"next_fire_at":"2026-08-30T16:00:00Z","consecutive_failures":0,"created_at":"2026-08-29T00:00:00Z","updated_at":"2026-08-29T00:00:00Z"},"revision":{"schedule_id":"\#(id.uuidString)","revision":\#(revision),"title":"Daily review","instruction":"Full instruction","agent_id":"00000000-0000-0000-0000-000000000742","agent_version":"1","policy_profile":"default","requested_scopes":\#(scopes),"browser_profile_id":\#(binding),"limits":{"max_steps":12,"max_model_calls":12,"max_tool_calls":24,"max_input_tokens":null,"max_output_tokens":null,"max_cost":"1","deadline_at":null,"synthesis_reserve_steps":0,"synthesis_reserve_model_calls":0,"synthesis_reserve_cost":"0"},"run_timeout_seconds":300,"cadence":{"kind":"DAILY","local_time":"09:00:00","timezone":"America/Los_Angeles"},"timezone":"America/Los_Angeles","misfire_grace_seconds":3600,"max_consecutive_failures":1\#(notifyField),"created_by_principal_id":"principal","created_at":"2026-08-29T00:00:00Z"},"replayed":false}"#
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

private final class ScheduleRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [URLRequest] = []
    func append(_ value: URLRequest) { lock.withLock { values.append(value) } }
    var snapshot: [URLRequest] { lock.withLock { values } }
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

private actor SuspendedProfileClients {
    let old: VeetbotAPIClient?
    let new: VeetbotAPIClient
    var continuation: CheckedContinuation<Void, Never>?
    var started = false

    init(old: VeetbotAPIClient?, new: VeetbotAPIClient) {
        self.old = old
        self.new = new
    }

    func load() async -> VeetbotAPIClient? {
        if started { return new }
        started = true
        await withCheckedContinuation { continuation = $0 }
        return old
    }

    func waitUntilSuspended() async {
        while continuation == nil { await Task.yield() }
    }

    func release() {
        continuation?.resume()
        continuation = nil
    }
}
