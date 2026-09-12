import Combine
import Foundation
import Testing
@testable import VeetbotCore

@Suite(.serialized) @MainActor struct EmailViewModelTests {
    private let threadID = UUID(uuidString: "00000000-0000-0000-0000-000000000801")!
    private let draftID = UUID(uuidString: "00000000-0000-0000-0000-000000000802")!
    private let runID = UUID(uuidString: "00000000-0000-0000-0000-000000000803")!
    private let approvalID = UUID(uuidString: "00000000-0000-0000-0000-000000000804")!

    /// A clearly labelled archive gesture must reach the originating thread's remote command.
    @Test(arguments: ["personal", "work"])
    func testArchiveUsesAccountBoundCommandAndPreservesDraft(account: String) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "COMPLETED")) }
            let archived = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let thread = self.archiveThreadJSON(account: account, inInbox: !archived,
                status: archived ? "completed" : nil, draft: self.draftJSON())
            if request.url!.path.hasSuffix("threads") {
                return (200, "{\"items\":[\(archived ? "" : thread)],\"next_cursor\":null}")
            }
            return (200, thread)
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Preserve my unfinished reply")
        let thread = try #require(model.thread)
        await model.setThreadArchived(thread, archived: true)
        let command = try #require(requests.snapshot.first { $0.url!.path.hasSuffix("archive") })
        let body = try Self.archiveRequestBody(command)
        #expect(body["archived"] as? Bool == true)
        #expect(body["expected_revision"] as? Int == 1)
        #expect(body["account_id"] == nil)
        #expect(body["idempotency_key"] as? String == command.value(forHTTPHeaderField: "Idempotency-Key"))
        #expect(!requests.snapshot.contains { $0.url!.path.hasSuffix("dismiss") })
        #expect(model.items.isEmpty)
        #expect(model.currentEdit?.body == "Preserve my unfinished reply")
        #expect(model.selectedThreadID == threadID)
    }

    /// An unsupported mailbox cannot borrow archive support from another connected account.
    @Test(arguments: [false, true])
    func testArchiveRefusesMissingOrFalseAccountCapability(explicitFalse: Bool) async throws {
        let requests = EmailRequestRecorder()
        let unsupported = Self.archiveAccountsJSON.replacingOccurrences(
            of: "\"archive_supported\":true,\"write_server_id\":\"gmail_write\",",
            with: explicitFalse ? "\"archive_supported\":false," : "")
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, unsupported) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            return (200, self.archiveThreadJSON(inInbox: true))
        }
        await model.reload()
        await model.openThread(threadID)
        await model.setThreadArchived(try #require(model.thread), archived: true)
        #expect(!requests.snapshot.contains { $0.httpMethod == "POST" })
        #expect(!model.unavailable)
    }

    /// A lost admission response must reuse its action key; explicit retry cannot create a second archive.
    @Test func testArchiveLostResponseReusesOneKey() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("archive") {
                if requests.snapshot.filter({ $0.httpMethod == "POST" }).count <= 2 { return (502, "Bad Gateway") }
                return (202, self.operationJSON(status: "COMPLETED"))
            }
            let completed = requests.snapshot.filter { $0.httpMethod == "POST" }.count >= 3
            let thread = self.archiveThreadJSON(inInbox: !completed, status: completed ? "completed" : nil)
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(thread)],\"next_cursor\":null}") }
            return (200, thread)
        }
        defer { model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        await model.setThreadArchived(row, archived: true)
        await model.setThreadArchived(row, archived: true)
        let commands = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(commands.count == 3)
        #expect(commands.allSatisfy { $0.url!.path.hasSuffix("archive") })
        #expect(Set(commands.compactMap { $0.value(forHTTPHeaderField: "Idempotency-Key") }).count == 1)
    }

    /// A new client must not substitute local dismissal when a server lacks the archive route.
    @Test(arguments: [404, 405])
    func testArchiveUnsupportedRouteNeverFallsBack(status: Int) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.httpMethod == "POST" { return (status, Self.error("not_found", "Unavailable")) }
            let thread = self.archiveThreadJSON(inInbox: true)
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(thread)],\"next_cursor\":null}") }
            return (200, thread)
        }
        await model.reload()
        let row = try #require(model.items.first)
        await model.setThreadArchived(row, archived: true)
        await model.setThreadArchived(row, archived: true)
        #expect(!model.unavailable)
        #expect(model.items.count == 1)
        let commands = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(commands.count == 1)
        #expect(commands.allSatisfy { $0.url!.path.hasSuffix("archive") })
    }

    /// A completed foreground poll removes the archived row without a second mutation or losing edits.
    @Test func testArchivePendingProjectionRecoversAcrossVisits() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("refresh") { return (202, self.operationJSON(status: "RUNNING")) }
            if request.url!.path.contains("/operations/") { return (200, self.operationJSON(status: "RUNNING")) }
            let recovered = requests.snapshot.contains { $0.url!.path == "/v1/email/threads/\(self.threadID.uuidString)" }
            let thread = self.archiveThreadJSON(inInbox: !recovered, status: recovered ? "completed" : "pending")
            if request.url!.path.hasSuffix("threads") {
                return (200, "{\"items\":[\(recovered ? "" : thread)],\"next_cursor\":null}")
            }
            return (200, thread)
        }
        defer { model.resetConnection() }
        await model.reload()
        let pending = try #require(model.items.first)
        #expect(!pending.isArchived)
        #expect(!model.canArchive(pending))
        #expect(model.archiveMessage(for: pending) == "Archiving in Gmail…")
        model.setActive(true)
        for _ in 0..<1500 {
            if requests.snapshot.contains(where: { $0.url!.path == "/v1/email/threads/\(threadID.uuidString)" }) { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        try await Task.sleep(nanoseconds: 50_000_000)
        #expect(model.items.isEmpty)
        #expect(!requests.snapshot.contains { $0.url!.path.hasSuffix("archive") || $0.url!.path.hasSuffix("dismiss") })
    }

    /// Failed and uncertain outcomes keep observed state; checking uncertain work never reissues the write.
    @Test(arguments: ["pending", "failed", "uncertain"])
    func testArchiveOutcomeKeepsMailboxStateAndDraft(status: String) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            let thread = self.archiveThreadJSON(inInbox: true, status: status, draft: self.draftJSON())
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(thread)],\"next_cursor\":null}") }
            return (200, thread)
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Keep this draft while Gmail recovers")
        await model.checkArchiveStatus(threadID)
        let thread = try #require(model.thread)
        #expect(!thread.isArchived)
        #expect(model.items.count == 1)
        #expect(model.archiveMessage(for: thread) != nil)
        #expect(model.canArchive(thread) == (status == "failed"))
        #expect(model.currentEdit?.body == "Keep this draft while Gmail recovers")
        #expect(!requests.snapshot.contains { $0.httpMethod == "POST" })
    }

    /// Restoring an archived conversation explicitly adds Inbox and preserves unrelated send-review errors.
    @Test func testMoveToInboxPreservesUnrelatedSendError() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("send-proposal") { return (400, Self.error("malformed_request", "Review failed.")) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "COMPLETED")) }
            let restored = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let thread = self.archiveThreadJSON(inInbox: restored, status: restored ? "completed" : nil,
                targetArchived: false, draft: self.draftJSON())
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(thread)],\"next_cursor\":null}") }
            return (200, thread)
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        await model.prepareSend()
        #expect(model.draftError == "Review failed.")
        model.changeEdit(\.body, to: "Unsent and retained")
        await model.setThreadArchived(try #require(model.thread), archived: false)
        #expect(model.thread?.inInbox == true)
        #expect(model.draftError == "Review failed.")
        #expect(model.currentEdit?.body == "Unsent and retained")
        let command = try #require(requests.snapshot.first { $0.url!.path.hasSuffix("archive") })
        #expect(try Self.archiveRequestBody(command)["archived"] as? Bool == false)
    }

    /// A previous terminal projection cannot discard the retry identity of a newer unacknowledged action.
    @Test func testArchiveOldTerminalProjectionPreservesNewerLostResponseKey() async throws {
        let requests = EmailRequestRecorder()
        let previousOperation = UUID(uuidString: "00000000-0000-0000-0000-000000000805")!
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("archive") { return (502, "Bad Gateway") }
            let previous = self.archiveThreadJSON(inInbox: false, status: "completed", operationID: previousOperation)
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(previous)],\"next_cursor\":null}") }
            return (200, previous)
        }
        defer { model.resetConnection() }
        await model.reload()
        let archived = try #require(model.items.first)
        await model.setThreadArchived(archived, archived: false)
        let firstAttempts = requests.snapshot.filter { $0.url!.path.hasSuffix("archive") }
        #expect(firstAttempts.count == 2)
        #expect(model.archiveErrors[threadID] != nil)

        // A read that still observes the prior operation supplies no outcome for the lost restore admission.
        await model.checkArchiveStatus(threadID)
        await model.setThreadArchived(try #require(model.items.first), archived: false)
        let commands = requests.snapshot.filter { $0.url!.path.hasSuffix("archive") }
        #expect(commands.count == 4)
        #expect(Set(commands.compactMap { $0.value(forHTTPHeaderField: "Idempotency-Key") }).count == 1)
        #expect(try commands.allSatisfy { try Self.archiveRequestBody($0)["archived"] as? Bool == false })
    }

    /// Recovering a status read shows the durable pending action instead of retaining a stale transport failure.
    @Test func testArchiveSuccessfulPendingStatusClearsReadFailure() async throws {
        let requests = EmailRequestRecorder()
        let pointPath = "/v1/email/threads/\(threadID.uuidString)"
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path == pointPath,
               requests.snapshot.filter({ $0.url!.path == pointPath }).count == 1 {
                return (502, "Bad Gateway")
            }
            let pending = self.archiveThreadJSON(inInbox: true, status: "pending")
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(pending)],\"next_cursor\":null}") }
            return (200, pending)
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.checkArchiveStatus(threadID)
        #expect(model.archiveErrors[threadID] != nil)
        await model.checkArchiveStatus(threadID)
        let pending = try #require(model.items.first)
        #expect(model.archiveErrors[threadID] == nil)
        #expect(model.archiveMessage(for: pending) == "Archiving in Gmail…")
        #expect(!pending.isArchived)
        #expect(!requests.snapshot.contains { $0.httpMethod == "POST" })
    }

    /// Admission metadata cannot replace Inbox state confirmed after the gesture's source snapshot was obtained.
    @Test func testArchiveAdmissionPreservesNewerConfirmedInboxState() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED")) }
            if request.url!.path.hasSuffix("threads") {
                return (200, "{\"items\":[\(self.archiveThreadJSON(inInbox: true))],\"next_cursor\":null}")
            }
            if requests.snapshot.contains(where: { $0.url!.path.hasSuffix("archive") }) {
                return (502, "Bad Gateway")
            }
            return (200, self.archiveThreadJSON(inInbox: false, draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.reload()
        let gestureSnapshot = try #require(model.items.first)
        await model.openThread(threadID)
        #expect(model.thread?.isArchived == true)
        await model.setThreadArchived(gestureSnapshot, archived: true)
        #expect(model.thread?.archiveOperation?.status == "pending")
        #expect(model.thread?.isArchived == true)
        #expect(model.thread?.inInbox == false)
    }

    /// An erased thread's resource error cannot disable archive for a different thread in the same account.
    @Test func testArchiveErasedThreadDoesNotDisableOtherAccountRow() async throws {
        let requests = EmailRequestRecorder()
        let otherID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path == "/v1/email/threads/\(self.threadID.uuidString)/archive" {
                return (404, Self.error("not_found", "email thread not found"))
            }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED")) }
            let other = self.archiveThreadJSON(inInbox: true, status: "pending")
                .replacingOccurrences(of: self.threadID.uuidString, with: otherID.uuidString)
            if request.url!.path.hasSuffix("threads") {
                let first = self.archiveThreadJSON(inInbox: true)
                let second = self.archiveThreadJSON(inInbox: true)
                    .replacingOccurrences(of: self.threadID.uuidString, with: otherID.uuidString)
                return (200, "{\"items\":[\(first),\(second)],\"next_cursor\":null}")
            }
            return (200, other)
        }
        defer { model.resetConnection() }
        await model.reload()
        let erased = try #require(model.items.first { $0.id == threadID })
        let other = try #require(model.items.first { $0.id == otherID })
        await model.setThreadArchived(erased, archived: true)
        #expect(model.archiveErrors[threadID] != nil)
        #expect(model.archiveUnavailableReason(for: other) == nil)
        await model.setThreadArchived(other, archived: true)
        #expect(requests.snapshot.contains { $0.url!.path == "/v1/email/threads/\(otherID.uuidString)/archive" })
        #expect(!requests.snapshot.contains { $0.url!.path.hasSuffix("dismiss") })
    }

    /// New correspondence discovered by an archive status read updates source and draft freshness while retaining edits.
    @Test func testArchiveSourceChangePreservesEditsAndShowsStaleDraft() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            let pointReads = requests.snapshot.filter { $0.url!.path == "/v1/email/threads/\(self.threadID.uuidString)" }.count
            var thread = self.archiveThreadJSON(inInbox: true, status: pointReads > 1 ? "failed" : nil,
                draft: self.draftJSON())
            if pointReads > 1 {
                thread = thread.replacingOccurrences(of: "\"revision\":1,\"summary\"", with: "\"revision\":2,\"summary\"")
                    .replacingOccurrences(of: "Board discussion", with: "A new reply arrived")
                    .replacingOccurrences(of: "\"stale\":false", with: "\"stale\":true")
            }
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(thread)],\"next_cursor\":null}") }
            return (200, thread)
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Keep my wording")
        await model.checkArchiveStatus(threadID)
        #expect(model.thread?.revision == 2)
        #expect(model.thread?.subject == "A new reply arrived")
        #expect(model.draft?.stale == true)
        #expect(model.currentEdit?.body == "Keep my wording")
        #expect(!model.canReview)
    }

    /// An old foreground status read cannot change mailbox state or clear the connection after hiding Email.
    @Test(arguments: [200, 403])
    func testArchiveStatusReadIgnoresObsoleteActivation(status: Int) async throws {
        let requests = EmailRequestRecorder()
        let path = "/v1/email/threads/\(threadID.uuidString)"
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("refresh") { return (202, self.operationJSON(status: "RUNNING")) }
            if request.url!.path == path {
                Thread.sleep(forTimeInterval: 0.15)
                return (status, status == 200 ? self.archiveThreadJSON(inInbox: false, status: "completed")
                    : Self.error("authorization_error", "Read revoked"))
            }
            return (200, "{\"items\":[\(self.archiveThreadJSON(inInbox: true))],\"next_cursor\":null}")
        }
        defer { model.resetConnection() }
        model.setActive(true)
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.url!.path.hasSuffix("refresh") } && !model.isRefreshing }
        let read = Task { await model.checkArchiveStatus(threadID) }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.url!.path == path } }
        model.setActive(false)
        await read.value
        #expect(model.items.first?.inInbox == true)
        #expect(model.accounts.count == 2)
        #expect(model.archiveErrors.isEmpty)
    }

    /// Rapid repeated gestures share the in-flight admission and retain the same target.
    @Test func testArchiveConcurrentGesturesCoalesce() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("archive") {
                Thread.sleep(forTimeInterval: 0.15)
                return (202, self.operationJSON(status: "RUNNING"))
            }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let thread = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(thread)],\"next_cursor\":null}") }
            return (200, thread)
        }
        defer { model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let first = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.url!.path.hasSuffix("archive") } }
        await model.setThreadArchived(row, archived: false)
        await first.value
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
        #expect(model.items.first?.archiveOperation?.targetArchived == true)
        #expect(model.items.first?.inInbox == true)
    }

    /// Archiving one row cannot discard the body read for a different selected conversation.
    @Test func testArchiveOtherRowDoesNotInvalidateSelectedDetailRead() async throws {
        let requests = EmailRequestRecorder()
        let responseGate = EmailArchiveResponseGate()
        let otherID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let otherPath = "/v1/email/threads/\(otherID.uuidString)"
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "COMPLETED"), nil) }
            let other = self.archiveThreadJSON(inInbox: true, draft: self.draftJSON())
                .replacingOccurrences(of: self.threadID.uuidString, with: otherID.uuidString)
            if request.url!.path == otherPath { return (200, other, responseGate) }
            let archived = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let first = self.archiveThreadJSON(inInbox: !archived, status: archived ? "completed" : nil)
            if request.url!.path.hasSuffix("threads") {
                return (200, "{\"items\":[\(archived ? "" : first + ",")\(other)],\"next_cursor\":null}", nil)
            }
            return (200, first, nil)
        }
        defer { responseGate.release(); model.resetConnection() }
        await model.reload()
        let first = try #require(model.items.first { $0.id == threadID })
        let opening = Task { await model.openThread(otherID) }
        try await waitForEmailTestCondition { responseGate.isWaiting }
        await model.setThreadArchived(first, archived: true)
        responseGate.release()
        await opening.value

        #expect(model.selectedThreadID == otherID)
        #expect(model.thread?.id == otherID)
        #expect(model.thread?.subject == "Board discussion")
        #expect(model.draftError == nil)
        #expect(!model.isLoadingThread)
    }

    /// A point read buffered before autosave cannot restore an older clean draft after the save succeeds.
    @Test func testArchiveDelayedPointReadPreservesNewerSavedDraft() async throws {
        let requests = EmailRequestRecorder()
        let responseGate = EmailArchiveResponseGate()
        let pointPath = "/v1/email/threads/\(threadID.uuidString)"
        let savedBody = "Keep the wording that was just saved."
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: savedBody), nil) }
            let previous = self.archiveThreadJSON(inInbox: true, status: "pending", draft: self.draftJSON())
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(previous)],\"next_cursor\":null}", nil) }
            let delayed = request.url!.path == pointPath
                && requests.snapshot.filter { $0.url!.path == pointPath }.count == 2
            return (200, previous, delayed ? responseGate : nil)
        }
        defer { responseGate.release(); model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        let checking = Task { await model.checkArchiveStatus(threadID) }
        try await waitForEmailTestCondition { responseGate.isWaiting }
        model.changeEdit(\.body, to: savedBody)
        #expect(await model.saveDraft())
        #expect(model.draft?.revision == 2)
        #expect(model.currentEdit?.isDirty == false)
        responseGate.release()
        await checking.value

        #expect(model.draft?.revision == 2)
        #expect(model.currentEdit?.base.revision == 2)
        #expect(model.currentEdit?.body == savedBody)
        #expect(model.currentEdit?.isDirty == false)
    }

    /// Builds an isolated transport whose selected response can be delivered after an independent request finishes.
    private func makeArchiveRaceModel(
        handler: @escaping (URLRequest) throws -> (Int, String, EmailArchiveResponseGate?)
    ) throws -> EmailViewModel {
        let id = EmailArchiveRaceURLProtocol.register(handler)
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [EmailArchiveRaceURLProtocol.self]
        configuration.httpAdditionalHeaders = ["X-Email-Archive-Race": id]
        let api = VeetbotAPIClient(transport: HTTPTransport(
            configuration: try ConnectionConfiguration(baseURLString: "https://email.test"),
            tokenStore: InMemoryTokenStore(token: "test-token"), session: URLSession(configuration: configuration)
        ))
        return EmailViewModel(makeAPIClient: { api })
    }

    /// Adds explicit feature support without changing unrelated account fixture defaults.
    private static var archiveAccountsJSON: String {
        accountsJSON.replacingOccurrences(of: "\"read_server_id\":\"gmail_read\"",
            with: "\"archive_supported\":true,\"write_server_id\":\"gmail_write\",\"read_server_id\":\"gmail_read\"")
            .replacingOccurrences(of: "\"read_server_id\":\"gmail_work_read\"",
                with: "\"archive_supported\":true,\"write_server_id\":\"gmail_work_write\",\"read_server_id\":\"gmail_work_read\"")
    }

    /// Supplies mailbox state and durable action outcomes through the real JSON decoder.
    private func archiveThreadJSON(account: String = "personal", inInbox: Bool, status: String? = nil,
        targetArchived: Bool = true, draft: String = "null", operationID: UUID? = nil) -> String {
        let operation = status.map { "{\"operation_id\":\"\(operationID ?? threadID)\",\"run_id\":\"\(runID)\",\"target_archived\":\(targetArchived),\"status\":\"\($0)\",\"error\":null}" } ?? "null"
        return threadJSON(draft: draft).replacingOccurrences(of: "\"account_id\":\"personal\"",
            with: "\"account_id\":\"\(account)\",\"in_inbox\":\(inInbox),\"archive_operation\":\(operation)")
    }

    /// Reads the URLSession request stream without bypassing the production transport.
    private static func archiveRequestBody(_ request: URLRequest) throws -> [String: Any] {
        if let data = request.httpBody { return try JSONSerialization.jsonObject(with: data) as! [String: Any] }
        let stream = try #require(request.httpBodyStream)
        stream.open()
        defer { stream.close() }
        var data = Data()
        var bytes = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&bytes, maxLength: bytes.count)
            if count <= 0 { break }
            data.append(bytes, count: count)
        }
        return try JSONSerialization.jsonObject(with: data) as! [String: Any]
    }

    /// A successful attention command updates its summary without replacing the open draft.
    @Test func testDismissalUpdatesSelectedThreadWithoutLosingDraftEdits() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let dismissed = requests.snapshot.contains { $0.url!.path.hasSuffix("dismiss") }
            if request.url!.path.hasSuffix("threads") {
                return (200, dismissed ? "{\"items\":[],\"next_cursor\":null}" : self.pageJSON())
            }
            return (200, self.threadJSON(draft: self.draftJSON()).replacingOccurrences(
                of: "\"revision\":1,\"summary\"",
                with: "\"dismissed_revision\":\(dismissed ? "1" : "null"),\"revision\":1,\"summary\""
            ))
        }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Keep my unfinished reply")
        await model.dismissSelectedThread()
        let selected = try #require(model.thread)
        let encoded = try JSONSerialization.jsonObject(with: JSONEncoder().encode(selected)) as! [String: Any]
        #expect(encoded["dismissed_revision"] as? Int == 1)
        #expect(model.items.isEmpty)
        #expect(model.currentEdit?.body == "Keep my unfinished reply")
        #expect(model.selectedThreadID == threadID)
        #expect(model.thread?.isHandled == true)
    }

    /// Undo clears attention state, while a newer source revision reopens an older dismissal.
    @Test func testHandledStateCanBeUndoneAndNewSourceReopensIt() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON()) }
            let undone = requests.snapshot.contains { $0.url!.path.hasSuffix("dismiss") }
            return (200, self.threadJSON(draft: self.draftJSON()).replacingOccurrences(
                of: "\"revision\":1,\"summary\"",
                with: "\"dismissed_revision\":\(undone ? "null" : "1"),\"revision\":1,\"summary\""
            ))
        }
        await model.openThread(threadID)
        let handled = try #require(model.thread)
        #expect(handled.isHandled)
        await model.setThreadHandled(handled, handled: false)
        #expect(model.thread?.isHandled == false)
        #expect(model.items.map(\.id) == [threadID])
        let changed = threadJSON().replacingOccurrences(of: "\"revision\":1,\"summary\"",
            with: "\"dismissed_revision\":1,\"revision\":2,\"summary\"")
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        #expect(try decoder.decode(EmailThreadView.self, from: Data(changed.utf8)).isHandled == false)
    }

    /// A rejected inbox checkbox leaves the row unchanged and exposes a recoverable error.
    @Test func testFailedHandledActionKeepsThreadAndReportsError() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            if request.url!.path.hasSuffix("dismiss") { return (409, Self.error("invalid_state", "New mail arrived. Refresh this thread.")) }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.reload()
        let row = try #require(model.items.first)
        await model.setThreadHandled(row, handled: true)
        #expect(model.items.map(\.id) == [threadID])
        #expect(model.items.first?.isHandled == false)
        #expect(model.selectedThreadID == nil)
        #expect(model.errorMessage != nil)
        #expect(!model.isPerformingAction)
    }

    /// Handling another inbox row must not erase a failed send review or the selected draft's edits.
    @Test func testHandlingAnotherThreadPreservesSelectedDraftActionFailure() async throws {
        let otherJSON = threadJSON().replacingOccurrences(
            of: threadID.uuidString, with: "00000000-0000-0000-0000-000000000899"
        )
        let other = try JSONDecoder.server.decode(EmailThreadView.self, from: Data(otherJSON.utf8))
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            if request.url!.path.hasSuffix("send-proposal") {
                return (400, Self.error("invalid_argument", "Review could not be prepared."))
            }
            if request.url!.path.hasSuffix("dismiss") {
                return (200, otherJSON.replacingOccurrences(of: "\"revision\":1,\"summary\"",
                    with: "\"dismissed_revision\":1,\"revision\":1,\"summary\""))
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.prepareSend()
        #expect(model.draftError == "Review could not be prepared.")
        model.changeEdit(\.body, to: "Keep this unsent edit")
        await model.setThreadHandled(other, handled: true)
        #expect(model.draftError == "Review could not be prepared.")
        #expect(model.selectedThreadID == threadID)
        #expect(model.currentEdit?.body == "Keep this unsent edit")
        #expect(model.thread?.isHandled == false)
    }

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

    /// The first refresh exhausts two transport POST attempts; one manual retry is POST three.
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

    /// Gateway and timeout recovery must poll the existing operation without a second admission.
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

    /// Sustained failure stops after three status reads, including past the next backoff deadline.
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

    /// Reading recovered mail must not erase a previously rejected owner action.
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

    /// A toolbar task can outlive Email visibility without owning the foreground loop task.
    @Test func testManualRefreshDoesNotReloadAfterEmailIsHidden() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                if requests.snapshot.filter({ $0.httpMethod == "POST" }).count == 2 {
                    Thread.sleep(forTimeInterval: 0.15)
                }
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.setActive(false) }
        for _ in 0..<1000 {
            if requests.snapshot.contains(where: { $0.httpMethod == "POST" }) && !model.isRefreshing { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        #expect(!model.isRefreshing)
        let manualRefresh = Task { await model.refresh() }
        for _ in 0..<1000 {
            if requests.snapshot.filter({ $0.httpMethod == "POST" }).count == 2 { break }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 2)
        model.setActive(false)
        let readsAtExit = requests.snapshot.filter { $0.httpMethod == "GET" }.count
        await manualRefresh.value
        #expect(requests.snapshot.filter { $0.httpMethod == "GET" }.count == readsAtExit)
        #expect(model.errorMessage == nil)
        #expect(!model.isRefreshing)
    }

    /// Initial, manual and polled projections retain their originating visit across hide and reentry.
    @Test(arguments: ["initial", "manual", "poll", "reopenedManual"], [200, 403, 404])
    func testForegroundProjectionReadIgnoresResponsesAfterHide(origin: String, status: Int) async throws {
        let initial = origin == "initial"
        let polling = origin == "poll"
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") {
                let count = requests.snapshot.filter { $0.url!.path.hasSuffix("accounts") }.count
                if count == (initial ? 1 : 3) {
                    Thread.sleep(forTimeInterval: 0.15)
                    return (status, status == 200
                        ? Self.accountsJSON.replacingOccurrences(of: "Personal", with: "Obsolete account")
                        : Self.error(status == 403 ? "permission_denied" : "not_found", "Obsolete projection failure"))
                }
                return (200, Self.accountsJSON)
            }
            if request.url!.path.hasSuffix("refresh") {
                return (200, self.operationJSON(status: polling ? "RUNNING" : "COMPLETED"))
            }
            if request.url!.path.contains("/operations/") { return (200, self.operationJSON(status: "COMPLETED")) }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.resetConnection() }
        if !initial {
            try await waitForEmailTestCondition { requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing }
        }
        let manual = (initial || polling) ? nil : Task { await model.refresh() }
        try await waitForEmailTestCondition { requests.snapshot.filter { $0.url!.path.hasSuffix("accounts") }.count == (initial ? 1 : 3) }
        model.setActive(false)
        if origin == "reopenedManual" { model.setActive(true) }
        await manual?.value
        try await Task.sleep(nanoseconds: 200_000_000)
        #expect(model.accounts.first?.label == (initial ? nil : "Personal"))
        #expect(model.items.count == (initial ? 0 : 1))
        #expect(model.errorMessage == nil)
        #expect(!model.unavailable)
    }

    /// A foreground thread refresh must not replace cached content or revoke the connection after hiding.
    @Test(arguments: [false, true], [200, 403])
    func testForegroundThreadReadIgnoresResponsesAfterHide(fallbackDraft: Bool, status: Int) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            if request.url!.path.hasSuffix("refresh") { return (200, self.operationJSON(status: "COMPLETED")) }
            if request.url!.path.contains("/drafts/") {
                Thread.sleep(forTimeInterval: 0.15)
                return (status, status == 200 ? self.draftJSON(revision: 2)
                    : Self.error("permission_denied", "Obsolete draft failure"))
            }
            if requests.snapshot.filter({ $0.url!.path.contains("/threads/") }).count == 3 {
                if fallbackDraft { return (200, self.threadJSON()) }
                Thread.sleep(forTimeInterval: 0.15)
                return (status, status == 200
                    ? self.threadJSON(draft: self.draftJSON()).replacingOccurrences(of: "Board discussion", with: "Obsolete subject")
                    : Self.error("permission_denied", "Obsolete thread failure"))
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.setActive(true)
        defer { model.resetConnection() }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing }
        model.changeEdit(\.body, to: "Retain this unsent reply")
        let manual = Task { await model.refresh() }
        try await waitForEmailTestCondition {
            fallbackDraft ? requests.snapshot.contains { $0.url!.path.contains("/drafts/") }
                : requests.snapshot.filter { $0.url!.path.contains("/threads/") }.count == 3
        }
        model.setActive(false)
        await manual.value
        #expect(model.thread?.subject == "Board discussion")
        #expect(model.currentEdit?.body == "Retain this unsent reply")
        #expect(model.selectedThreadID == threadID)
        #expect(model.draft?.revision == 1)
        #expect(model.draftError == nil)
    }

    /// Approval recovery retains the foreground identity through both of its dependent reads.
    @Test(arguments: ["accounts", "approval", "directApproval"], [200, 401])
    func testForegroundApprovalRecoveryIgnoresResponsesAfterHide(stage: String, status: Int) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") {
                if stage == "accounts", requests.snapshot.filter({ $0.url!.path.hasSuffix("accounts") }).count == 3 {
                    Thread.sleep(forTimeInterval: 0.15)
                    return (status, status == 200
                        ? Self.accountsJSON.replacingOccurrences(of: "Personal", with: "Obsolete approval account")
                        : Self.error("unauthenticated", "Obsolete approval lookup"))
                }
                return (200, Self.accountsJSON)
            }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            if request.url!.path.hasSuffix("refresh") { return (200, self.operationJSON(status: "COMPLETED")) }
            if request.url!.path.contains("/approvals/") {
                if stage != "accounts" { Thread.sleep(forTimeInterval: 0.15) }
                return (status, status == 200 ? self.approvalJSON(body: "Thanks, I will review the agenda.")
                    : Self.error("unauthenticated", "Obsolete approval lookup"))
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.setActive(true)
        defer { model.resetConnection() }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing }
        let read = Task {
            if stage == "directApproval" { await model.loadReview() }
            else { await model.openThread(threadID, approvalID: approvalID, refreshOnly: true) }
        }
        try await waitForEmailTestCondition {
            stage == "accounts" ? requests.snapshot.filter { $0.url!.path.hasSuffix("accounts") }.count == 3
                : requests.snapshot.contains { $0.url!.path.contains("/approvals/") }
        }
        model.setActive(false)
        await read.value
        #expect(model.accounts.first?.label == "Personal")
        #expect(model.selectedThreadID == threadID)
        #expect(model.review == nil)
        #expect(model.draftError == nil)
    }

    /// Learning and draft-history panels cannot publish results or erase mail after their active visit ends.
    @Test(arguments: ["learning", "revisions"], [200, 403])
    func testForegroundPanelReadIgnoresResponsesAfterHide(panel: String, status: Int) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            if request.url!.path.hasSuffix("refresh") { return (200, self.operationJSON(status: "COMPLETED")) }
            if request.url!.path.hasSuffix(panel) {
                Thread.sleep(forTimeInterval: 0.15)
                let body = panel == "learning"
                    ? "{\"paused\":true,\"profile_revision\":2,\"excluded_sources\":0,\"style_examples\":1,\"history_processed\":25,\"history_complete\":false}"
                    : "{\"items\":[\(self.draftJSON())],\"next_cursor\":null}"
                return (status, status == 200 ? body : Self.error("permission_denied", "Obsolete panel failure"))
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.setActive(true)
        defer { model.resetConnection() }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing }
        let read = Task {
            if panel == "learning" { await model.loadLearning() }
            else { await model.loadRevisions() }
        }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.url!.path.hasSuffix(panel) } }
        model.setActive(false)
        await read.value
        #expect(model.learning == nil)
        #expect(model.revisions.isEmpty)
        #expect(model.selectedThreadID == threadID)
        #expect(model.errorMessage == nil)
        #expect(model.draftError == nil)
    }

    /// Foreground pagination follows the same visibility boundary as complete inbox reloads.
    @Test(arguments: [200, 403])
    func testForegroundNextPageIgnoresResponsesAfterHide(status: Int) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") { return (200, self.operationJSON(status: "COMPLETED")) }
            if request.url!.query?.contains("cursor=") == true {
                Thread.sleep(forTimeInterval: 0.15)
                return (status, status == 200
                    ? self.pageJSON().replacingOccurrences(of: self.threadID.uuidString,
                        with: "00000000-0000-0000-0000-000000000899")
                    : Self.error("permission_denied", "Obsolete page failure"))
            }
            return (200, self.pageJSON(cursor: "next"))
        }
        model.setActive(true)
        defer { model.resetConnection() }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing }
        let page = Task { await model.loadMore() }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.url!.query?.contains("cursor=") == true } }
        model.setActive(false)
        await page.value
        #expect(model.items.map(\.id) == [threadID])
        #expect(model.errorMessage == nil)
        #expect(!model.isLoading)
    }

    /// Returning to Email must resume admission even if an older toolbar request has not returned.
    @Test func testReactivationDoesNotReuseAnObsoleteManualRefresh() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                if requests.snapshot.filter({ $0.httpMethod == "POST" }).count == 2 {
                    Thread.sleep(forTimeInterval: 0.15)
                    return (200, self.operationJSON(status: "FAILED"))
                }
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.resetConnection() }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing }
        let manual = Task { await model.refresh() }
        try await waitForEmailTestCondition { requests.snapshot.filter { $0.httpMethod == "POST" }.count == 2 }
        model.setActive(false)
        model.setActive(true)
        await manual.value
        try await Task.sleep(nanoseconds: 200_000_000)
        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(posts.count == 3)
        if posts.count == 3 {
            #expect(posts[1].value(forHTTPHeaderField: "Idempotency-Key") == posts[2].value(forHTTPHeaderField: "Idempotency-Key"))
        }
        #expect(model.errorMessage == nil)
        #expect(!model.isRefreshing)
    }

    /// Already-cancelled standalone reads do not clear cached state, even while the mode is inactive.
    @Test func testCancelledStandaloneReadsPreserveCachedState() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.reload()
        await model.openThread(threadID)
        let cancelled = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            await model.reload()
            await model.openThread(UUID())
        }
        await cancelled.value
        #expect(model.items.count == 1)
        #expect(model.selectedThreadID == threadID)
        #expect(model.thread?.subject == "Board discussion")
        #expect(model.errorMessage == nil)
        #expect(model.draftError == nil)
    }

    /// Waits for a concrete transport boundary without depending on fixed scheduling delays.
    private func waitForEmailTestCondition(_ condition: () -> Bool) async throws {
        for _ in 0..<800 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        #expect(condition(), "Expected email transport boundary within four seconds")
    }

    /// Cancelling the owned poll or replacing its connection must not surface an old failure.
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

    /// Produces a content-free admitted-operation response for the transport fixture.
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

    /// Isolates each model's URLSession route and allows real transport errors from its handler.
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

/// Holds one response without blocking URLSession from servicing an independent save or archive request.
private final class EmailArchiveResponseGate: @unchecked Sendable {
    private let lock = NSLock()
    private var delivery: (() -> Void)?
    private var released = false

    /// Signals that the original HTTP result has been captured and can safely be ordered after another request.
    var isWaiting: Bool { lock.withLock { delivery != nil } }

    /// Retains the response until release, including the case where cleanup released it before registration.
    func install(_ callback: @escaping () -> Void) {
        let deliverNow = lock.withLock {
            if released { return true }
            delivery = callback
            return false
        }
        if deliverNow { callback() }
    }

    /// Delivers at most once and runs callbacks outside the state lock.
    func release() {
        let callback = lock.withLock {
            released = true
            let callback = delivery
            delivery = nil
            return callback
        }
        callback?()
    }
}

/// Allows deterministic response ordering while preserving the production JSON and HTTP transport paths.
private final class EmailArchiveRaceURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var handlers: [String: (URLRequest) throws -> (Int, String, EmailArchiveResponseGate?)] = [:]

    /// Each model receives a distinct response handler so delayed delivery cannot affect another test.
    static func register(_ handler: @escaping (URLRequest) throws -> (Int, String, EmailArchiveResponseGate?)) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }

    /// Intercepts only the ephemeral test session that explicitly installs this protocol.
    override class func canInit(with request: URLRequest) -> Bool { true }

    /// Leaves the production request unchanged for route and body assertions.
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    /// Captures server state now and optionally waits for the test to release its response.
    override func startLoading() {
        guard let id = request.value(forHTTPHeaderField: "X-Email-Archive-Race"),
              let handler = Self.lock.withLock({ Self.handlers[id] }) else { return }
        do {
            let (status, body, gate) = try handler(request)
            let deliver = {
                let response = HTTPURLResponse(url: self.request.url!, statusCode: status, httpVersion: nil,
                    headerFields: ["Content-Type": "application/json"])!
                self.client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                self.client?.urlProtocol(self, didLoad: Data(body.utf8))
                self.client?.urlProtocolDidFinishLoading(self)
            }
            if let gate { gate.install(deliver) } else { deliver() }
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    /// Deferred callbacks are released explicitly by each test, including cleanup paths.
    override func stopLoading() {}
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
    /// Allocates a distinct fixture host so concurrent tests cannot replace each other's routes.
    static func register(_ handler: @escaping (URLRequest) throws -> (Int, String)) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }
    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    /// Delivers HTTP responses and URLSession failures through the same production transport path.
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
