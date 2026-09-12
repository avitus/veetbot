import Combine
import Foundation
import Testing
@testable import VeetbotCore

@Suite(.serialized) @MainActor struct EmailViewModelTests {
    private let threadID = UUID(uuidString: "00000000-0000-0000-0000-000000000801")!
    private let draftID = UUID(uuidString: "00000000-0000-0000-0000-000000000802")!
    private let runID = UUID(uuidString: "00000000-0000-0000-0000-000000000803")!
    private let approvalID = UUID(uuidString: "00000000-0000-0000-0000-000000000804")!

    @Test func testReviewRefusesAnApprovalForDifferentMessageContent() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/approvals/") { return (200, self.approvalJSON(body: "Different approved message")) }
            return (200, self.threadJSON(draft: self.draftJSON(status: "awaiting_approval")))
        }
        await model.openThread(threadID)
        await model.loadReview()
        #expect(model.review == nil, "a review must display the actual message being approved")
    }

    @Test func testReviewAcceptsTheExactAccountThreadRecipientsAndBody() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/approvals/") { return (200, self.approvalJSON(body: "Thanks, I will review the agenda.")) }
            return (200, self.threadJSON(draft: self.draftJSON(status: "awaiting_approval")))
        }
        await model.openThread(threadID)
        await model.loadReview()
        #expect(model.review?.id == approvalID)
        model.changeEdit(\.body, to: "A revised response")
        #expect(model.review == nil)
        #expect(model.reviewDraft == nil)
    }

    @Test func testApprovalCannotSubstituteAccountThreadOrRecipients() async throws {
        let valid = approvalJSON(body: "Thanks, I will review the agenda.")
        for invalid in [
            valid.replacingOccurrences(of: "mcp.gmail_send.send_message", with: "mcp.gmail_work_send.send_message"),
            valid.replacingOccurrences(of: "provider-thread", with: "another-thread"),
            valid.replacingOccurrences(of: "alex@example.test", with: "another@example.test")
        ] {
            let model = try makeModel { request in
                if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
                if request.url!.path.contains("/approvals/") { return (200, invalid) }
                return (200, self.threadJSON(draft: self.draftJSON(status: "awaiting_approval")))
            }
            await model.openThread(threadID)
            await model.loadReview()
            #expect(model.review == nil)
            #expect(model.draftError != nil)
        }
    }

    @Test func testReviewAcceptsCanonicalGmailRecipientStrings() async throws {
        let approval = approvalJSON(body: "Thanks, I will review the agenda.")
            .replacingOccurrences(of: "\"to\":[\"alex@example.test\"]", with: "\"to\":\"alex@example.test\"")
            .replacingOccurrences(of: "\"cc\":[],\"bcc\":[]", with: "\"cc\":null,\"bcc\":null")
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/approvals/") { return (200, approval) }
            return (200, self.threadJSON(draft: self.draftJSON(status: "awaiting_approval")))
        }
        await model.openThread(threadID)
        await model.loadReview()
        #expect(model.review?.id == approvalID)
    }

    @Test func testDraftToolCannotSubstituteTheSelectedMailbox() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            if request.url!.path.contains("/approvals/") {
                return (200, self.approvalJSON(body: "Thanks, I will review the agenda.").replacingOccurrences(of: "mcp.gmail_send", with: "mcp.gmail_work_send"))
            }
            return (200, self.threadJSON(draft: self.draftJSON(status: "awaiting_approval").replacingOccurrences(of: "mcp.gmail_send", with: "mcp.gmail_work_send")))
        }
        await model.reload()
        await model.openThread(threadID)
        await model.loadReview()
        #expect(model.review == nil)
    }

    @Test func testLateMailResponseCannotRepopulateAForgottenConnection() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            Thread.sleep(forTimeInterval: 0.04)
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        let opening = Task { await model.openThread(threadID) }
        for _ in 0..<100 {
            if !requests.snapshot.isEmpty { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        #expect(!requests.snapshot.isEmpty)
        model.resetConnection()
        await opening.value
        #expect(model.thread == nil)
        #expect(model.draft == nil)
        #expect(model.edits.isEmpty)
        #expect(model.selectedThreadID == nil)
    }

    @Test func testConflictingSavePreservesBothVersionsAndCannotProposeSend() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "PUT" { return (409, Self.error("invalid_state", "Draft changed on another device.")) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON(revision: 2, body: "Other device edit")) }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My unfinished edit")
        #expect(await model.saveDraft() == false)
        #expect(model.currentEdit?.body == "My unfinished edit")
        #expect(model.conflict?.body == "Other device edit")
        await model.prepareSend()
        #expect(!requests.snapshot.contains { $0.url!.path.hasSuffix("send-proposal") })
        model.useServerDraft()
        #expect(model.currentEdit?.body == "Other device edit")
        #expect(model.conflict == nil)
    }

    @Test func testNoRefreshAdmissionAfterEmailLeavesForeground() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(refreshNanoseconds: 10_000_000) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                return (202, "{\"operation_id\":\"\(self.threadID)\",\"run_id\":\"\(self.runID)\",\"status\":\"COMPLETED\",\"replayed\":false}")
            }
            return (200, self.pageJSON())
        }
        await model.refresh()
        #expect(requests.snapshot.isEmpty)
        model.setActive(true)
        for _ in 0..<100 {
            if requests.snapshot.contains(where: { $0.httpMethod == "POST" }) { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        model.setActive(false)
        let count = requests.snapshot.filter { $0.httpMethod == "POST" }.count
        #expect(count > 0)
        try await Task.sleep(nanoseconds: 40_000_000)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == count)
    }

    @Test func testForegroundReturnStartsOneLoopAndVisibleCadenceRepeats() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(refreshNanoseconds: 20_000_000) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                return (200, "{\"operation_id\":\"\(self.threadID)\",\"run_id\":\"\(self.runID)\",\"status\":\"COMPLETED\",\"replayed\":false}")
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        model.setActive(true)
        for _ in 0..<200 {
            if requests.snapshot.filter({ $0.httpMethod == "POST" }).count >= 2 { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count >= 2)
        model.setActive(false)
        let pausedCount = requests.snapshot.filter { $0.httpMethod == "POST" }.count
        try await Task.sleep(nanoseconds: 50_000_000)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == pausedCount)
        model.setActive(true)
        for _ in 0..<100 {
            if requests.snapshot.filter({ $0.httpMethod == "POST" }).count > pausedCount { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        model.setActive(false)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == pausedCount + 1)
    }

    @Test func testNewAccountFailureDoesNotShowPreviousAccountMail() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.query?.contains("account_id=work") == true { return (503, Self.error("service_unavailable", "Work temporarily unavailable.")) }
            return (200, self.pageJSON())
        }
        await model.reload()
        #expect(model.items.count == 1)
        model.setAccount("work")
        for _ in 0..<100 {
            if model.errorMessage != nil { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        #expect(model.errorMessage != nil)
        #expect(model.items.isEmpty, "personal rows must not remain below the Work filter")
    }

    @Test func testUnchangedRefreshDoesNotReplaceOwnerDraftEdits() async throws {
        let model = try makeModel { _ in (200, self.threadJSON(draft: self.draftJSON())) }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Keep these local words")
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.currentEdit?.body == "Keep these local words")
        #expect(model.conflict == nil)
        model.resetConnection()
        #expect(model.edits.isEmpty)
        #expect(model.thread == nil)
        #expect(model.review == nil)
    }

    @Test func testRepeatedCursorStopsFurtherRequestsWithoutDuplicatingRows() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            return (200, self.pageJSON(cursor: "same"))
        }
        await model.reload()
        await model.loadMore()
        #expect(model.items.count == 1)
        #expect(model.hasMore == false)
        let count = requests.snapshot.count
        await model.loadMore()
        #expect(requests.snapshot.count == count)
    }

    @Test func testBackgroundRerankingDoesNotMoveRowsUnderTheOwner() async throws {
        let requests = EmailRequestRecorder()
        let first = threadJSON()
        let second = first.replacingOccurrences(of: threadID.uuidString, with: "00000000-0000-0000-0000-000000000805")
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let count = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
            return (200, "{\"items\":[\(count == 1 ? first + "," + second : second + "," + first)],\"next_cursor\":null}")
        }
        await model.reload()
        let originalOrder = model.items.map(\.id)
        await model.reload(preserveOrder: true)
        #expect(model.items.map(\.id) == originalOrder)
    }

    @Test func testEmptyInboxPollingDoesNotRestartTheLoadingIndicator() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            return (200, "{\"items\":[],\"next_cursor\":null}")
        }
        var loading: [Bool] = []
        let subscription = model.$isLoading.removeDuplicates().sink { loading.append($0) }
        defer { subscription.cancel() }
        await model.reload()
        #expect(loading == [false, true, false])
        loading = []
        for _ in 0..<3 { await model.reload(preserveOrder: true) }
        #expect(loading.isEmpty, "an empty inbox must remain stable during status polling")
        #expect(model.items.isEmpty)
    }

    @Test func testFailedRefreshRemainsVisibleAcrossSuccessfulProjectionReads() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                return (200, "{\"operation_id\":\"\(self.threadID)\",\"run_id\":\"\(self.runID)\",\"status\":\"FAILED\",\"replayed\":false}")
            }
            return (200, "{\"items\":[],\"next_cursor\":null}")
        }
        model.setActive(true)
        for _ in 0..<100 {
            if model.errorMessage != nil { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        #expect(model.errorMessage != nil)
        await model.reload(preserveOrder: true)
        #expect(model.errorMessage != nil)
        model.setActive(false)
        model.resetConnection()
        #expect(model.errorMessage == nil)
    }

    @Test func testGatewayRefreshFailurePersistsUntilConfirmedRefreshSuccess() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                if requests.snapshot.filter({ $0.httpMethod == "POST" }).count <= 2 {
                    return (502, "<html>Bad Gateway</html>")
                }
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.setActive(false) }
        for _ in 0..<1000 {
            if model.errorMessage != nil && !model.isRefreshing { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        #expect(model.errorMessage != nil)
        await model.reload(preserveOrder: true)
        #expect(model.errorMessage != nil, "cached reads do not prove refresh recovered")
        #expect(model.items.count == 1)
        await model.refresh()
        #expect(model.errorMessage == nil)
        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(posts.count == 3)
        #expect(Set(posts.compactMap { $0.value(forHTTPHeaderField: "Idempotency-Key") }).count == 1)
    }

    @Test(arguments: [false, true])
    func testOperationPollingRecoversAfterATransientGatewayFailureWithoutNewAdmission(timeout: Bool) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") { return (200, self.operationJSON(status: "RUNNING")) }
            if request.url!.path.contains("/operations/") {
                if requests.snapshot.filter({ $0.url!.path.contains("/operations/") }).count == 1 {
                    if timeout { throw URLError(.timedOut) }
                    return (502, "<html>Bad Gateway</html>")
                }
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.setActive(false) }
        for _ in 0..<400 {
            if model.errorMessage != nil { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        #expect(model.errorMessage != nil)
        await model.reload(preserveOrder: true)
        #expect(model.errorMessage != nil)
        for _ in 0..<500 {
            if requests.snapshot.filter({ $0.url!.path.contains("/operations/") }).count >= 2 && model.errorMessage == nil { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        #expect(requests.snapshot.filter { $0.url!.path.contains("/operations/") }.count == 2)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
        #expect(model.errorMessage == nil)
        #expect(model.items.count == 1)
    }

    @Test func testOperationPollingExhaustsBoundedRetriesWithoutSubmittingAnotherRefresh() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") { return (200, self.operationJSON(status: "RUNNING")) }
            if request.url!.path.contains("/operations/") { return (502, "<html>Bad Gateway</html>") }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.setActive(false) }
        for _ in 0..<1600 {
            if requests.snapshot.filter({ $0.url!.path.contains("/operations/") }).count >= 3 { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        #expect(requests.snapshot.filter { $0.url!.path.contains("/operations/") }.count == 3)
        // A fourth exponential-backoff attempt would arrive after sixteen
        // more seconds. Wait past that boundary to prove the retry cap.
        try await Task.sleep(nanoseconds: 17_000_000_000)
        #expect(requests.snapshot.filter { $0.url!.path.contains("/operations/") }.count == 3)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
        #expect(model.errorMessage != nil)
    }

    @Test(arguments: [false, true])
    func testRecoveredThreadReadClearsOnlyItsOwnGatewayFailure(preserveActionFailure: Bool) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "POST" { return (400, Self.error("invalid_argument", "Feedback was rejected.")) }
            let reads = requests.snapshot.filter { $0.url!.path.contains("/threads/") }.count
            if reads == 2 { return (502, "<html>Bad Gateway</html>") }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        if preserveActionFailure { await model.giveFeedback(target: .thread, judgment: "important") }
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.draftError != nil)
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.draftError == (preserveActionFailure ? "Feedback was rejected." : nil))
        #expect(model.draft?.body == "Thanks, I will review the agenda.")
    }

    @Test(arguments: [false, true])
    func testLeavingEmailDuringAnOperationReadDoesNotReportCancellation(resetConnection: Bool) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") { return (200, self.operationJSON(status: "RUNNING")) }
            if request.url!.path.contains("/operations/") {
                Thread.sleep(forTimeInterval: 0.15)
                return (502, "<html>Bad Gateway</html>")
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        for _ in 0..<400 {
            if requests.snapshot.contains(where: { $0.url!.path.contains("/operations/") }) { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        #expect(requests.snapshot.contains { $0.url!.path.contains("/operations/") })
        model.setActive(false)
        if resetConnection { model.resetConnection() }
        try await Task.sleep(nanoseconds: 250_000_000)
        #expect(model.errorMessage == nil)
        #expect(model.draftError == nil)
    }

    private func operationJSON(status: String) -> String {
        "{\"operation_id\":\"\(threadID)\",\"run_id\":\"\(runID)\",\"status\":\"\(status)\",\"replayed\":false}"
    }

    @Test func testUnavailableFeatureDoesNotRequestMailboxWrites() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            return (404, Self.error("not_found", "Not found"))
        }
        await model.reload()
        #expect(model.unavailable)
        #expect(requests.snapshot.allSatisfy { $0.httpMethod == "GET" })
    }

    @Test func testFeedbackAcknowledgesJudgmentAndScope() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("/feedback") {
                return (200, "{\"feedback_id\":\"\(self.threadID)\",\"thread\":\(self.threadJSON())}")
            }
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            return (200, self.threadJSON())
        }
        await model.openThread(threadID)
        await model.giveFeedback(target: .person, judgment: "less_important", targetValue: "alex@example.test")
        #expect(model.feedbackMessage == "Marked this person as less important.")
        #expect(model.feedbackID == threadID)
        model.clearSelection()
        #expect(model.feedbackMessage == nil)
        #expect(model.feedbackID == nil)
    }

    @Test func testExpandedInboxRefreshHonorsTheServerPageLimit() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems ?? []
            let limit = Int(query.first { $0.name == "limit" }?.value ?? "5")!
            let offset = Int(query.first { $0.name == "cursor" }?.value ?? "0")!
            if limit > 100 { return (400, Self.error("invalid_argument", "Page limit exceeded.")) }
            let end = min(offset + limit, 105)
            let rows = (offset..<end).map { i in
                self.threadJSON().replacingOccurrences(of: self.threadID.uuidString, with: String(format: "00000000-0000-0000-0000-%012d", 1000 + i))
            }.joined(separator: ",")
            return (200, "{\"items\":[\(rows)],\"next_cursor\":\(end < 105 ? "\"\(end)\"" : "null")}")
        }
        await model.reload()
        for _ in 0..<20 { await model.loadMore() }
        #expect(model.items.count == 105)
        await model.reload(preserveOrder: true)
        #expect(model.errorMessage == nil)
        #expect(model.items.count == 105)
    }

    @Test func testRevokedReadScopeClearsDisplayedMailAndLocalEdits() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") {
                let reads = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
                return reads > 1 ? (403, Self.error("forbidden", "Email access was revoked.")) : (200, self.pageJSON())
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Unfinished private edit")
        await model.reload(preserveOrder: true)
        #expect(model.items.isEmpty)
        #expect(model.thread == nil)
        #expect(model.edits.isEmpty)
        #expect(model.errorMessage != nil)
    }

    @Test func testWritingExampleSavesEditsThenEndorsesTheExactRevision() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Use my revised style.")) }
            if request.url!.path.hasSuffix("style-example") {
                return (200, "{\"paused\":true,\"profile_revision\":2,\"excluded_sources\":0,\"style_examples\":1,\"history_processed\":25,\"history_complete\":false}")
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Use my revised style.")
        await model.endorseDraftStyle()
        let writes = requests.snapshot.filter { $0.httpMethod != "GET" }
        #expect(writes.map(\.httpMethod) == ["PUT", "POST"])
        let endorsement = try #require(writes.last)
        #expect(endorsement.url?.path == "/v1/email/drafts/\(draftID.uuidString)/style-example")
        let data: Data
        if let body = endorsement.httpBody { data = body }
        else {
            let stream = try #require(endorsement.httpBodyStream)
            stream.open()
            defer { stream.close() }
            var body = Data()
            var bytes = [UInt8](repeating: 0, count: 1024)
            while stream.hasBytesAvailable {
                let count = stream.read(&bytes, maxLength: bytes.count)
                if count <= 0 { break }
                body.append(contentsOf: bytes.prefix(count))
            }
            data = body
        }
        let payload = try JSONDecoder().decode([String: Int].self, from: data)
        #expect(payload == ["expected_revision": 2])
        #expect(model.learning?.paused == true)
        #expect(model.learning?.styleExamples == 1)
    }

    @Test func testWritingExampleCannotEndorseAfterAConflictingSave() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "PUT" { return (409, Self.error("invalid_state", "Draft changed.")) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON(revision: 2)) }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My local wording")
        await model.endorseDraftStyle()
        #expect(model.currentEdit?.body == "My local wording")
        #expect(model.conflict != nil)
        #expect(!requests.snapshot.contains { $0.url!.path.hasSuffix("style-example") })
    }

    private func makeModel(
        refreshNanoseconds: UInt64 = 60_000_000_000,
        handler: @escaping (URLRequest) throws -> (Int, String)
    ) throws -> EmailViewModel {
        let id = EmailTestURLProtocol.register(handler)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [EmailTestURLProtocol.self]
        configuration.httpAdditionalHeaders = ["X-Email-Test": id]
        let api = VeetbotAPIClient(transport: HTTPTransport(
            configuration: try ConnectionConfiguration(baseURLString: "https://email.test"),
            tokenStore: InMemoryTokenStore(token: "test-token"), session: URLSession(configuration: configuration)
        ))
        return EmailViewModel(makeAPIClient: { api }, refreshNanoseconds: refreshNanoseconds)
    }

    private static let accountsJSON = """
        {"items":[{"id":"personal","label":"Personal","email_address":"owner@example.test","status":"ready","last_synced_at":"2026-09-11T00:00:00Z","history_complete":false,"history_processed":25,"read_server_id":"gmail_read","send_server_id":"gmail_send"},{"id":"work","label":"Work","email_address":"owner@work.test","status":"ready","last_synced_at":null,"history_complete":false,"history_processed":0,"read_server_id":"gmail_work_read","send_server_id":"gmail_work_send"}],"next_cursor":null}
        """
    private static func error(_ code: String, _ message: String) -> String {
        "{\"error\":{\"code\":\"\(code)\",\"message\":\"\(message)\",\"details\":{},\"request_id\":\"test\"}}"
    }
    private func pageJSON(cursor: String? = nil) -> String {
        "{\"items\":[\(threadJSON())],\"next_cursor\":\(cursor.map { "\"\($0)\"" } ?? "null")}"
    }
    private func threadJSON(draft: String = "null") -> String {
        """
        {"id":"\(threadID)","account_id":"personal","subject":"Board discussion","senders":["alex@example.test"],"updated_at":"2026-09-11T00:00:00Z","revision":1,"summary":"Review the board agenda.","reason":"Your board colleague.","needs_reply":true,"draft_id":"\(draftID)","session_id":"\(threadID)","priority":0.95,"complete":true,"messages":[],"draft":\(draft)}
        """
    }
    private func draftJSON(revision: Int = 1, body: String = "Thanks, I will review the agenda.", status: String = "ready") -> String {
        """
        {"id":"\(draftID)","thread_id":"\(threadID)","account_id":"personal","revision":\(revision),"source_revision":1,"provider_thread_id":"provider-thread","send_tool_name":"mcp.gmail_send.send_message","to":["alex@example.test"],"cc":[],"bcc":[],"subject":"Re: Board discussion","body":"\(body)","status":"\(status)","stale":false,"run_id":"\(runID)","approval_id":"\(approvalID)","session_id":"\(threadID)","updated_at":"2026-09-11T00:00:00Z"}
        """
    }
    private func approvalJSON(body: String) -> String {
        """
        {"id":"\(approvalID)","run_id":"\(runID)","session_id":"\(threadID)","status":"PENDING","tool_name":"mcp.gmail_send.send_message","action_summary":"Send reply","arguments":{"thread_id":"provider-thread","to":["alex@example.test"],"cc":[],"bcc":[],"subject":"Re: Board discussion","body":"\(body)"},"risk":"HIGH","policy_reason":"Approval required","expires_at":null,"created_at":"2026-09-11T00:00:00Z","resolved_at":null,"resolved_by":null,"decision":null}
        """
    }
}

private final class EmailRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [URLRequest] = []
    func append(_ value: URLRequest) { lock.withLock { values.append(value) } }
    var snapshot: [URLRequest] { lock.withLock { values } }
}

private final class EmailTestURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var handlers: [String: (URLRequest) throws -> (Int, String)] = [:]
    static func register(_ handler: @escaping (URLRequest) throws -> (Int, String)) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }
    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard let id = request.value(forHTTPHeaderField: "X-Email-Test"),
            let handler = Self.lock.withLock({ Self.handlers[id] }) else { return }
        do {
            let (status, body) = try handler(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }
    override func stopLoading() {}
}
