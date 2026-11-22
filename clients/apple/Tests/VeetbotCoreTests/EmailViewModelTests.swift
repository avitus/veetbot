import Combine
import CryptoKit
import Foundation
import Testing
#if os(macOS)
import AppKit
import SwiftUI
#endif
@testable import VeetbotCore

@Suite(.serialized) @MainActor struct EmailViewModelTests {
    private let threadID = UUID(uuidString: "00000000-0000-0000-0000-000000000801")!
    private let draftID = UUID(uuidString: "00000000-0000-0000-0000-000000000802")!
    private let runID = UUID(uuidString: "00000000-0000-0000-0000-000000000803")!
    private let approvalID = UUID(uuidString: "00000000-0000-0000-0000-000000000804")!

    /// The independently authorized inbox can render while account freshness is still loading.
    @Test func testInboxRowsAppearBeforeSlowAccountMetadata() async throws {
        let gate = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, gate) }
            return (200, self.pageJSON(), nil)
        }
        defer { gate.release(); model.resetConnection() }
        let loading = Task { await model.reload() }
        try await waitForEmailTestCondition { gate.isWaiting }
        try await waitForEmailTestCondition { model.items.count == 1 }
        #expect(!model.isLoading)
        #expect(model.accounts.isEmpty)
        gate.release()
        await loading.value
        #expect(model.accounts.count == 2)
    }

    /// Finishing account metadata cannot dismiss the loader owned by a newer page read.
    @Test(arguments: [200, 404])
    func testLateAccountsDoNotFinishAnOverlappingPageLoad(status: Int) async throws {
        let accounts = EmailArchiveResponseGate()
        let page = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("accounts") { return (status, status == 200 ? Self.accountsJSON : Self.error("not_found", "Unavailable"), accounts) }
            let paging = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.contains { $0.name == "cursor" } == true
            return (200, self.pageJSON(cursor: paging ? nil : "next"), paging ? page : nil)
        }
        defer { accounts.release(); page.release(); model.resetConnection() }
        let loading = Task { await model.reload() }
        try await waitForEmailTestCondition { model.items.count == 1 }
        let paging = Task { await model.loadMore() }
        try await waitForEmailTestCondition { page.isWaiting }
        #expect(model.isLoading)
        accounts.release()
        await loading.value
        #expect(model.isLoading == (status == 200))
        page.release()
        await paging.value
        #expect(!model.isLoading)
        #expect(model.items.count == (status == 200 ? 1 : 0))
        #expect(model.unavailable == (status == 404))
    }

    /// Late metadata failures remain visible; revoked access removes the early rows.
    @Test(arguments: [403, 500])
    func testAccountFailureAfterEarlyInboxPublication(status: Int) async throws {
        let gate = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("accounts") {
                return (status, "{\"error\":{\"code\":\"forbidden\",\"message\":\"Unavailable\"}}", gate)
            }
            return (200, self.pageJSON(), nil)
        }
        defer { gate.release(); model.resetConnection() }
        let loading = Task { await model.reload() }
        try await waitForEmailTestCondition { model.items.count == 1 }
        gate.release()
        await loading.value
        #expect(model.errorMessage != nil)
        #expect(model.items.count == (status == 403 ? 0 : 1))
    }

    /// Connection reset invalidates the shared conversation cache as well as list/detail state.
    @Test func testConnectionResetClearsPreparedConversations() async throws {
        let thread = try JSONDecoder.server.decode(EmailThreadView.self, from: Data(threadJSON().utf8))
        _ = try await EmailConversationCache.shared.conversation(for: thread, ownerAddress: nil)
        #expect(EmailConversationCache.shared.parses(of: threadID) == 1)
        EmailViewModel(makeAPIClient: { nil }).resetConnection()
        #expect(EmailConversationCache.shared.parses(of: threadID) == 0)
    }

    @Test func testDraftAttachmentManifestSurvivesNativeRoundTrip() throws {
        var payload = try #require(JSONSerialization.jsonObject(with: Data(draftJSON().utf8)) as? [String: Any])
        let fileID = UUID().uuidString
        payload["attachments"] = [["artifact_id": fileID, "filename": "board.pdf",
            "media_type": "application/pdf", "size_bytes": 123, "sha256": String(repeating: "a", count: 64)]]
        let draft = try JSONDecoder.server.decode(EmailDraftView.self,
            from: JSONSerialization.data(withJSONObject: payload))
        let encoded = try #require(JSONSerialization.jsonObject(with: JSONEncoder.server.encode(draft)) as? [String: Any])
        let files = try #require(encoded["attachments"] as? [[String: Any]])
        #expect(files.first?["artifact_id"] as? String == fileID)
    }

    @Test(arguments: ["matching", "different_id", "different_digest"])
    func testReviewAcceptsRedactedFilenameOnlyForTheSameFile(change: String) async throws {
        let fileID = UUID().uuidString
        let file: [String: Any] = ["artifact_id": fileID, "filename": "token=abc.pdf",
            "media_type": "application/pdf", "size_bytes": 123, "sha256": String(repeating: "a", count: 64)]
        var draft = try #require(JSONSerialization.jsonObject(with: Data(draftJSON(status: "awaiting_approval").utf8)) as? [String: Any])
        draft["attachments"] = [file]
        draft["send_tool_name"] = "mcp.gmail_send.send_attached_message"
        let draftText = String(data: try JSONSerialization.data(withJSONObject: draft), encoding: .utf8)!
        var presented = file
        presented["filename"] = "[REDACTED]"
        if change == "different_id" { presented["artifact_id"] = UUID().uuidString }
        if change == "different_digest" { presented["sha256"] = String(repeating: "b", count: 64) }
        var approval = try #require(JSONSerialization.jsonObject(with: Data(approvalJSON(body: "Thanks, I will review the agenda.").utf8)) as? [String: Any])
        var arguments = try #require(approval["arguments"] as? [String: Any])
        arguments["attachments"] = [presented]
        approval["arguments"] = arguments
        approval["tool_name"] = "mcp.gmail_send.send_attached_message"
        let approvalText = String(data: try JSONSerialization.data(withJSONObject: approval), encoding: .utf8)!
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/approvals/") { return (200, approvalText) }
            return (200, self.threadJSON(draft: draftText))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.loadReview()
        #expect((model.review != nil) == (change == "matching"))
    }

    @Test func testSendAcknowledgesTheDisplayedAttachmentIDs() async throws {
        let fileID = UUID()
        var payload = try #require(JSONSerialization.jsonObject(with: Data(draftJSON().utf8)) as? [String: Any])
        payload["attachments"] = [["artifact_id": fileID.uuidString, "filename": "board.pdf",
            "media_type": "application/pdf", "size_bytes": 123, "sha256": String(repeating: "a", count: 64)]]
        let withFile = String(data: try JSONSerialization.data(withJSONObject: payload), encoding: .utf8)!
        let requests = EmailRequestRecorder()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("/send") {
                requests.append(request)
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "QUEUED")) }
            return (200, self.threadJSON(draft: withFile))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.sendNow()
        let request = try #require(requests.snapshot.first)
        let sent = try Self.archiveRequestBody(request)
        #expect(sent["reviewed_attachment_ids"] as? [String] == [fileID.uuidString])
    }

    @Test func testRemovingDraftAttachmentSavesOnlyReviewedIDs() async throws {
        let fileID = UUID()
        var payload = try #require(JSONSerialization.jsonObject(with: Data(draftJSON().utf8)) as? [String: Any])
        payload["attachments"] = [["artifact_id": fileID.uuidString, "filename": "board.pdf",
            "media_type": "application/pdf", "size_bytes": 123, "sha256": String(repeating: "a", count: 64)]]
        let withFile = String(data: try JSONSerialization.data(withJSONObject: payload), encoding: .utf8)!
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            if request.httpMethod == "PUT" {
                requests.append(request)
                return (200, self.draftJSON(revision: 2).dropLast() + ",\"attachments\":[]}")
            }
            return (200, self.threadJSON(draft: withFile))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.removeDraftAttachment(fileID)
        let request = try #require(requests.snapshot.first)
        let sent = try Self.archiveRequestBody(request)
        #expect(sent["attachment_ids"] as? [String] == [])
        #expect(model.currentEdit?.attachmentIDs == [])
        #expect(model.currentEdit?.isDirty == false)
    }

    @Test func testCollectionSwitchShowsCachedRowsBeforeSlowRefresh() async throws {
        let requests = EmailRequestRecorder()
        let gate = DispatchSemaphore(value: 0)
        defer { gate.signal() }
        let otherID = UUID()
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            requests.append(request)
            if requests.snapshot.count > 2 {
                _ = gate.wait(timeout: .now() + 5)
                return (502, Self.error("service_unavailable", "Temporary failure"))
            }
            let page = self.pageJSON(cursor: "next")
            return (200, request.url!.query!.contains("view=inbox")
                ? page.replacingOccurrences(of: self.threadID.uuidString, with: otherID.uuidString) : page)
        }
        defer { model.resetConnection() }
        await model.reload()
        model.setListView("inbox")
        try await waitForEmailTestCondition { model.items.first?.id == otherID && !model.isReadingList }
        model.setListView("priority")
        // No yield or network wait is allowed before the cached list is visible.
        #expect(model.items.map(\.id) == [threadID])
        #expect(model.hasMore)
        #expect(!model.isLoading)
        try await waitForEmailTestCondition { requests.snapshot.count == 3 }
        #expect(model.items.map(\.id) == [threadID])
        #expect(!model.isLoading)
        gate.signal()
        try await waitForEmailTestCondition { !model.isReadingList }
        #expect(model.items.map(\.id) == [threadID])
        model.setListView("inbox")
        #expect(model.items.map(\.id) == [otherID])
    }

    @Test(arguments: ["connection", "account", "search"])
    func testCollectionCacheDoesNotCrossQueryBoundaries(boundary: String) async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            return (200, self.pageJSON())
        }
        defer { model.resetConnection() }
        await model.reload()
        model.setListView("inbox")
        try await waitForEmailTestCondition { !model.isReadingList && !model.items.isEmpty }
        switch boundary {
        case "connection": model.resetConnection(); model.setListView("inbox")
        case "account": model.setAccount("work")
        default: model.setSearchText("unrelated")
        }
        model.setListView("priority")
        #expect(model.items.isEmpty)
    }

    @Test func testHandledChangeInvalidatesCachedImportantList() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("dismiss") {
                return (200, self.threadJSON().replacingOccurrences(of: "\"summary\"",
                    with: "\"dismissed_revision\":1,\"summary\""))
            }
            return (200, self.pageJSON())
        }
        defer { model.resetConnection() }
        await model.reload()
        model.setListView("inbox")
        try await waitForEmailTestCondition { !model.isReadingList && !model.items.isEmpty }
        let row = try #require(model.items.first)
        #expect(await model.setThreadHandled(row, handled: true, confirms: false))
        model.setListView("priority")
        #expect(model.items.isEmpty, "Do not redisplay the old unhandled Important snapshot")
    }

    /// A thread read can discover mailbox changes made on another device.
    @Test(arguments: ["\"in_inbox\":false", "\"in_spam\":true", "\"dismissed_revision\":1"])
    func testThreadReadInvalidatesChangedCollectionCache(change: String) async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON()) }
            if request.url!.path.hasSuffix(self.threadID.uuidString) {
                return (200, self.threadJSON().replacingOccurrences(of: "\"summary\"", with: "\(change),\"summary\""))
            }
            return (200, self.pageJSON())
        }
        defer { model.resetConnection() }
        await model.reload()
        model.setListView("inbox")
        try await waitForEmailTestCondition { !model.isReadingList && !model.items.isEmpty }
        await model.openThread(threadID)
        model.setListView("priority")
        #expect(model.items.isEmpty, "Do not restore a snapshot invalidated by another device")
    }

    /// A new source revision reopens handled mail even if the dismissal marker stays unchanged.
    @Test(arguments: [1, 2])
    func testThreadRevisionInvalidatesCacheOnlyWhenHandledStateChanges(revision: Int) async throws {
        let otherID = UUID()
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON()) }
            let handled = self.threadJSON().replacingOccurrences(of: "\"summary\"", with: "\"dismissed_revision\":1,\"summary\"")
            if request.url!.path.hasSuffix(self.threadID.uuidString) {
                return (200, handled.replacingOccurrences(of: "\"revision\":1", with: "\"revision\":\(revision)"))
            }
            if request.url!.query?.contains("view=inbox") == true {
                return (200, "{\"items\":[\(handled)],\"next_cursor\":null}")
            }
            return (200, self.pageJSON().replacingOccurrences(of: self.threadID.uuidString, with: otherID.uuidString))
        }
        defer { model.resetConnection() }
        await model.reload()
        #expect(model.items.map(\.id) == [otherID])
        model.setListView("inbox")
        try await waitForEmailTestCondition { !model.isReadingList && model.items.first?.id == threadID }
        await model.openThread(threadID)
        #expect(model.thread?.isHandled == (revision == 1))
        model.setListView("priority")
        #expect(model.items.map(\.id) == (revision == 1 ? [otherID] : []))
    }

    /// An older reload or page must not restore rows invalidated by a newer detail read.
    @Test(arguments: [false, true])
    func testThreadReadInvalidatesInflightCollectionRead(paging: Bool) async throws {
        let requests = EmailRequestRecorder()
        let gate = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON(), nil) }
            if request.url!.path.hasSuffix(self.threadID.uuidString) {
                return (200, self.archiveThreadJSON(inInbox: false), nil)
            }
            requests.append(request)
            if requests.snapshot.count > 3 { return (502, Self.error("service_unavailable", "Temporary failure"), nil) }
            let held = requests.snapshot.count == 3
            return (200, self.pageJSON(cursor: held && paging ? nil : "next"), held ? gate : nil)
        }
        defer { gate.release(); model.resetConnection() }
        await model.reload()
        model.setListView("inbox")
        try await waitForEmailTestCondition { !model.isReadingList && requests.snapshot.count == 2 }
        let pending = Task {
            if paging { await model.loadMore() } else { await model.reload(preserveOrder: true) }
        }
        try await waitForEmailTestCondition { gate.isWaiting }
        await model.openThread(threadID)
        // The replacement read may own the flag now; the abandoned read stays gated.
        try await waitForEmailTestCondition { requests.snapshot.count == 4 && !model.isReadingList }
        #expect(!model.isReadingList && !model.isLoading)
        #expect(model.items.isEmpty, "The accepted archive must also remove the visible Inbox row")
        gate.release()
        await pending.value
        model.setListView("priority")
        model.setListView("inbox")
        #expect(model.items.isEmpty, "An obsolete response must not recreate the invalidated snapshot")
    }

    /// A changed detail replaces invalid pagination without clearing surviving visible mail.
    @Test func testThreadReadRestoresPaginationAfterMailboxChange() async throws {
        let requests = EmailRequestRecorder()
        let gate = EmailArchiveResponseGate()
        let survivorID = UUID()
        let survivor = threadJSON().replacingOccurrences(of: threadID.uuidString, with: survivorID.uuidString)
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON(), nil) }
            if request.url!.path.hasSuffix(self.threadID.uuidString) {
                return (200, self.archiveThreadJSON(inInbox: false), nil)
            }
            requests.append(request)
            if requests.snapshot.count == 1 {
                return (200, "{\"items\":[\(self.threadJSON()),\(survivor)],\"next_cursor\":\"old\"}", nil)
            }
            return (200, "{\"items\":[\(survivor)],\"next_cursor\":\"fresh\"}", gate)
        }
        defer { gate.release(); model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        #expect(model.items.map(\.id) == [survivorID])
        #expect(!model.isLoading)
        try await waitForEmailTestCondition { gate.isWaiting }
        #expect(model.items.map(\.id) == [survivorID], "Keep surviving rows while replacement pagination loads")
        #expect(requests.snapshot.last?.url?.query?.contains("view=priority") == true)
        gate.release()
        try await waitForEmailTestCondition { !model.isReadingList }
        #expect(model.hasMore, "Replacement metadata must restore automatic pagination")
        #expect(model.thread?.id == threadID, "List repair must preserve the selected detail")
    }

    /// Opening mail is inert; each load or retry requires another explicit request.
    @Test func testImagesLoadOnlyByExplicitRequestAndDoNotRetryOnFailure() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("images") {
                requests.append(request)
                if requests.snapshot.count == 3 { return (200, "image-fixture") }
                return (502, Self.error("email_image_unavailable", "Image could not be loaded."))
            }
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON()) }
            let message = """
            {"id":"image-message","sender":"alex@example.test","to":[],"cc":[],"subject":"Photo","body":"Photo","reader_image_body":"![Photo](veetbot-email-image:0)","sent_at":"2026-09-11T00:00:00Z","complete":true}
            """
            return (200, self.threadJSON().replacingOccurrences(of: "\"messages\":[]", with: "\"messages\":[\(message)]"))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        #expect(requests.snapshot.isEmpty)
        let message = try #require(model.thread?.messages?.first)
        for count in 1...2 {
            do {
                _ = try await model.loadImage(threadID: threadID, messageID: message.id, index: 0)
                Issue.record("Unavailable image unexpectedly loaded")
            } catch {
                #expect(requests.snapshot.count == count)
            }
        }
        let data = try await model.loadImage(threadID: threadID, messageID: message.id, index: 0)
        #expect(data == Data("image-fixture".utf8))
        #expect(requests.snapshot.count == 3)
    }

    #if os(macOS)
    /// A confirmed handled anchor stops restoration paging; an eligible displaced anchor is still found.
    @Test(arguments: [true, false], [1, 2]) func testMacImportantRefreshSkipsOnlyIneligibleAnchors(handled: Bool, selectedRevision: Int) async throws {
        let requests = EmailRequestRecorder()
        let otherID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let other = threadJSON().replacingOccurrences(of: threadID.uuidString, with: otherID.uuidString)
        let selected = threadJSON().replacingOccurrences(of: "\"revision\":1,", with: "\"revision\":\(selectedRevision),")
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON()) }
            if request.url!.path.hasSuffix("dismiss") {
                return (200, selected.replacingOccurrences(of: "\"summary\"",
                    with: "\"dismissed_revision\":\(selectedRevision),\"summary\""))
            }
            guard request.url!.path.hasSuffix("threads") else { return (200, selected) }
            requests.append(request)
            if requests.snapshot.count == 1 { return (200, self.pageJSON()) }
            if request.url!.query?.contains("cursor=") == true {
                if handled { return (503, Self.error("service_unavailable", "Unexpected anchor scan.")) }
                return (200, self.pageJSON(cursor: "later"))
            }
            return (200, "{\"items\":[\(other)],\"next_cursor\":\"next\"}")
        }
        defer { model.resetConnection() }
        #expect(model.listView == "priority")
        await model.reload()
        await model.openThread(threadID)
        model.scrollAnchor = threadID
        if handled {
            let detail = try #require(model.thread)
            #expect(await model.setThreadHandled(detail, handled: true))
        } else {
            await model.reload(preserveOrder: true)
        }
        #expect(requests.snapshot.count == (handled ? 2 : 3))
        #expect(model.items.map(\.id) == (handled ? [otherID] : [threadID, otherID]))
        #expect(model.hasMore)
        #expect(model.errorMessage == nil)
        #expect(model.selectedThreadID == threadID)
    }

    /// Each Mac list stops chasing a visible thread that has just left it: marking it handled
    /// takes it out of Important and Needs you, and marking it not handled takes it out of
    /// Handled. Inbox keeps handled mail, so there the refresh still finds it (ADR-0303).
    @Test(arguments: ["priority", "needs_you", "handled", "inbox"])
    func testMacListRefreshChasesOnlyAnchorsStillInTheList(view: String) async throws {
        let requests = EmailRequestRecorder()
        let otherID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let other = threadJSON().replacingOccurrences(of: threadID.uuidString, with: otherID.uuidString)
        let inHandled = view == "handled"
        let stillListed = view == "inbox"
        let base = inHandled
            ? threadJSON().replacingOccurrences(of: "\"summary\"", with: "\"dismissed_revision\":1,\"summary\"")
            : threadJSON()
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON()) }
            if request.url!.path.hasSuffix("dismiss") {
                return (200, self.threadJSON().replacingOccurrences(of: "\"summary\"",
                    with: "\"dismissed_revision\":\(inHandled ? "null" : "1"),\"summary\""))
            }
            guard request.url!.path.hasSuffix("threads") else { return (200, base) }
            requests.append(request)
            if requests.snapshot.count == 1 { return (200, "{\"items\":[\(base)],\"next_cursor\":null}") }
            if request.url!.query?.contains("cursor=") == true {
                if !stillListed { return (503, Self.error("service_unavailable", "Unexpected anchor scan.")) }
                return (200, self.pageJSON(cursor: "later"))
            }
            return (200, "{\"items\":[\(other)],\"next_cursor\":\"next\"}")
        }
        defer { model.resetConnection() }
        if view == model.listView { await model.reload() } else { model.setListView(view) }
        for _ in 0..<200 where model.items.isEmpty { try await Task.sleep(for: .milliseconds(10)) }
        await model.openThread(threadID)
        model.scrollAnchor = threadID
        let detail = try #require(model.thread)
        #expect(await model.setThreadHandled(detail, handled: !inHandled, confirms: false))
        #expect(requests.snapshot.count == (stillListed ? 3 : 2))
        #expect(model.items.map(\.id) == (stillListed ? [threadID, otherID] : [otherID]))
        #expect(model.errorMessage == nil)
    }

    /// Keeping the visible thread in place searches at most five pages beyond the rows on
    /// screen; past that the refresh keeps its rows and gives up the position (ADR-0303).
    @Test func testRestoringTheVisibleThreadSearchesABoundedNumberOfPages() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            guard request.url!.path.hasSuffix("threads") else { return (200, self.threadJSON()) }
            requests.append(request)
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems ?? []
            let page = Int(query.first { $0.name == "cursor" }?.value ?? "0")!
            let id = String(format: "00000000-0000-0000-0000-%012d", 801 + page)
            let row = self.threadJSON().replacingOccurrences(of: self.threadID.uuidString, with: id)
            return (200, "{\"items\":[\(row)],\"next_cursor\":\(page < 40 ? "\"\(page + 1)\"" : "null")}")
        }
        defer { model.resetConnection() }
        await model.reload()
        let before = requests.snapshot.count
        model.scrollAnchor = UUID()
        await model.reload(preserveOrder: true)
        #expect(requests.snapshot.count - before == 6, "one page for the rows, then five to search")
        #expect(model.items.count == 6)
        #expect(model.errorMessage == nil)
    }

    /// Every Mac list scrolls through a page boundary and retains its visible conversation on
    /// refresh, Inbox and the collections as well as Important (ADR-0303).
    @Test(arguments: ["priority", "inbox", "needs_you", "handled", "other"])
    func testMacListsRequestFullPagesAndScroll(view: String) async throws {
        let requests = EmailRequestRecorder()
        let reorderedAnchor = EmailTestAnchor()
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if !request.url!.path.hasSuffix("threads") { return (200, self.threadJSON()) }
            requests.append(request)
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems ?? []
            let limit = Int(query.first { $0.name == "limit" }!.value!)!
            let start = Int(query.first { $0.name == "cursor" }?.value ?? "0")!
            let end = min(start + limit, 107)
            let rows = (start..<end).map { i in
                let row = self.threadJSON().replacingOccurrences(of: self.threadID.uuidString,
                    with: String(format: "00000000-0000-0000-0000-%012d", 801 + i))
                // During refresh the visible conversation receives mail and moves far off-screen.
                return UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", 801 + i)) == reorderedAnchor.id
                    ? row.replacingOccurrences(of: "2026-09-11", with: "2026-09-12") : row
            }.joined(separator: ",")
            return (200, "{\"items\":[\(rows)],\"next_cursor\":\(end < 107 ? "\"\(end)\"" : "null"),\"total_count\":107}")
        }
        defer { model.resetConnection() }
        if view == model.listView { await model.reload() } else { model.setListView(view) }
        for _ in 0..<200 where model.items.isEmpty { try await Task.sleep(for: .milliseconds(10)) }
        #expect(model.items.count == 100)
        #expect(model.listTotal == 107)
        await model.openThread(threadID)
        // Exercise the live SwiftUI scroll view, including the automatic next page.
        let frame = NSRect(x: 0, y: 0, width: 1200, height: 800)
        let host = NSHostingView(rootView: EmailModeView(model: model, discussInChat: {}))
        let window = NSWindow(contentRect: frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = host
        defer { window.contentView = nil }
        host.frame = frame
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(150))

        /// Find the long inbox document among the sidebar and reader scroll views.
        func inboxScroll(in view: NSView) -> NSScrollView? {
            if let scroll = view as? NSScrollView, let document = scroll.documentView,
               document.frame.height > frame.height * 3 { return scroll }
            return view.subviews.lazy.compactMap { inboxScroll(in: $0) }.first
        }
        let scroll = try #require(inboxScroll(in: host))
        #expect(model.items.count == 100, "Opening the list should not fetch the next page")
        scroll.contentView.scroll(to: NSPoint(x: 0, y: scroll.documentView!.bounds.height - scroll.contentView.bounds.height))
        scroll.reflectScrolledClipView(scroll.contentView)
        for _ in 0..<200 where model.items.count == 100 { try await Task.sleep(for: .milliseconds(10)) }
        #expect(model.items.count == 107)
        #expect(!model.hasMore)
        #expect(requests.snapshot.map { URLComponents(url: $0.url!, resolvingAgainstBaseURL: false)!
            .queryItems!.first { $0.name == "limit" }!.value } == ["100", "100"])
        try await Task.sleep(for: .milliseconds(150))
        let visible = try #require(model.scrollAnchor)
        #expect(visible != threadID)
        #expect(scroll.documentVisibleRect.minY > scroll.documentVisibleRect.height)
        reorderedAnchor.set(visible)
        await model.reload(preserveOrder: true)
        #expect(model.items.first?.id == visible)
        try await Task.sleep(for: .milliseconds(120))
        for _ in 0..<100 where model.scrollAnchor != visible { try await Task.sleep(for: .milliseconds(20)) }
        #expect(model.scrollAnchor == visible)
        // The saved row is now first: its top must be inside the actual AppKit viewport.
        // A stale scrollAnchor alone cannot satisfy this geometry assertion.
        #expect(scroll.documentVisibleRect.minY < 1)
        #expect(model.items.count == 107)
        #expect(model.selectedThreadID == threadID)
    }

    /// Renders real AppKit list geometry without desktop automation or private mailbox data.
    @Test func testEmailInboxRenderedLayout() async throws {
        let rows = variedInboxRows()
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            return (200, "{\"items\":[\(rows)],\"next_cursor\":null}")
        }
        await model.reload()
        // A divider drag is remembered; keep this one out of the owner's defaults.
        let suiteName = "veetbot.tests.email-inbox-layout.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let store = EmailInboxWidthStore(defaults: defaults)
        let host = NSHostingView(rootView: EmailModeView(
            model: model, viewportHeight: 800, inboxWidth: store, discussInChat: {}))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = host
        host.frame = NSRect(x: 0, y: 0, width: 1200, height: 800)
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        host.layoutSubtreeIfNeeded()
        /// Find the first matching AppKit descendant in the rendered email hierarchy.
        func descendant<T: NSView>(_ type: T.Type, in view: NSView) -> T? {
            if let match = view as? T { return match }
            return view.subviews.lazy.compactMap { descendant(type, in: $0) }.first
        }
        defer {
            window.contentView = nil
            model.resetConnection()
        }
        /// Save the current rendered inbox as a fixture image for layout inspection.
        func snapshot(_ name: String) throws {
            guard let directory = ProcessInfo.processInfo.environment["VEETBOT_LAYOUT_SNAPSHOTS"],
                  let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return }
            host.cacheDisplay(in: host.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])?.write(
                to: URL(fileURLWithPath: directory).appendingPathComponent("\(name).png"))
        }
        #expect(model.items.count == 5)
        // Every Mac list is a scroll view now (ADR-0303): the inbox is the tallest one in
        // the split's first pane, beside the reader.
        let split = try #require(descendant(NSSplitView.self, in: host))
        /// Every scroll view under a pane.
        func scrollViews(in view: NSView) -> [NSScrollView] {
            let own: [NSScrollView] = (view as? NSScrollView).map { [$0] } ?? []
            return own + view.subviews.flatMap { scrollViews(in: $0) }
        }
        let scroll = try #require(scrollViews(in: split.subviews[0]).max { $0.frame.height < $1.frame.height })
        #expect(host.convert(scroll.bounds, from: scroll).minY < 150,
                "Inbox controls must leave mail near the top of the available content")
        let document = try #require(scroll.documentView)
        #expect(document.frame.height <= scroll.contentView.bounds.height + 1,
                "Five priorities must fit completely without scrolling at the default text size")
        try snapshot("email-sidebar-default")
        // Before any drag the inbox moves to the design's width once the split has laid out.
        for _ in 0..<10 where abs(scroll.frame.width - EmailInboxWidthStore.idealWidth) > 2 {
            try await Task.sleep(nanoseconds: 100_000_000)
            host.layoutSubtreeIfNeeded()
        }
        #expect(abs(scroll.frame.width - EmailInboxWidthStore.idealWidth) <= 2,
                "Before any drag the inbox opens at the design's width, not \(scroll.frame.width)")
        let originalWidth = scroll.frame.width
        split.setPosition(500, ofDividerAt: 0)
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        host.layoutSubtreeIfNeeded()
        #expect(scroll.frame.width > originalWidth + 80, "The list must follow its divider to a useful wider width")
        #expect(scroll.frame.width <= EmailInboxWidthStore.bounds.upperBound)
        try snapshot("email-sidebar-wide")
        window.setContentSize(NSSize(width: 780, height: 500))
        host.rootView = EmailModeView(model: model, viewportHeight: 500, inboxWidth: store, discussInChat: {})
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(nanoseconds: 200_000_000)
        host.layoutSubtreeIfNeeded()
        #expect(scroll.frame.width >= EmailInboxWidthStore.bounds.lowerBound,
                "A small window must retain usable inbox controls")
        #expect(scroll.frame.width <= host.bounds.width - 360,
                "Resizing the window must preserve room to read the selected email")
        #expect(host.bounds.contains(host.convert(scroll.bounds, from: scroll)))
        try snapshot("email-sidebar-small-window")
    }

    /// The inbox reopens at the width its divider was dragged to, as after quitting the app;
    /// a window too narrow for that width squeezes the column without replacing the choice.
    @Test func testEmailInboxReopensAtTheWidthItsDividerWasDraggedTo() async throws {
        let suiteName = "veetbot.tests.email-inbox-width.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        var handling: NSEvent?
        let store = EmailInboxWidthStore(defaults: defaults, currentEvent: { handling })
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            return (200, "{\"items\":[\(self.threadJSON())],\"next_cursor\":null}")
        }
        await model.reload()
        defer { model.resetConnection() }
        /// Find the first matching AppKit descendant in the rendered email hierarchy.
        func descendant<T: NSView>(_ type: T.Type, in view: NSView) -> T? {
            if let match = view as? T { return match }
            return view.subviews.lazy.compactMap { descendant(type, in: $0) }.first
        }
        /// Settle SwiftUI's asynchronous AppKit updates before measuring the columns.
        func settle(_ window: NSWindow) async throws {
            window.contentView?.layoutSubtreeIfNeeded()
            try await Task.sleep(nanoseconds: 200_000_000)
            window.contentView?.layoutSubtreeIfNeeded()
        }
        /// Opens the inbox in a new window, as a launch does, and returns its divider.
        func open() async throws -> (NSWindow, NSSplitView) {
            let host = NSHostingView(rootView: EmailModeView(
                model: model, viewportHeight: 800, inboxWidth: store, discussInChat: {}))
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false
            window.contentView = host
            host.frame = NSRect(x: 0, y: 0, width: 1200, height: 800)
            try await settle(window)
            return (window, try #require(descendant(NSSplitView.self, in: host)))
        }
        func inboxWidth(_ split: NSSplitView) -> CGFloat { split.subviews.first?.frame.width ?? 0 }
        /// After a divider move the split takes a few layout passes to settle; lay out
        /// as display cycles would, for up to two seconds, until the inbox is `width` wide.
        func inbox(of split: NSSplitView, in window: NSWindow, settlesAt width: CGFloat) async throws -> Bool {
            for _ in 0..<10 where abs(inboxWidth(split) - width) > 2 { try await settle(window) }
            return abs(inboxWidth(split) - width) <= 2
        }
        /// A pointer event in `window`, as AppKit hands it to the split view.
        func pointer(_ type: NSEvent.EventType, at x: CGFloat, in window: NSWindow) -> NSEvent? {
            NSEvent.mouseEvent(
                with: type, location: NSPoint(x: x, y: 400), modifierFlags: [],
                timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: window.windowNumber,
                context: nil, eventNumber: 0, clickCount: 1, pressure: 1)
        }

        let (first, split) = try await open()
        #expect(store.width == nil)
        #expect(try await inbox(of: split, in: first, settlesAt: EmailInboxWidthStore.idealWidth),
                "Before any drag the inbox opens at the design's width, not \(inboxWidth(split))")
        // AppKit moves the divider while it handles the owner's pointer drag.
        handling = pointer(.leftMouseDragged, at: 480, in: first)
        split.setPosition(480, ofDividerAt: 0)
        handling = pointer(.leftMouseUp, at: 480, in: first)
        #expect(try await inbox(of: split, in: first, settlesAt: 480),
                "The divider moved the inbox to \(inboxWidth(split))")
        #expect(store.width.map { abs($0 - 480) <= 2 } == true)
        first.setContentSize(NSSize(width: 780, height: 800))
        try await settle(first)
        #expect(inboxWidth(split) <= 420, "A narrow window keeps room to read, not \(inboxWidth(split))")
        first.contentView = nil

        let (second, reopened) = try await open()
        defer { second.contentView = nil }
        #expect(try await inbox(of: reopened, in: second, settlesAt: 480),
                "The inbox must reopen at the width the owner dragged it to, not \(inboxWidth(reopened))")
    }

    /// The actual white editor follows the reader when a large window is resized.
    @Test func testMacReaderUsesWideWindowsAndShrinksWithThem() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON()) }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        let host = NSHostingView(rootView: EmailModeView(model: model, viewportHeight: 1000, discussInChat: {}))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 2200, height: 1000),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = host
        defer { window.contentView = nil; model.resetConnection() }
        func editors(in view: NSView) -> [NSTextView] {
            (view as? NSTextView).map { [$0] } ?? view.subviews.flatMap { editors(in: $0) }
        }
        host.frame = NSRect(x: 0, y: 0, width: 2200, height: 1000)
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(200))
        host.layoutSubtreeIfNeeded()
        let wide = try #require(editors(in: host).first { $0.isEditable }).bounds.width
        #expect(wide > 900, "A large monitor must not leave the reply inside a 680-point column")
        window.setContentSize(NSSize(width: 1200, height: 1000))
        host.layoutSubtreeIfNeeded()
        try await Task.sleep(for: .milliseconds(200))
        host.layoutSubtreeIfNeeded()
        let narrow = try #require(editors(in: host).first { $0.isEditable }).bounds.width
        #expect(narrow < wide - 300)
        #expect(narrow > 100)
    }

    /// A sent reply is a record: its rendered body accepts no typing and holds no insertion point,
    /// even when the owner was editing it just before approving the send.
    @Test func testSentReplyRendersWithoutAnEditableBody() async throws {
        var status = "ready"
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON(status: status)) }
            return (200, self.threadJSON(draft: self.draftJSON(status: status)))
        }
        await model.openThread(threadID)
        let host = NSHostingView(rootView: EmailModeView(model: model, viewportHeight: 800, discussInChat: {}))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host
        defer {
            window.contentView = nil
            model.resetConnection()
        }
        /// Settle SwiftUI's asynchronous AppKit updates before inspecting the hierarchy.
        func settle() async throws {
            host.layoutSubtreeIfNeeded()
            try await Task.sleep(nanoseconds: 200_000_000)
            host.layoutSubtreeIfNeeded()
        }
        /// Collect every AppKit text view the reply surface rendered.
        func textViews(in view: NSView) -> [NSTextView] {
            (view as? NSTextView).map { [$0] } ?? view.subviews.flatMap { textViews(in: $0) }
        }
        try await settle()
        let editor = try #require(textViews(in: host).first { $0.isEditable },
                                  "A ready draft must render an editable reply body")
        #expect(window.makeFirstResponder(editor))

        status = "sent"
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.draft?.status == "sent")
        try await settle()

        #expect(textViews(in: host).allSatisfy { !$0.isEditable }, "A sent reply must not accept typing")
        #expect((window.firstResponder as? NSTextView)?.isEditable != true,
                "A sent reply must not keep a blinking insertion point")
    }
    #endif

    /// Archiving the selected thread from its row or detail advances in visible order before admission
    /// and retains edits if the write fails.
    @Test(arguments: [0, 1, 2, 3, 4], ["failed-detail", "completed-detail", "failed-row", "completed-row"])
    func testSelectedArchiveAdvancesBeforeAdmission(selectedIndex: Int, gesture: String) async throws {
        let outcome = String(gesture.prefix { $0 != "-" })
        let fromRow = gesture.hasSuffix("-row")
        let admission = EmailArchiveResponseGate()
        let nextRead = EmailArchiveResponseGate()
        let requests = EmailRequestRecorder()
        let ids = [threadID, UUID(), UUID()]
        let onlyRow = selectedIndex == 3
        let moveManually = selectedIndex == 4
        let index = moveManually ? 0 : selectedIndex
        let selected = onlyRow ? threadID : ids[index]
        let next = onlyRow ? nil : ids[index == 2 ? 1 : index + 1]
        let visible = onlyRow ? [threadID] : ids
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") {
                return (202, self.operationJSON(status: "QUEUED")
                    .replacingOccurrences(of: self.threadID.uuidString, with: selected.uuidString), admission)
            }
            let requested = requests.snapshot.contains { $0.httpMethod == "POST" }
            let rows = visible.map { id in
                self.archiveThreadJSON(inInbox: !(requested && id == selected && outcome == "completed"),
                    status: requested && id == selected ? outcome : nil,
                    draft: self.draftJSON())
                    .replacingOccurrences(of: self.threadID.uuidString, with: id.uuidString)
                    .replacingOccurrences(of: self.draftID.uuidString, with: id.uuidString)
            }
            if request.url!.path.hasSuffix("threads") {
                let included = rows.enumerated().filter { !requested || outcome != "completed" || visible[$0.offset] != selected }.map(\.element)
                return (200, "{\"items\":[\(included.joined(separator: ","))],\"next_cursor\":null}", nil)
            }
            let index = visible.firstIndex { request.url!.path.hasSuffix($0.uuidString) }!
            return (200, rows[index], visible[index] == next ? nextRead : nil)
        }
        defer { admission.release(); nextRead.release(); model.resetConnection() }
        await model.reload()
        await model.openThread(selected)
        model.changeEdit(\.body, to: "Keep this unsent draft")
        let snapshot = try #require(fromRow ? model.items.first { $0.id == selected } : model.thread)
        let archiving = Task { await model.setThreadArchived(snapshot, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        #expect(model.selectedThreadID == next)
        #expect(model.thread == nil)
        #expect(model.draft == nil)
        #expect(model.edits[selected]?.body == "Keep this unsent draft")
        #expect(!model.items.contains { $0.id == selected })
        let expectedSelection = moveManually ? ids[2] : next
        if moveManually {
            try await waitForEmailTestCondition { nextRead.isWaiting }
            await model.openThread(ids[2])
        }
        nextRead.release()
        if let next, !moveManually {
            try await waitForEmailTestCondition { model.thread?.id == next }
        } else {
            #expect(!model.isLoadingThread)
        }
        admission.release()
        await archiving.value
        #expect(model.selectedThreadID == expectedSelection)
        #expect(model.thread?.id == expectedSelection)
        #expect(model.items.contains { $0.id == selected } == (outcome == "failed"))
        if outcome == "failed" {
            #expect(model.archiveMessage(for: try #require(model.items.first { $0.id == selected })) != nil)
        }
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
    }

    /// A slow legacy draft response must not hide the already available conversation.
    @Test func testThreadIsReadableBeforeFallbackDraftArrives() async throws {
        let draftRead = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON(), draftRead) }
            return (200, self.threadJSON(), nil)
        }
        defer { draftRead.release(); model.resetConnection() }
        let opening = Task { await model.openThread(threadID) }
        try await waitForEmailTestCondition { draftRead.isWaiting }
        #expect(model.thread?.id == threadID)
        #expect(!model.isLoadingThread, "Available messages must replace the opening spinner immediately")
        draftRead.release()
        await opening.value
        #expect(model.draft?.id == draftID)
    }

    /// Foreground polling cannot supersede a slow initial read and prolong the opening spinner.
    @Test func testRefreshReusesInFlightThreadRead() async throws {
        let threadRead = EmailArchiveResponseGate()
        let duplicateRead = EmailArchiveResponseGate()
        let requests = EmailRequestRecorder()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            return (200, self.threadJSON(draft: self.draftJSON()), requests.snapshot.count == 1 ? threadRead : duplicateRead)
        }
        defer { threadRead.release(); duplicateRead.release(); model.resetConnection() }
        let opening = Task { await model.openThread(threadID) }
        try await waitForEmailTestCondition { threadRead.isWaiting }
        let refreshing = Task { await model.openThread(threadID, refreshOnly: true) }
        try await Task.sleep(nanoseconds: 50_000_000)
        #expect(requests.snapshot.count == 1, "Polling must not replace the selected thread's pending read")
        threadRead.release()
        duplicateRead.release()
        await opening.value
        await refreshing.value
        #expect(model.thread?.id == threadID)
        #expect(!model.isLoadingThread)
    }

    /// A completed draft operation still refreshes displayed mail while an older fallback read is pending.
    @Test func testNewDraftRefreshSupersedesAnOlderFallbackRead() async throws {
        let draftRead = EmailArchiveResponseGate()
        let requests = EmailRequestRecorder()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON(), draftRead) }
            let count = requests.snapshot.filter { $0.url!.path.contains("/threads/") }.count
            return (200, count == 1 ? self.threadJSON() : self.threadJSON(draft: self.draftJSON(revision: 2)), nil)
        }
        defer { draftRead.release(); model.resetConnection() }
        let opening = Task { await model.openThread(threadID) }
        try await waitForEmailTestCondition { draftRead.isWaiting }
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.draft?.revision == 2)
        draftRead.release()
        await opening.value
        #expect(model.draft?.revision == 2, "An older draft response must not overwrite the completed operation")
    }

    /// A held admission response cannot delay removal, block the next row, or let a refresh resurrect it.
    @Test func testArchiveDisappearsBeforeAdmissionWithoutBlockingNextRow() async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let otherID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") {
                let first = request.url!.path.contains(self.threadID.uuidString)
                let operation = self.operationJSON(status: "QUEUED")
                    .replacingOccurrences(of: self.threadID.uuidString, with: first ? self.threadID.uuidString : otherID.uuidString)
                return (202, operation, first ? admission : nil)
            }
            let first = self.archiveThreadJSON(inInbox: true, draft: self.draftJSON())
            let other = first.replacingOccurrences(of: self.threadID.uuidString, with: otherID.uuidString)
            if request.url!.path.hasSuffix("threads") {
                return (200, "{\"items\":[\(first),\(other)],\"next_cursor\":null}", nil)
            }
            let requested = requests.snapshot.contains { $0.url!.path == request.url!.path + "/archive" }
            let value = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil, draft: self.draftJSON())
            return (200, request.url!.path.contains(otherID.uuidString)
                ? value.replacingOccurrences(of: self.threadID.uuidString, with: otherID.uuidString) : value, nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Keep my unsent reply")
        let first = try #require(model.items.first)
        let next = try #require(model.items.last)
        let archiving = Task { await model.setThreadArchived(first, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        #expect(model.items.map(\.id) == [otherID])
        #expect(model.archiveMessage(for: first) == nil)
        #expect(model.selectedThreadID == otherID)
        #expect(model.edits[draftID]?.body == "Keep my unsent reply")
        #expect(model.canArchive(next))
        await model.reload(preserveOrder: true)
        #expect(model.items.map(\.id) == [otherID])
        await model.setThreadArchived(next, archived: true)
        #expect(model.items.isEmpty)
        #expect(model.selectedThreadID == nil)
        admission.release()
        await archiving.value
        #expect(model.items.isEmpty)
        #expect(model.thread == nil)
        #expect(model.archiveMessage(for: first) == nil)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 2)
    }

    /// Optimistic removal is reversible for failed, uncertain and unreadable outcomes, without losing edits.
    @Test(arguments: ["completed", "failed", "uncertain", "unreadable"])
    func testBackgroundArchiveRestoresOnlyUnconfirmedOutcomes(outcome: String) async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(inInbox: !(requested && outcome == "completed"),
                status: requested ? outcome : nil, draft: self.draftJSON())
            if request.url!.path.hasSuffix("threads") {
                return (200, "{\"items\":[\(requested && outcome == "completed" ? "" : row)],\"next_cursor\":null}", nil)
            }
            if requested && outcome == "unreadable" {
                return (400, Self.error("malformed_request", "Could not check Gmail status."), statusRead)
            }
            return (200, row, requested ? statusRead : nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My draft survives")
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        #expect(model.items.isEmpty)
        #expect(model.thread == nil, "Archiving the only selected row empties the reading pane")
        #expect(model.archiveMessage(for: row) == nil)
        statusRead.release()
        await archiving.value
        #expect(model.items.count == (outcome == "completed" ? 0 : 1))
        #expect((model.items.first.flatMap(model.archiveMessage(for:)) == nil) == (outcome == "completed"))
        #expect(model.edits[draftID]?.body == "My draft survives")
        #expect(model.selectedThreadID == nil, "A restored row must not replace the owner's new selection")
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
    }

    /// A pending archive keeps every count on screen, less the conversation it hid.
    @Test(arguments: ["completed", "failed"])
    func testCollectionCountsStayVisibleWhileArchivePending(outcome: String) async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let archived = requested && outcome == "completed"
            let row = self.archiveThreadJSON(inInbox: !archived, status: requested ? outcome : nil)
            if request.url!.path.hasSuffix("threads") {
                let counts = archived
                    ? "{\"priority\":6,\"inbox\":22,\"needs_you\":8,\"handled\":4,\"suspected_spam\":2}"
                    : "{\"priority\":7,\"inbox\":23,\"needs_you\":9,\"handled\":4,\"suspected_spam\":2}"
                return (200, "{\"items\":[\(archived ? "" : row)],\"next_cursor\":null,"
                    + "\"collection_counts\":\(counts),\"total_count\":\(archived ? 6 : 7)}", nil)
            }
            return (200, row, requested ? statusRead : nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        #expect(model.collectionCounts?.inbox == 23)
        #expect(model.listTotal == 7)
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        #expect(model.items.isEmpty)
        // The hidden row was Important, in Inbox and awaiting a reply; it was neither handled nor suspected spam.
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 22, needsYou: 8, handled: 4, priority: 6, suspectedSpam: 2))
        #expect(model.listTotal == 6)
        statusRead.release()
        await archiving.value
        let restored = outcome == "failed"
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: restored ? 23 : 22, needsYou: restored ? 9 : 8, handled: 4,
            priority: restored ? 7 : 6, suspectedSpam: 2))
        #expect(model.listTotal == (restored ? 7 : 6))
    }

    /// Counts as the server reports them in the pending-archive tests below. Their one
    /// conversation is Important, unhandled, awaiting a reply and not suspected spam, so
    /// three counts include it until Gmail confirms its archive.
    nonisolated private static func archiveCountsJSON(counted: Bool, inbox: Int = 23) -> String {
        "{\"priority\":\(counted ? 7 : 6),\"inbox\":\(counted ? inbox : inbox - 1),"
            + "\"needs_you\":\(counted ? 9 : 8),\"handled\":0,\"suspected_spam\":2}"
    }

    /// What Email shows while that conversation is hidden, and once Gmail has removed it.
    nonisolated private static func archiveCountsWithoutIt(inbox: Int = 23) -> EmailCollectionCounts {
        EmailCollectionCounts(inbox: inbox - 1, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2)
    }

    /// How many times a conversation's own status has been read since its archive was asked for,
    /// the read being answered included. These tests hold the first and answer the rest at once.
    nonisolated private static func statusReads(_ requests: EmailRequestRecorder) -> Int {
        requests.snapshot.drop { !$0.url!.path.hasSuffix("archive") }
            .filter { $0.httpMethod == "GET" && $0.url!.path.contains("/threads/") }.count
    }

    /// Handled cannot list an unhandled conversation whose archive is pending, but its counts
    /// still include it: the adjustment holds there until that archive settles.
    @Test(arguments: ["completed", "failed"])
    func testPendingArchiveKeepsItsCountAdjustmentInAnotherCollection(outcome: String) async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let settled = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let status: String? = !requested ? nil : settled.isSet ? outcome : "pending"
            let archived = status == "completed"
            let row = self.archiveThreadJSON(inInbox: !archived, status: status)
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            let listed = !archived && request.url?.query?.contains("view=handled") != true
            return (200, "{\"items\":[\(listed ? row : "")],\"next_cursor\":null,"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: !archived)),"
                + "\"total_count\":\(listed ? 7 : 0)}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(requests.snapshot.contains { $0.url?.query?.contains("view=handled") == true })
        #expect(model.items.isEmpty)
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(),
                "Gmail has not confirmed the archive, so the hidden conversation stays out of the counts")
        #expect(model.listTotal == 0)
        #expect(Self.statusReads(requests) == 2, "The page could not show the conversation, so its status was read")
        #expect(requests.snapshot.filter { $0.url?.query?.contains("view=handled") == true }.count == 1,
                "Its status agreed with what was held, so the counts were read once")
        statusRead.release()
        await archiving.value
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        settled.set()
        await model.checkArchiveStatus(threadID)
        let restored = outcome == "failed"
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: restored ? 23 : 22, needsYou: restored ? 9 : 8, handled: 0,
            priority: restored ? 7 : 6, suspectedSpam: 2))
    }

    /// A list the hidden conversation belongs to may still not return it: it waits on a later
    /// page, or Gmail has already removed it. Its own status tells the two apart, so the counts
    /// are lowered for the first and left as read for the second.
    @Test(arguments: [false, true])
    func testListThatDoesNotReturnTheHiddenRowReadsItsStatus(removed: Bool) async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let inbox = removed ? 23 : 150
        let other = archiveThreadJSON(inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "00000000-0000-0000-0000-000000000899")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(
                inInbox: !(requested && removed), status: !requested ? nil : removed ? "completed" : "pending")
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            guard request.url?.query?.contains("view=inbox") == true else {
                return (200, "{\"items\":[\(row)],\"next_cursor\":null,\"collection_counts\":"
                    + "\(Self.archiveCountsJSON(counted: true, inbox: inbox)),\"total_count\":7}", nil)
            }
            // Inbox pages by latest activity: the hidden row waits on a later page, or is gone.
            return (200, "{\"items\":[\(other)],\"next_cursor\":\(removed ? "null" : "\"100\""),"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: !removed, inbox: inbox)),"
                + "\"total_count\":\(removed ? inbox - 1 : inbox)}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.items.count == 1)
        #expect(model.items.first?.id != threadID)
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(inbox: inbox))
        #expect(model.listTotal == inbox - 1)
        statusRead.release()
        await archiving.value
    }

    /// A refresh that no longer returns a row the list held does not settle it by itself. Here
    /// the conversation's status shows Gmail's confirmation, so its counts are shown as read.
    @Test func testRefreshOfThePagedListThatHeldTheRowEndsItsCountAdjustment() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let confirmed = EmailTestFlag()
        let other = archiveThreadJSON(inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "00000000-0000-0000-0000-000000000899")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(
                inInbox: !confirmed.isSet, status: !requested ? nil : confirmed.isSet ? "completed" : "pending")
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            // Gmail confirms before the held status read can say so; the list reads see it first.
            let listed = confirmed.isSet ? other : "\(row),\(other)"
            return (200, "{\"items\":[\(listed)],\"next_cursor\":\"100\",\"collection_counts\":"
                + "\(Self.archiveCountsJSON(counted: !confirmed.isSet, inbox: 150)),"
                + "\"total_count\":\(confirmed.isSet ? 149 : 150)}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.items.count == 2 }
        let row = try #require(model.items.first { $0.id == threadID })
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(inbox: 150))
        confirmed.set()
        await model.reload(preserveOrder: true)
        #expect(model.items.count == 1)
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(inbox: 150))
        #expect(model.listTotal == 149)
        statusRead.release()
        await archiving.value
    }

    /// A new arrival can push the hidden row past the rows a refresh reads again. Missing from
    /// that page is not Gmail's confirmation: the conversation's own status says it is still in
    /// the Inbox, so the counts that came with the page still include it.
    @Test func testRefreshThatNoLongerReachesTheHiddenRowKeepsItsCountAdjustment() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let arrived = EmailTestFlag()
        let other = archiveThreadJSON(inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "00000000-0000-0000-0000-000000000899")
        let arrival = archiveThreadJSON(inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "00000000-0000-0000-0000-000000000898")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            // The arrival takes the first row; the hidden one is now the first row of page two.
            let listed = arrived.isSet ? "\(arrival),\(other)" : "\(row),\(other)"
            let inbox = arrived.isSet ? 151 : 150
            return (200, "{\"items\":[\(listed)],\"next_cursor\":\"100\",\"collection_counts\":"
                + "\(Self.archiveCountsJSON(counted: true, inbox: inbox)),\"total_count\":\(inbox)}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.items.count == 2 }
        let row = try #require(model.items.first { $0.id == threadID })
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(inbox: 150))
        arrived.set()
        await model.reload(preserveOrder: true)
        #expect(model.items.count == 2)
        #expect(!model.items.contains { $0.id == threadID })
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(inbox: 151),
                "The page stopped short of the hidden row; its counts did not")
        #expect(model.listTotal == 150)
        statusRead.release()
        await archiving.value
    }

    /// Feedback given while an archive is pending can end the hidden conversation's need for a
    /// reply. The server then leaves it out of Needs you while it stays in the Inbox, so it is
    /// taken off the Inbox and Important counts only.
    @Test func testPendingArchiveIsTakenOnlyFromTheCountsThatStillHoldIt() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let answered = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!answered.isSet)")
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            let listed = request.url?.query?.contains("view=handled") != true
            return (200, "{\"items\":[\(listed ? row : "")],\"next_cursor\":null,\"collection_counts\":"
                + "{\"priority\":7,\"inbox\":23,\"needs_you\":\(answered.isSet ? 8 : 9),\"handled\":0,"
                + "\"suspected_spam\":2},\"total_count\":\(listed ? 7 : 0)}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        answered.set()
        await model.reload(preserveOrder: true)
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 22, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2),
            "Needs you is 8 on the server once the conversation needs no reply; it is not 7")
        statusRead.release()
        await archiving.value
    }

    /// Loading another page brings newer counts with it, so it too reads the status of the
    /// hidden rows it did not return.
    @Test func testLoadingAnotherPageReadsTheStatusOfHiddenRowsToo() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let answered = EmailTestFlag()
        let other = archiveThreadJSON(inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "00000000-0000-0000-0000-000000000899")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!answered.isSet)")
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            // Page one holds the conversation; page two is one other row.
            let second = request.url?.query?.contains("cursor=100") == true
            return (200, "{\"items\":[\(second ? other : row)],\"next_cursor\":\(second ? "null" : "\"100\""),"
                + "\"collection_counts\":{\"priority\":7,\"inbox\":101,\"needs_you\":\(answered.isSet ? 8 : 9),"
                + "\"handled\":0,\"suspected_spam\":2},\"total_count\":101}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.items.count == 1 && !model.isReadingList }
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        let shown = EmailCollectionCounts(inbox: 100, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2)
        #expect(model.collectionCounts == shown)
        answered.set()
        await model.loadMore()
        #expect(model.items.count == 1, "Page two's row; the archived one stays hidden")
        #expect(model.collectionCounts == shown, "Needs you is 8 on the server now; it is not 7")
        statusRead.release()
        await archiving.value
    }

    /// A list kept from an earlier visit comes back with its own counts before it is read
    /// again. A hidden conversation that list never held is still inside those counts.
    @Test func testReturningToAKeptListKeepsTheCountAdjustment() async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let other = archiveThreadJSON(inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "00000000-0000-0000-0000-000000000899")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            let row = self.archiveThreadJSON(inInbox: true)
            guard request.url!.path.hasSuffix("threads") else { return (200, row, nil) }
            // The Inbox's first page stops short of the conversation; Handled never lists it.
            let query = request.url?.query ?? ""
            let inbox = query.contains("view=inbox")
            let items = query.contains("view=priority") ? row : inbox ? other : ""
            return (200, "{\"items\":[\(items)],\"next_cursor\":\(inbox ? "\"100\"" : "null"),"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: true, inbox: 150)),"
                + "\"total_count\":\(inbox ? 150 : 7)}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(inbox: 150))
        model.setCollection(.inbox)
        // Shown at once from the kept list, before the read that follows it has answered.
        #expect(model.items.count == 1)
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(inbox: 150))
        admission.release()
        await archiving.value
    }

    /// A kept list comes back with the counts it was read with, so the hidden conversation is
    /// taken off them as it stood for that read. A later page that showed it changed speaks
    /// only for its own counts.
    @Test func testKeptListComesBackWithTheMembershipItsCountsHad() async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let answered = EmailTestFlag()
        let other = archiveThreadJSON(inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "00000000-0000-0000-0000-000000000899")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            let row = self.archiveThreadJSON(inInbox: true)
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!answered.isSet)")
            guard request.url!.path.hasSuffix("threads") else { return (200, row, nil) }
            // Important holds the conversation; the Inbox's first page stops short of it.
            let inbox = request.url?.query?.contains("view=inbox") == true
            return (200, "{\"items\":[\(inbox ? other : row)],\"next_cursor\":\(inbox ? "\"100\"" : "null"),"
                + "\"collection_counts\":{\"priority\":7,\"inbox\":150,\"needs_you\":\(answered.isSet ? 8 : 9),"
                + "\"handled\":0,\"suspected_spam\":2},\"total_count\":\(inbox ? 150 : 7)}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        let kept = Self.archiveCountsWithoutIt(inbox: 150)
        #expect(model.collectionCounts == kept)
        answered.set()
        model.setListView("priority")
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 149, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2))
        model.setCollection(.inbox)
        // The kept Inbox is shown at once, with the counts of the read that produced it.
        #expect(model.collectionCounts == kept, "Those counts held the conversation in Needs you")
        admission.release()
        await archiving.value
    }

    /// The counts and a hidden conversation's status are two reads. When its need for a reply
    /// ends between them, the counts still hold it where its status no longer puts it. They are
    /// not read again for that: Needs you is one too high until the next refresh puts it right.
    @Test func testCountsAreRightAgainAtTheNextRefreshWhenAHiddenConversationChangedBetweenTwoReads() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let armed = EmailTestFlag()
        let answered = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            // The list read that follows arming is the last to see the reply need.
            let needsYou = answered.isSet ? 8 : 9
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!answered.isSet)")
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            if armed.isSet { answered.set() }
            let listed = request.url?.query?.contains("view=handled") != true
            return (200, "{\"items\":[\(listed ? row : "")],\"next_cursor\":null,\"collection_counts\":"
                + "{\"priority\":7,\"inbox\":23,\"needs_you\":\(needsYou),\"handled\":0,\"suspected_spam\":2},"
                + "\"total_count\":\(listed ? 7 : 0)}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        let listReads = { requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count }
        let before = listReads()
        armed.set()
        await model.reload(preserveOrder: true)
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 22, needsYou: 9, handled: 0, priority: 6, suspectedSpam: 2),
            "The counts were read before the reply need ended and its status after, so Needs you is one high")
        #expect(listReads() == before + 1, "The counts are not read again for it")
        await model.reload(preserveOrder: true)
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 22, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2),
            "The next refresh puts it right")
        statusRead.release()
        await archiving.value
    }

    /// A page that comes back unchanged is reused as it stands. It cannot vouch for a hidden
    /// conversation it does not hold, so such a page is read in full, with that conversation's
    /// status. Here another conversation took its place in Needs you, leaving the counts equal.
    @Test func testUnchangedPageIsNotReusedForAHiddenConversationItDoesNotHold() async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let answered = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            let row = self.archiveThreadJSON(inInbox: true)
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!answered.isSet)")
            guard request.url!.path.hasSuffix("threads") else { return (200, row, nil) }
            let listed = request.url?.query?.contains("view=handled") != true
            return (200, "{\"items\":[\(listed ? row : "")],\"next_cursor\":null,\"snapshot_version\":\"same\","
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: true)),"
                + "\"total_count\":\(listed ? 7 : 0)}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        answered.set()
        await model.reload(preserveOrder: true)
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 22, needsYou: 9, handled: 0, priority: 6, suspectedSpam: 2),
            "Needs you is 9 without the hidden conversation now")
        admission.release()
        await archiving.value
    }

    /// The reader's copy can be older than the counts on screen. Archiving from it reads the
    /// counts again with the conversation's status, so the two describe the same moment.
    @Test func testArchiveFromTheReaderReadsTheCountsAgainWithItsStatus() async throws {
        let requests = EmailRequestRecorder()
        let answered = EmailTestFlag()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED")) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            guard request.url!.path.hasSuffix("threads") else {
                return (200, self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
                    .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!answered.isSet)"))
            }
            return (200, "{\"items\":[],\"next_cursor\":null,\"collection_counts\":{\"priority\":7,\"inbox\":23,"
                + "\"needs_you\":\(answered.isSet ? 8 : 9),\"handled\":0,\"suspected_spam\":2},\"total_count\":0}")
        }
        defer { model.resetConnection() }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        await model.openThread(threadID)
        answered.set()
        await model.reload(preserveOrder: true)
        #expect(model.collectionCounts?.needsYou == 8)
        let open = try #require(model.thread)
        #expect(open.needsReply, "The reader still shows the conversation as it was first read")
        await model.setThreadArchived(open, archived: true)
        let settled = EmailCollectionCounts(inbox: 22, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2)
        try await waitForEmailTestCondition { model.collectionCounts == settled && !model.isReadingList }
        #expect(model.collectionCounts == settled)
    }

    /// An archive begun on another device and first seen in the reader is read after the
    /// counts on screen. They are read again, so they are not older than what hid it.
    @Test func testArchiveFirstSeenInTheReaderReadsTheCountsAgain() async throws {
        let answered = EmailTestFlag()
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            guard request.url!.path.hasSuffix("threads") else {
                return (200, self.archiveThreadJSON(inInbox: true, status: "pending")
                    .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!answered.isSet)"))
            }
            return (200, "{\"items\":[],\"next_cursor\":null,\"collection_counts\":{\"priority\":7,\"inbox\":23,"
                + "\"needs_you\":\(answered.isSet ? 8 : 9),\"handled\":0,\"suspected_spam\":2},\"total_count\":0}")
        }
        defer { model.resetConnection() }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        #expect(model.collectionCounts?.needsYou == 9)
        answered.set()
        await model.openThread(threadID)
        let settled = EmailCollectionCounts(inbox: 22, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2)
        try await waitForEmailTestCondition { model.collectionCounts == settled && !model.isReadingList }
        #expect(model.collectionCounts == settled)
    }

    /// A kept list's counts were adjusted for the conversations hidden when it was read. Once
    /// another conversation is hidden it is not shown again as it was: its counts wait for a read.
    @Test func testKeptListsAreNotShownAgainOnceAnotherConversationIsHidden() async throws {
        let other = archiveThreadJSON(inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "00000000-0000-0000-0000-000000000899")
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            guard request.url!.path.hasSuffix("threads") else {
                return (200, self.archiveThreadJSON(inInbox: true, status: "pending"))
            }
            let inbox = request.url?.query?.contains("view=inbox") == true
            let row = self.archiveThreadJSON(inInbox: true)
            return (200, "{\"items\":[\(inbox ? other : row)],\"next_cursor\":\(inbox ? "\"100\"" : "null"),"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: true, inbox: 150)),"
                + "\"total_count\":\(inbox ? 150 : 7)}")
        }
        defer { model.resetConnection() }
        await model.reload()
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        await model.openThread(threadID)
        model.setListView("priority")
        #expect(model.collectionCounts == nil, "The kept Important list knew nothing of this archive")
        #expect(model.items.isEmpty)
    }

    /// Nor is a kept list shown again as it was once a conversation hidden when it was read has
    /// settled: its counts were lowered for a conversation Gmail has since removed.
    @Test func testKeptListsAreNotShownAgainOnceAHiddenConversationSettles() async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let confirmed = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            // Other mail keeps archived conversations; there the row itself shows the confirmation.
            let row = self.archiveThreadJSON(inInbox: !confirmed.isSet, status: confirmed.isSet ? "completed" : nil)
            guard request.url!.path.hasSuffix("threads") else { return (200, row, nil) }
            return (200, "{\"items\":[\(row)],\"next_cursor\":null,"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: !confirmed.isSet)),"
                + "\"total_count\":7}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        await model.reload(preserveOrder: true)
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(), "Important is kept, lowered for the hidden row")
        confirmed.set()
        model.setListView("other")
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        #expect(model.items.count == 1, "Other mail shows the archived conversation again")
        model.setListView("priority")
        #expect(model.collectionCounts == nil, "The kept Important counts still held the conversation")
        admission.release()
        await archiving.value
    }

    /// After another page is loaded the counts are that page's. A row from the first page is
    /// older than they are, so archiving it reads the counts again.
    @Test func testArchivingARowFromAnEarlierPageReadsTheCountsAgain() async throws {
        let requests = EmailRequestRecorder()
        let answered = EmailTestFlag()
        let other = archiveThreadJSON(inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "00000000-0000-0000-0000-000000000899")
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED")) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!answered.isSet)")
            guard request.url!.path.hasSuffix("threads") else { return (200, row) }
            let second = request.url?.query?.contains("cursor=100") == true
            return (200, "{\"items\":[\(second ? other : row)],\"next_cursor\":\(second ? "null" : "\"100\""),"
                + "\"collection_counts\":{\"priority\":7,\"inbox\":101,\"needs_you\":\(answered.isSet ? 8 : 9),"
                + "\"handled\":0,\"suspected_spam\":2},\"total_count\":101}")
        }
        defer { model.resetConnection() }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.items.count == 1 && !model.isReadingList }
        answered.set()
        await model.loadMore()
        #expect(model.collectionCounts?.needsYou == 8)
        let row = try #require(model.items.first { $0.id == threadID })
        #expect(row.needsReply, "The first page's row is as it was read")
        await model.setThreadArchived(row, archived: true)
        let settled = EmailCollectionCounts(inbox: 100, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2)
        try await waitForEmailTestCondition { model.collectionCounts == settled && !model.isReadingList }
        #expect(model.collectionCounts == settled)
    }

    /// A status read replaces a listed row with a newer copy. The counts on screen came with
    /// the older row and hold the conversation as that row says, so that is what archiving it
    /// takes off them, with no further read of the list.
    @Test func testArchivingARowAStatusReadReplacedTakesOffWhatItsCountsHeld() async throws {
        let requests = EmailRequestRecorder()
        let answered = EmailTestFlag()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED")) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!answered.isSet)")
            guard request.url!.path.hasSuffix("threads") else { return (200, row) }
            return (200, "{\"items\":[\(row)],\"next_cursor\":null,\"collection_counts\":{\"priority\":7,\"inbox\":23,"
                + "\"needs_you\":\(answered.isSet ? 8 : 9),\"handled\":0,\"suspected_spam\":2},\"total_count\":7}")
        }
        defer { model.resetConnection() }
        await model.reload()
        answered.set()
        await model.checkArchiveStatus(threadID)
        let row = try #require(model.items.first)
        #expect(!row.needsReply, "The status read put its newer copy in the list")
        #expect(model.collectionCounts?.needsYou == 9, "The counts are still the ones read before it")
        let listReads = { requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count }
        let before = listReads()
        await model.setThreadArchived(row, archived: true)
        let settled = EmailCollectionCounts(inbox: 22, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2)
        try await waitForEmailTestCondition { model.collectionCounts == settled && !model.isReadingList }
        #expect(model.collectionCounts == settled)
        #expect(listReads() == before, "The row that came with the counts answers for them")
    }

    /// Other mail lists everything that is not Important, and a search of it lists all mail that
    /// matches. Their totals count conversations the way those lists do, not the way the five
    /// sidebar counts do, so a hidden conversation comes off the total of the list it was in.
    @Test(arguments: [false, true])
    func testListTotalLeavesOutAConversationHiddenFromOtherMail(searching: Bool) async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            let row = self.archiveThreadJSON(inInbox: true)
                .replacingOccurrences(of: "\"priority\":0.95", with: "\"priority\":0.4")
            guard request.url!.path.hasSuffix("threads") else { return (200, row, nil) }
            return (200, "{\"items\":[\(row)],\"next_cursor\":null,\"collection_counts\":{\"priority\":7,"
                + "\"inbox\":23,\"needs_you\":9,\"handled\":0,\"suspected_spam\":2},\"total_count\":40}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        model.setListView("other")
        if searching { model.setSearchText("board") }
        try await waitForEmailTestCondition { model.items.count == 1 && !model.isReadingList }
        #expect(requests.snapshot.last { $0.url!.path.hasSuffix("threads") }?.url?.query?
            .contains("view=\(searching ? "all" : "other")") == true)
        #expect(model.listTotal == 40)
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        #expect(model.items.isEmpty)
        #expect(model.listTotal == 39, "The list's total counted the row that is now hidden")
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 22, needsYou: 8, handled: 0, priority: 7, suspectedSpam: 2),
            "It was in the Inbox awaiting a reply, and never Important")
        admission.release()
        await archiving.value
    }

    /// A list read can be the first to learn that Gmail confirmed an archive. It shows its own
    /// answer: the conversation leaves the Inbox before its hide is lifted, and the mail that
    /// answer brought stays on screen when the list cannot be read afterwards.
    @Test func testArchiveConfirmedDuringAListReadIsShownWithThatReadsAnswer() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let confirmed = EmailTestFlag()
        let answeredOnce = EmailTestFlag()
        let otherID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let other = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(otherID)")
        let arrival = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(arrivalID)")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(
                inInbox: !confirmed.isSet, status: !requested ? nil : confirmed.isSet ? "completed" : "pending")
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            guard confirmed.isSet else {
                return (200, "{\"items\":[\(row),\(other)],\"next_cursor\":\"100\",\"collection_counts\":"
                    + "\(Self.archiveCountsJSON(counted: true, inbox: 150)),\"total_count\":150}", nil)
            }
            // The first list read after Gmail's confirmation answers, with new mail. Every later one fails.
            if answeredOnce.isSet { return (503, Self.error("unavailable", "Try again."), nil) }
            answeredOnce.set()
            return (200, "{\"items\":[\(arrival),\(other)],\"next_cursor\":\"100\",\"collection_counts\":"
                + "\(Self.archiveCountsJSON(counted: false, inbox: 150)),\"total_count\":149}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.items.count == 2 && !model.isReadingList }
        let row = try #require(model.items.first { $0.id == threadID })
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        confirmed.set()
        await model.reload(preserveOrder: true)
        #expect(Set(model.items.map(\.id)) == [otherID, arrivalID],
                "The read that learnt of the confirmation is shown, with the mail it brought")
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(inbox: 150))
        #expect(model.listTotal == 149)
        await model.reload(preserveOrder: true)
        #expect(model.errorMessage != nil, "The list could not be read this time")
        #expect(Set(model.items.map(\.id)) == [otherID, arrivalID], "A later failure takes nothing back")
        statusRead.release()
        await archiving.value
        #expect(Set(model.items.map(\.id)) == [otherID, arrivalID])
    }

    /// The status check that learns of Gmail's confirmation reads the list afterwards. When that
    /// read fails, the conversation is already out of the Inbox and stays off its counts.
    @Test func testConfirmedArchiveStaysOutOfTheInboxWhenTheListCannotBeRead() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let confirmed = EmailTestFlag()
        let otherID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let other = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(otherID)")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(
                inInbox: !confirmed.isSet, status: !requested ? nil : confirmed.isSet ? "completed" : "pending")
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            if confirmed.isSet { return (503, Self.error("unavailable", "Try again."), nil) }
            return (200, "{\"items\":[\(row),\(other)],\"next_cursor\":\"100\",\"collection_counts\":"
                + "\(Self.archiveCountsJSON(counted: true, inbox: 150)),\"total_count\":150}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.items.count == 2 && !model.isReadingList }
        let row = try #require(model.items.first { $0.id == threadID })
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        confirmed.set()
        await model.checkArchiveStatus(threadID)
        #expect(model.items.map(\.id) == [otherID], "Gmail confirmed the archive, so it stays out of the Inbox")
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(inbox: 150),
                "The counts on screen were read before the confirmation and stay lowered for it")
        #expect(model.listTotal == 149)
        statusRead.release()
        await archiving.value
        #expect(model.items.map(\.id) == [otherID])
    }

    /// Other mail keeps archived conversations. One whose archive Gmail confirms comes back
    /// there and stays in its total, while the Inbox counts it left keep it taken off, here
    /// with the list unreadable since the confirmation.
    @Test func testConfirmedArchiveComesBackInOtherMail() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let confirmed = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(
                inInbox: !confirmed.isSet, status: !requested ? nil : confirmed.isSet ? "completed" : "pending")
                .replacingOccurrences(of: "\"priority\":0.95", with: "\"priority\":0.4")
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            if confirmed.isSet { return (503, Self.error("unavailable", "Try again."), nil) }
            return (200, "{\"items\":[\(row)],\"next_cursor\":null,\"collection_counts\":{\"priority\":7,"
                + "\"inbox\":23,\"needs_you\":9,\"handled\":0,\"suspected_spam\":2},\"total_count\":40}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        model.setListView("other")
        try await waitForEmailTestCondition { model.items.count == 1 && !model.isReadingList }
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        #expect(model.items.isEmpty)
        confirmed.set()
        await model.checkArchiveStatus(threadID)
        #expect(model.items.map(\.id) == [threadID], "Other mail still lists the archived conversation")
        #expect(model.items.first?.isArchived == true)
        #expect(model.listTotal == 40, "It is shown again, so the total keeps it")
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 22, needsYou: 8, handled: 0, priority: 7, suspectedSpam: 2),
            "It left the Inbox and Needs you, whose counts were read before Gmail confirmed")
        statusRead.release()
        await archiving.value
    }

    /// A hidden conversation's status can be refused once its account is revoked, here while a
    /// list read is checking it. The refused account's mail goes at once. The other accounts
    /// keep theirs only once the list, asked again after the refusal, has answered: the read
    /// that was in flight was asked before it and confirms nothing.
    @Test(arguments: [200, 503])
    func testRefusalSeenDuringAListReadWaitsForTheListToAnswerAgain(listAfterwards: Int) async throws {
        let admission = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let answeredOnce = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            let work = self.archiveThreadJSON(account: "work", inInbox: true)
            guard request.url!.path.hasSuffix("threads") else {
                return revoked.isSet
                    ? (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
                    : (200, work, nil)
            }
            guard revoked.isSet else {
                return (200, "{\"items\":[\(work),\(personal)],\"next_cursor\":null,"
                    + "\"collection_counts\":\(Self.archiveCountsJSON(counted: true)),\"total_count\":7}", nil)
            }
            // The read in flight answers. The one asked for after the refusal answers as the case says.
            if answeredOnce.isSet, listAfterwards == 503 { return (503, Self.error("unavailable", "Try again."), nil) }
            answeredOnce.set()
            return (200, "{\"items\":[\(personal)],\"next_cursor\":null,"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: false)),\"total_count\":6}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first { $0.id == threadID })
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        revoked.set()
        await model.reload(preserveOrder: true)
        let kept = listAfterwards == 200
        #expect(model.items.map(\.id) == (kept ? [personalID] : []),
                "Personal mail stays only once the list has answered after the refusal")
        #expect(model.collectionCounts == (kept ? Self.archiveCountsWithoutIt() : nil))
        #expect(model.accounts.count == (kept ? 2 : 0))
        #expect(model.errorMessage != nil)
        admission.release()
        await archiving.value
        #expect(model.items.map(\.id) == (kept ? [personalID] : []))
    }

    /// A refused status read cannot tell one revoked account from the whole of Email. It drops
    /// that conversation's account and asks the list. Only a list that answers keeps the other
    /// accounts' mail. One that is refused as well, or cannot be read, confirms nothing, and
    /// everything is cleared as after any refusal.
    @Test(arguments: [200, 403, 503])
    func testDeniedStatusReadLetsTheListSayWhatWasRevoked(list: Int) async throws {
        let revoked = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let otherWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let otherWork = archiveThreadJSON(account: "work", inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "\(otherWorkID)")
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            guard request.url!.path.hasSuffix("threads") else {
                return revoked.isSet ? (403, Self.error("permission_denied", "Work mail can no longer be read.")) : (200, work)
            }
            if revoked.isSet, list == 403 { return (403, Self.error("permission_denied", "Email can no longer be read.")) }
            if revoked.isSet, list == 503 { return (503, Self.error("unavailable", "Try again.")) }
            return (200, "{\"items\":[\(revoked.isSet ? personal : "\(work),\(otherWork),\(personal)")],\"next_cursor\":null}")
        }
        defer { model.resetConnection() }
        await model.reload()
        #expect(model.items.map(\.id) == [otherWorkID, personalID], "The Work archive is pending, so its row is hidden")
        #expect(model.accounts.count == 2)
        revoked.set()
        await model.checkArchiveStatus(threadID)
        #expect(model.items.map(\.id) == (list == 200 ? [personalID] : []),
                "Personal's rows stay only when the list has answered after the refusal")
        #expect(model.accounts.count == (list == 200 ? 2 : 0))
        #expect(model.errorMessage != nil)
    }

    /// The one-second status poll is where a refusal usually arrives. The poll asks the list
    /// itself, and the list's answer decides as it does for any other refused read.
    @Test(arguments: [200, 503])
    func testRefusalSeenByTheStatusPollWaitsForTheListToAnswer(list: Int) async throws {
        let requests = EmailRequestRecorder()
        let revoked = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let model = try makeModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if path.hasSuffix("refresh") || path.contains("/operations/") {
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            guard path.hasSuffix("threads") else {
                return revoked.isSet ? (403, Self.error("permission_denied", "Work mail can no longer be read.")) : (200, work)
            }
            if revoked.isSet, list == 503 { return (503, Self.error("unavailable", "Try again.")) }
            return (200, "{\"items\":[\(revoked.isSet ? personal : "\(work),\(personal)")],\"next_cursor\":null}")
        }
        defer { model.resetConnection() }
        model.setActive(true)
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing && !model.isReadingList
        }
        #expect(model.items.map(\.id) == [personalID], "The Work archive is pending, so its row is hidden")
        revoked.set()
        // The poll reads the hidden conversation's status a second after it was first seen.
        try await waitForEmailTestCondition {
            list == 200 ? model.errorMessage != nil && !model.isReadingList : model.accounts.isEmpty
        }
        #expect(model.items.map(\.id) == (list == 200 ? [personalID] : []))
        #expect(model.accounts.count == (list == 200 ? 2 : 0))
        #expect(model.errorMessage != nil)
        // The refused conversation's poll went with it. Left running, it would ask again within a second.
        let statusReads = { requests.snapshot.filter { $0.url!.path.contains("/threads/") }.count }
        let before = statusReads()
        try await Task.sleep(nanoseconds: 1_300_000_000)
        #expect(statusReads() == before, "Nothing follows a conversation that was refused")
    }

    /// Two conversations of a revoked account can be refused one after the other. Each refusal
    /// asks the list again, and the later request replaces the earlier one. The earlier answer
    /// is never shown, and the earlier refusal does not clear Email because its request was
    /// replaced: the later answer decides, and is the one shown.
    @Test func testLaterRefusalsListReadDecidesForAnEarlierOne() async throws {
        let requests = EmailRequestRecorder()
        let firstList = EmailArchiveResponseGate()
        let secondList = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let secondWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let otherWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000897")!
        let obsoleteID = UUID(uuidString: "00000000-0000-0000-0000-000000000896")!
        let currentID = UUID(uuidString: "00000000-0000-0000-0000-000000000895")!
        let personalRow = { (id: UUID) in
            self.archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(self.threadID)", with: "\(id)")
        }
        let otherWork = archiveThreadJSON(account: "work", inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "\(otherWorkID)")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard path.hasSuffix("threads") else {
                return (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
            }
            guard revoked.isSet else {
                let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
                let secondWork = work.replacingOccurrences(of: "\(self.threadID)", with: "\(secondWorkID)")
                return (200, "{\"items\":[\(work),\(secondWork),\(otherWork),\(personalRow(personalID))],"
                    + "\"next_cursor\":null}", nil)
            }
            // Each refusal asks the list again. Both answers are held so that the test orders
            // them, and each brings a row of its own so that they can be told apart.
            let first = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count == 2
            return (200, "{\"items\":[\(personalRow(first ? obsoleteID : currentID)),\(personalRow(personalID))],"
                + "\"next_cursor\":null}", first ? firstList : secondList)
        }
        defer { firstList.release(); secondList.release(); model.resetConnection() }
        await model.reload()
        #expect(model.items.map(\.id) == [otherWorkID, personalID], "Both Work archives are pending, so their rows are hidden")
        revoked.set()
        let first = Task { await model.checkArchiveStatus(threadID) }
        try await waitForEmailTestCondition { firstList.isWaiting }
        #expect(model.items.map(\.id) == [personalID], "Work's other row went with the refusal, before the list answered")
        let second = Task { await model.checkArchiveStatus(secondWorkID) }
        try await waitForEmailTestCondition { secondList.isWaiting }
        firstList.release()
        await first.value
        #expect(model.items.map(\.id) == [personalID], "The replaced answer is not shown, and nothing is cleared for it")
        #expect(model.accounts.count == 2)
        secondList.release()
        await second.value
        #expect(Set(model.items.map(\.id)) == [personalID, currentID], "The later answer is the one shown")
        #expect(!model.items.contains { $0.id == obsoleteID })
        #expect(model.accounts.count == 2)
    }

    /// A conversation can still be loading in the reader when its account is refused. Its
    /// answer was asked for before the refusal and must not be shown after it. The reader is
    /// cleared for a conversation of the refused account, and for one whose account nothing on
    /// screen tells. A conversation known to be from another account stays, also when the list
    /// it was opened from has been left since.
    @Test(arguments: ["work", "unlisted", "personal", "personalElsewhere"])
    func testRefusedAccountsConversationStillLoadingIsNotShown(opening: String) async throws {
        let detail = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let openWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let unlistedID = UUID(uuidString: "00000000-0000-0000-0000-000000000897")!
        let openID = opening == "work" ? openWorkID : opening == "unlisted" ? unlistedID : personalID
        let kept = opening.hasPrefix("personal")
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let openWork = archiveThreadJSON(account: "work", inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "\(openWorkID)")
        let model = try makeArchiveRaceModel { request in
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            guard path.hasSuffix("threads") else {
                // The hidden Work conversation's status is refused once Work is revoked. The
                // conversation being opened answers late, with what it held before the refusal.
                guard path.contains("\(openID)") else {
                    return revoked.isSet
                        ? (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
                        : (200, work, nil)
                }
                return (200, self.archiveThreadJSON(account: kept ? "personal" : "work", inInbox: true)
                    .replacingOccurrences(of: "\"draft_id\":\"\(self.draftID)\"", with: "\"draft_id\":null")
                    .replacingOccurrences(of: "\(self.threadID)", with: "\(openID)"), detail)
            }
            // Handled lists none of them.
            if request.url?.query?.contains("view=handled") == true {
                return (200, "{\"items\":[],\"next_cursor\":null}", nil)
            }
            return (200, "{\"items\":[\(revoked.isSet ? personal : "\(work),\(openWork),\(personal)")],\"next_cursor\":null}", nil)
        }
        defer { detail.release(); model.resetConnection() }
        await model.reload()
        let opened = Task { await model.openThread(openID) }
        try await waitForEmailTestCondition { detail.isWaiting }
        if opening == "personalElsewhere" {
            // The owner moves to a list without the row the conversation was opened from.
            model.setCollection(.handled)
            try await waitForEmailTestCondition { model.items.isEmpty && !model.isReadingList }
        }
        revoked.set()
        await model.checkArchiveStatus(threadID)
        #expect(model.items.map(\.id) == (opening == "personalElsewhere" ? [] : [personalID]),
                "The list answered, so what it returned stays")
        #expect(model.accounts.count == 2)
        detail.release()
        await opened.value
        #expect(model.thread?.id == (kept ? personalID : nil),
                "What a refused account's conversation held is not shown after the refusal")
        #expect(model.selectedThreadID == (kept ? personalID : nil))
    }

    /// If the list cannot even be asked after a refusal, nothing is left to confirm what may
    /// still be read, and everything is cleared.
    @Test func testRefusalClearsEverythingWhenTheListCannotBeAsked() async throws {
        let refused = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let id = EmailArchiveRaceURLProtocol.register { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard request.url!.path.hasSuffix("threads") else {
                refused.set()
                return (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
            }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            return (200, "{\"items\":[\(work),\(personal)],\"next_cursor\":null}", nil)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [EmailArchiveRaceURLProtocol.self]
        configuration.httpAdditionalHeaders = ["X-Email-Archive-Race": id]
        let api = VeetbotAPIClient(transport: HTTPTransport(
            configuration: try ConnectionConfiguration(baseURLString: "https://email.test"),
            tokenStore: InMemoryTokenStore(token: "test-token"), session: URLSession(configuration: configuration)
        ))
        // Once the refusal has been answered, no client is handed out for another request.
        let model = EmailViewModel(makeAPIClient: { refused.isSet ? nil : api })
        defer { model.resetConnection() }
        await model.reload()
        #expect(model.items.map(\.id) == [personalID], "The Work archive is pending, so its row is hidden")
        #expect(model.accounts.count == 2)
        await model.checkArchiveStatus(threadID)
        #expect(model.items.isEmpty, "The list could not be asked, so nothing confirmed what may still be read")
        #expect(model.accounts.isEmpty)
        #expect(model.errorMessage != nil)
    }

    /// Two archives of one account can be pending when that account is refused. The first
    /// refusal retires both: the second is not asked about afterwards, so it cannot come back
    /// as a refusal with nothing left to say whose it was and close another account's
    /// conversation, taking the owner's unsaved words with it.
    @Test func testRefusalRetiresEveryArchiveOfItsAccountAndKeepsAnotherAccountsUnsavedEdit() async throws {
        let requests = EmailRequestRecorder()
        let revoked = EmailTestFlag()
        let firstWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let secondWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000897")!
        let model = try makeModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            // The autosave fails, so the words stay unsaved however long the test takes.
            if request.httpMethod == "PUT" { return (503, Self.error("unavailable", "Try again.")) }
            if path.contains("/drafts/") { return (200, "{\"items\":[],\"next_cursor\":null}") }
            // The Personal conversation is the one open, with a draft.
            let personal = self.archiveThreadJSON(inInbox: true, draft: self.draftJSON())
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            let firstWork = work.replacingOccurrences(of: "\(self.threadID)", with: "\(firstWorkID)")
            let secondWork = work.replacingOccurrences(of: "\(self.threadID)", with: "\(secondWorkID)")
            guard path.hasSuffix("threads") else {
                if path.contains("\(self.threadID)") { return (200, personal) }
                return revoked.isSet ? (403, Self.error("permission_denied", "Work mail can no longer be read."))
                    : (200, path.contains("\(firstWorkID)") ? firstWork : secondWork)
            }
            return (200, "{\"items\":[\(revoked.isSet ? personal : "\(firstWork),\(secondWork),\(personal)")],\"next_cursor\":null}")
        }
        defer { model.resetConnection() }
        await model.reload()
        #expect(model.items.map(\.id) == [threadID], "Both Work archives are pending, so their rows are hidden")
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My unsaved words")
        revoked.set()
        await model.checkArchiveStatus(firstWorkID)
        #expect(model.items.map(\.id) == [threadID], "The list answered, so Personal mail stays")
        #expect(model.thread?.id == threadID, "The open conversation is Personal's and stays open")
        #expect(model.currentEdit?.body == "My unsaved words", "The owner's unsaved words are kept")
        #expect(model.currentEdit?.isDirty == true)
        #expect(!requests.snapshot.contains { $0.httpMethod == "GET" && $0.url!.path.contains("\(secondWorkID)") },
                "The other Work archive went with the first refusal and is not asked about")
    }

    /// A status read can still be out for the second archive when the first one's refusal
    /// retires both. Its answer, a refusal too, is for an archive that is no longer followed:
    /// nothing is closed for it and nothing is read again.
    @Test func testStatusReadStillOutForARetiredArchiveChangesNothing() async throws {
        let requests = EmailRequestRecorder()
        let late = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let firstWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let secondWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000897")!
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if path.contains("/drafts/") { return (200, "{\"items\":[],\"next_cursor\":null}", nil) }
            let personal = self.archiveThreadJSON(inInbox: true, draft: self.draftJSON())
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            let firstWork = work.replacingOccurrences(of: "\(self.threadID)", with: "\(firstWorkID)")
            let secondWork = work.replacingOccurrences(of: "\(self.threadID)", with: "\(secondWorkID)")
            guard path.hasSuffix("threads") else {
                if path.contains("\(self.threadID)") { return (200, personal, nil) }
                // The second archive's status is refused too, and that answer arrives last.
                return (403, Self.error("permission_denied", "Work mail can no longer be read."),
                    path.contains("\(secondWorkID)") ? late : nil)
            }
            return (200, "{\"items\":[\(revoked.isSet ? personal : "\(firstWork),\(secondWork),\(personal)")],\"next_cursor\":null}", nil)
        }
        defer { late.release(); model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        let second = Task { await model.checkArchiveStatus(secondWorkID) }
        try await waitForEmailTestCondition { late.isWaiting }
        revoked.set()
        await model.checkArchiveStatus(firstWorkID)
        #expect(model.thread?.id == threadID)
        let listReads = { requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count }
        let before = listReads()
        late.release()
        await second.value
        #expect(model.thread?.id == threadID, "The open Personal conversation stays open")
        #expect(model.items.map(\.id) == [threadID])
        #expect(listReads() == before, "The retired archive's late answer asks for nothing")
    }

    /// A refusal can name a conversation whose account nothing ever told. The reader closes
    /// then, because what it shows might be that account's. The owner's unsaved words are not
    /// thrown away for it: they wait with their draft and are there when it is opened again.
    @Test func testRefusalOfAnUnknownConversationClosesTheReaderButKeepsUnsavedWords() async throws {
        let unknownID = UUID(uuidString: "00000000-0000-0000-0000-000000000896")!
        let model = try makeModel { request in
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            // The autosave fails, so the words stay unsaved however long the test takes.
            if request.httpMethod == "PUT" { return (503, Self.error("unavailable", "Try again.")) }
            if path.contains("/drafts/") { return (200, "{\"items\":[],\"next_cursor\":null}") }
            let personal = self.archiveThreadJSON(inInbox: true, draft: self.draftJSON())
            guard path.hasSuffix("threads") else {
                return path.contains("\(self.threadID)") ? (200, personal)
                    : (403, Self.error("permission_denied", "This mail can no longer be read."))
            }
            return (200, "{\"items\":[\(personal)],\"next_cursor\":null}")
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My unsaved words")
        await model.checkArchiveStatus(unknownID)
        #expect(model.thread == nil, "Nothing says whose the refused conversation was, so the reader closes")
        #expect(model.items.map(\.id) == [threadID], "The list answered, so the mail it returned stays")
        await model.openThread(threadID)
        #expect(model.currentEdit?.body == "My unsaved words", "The unsaved words were kept with their draft")
        #expect(model.currentEdit?.isDirty == true, "They are still an edit, not what the server holds")
    }

    /// A list read asks for a hidden conversation's status and is then replaced by another list
    /// read, which shows that the archive failed and brings the row back. The first read's
    /// answer is older than that. Whether it says the archive is still pending or is a refusal,
    /// it changes nothing: the row is not hidden again, and no mail is dropped for it.
    @Test(arguments: ["pending", "refused"])
    func testReplacedListReadsStatusAnswerChangesNothing(answer: String) async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let failed = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard request.url!.path.hasSuffix("threads") else {
                // The second status read was asked for while the archive was pending and answers late.
                let pending = self.archiveThreadJSON(inInbox: true, status: "pending")
                guard requests.snapshot.filter({ $0.url!.path.contains("/threads/") }).count == 2 else {
                    return (200, pending, nil)
                }
                return answer == "refused"
                    ? (403, Self.error("permission_denied", "This mail can no longer be read."), statusRead)
                    : (200, pending, statusRead)
            }
            let listed = request.url?.query?.contains("view=handled") != true
            let row = self.archiveThreadJSON(inInbox: true, status: failed.isSet ? "failed" : "pending")
            return (200, "{\"items\":[\(listed ? row : "")],\"next_cursor\":null}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        #expect(model.items.isEmpty, "The archive is pending, so its row is hidden")
        // Handled does not list it, so each read of that list asks for its status.
        model.setCollection(.handled)
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.url!.path.contains("/threads/") } && !model.isReadingList
        }
        let replaced = Task { await model.reload(preserveOrder: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        failed.set()
        model.setListView("priority")
        try await waitForEmailTestCondition { model.items.map(\.id) == [self.threadID] && !model.isReadingList }
        let listReads = { requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count }
        let before = listReads()
        statusRead.release()
        // The replaced read takes its late answer and ends.
        await replaced.value
        #expect(model.items.map(\.id) == [threadID], "The failed archive's row stays in view")
        #expect(model.errorMessage == nil)
        #expect(listReads() == before, "Nothing is read again for an answer that belongs to a replaced read")
    }

    /// After a refusal the list is asked what still reads. If the owner changes collection
    /// meanwhile, the new collection's list read replaces that one, and it too was asked after
    /// the refusal. The replaced read coming back decides nothing: nothing is cleared for it and
    /// the owner's unsaved words stay. The new read decides: its answer keeps the other
    /// accounts' mail, and its failure clears everything.
    @Test(arguments: [200, 503])
    func testCollectionSwitchDuringRefusalRecoveryLetsTheNewListReadDecide(newList: Int) async throws {
        let recovery = EmailArchiveResponseGate()
        let switched = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let workID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let model = try makeArchiveRaceModel { request in
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.httpMethod == "PUT" { return (503, Self.error("unavailable", "Try again."), nil) }
            if path.contains("/drafts/") { return (200, "{\"items\":[],\"next_cursor\":null}", nil) }
            let personal = self.archiveThreadJSON(inInbox: true, draft: self.draftJSON())
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
                .replacingOccurrences(of: "\(self.threadID)", with: "\(workID)")
            guard path.hasSuffix("threads") else {
                if path.contains("\(self.threadID)") { return (200, personal, nil) }
                return (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
            }
            guard revoked.isSet else { return (200, "{\"items\":[\(work),\(personal)],\"next_cursor\":null}", nil) }
            // Both list reads asked after the refusal are held, so that the test orders them.
            guard request.url?.query?.contains("view=inbox") == true else {
                return (200, "{\"items\":[\(personal)],\"next_cursor\":null}", recovery)
            }
            return newList == 200 ? (200, "{\"items\":[\(personal)],\"next_cursor\":null}", switched)
                : (503, Self.error("unavailable", "Try again."), switched)
        }
        defer { recovery.release(); switched.release(); model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My unsaved words")
        revoked.set()
        let refusal = Task { await model.checkArchiveStatus(workID) }
        try await waitForEmailTestCondition { recovery.isWaiting }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { switched.isWaiting }
        recovery.release()
        await refusal.value
        #expect(model.accounts.count == 2, "The replaced list read coming back decides nothing")
        #expect(model.thread?.id == threadID)
        #expect(model.currentEdit?.body == "My unsaved words")
        switched.release()
        try await waitForEmailTestCondition {
            !model.isReadingList && (newList == 200 ? model.errorMessage != nil : model.accounts.isEmpty)
        }
        #expect(model.items.map(\.id) == (newList == 200 ? [threadID] : []))
        #expect(model.accounts.count == (newList == 200 ? 2 : 0))
        #expect(model.thread?.id == (newList == 200 ? threadID : nil))
        #expect(model.currentEdit?.body == (newList == 200 ? "My unsaved words" : nil))
        #expect(model.errorMessage != nil)
    }

    /// Leaving Email while the list has not yet answered after a refusal leaves nothing to
    /// confirm what may still be read. Everything is cleared at once, not when an answer arrives.
    @Test func testLeavingEmailBeforeTheListAnswersAfterARefusalClearsEverything() async throws {
        let requests = EmailRequestRecorder()
        let recovery = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if path.hasSuffix("refresh") || path.contains("/operations/") {
                return (200, self.operationJSON(status: "COMPLETED"), nil)
            }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            guard path.hasSuffix("threads") else {
                return revoked.isSet
                    ? (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
                    : (200, work, nil)
            }
            guard revoked.isSet else { return (200, "{\"items\":[\(work),\(personal)],\"next_cursor\":null}", nil) }
            return (200, "{\"items\":[\(personal)],\"next_cursor\":null}", recovery)
        }
        defer { recovery.release(); model.resetConnection() }
        model.setActive(true)
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing && !model.isReadingList
        }
        #expect(model.items.map(\.id) == [personalID], "The Work archive is pending, so its row is hidden")
        revoked.set()
        // The poll reads the hidden conversation's status a second after it was first seen.
        try await waitForEmailTestCondition { recovery.isWaiting }
        #expect(model.accounts.count == 2, "The list has been asked and has not answered yet")
        model.setActive(false)
        #expect(model.items.isEmpty, "Nothing is left to confirm what may still be read")
        #expect(model.accounts.isEmpty)
        #expect(model.errorMessage != nil)
        let posts = { requests.snapshot.filter { $0.httpMethod == "POST" }.count }
        let before = posts()
        await model.refresh()
        #expect(posts() == before, "The owner has left Email, so its visit is over and nothing refreshes")
    }

    /// An archive, or a move back to the Inbox, can still be waiting to be admitted when another
    /// conversation of its account is refused. It is retired with that account. Its answer,
    /// admitted or failed, is for nothing that is still followed: no status is asked for,
    /// nothing is reported, and a conversation of another account stays open.
    @Test(arguments: [202, 500], [false, true])
    func testArchiveAnswerForARetiredRequestChangesNothing(answer: Int, restoring: Bool) async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let firstWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let secondWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000897")!
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if path.contains("/drafts/") { return (200, "{\"items\":[],\"next_cursor\":null}", nil) }
            if path.hasSuffix("archive") {
                return (answer, answer == 202 ? self.operationJSON(status: "QUEUED")
                    : Self.error("internal", "Try again."), admission)
            }
            let personal = self.archiveThreadJSON(inInbox: true, draft: self.draftJSON())
            let firstWork = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
                .replacingOccurrences(of: "\(self.threadID)", with: "\(firstWorkID)")
            let secondWork = self.archiveThreadJSON(account: "work", inInbox: !restoring)
                .replacingOccurrences(of: "\(self.threadID)", with: "\(secondWorkID)")
            guard path.hasSuffix("threads") else {
                if path.contains("\(self.threadID)") { return (200, personal, nil) }
                return (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
            }
            return (200, "{\"items\":[\(revoked.isSet ? personal : "\(firstWork),\(secondWork),\(personal)")],\"next_cursor\":null}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        #expect(model.items.map(\.id) == [secondWorkID, threadID])
        await model.openThread(threadID)
        let row = try #require(model.items.first { $0.id == secondWorkID })
        let archiving = Task { await model.setThreadArchived(row, archived: !restoring) }
        try await waitForEmailTestCondition { admission.isWaiting }
        revoked.set()
        await model.checkArchiveStatus(firstWorkID)
        #expect(model.items.map(\.id) == [threadID], "The list answered, so Personal mail stays")
        #expect(model.thread?.id == threadID)
        let gets = { requests.snapshot.filter { $0.httpMethod == "GET" && $0.url!.path.contains("/threads") }.count }
        let before = gets()
        admission.release()
        await archiving.value
        #expect(model.thread?.id == threadID, "The open Personal conversation stays open")
        #expect(gets() == before, "Neither the retired archive's status nor the list is asked for")
        #expect(model.archiveErrors[secondWorkID] == nil, "Nothing is reported for a retired archive")
    }

    /// A conversation opened directly is in no list. When its own status is refused, the open
    /// conversation says whose mail that was, and that account's other rows go at once, before
    /// the list has answered.
    @Test func testRefusedOpenConversationSaysWhoseRowsGo() async throws {
        let recovery = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let otherWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000897")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let otherWork = archiveThreadJSON(account: "work", inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "\(otherWorkID)")
        let model = try makeArchiveRaceModel { request in
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard path.hasSuffix("threads") else {
                // The conversation opened directly reads once, and is refused after that.
                return revoked.isSet
                    ? (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
                    : (200, self.archiveThreadJSON(account: "work", inInbox: true)
                        .replacingOccurrences(of: "\"draft_id\":\"\(self.draftID)\"", with: "\"draft_id\":null"), nil)
            }
            guard revoked.isSet else { return (200, "{\"items\":[\(otherWork),\(personal)],\"next_cursor\":null}", nil) }
            return (200, "{\"items\":[\(personal)],\"next_cursor\":null}", recovery)
        }
        defer { recovery.release(); model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        #expect(model.thread?.accountID == "work")
        revoked.set()
        let refusal = Task { await model.checkArchiveStatus(threadID) }
        try await waitForEmailTestCondition { recovery.isWaiting }
        #expect(model.items.map(\.id) == [personalID], "Work's other row went with the refusal, before the list answered")
        #expect(model.thread == nil)
        recovery.release()
        await refusal.value
        #expect(model.items.map(\.id) == [personalID])
        #expect(model.accounts.count == 2)
    }

    /// When the list read that decides after a refusal fails, everything is cleared. Email is
    /// still on screen, though, and its visit goes on: Refresh asks again without the owner
    /// having to leave and come back.
    @Test func testRefreshStaysAvailableWhenTheListFailsAfterARefusal() async throws {
        let requests = EmailRequestRecorder()
        let revoked = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let model = try makeModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if path.hasSuffix("refresh") || path.contains("/operations/") {
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            guard path.hasSuffix("threads") else {
                return revoked.isSet ? (403, Self.error("permission_denied", "Work mail can no longer be read.")) : (200, work)
            }
            if revoked.isSet { return (503, Self.error("unavailable", "Try again.")) }
            return (200, "{\"items\":[\(work),\(personal)],\"next_cursor\":null}")
        }
        defer { model.resetConnection() }
        let refreshes = { requests.snapshot.filter { $0.httpMethod == "POST" && $0.url!.path.hasSuffix("refresh") }.count }
        model.setActive(true)
        try await waitForEmailTestCondition { refreshes() == 1 && !model.isRefreshing && !model.isReadingList }
        #expect(model.items.map(\.id) == [personalID], "The Work archive is pending, so its row is hidden")
        revoked.set()
        // The poll reads the hidden conversation's status a second after it was first seen.
        try await waitForEmailTestCondition { model.accounts.isEmpty }
        #expect(model.items.isEmpty, "The list failed after the refusal, so nothing confirmed what may be read")
        #expect(model.errorMessage != nil)
        let before = refreshes()
        await model.refresh()
        #expect(refreshes() == before + 1, "Email is still on screen, so Refresh asks again")
    }

    /// The list read that decides after a refusal can itself end in three ways that clear
    /// everything. Only one of them, needing to sign in again, is reported to the connection.
    @Test(arguments: [401, 403, 503])
    func testListReadAfterARefusalTakesItsOwnErrorRoute(list: Int) async throws {
        let revoked = EmailTestFlag()
        let signIn = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let model = try makeModel { request in
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            guard path.hasSuffix("threads") else {
                return revoked.isSet ? (403, Self.error("permission_denied", "Work mail can no longer be read.")) : (200, work)
            }
            guard revoked.isSet else { return (200, "{\"items\":[\(work),\(personal)],\"next_cursor\":null}") }
            return (list, Self.error(list == 401 ? "unauthenticated" : list == 403 ? "permission_denied" : "unavailable",
                "The list could not be read."))
        }
        defer { model.resetConnection() }
        model.authenticationFailure = { _ in signIn.set() }
        await model.reload()
        #expect(model.items.map(\.id) == [personalID])
        revoked.set()
        await model.checkArchiveStatus(threadID)
        #expect(model.items.isEmpty)
        #expect(model.accounts.isEmpty)
        #expect(model.errorMessage != nil)
        #expect(signIn.isSet == (list == 401), "Only a list that needs the owner to sign in again asks for it")
    }

    /// A status check learns that an archive was confirmed and reads the list. The list shows
    /// that the conversation has since come back and has a newer archive pending, which hides
    /// its row. The older outcome is applied before the list is read, so it does not lift the
    /// newer archive's hide afterwards. The list read also brings mail that has arrived since,
    /// which shows that its answer was applied.
    @Test func testConfirmedArchiveDoesNotLiftANewerArchivesHide() async throws {
        let requests = EmailRequestRecorder()
        let confirmed = EmailTestFlag()
        let newerOperation = UUID(uuidString: "00000000-0000-0000-0000-000000000877")!
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            let other = { (json: String) in json.replacingOccurrences(of: "\"priority\":0.95", with: "\"priority\":0.4") }
            guard request.url!.path.hasSuffix("threads") else {
                // The first archive, begun on another device, has been confirmed.
                return (200, other(self.archiveThreadJSON(inInbox: false, status: "completed")))
            }
            // When the list is read again the conversation is back in the Inbox, with a newer
            // archive pending, and new mail has arrived beside it.
            let row = other(self.archiveThreadJSON(inInbox: true, status: "pending", operationID: confirmed.isSet ? newerOperation : nil))
            let arrival = other(self.archiveThreadJSON(inInbox: true)).replacingOccurrences(of: "\(self.threadID)", with: "\(arrivalID)")
            return (200, "{\"items\":[\(confirmed.isSet ? "\(row),\(arrival)" : row)],\"next_cursor\":null}")
        }
        defer { model.resetConnection() }
        model.setListView("other")
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.url!.path.hasSuffix("threads") } && !model.isReadingList
        }
        #expect(model.items.isEmpty, "The first archive is pending, so its row is hidden")
        confirmed.set()
        await model.checkArchiveStatus(threadID)
        #expect(model.items.map(\.id) == [arrivalID], "The list was read after the confirmation, and its answer is shown")
        #expect(!model.items.contains { $0.id == threadID }, "The newer archive is pending, so the row stays hidden")
    }

    /// A list loaded two pages deep is read again as deep as it was loaded, also after a row has
    /// left it: because Gmail confirmed its archive, because its account was refused, because
    /// opening it showed that it has left the list, or because the owner excluded it. The rows
    /// on screen are then one fewer than the list was loaded to. Reading only as deep as they go
    /// reads one page where two were loaded, and once new mail has arrived, the row loaded from
    /// the second page is no longer listed. The depth belongs to the list, not to the read asked
    /// for the row that left: another read that replaces that one before it answers, as a
    /// regular refresh or Try again can, reads as deep.
    @Test(arguments: ["confirmed", "refused", "opened", "excluded"], [false, true])
    func testListStaysAsDeepAsItWasLoadedWhenARowLeaves(how: String, replaced: Bool) async throws {
        let left = EmailTestFlag()
        let afterLeaving = EmailRequestRecorder()
        let firstRead = EmailArchiveResponseGate()
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let tailID = UUID(uuidString: "00000000-0000-0000-0000-000000002099")!
        let withoutDraft = { (json: String) in
            json.replacingOccurrences(of: "\"draft_id\":\"\(self.draftID)\"", with: "\"draft_id\":null")
        }
        let row = { (id: String) in
            withoutDraft(self.archiveThreadJSON(inInbox: true)).replacingOccurrences(of: "\(self.threadID)", with: id)
        }
        let fillers = (0..<100).map { row(String(format: "00000000-0000-0000-0000-%012d", 2000 + $0)) }
        // The row that leaves. A pending archive hides it where the archive, or a refusal of
        // the archive's status, is what takes it away.
        let hidden = how == "confirmed" || how == "refused"
        let lead = withoutDraft(archiveThreadJSON(
            account: how == "refused" ? "work" : "personal", inInbox: true, status: hidden ? "pending" : nil))
        let model = try makeArchiveRaceModel { request in
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if path.hasSuffix("exclude") { return (200, "{}", nil) }
            guard path.hasSuffix("threads") else {
                // The conversation itself, as each cause finds it once the row is to leave.
                guard left.isSet else { return (200, lead, nil) }
                switch how {
                case "confirmed": return (200, withoutDraft(self.archiveThreadJSON(inInbox: false, status: "completed")), nil)
                case "refused": return (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
                default: return (200, withoutDraft(self.archiveThreadJSON(inInbox: false)), nil)
                }
            }
            // Page one holds a hundred rows, led by the row that leaves or, afterwards, by mail
            // that has arrived since. Page two holds the last row.
            let second = request.url?.query?.contains("cursor=100") == true
            let first = ([left.isSet ? row("\(arrivalID)") : lead] + fillers.prefix(99)).joined(separator: ",")
            let body = "{\"items\":[\(second ? fillers[99] : first)],\"next_cursor\":\(second ? "null" : "\"100\"")}"
            guard left.isSet else { return (200, body, nil) }
            afterLeaving.append(request)
            // The read asked for the row that left is held where another read is to replace it.
            return (200, body, replaced && afterLeaving.snapshot.count == 1 ? firstRead : nil)
        }
        defer { firstRead.release(); model.resetConnection() }
        await model.reload()
        await model.loadMore()
        #expect(model.items.count == (hidden ? 100 : 101), "A hundred and one rows are loaded")
        #expect(model.items.contains { $0.id == tailID })
        if how == "excluded" { await model.openThread(threadID) }
        left.set()
        let leaving = Task {
            switch how {
            case "opened": await model.openThread(self.threadID)
            case "excluded": await model.excludeSelectedThread()
            default: await model.checkArchiveStatus(self.threadID)
            }
        }
        if replaced {
            try await waitForEmailTestCondition { firstRead.isWaiting }
            // What a regular refresh and Try again ask: the list again, keeping the rows on screen.
            await model.reload(preserveOrder: true)
            firstRead.release()
        }
        await leaving.value
        try await waitForEmailTestCondition { model.items.contains { $0.id == arrivalID } && !model.isReadingList }
        #expect(model.items.contains { $0.id == arrivalID }, "The list was read again after the row left")
        #expect(!model.items.contains { $0.id == self.threadID })
        #expect(model.items.contains { $0.id == tailID }, "The row loaded from the second page is still listed")
    }

    /// The regular refresh asks the list first while a refusal waits, and so replaces the list
    /// read the refusal asked for. It reads as deep as the list was loaded: the refused
    /// account's row has left the screen, new mail has arrived, and the row loaded from the
    /// second page is still listed.
    @Test func testRegularRefreshAfterARefusalReadsAsDeepAsTheListWasLoaded() async throws {
        let requests = EmailRequestRecorder()
        let afterRefusal = EmailRequestRecorder()
        let recovery = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let tailID = UUID(uuidString: "00000000-0000-0000-0000-000000002099")!
        let row = { (id: String) in
            self.archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(self.threadID)", with: id)
        }
        let fillers = (0..<100).map { row(String(format: "00000000-0000-0000-0000-%012d", 2000 + $0)) }
        let work = archiveThreadJSON(account: "work", inInbox: true, status: "pending")
        let model = try makeArchiveRaceModel(refreshNanoseconds: 1_500_000_000) { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            // No refresh is admitted, so a regular refresh reads the list only by asking it first.
            if path.hasSuffix("refresh") { return (500, Self.error("internal", "Try again."), nil) }
            guard path.hasSuffix("threads") else {
                return revoked.isSet
                    ? (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
                    : (200, work, nil)
            }
            // Page one holds a hundred rows, led by Work's row or, once Work is refused, by mail
            // that has arrived since. Page two holds the last row.
            let second = request.url?.query?.contains("cursor=100") == true
            let first = ([revoked.isSet ? row("\(arrivalID)") : work] + fillers.prefix(99)).joined(separator: ",")
            let body = "{\"items\":[\(second ? fillers[99] : first)],\"next_cursor\":\(second ? "null" : "\"100\"")}"
            guard revoked.isSet else { return (200, body, nil) }
            afterRefusal.append(request)
            // The list read the refusal asks for is never answered. The regular refresh's is.
            return (200, body, afterRefusal.snapshot.count == 1 ? recovery : nil)
        }
        defer { recovery.release(); model.resetConnection() }
        model.setActive(true)
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing && !model.isReadingList
        }
        await model.loadMore()
        #expect(model.items.count == 100, "A hundred and one rows are loaded, one of them hidden by its pending archive")
        #expect(model.items.contains { $0.id == tailID })
        revoked.set()
        // The poll reads the hidden conversation's status within a second and is refused.
        try await waitForEmailTestCondition { recovery.isWaiting }
        // The next regular refresh comes round within a second and a half and asks the list first.
        try await waitForEmailTestCondition { model.items.contains { $0.id == arrivalID } && !model.isReadingList }
        #expect(model.items.contains { $0.id == arrivalID }, "The regular refresh's list read answered")
        #expect(model.items.contains { $0.id == tailID }, "The row loaded from the second page is still listed")
    }

    /// How deep a list was loaded belongs to that list. After a row has left a list loaded two
    /// pages deep, the owner turns to another collection. That collection is read from its
    /// first page only, also by a read that keeps the rows on screen and replaces the
    /// collection's first read before it answers.
    @Test func testAnotherCollectionDoesNotTakeTheDepthOfTheListThatWasLeft() async throws {
        let requests = EmailRequestRecorder()
        let recovery = EmailArchiveResponseGate()
        let firstInbox = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let row = { (id: String) in
            self.archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(self.threadID)", with: id)
        }
        let fillers = (0..<101).map { row(String(format: "00000000-0000-0000-0000-%012d", 2000 + $0)) }
        let work = archiveThreadJSON(account: "work", inInbox: true, status: "pending")
        let inboxReads = { requests.snapshot.filter { $0.url?.query?.contains("view=inbox") == true } }
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard path.hasSuffix("threads") else {
                return revoked.isSet
                    ? (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
                    : (200, work, nil)
            }
            // Every list has a second page: a hundred rows, then one more.
            let second = request.url?.query?.contains("cursor=100") == true
            let first = ([revoked.isSet ? fillers[100] : work] + fillers.prefix(99)).joined(separator: ",")
            let body = "{\"items\":[\(second ? fillers[99] : first)],\"next_cursor\":\(second ? "null" : "\"100\"")}"
            guard revoked.isSet else { return (200, body, nil) }
            // The list read the refusal asks for is held, and so is the Inbox's first read.
            if request.url?.query?.contains("view=inbox") != true { return (200, body, recovery) }
            return (200, body, inboxReads().count == 1 ? firstInbox : nil)
        }
        defer { recovery.release(); firstInbox.release(); model.resetConnection() }
        await model.reload()
        await model.loadMore()
        #expect(model.items.count == 100, "A hundred and one rows of Important are loaded, one of them hidden")
        revoked.set()
        let refusal = Task { await model.checkArchiveStatus(threadID) }
        try await waitForEmailTestCondition { recovery.isWaiting }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { firstInbox.isWaiting }
        await model.reload(preserveOrder: true)
        #expect(model.items.count == 100, "The Inbox is read from its first page")
        #expect(model.hasMore)
        #expect(!inboxReads().contains { $0.url?.query?.contains("cursor=100") == true },
                "The depth Important was loaded to is not asked of the Inbox")
        firstInbox.release()
        recovery.release()
        await refusal.value
    }

    /// A kept list keeps its depth. A list read can return a hundred and one rows and show a
    /// hundred, because Gmail confirmed an archive while it was out: the list is then loaded
    /// one row deeper than the rows on screen go. The owner turns to another collection and
    /// comes back to the kept list. The read that follows goes as deep as the list was
    /// loaded, so once new mail has arrived, the row loaded from the second page is still listed.
    @Test func testKeptListComesBackAsDeepAsItWasLoaded() async throws {
        let confirmed = EmailTestFlag()
        let arrived = EmailTestFlag()
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let tailID = UUID(uuidString: "00000000-0000-0000-0000-000000002099")!
        let row = { (id: String) in
            self.archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(self.threadID)", with: id)
        }
        let fillers = (0..<100).map { row(String(format: "00000000-0000-0000-0000-%012d", 2000 + $0)) }
        let model = try makeModel { request in
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            guard path.hasSuffix("threads") else {
                return (200, self.archiveThreadJSON(
                    inInbox: !confirmed.isSet, status: confirmed.isSet ? "completed" : "pending"))
            }
            // Page one holds a hundred rows, led by the conversation being archived. The list
            // still returns it for a while after Gmail has confirmed; then new mail leads.
            // Page two holds the last row.
            let second = request.url?.query?.contains("cursor=100") == true
            let lead = arrived.isSet ? row("\(arrivalID)") : self.archiveThreadJSON(inInbox: true, status: "pending")
            let first = ([lead] + fillers.prefix(99)).joined(separator: ",")
            return (200, "{\"items\":[\(second ? fillers[99] : first)],\"next_cursor\":\(second ? "null" : "\"100\"")}")
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.loadMore()
        #expect(model.items.count == 100, "A hundred and one rows are loaded, one of them hidden by its pending archive")
        // Gmail confirms while the list is read again: the read returns the conversation on page
        // one, learns of the confirmation from its status, and leaves it out.
        confirmed.set()
        await model.reload(preserveOrder: true)
        #expect(model.items.count == 100, "A hundred and one rows came back, and a hundred are shown")
        #expect(!model.items.contains { $0.id == self.threadID })
        #expect(model.items.contains { $0.id == tailID })
        arrived.set()
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.items.contains { $0.id == arrivalID } && !model.isReadingList }
        // Important was kept. It comes back with its rows and is read again in place.
        model.setListView("priority")
        #expect(model.items.count == 100, "The kept list is shown at once")
        try await waitForEmailTestCondition { model.items.contains { $0.id == arrivalID } && !model.isReadingList }
        #expect(model.items.contains { $0.id == tailID }, "The row loaded from the second page is still listed")
    }

    /// More mail does nothing while a refusal waits for the list. The refusal has taken the
    /// list for the read it asks for, and until a list read asked after the refusal has
    /// answered, the cursor on screen belongs to a list that is still being decided. Here the
    /// read asked for the refusal is replaced by one whose caller is then cancelled, so no list
    /// read is out, and More mail still asks nothing. Once a list read has answered, it works.
    @Test func testMoreMailDoesNothingWhileARefusalWaitsForTheList() async throws {
        let requests = EmailRequestRecorder()
        let afterRefusal = EmailRequestRecorder()
        let recovery = EmailArchiveResponseGate()
        let replacement = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let tailID = UUID(uuidString: "00000000-0000-0000-0000-000000002099")!
        let row = { (id: String) in
            self.archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(self.threadID)", with: id)
        }
        let fillers = (0..<100).map { row(String(format: "00000000-0000-0000-0000-%012d", 2000 + $0)) }
        let work = archiveThreadJSON(account: "work", inInbox: true, status: "pending")
        let pages = { requests.snapshot.filter { $0.url?.query?.contains("cursor=") == true }.count }
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard path.hasSuffix("threads") else {
                return revoked.isSet
                    ? (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
                    : (200, work, nil)
            }
            // The list has more: a hundred rows, then one more.
            let second = request.url?.query?.contains("cursor=100") == true
            let first = ([revoked.isSet ? row("\(arrivalID)") : work] + fillers.prefix(99)).joined(separator: ",")
            let body = "{\"items\":[\(second ? fillers[99] : first)],\"next_cursor\":\(second ? "null" : "\"100\"")}"
            guard revoked.isSet, !second else { return (200, body, nil) }
            afterRefusal.append(request)
            // The list read the refusal asks for is held, and so is the one that replaces it.
            switch afterRefusal.snapshot.count {
            case 1: return (200, body, recovery)
            case 2: return (200, body, replacement)
            default: return (200, body, nil)
            }
        }
        defer { recovery.release(); replacement.release(); model.resetConnection() }
        await model.reload()
        #expect(model.items.count == 99, "A hundred rows are loaded, one of them hidden by its pending archive")
        #expect(model.hasMore)
        revoked.set()
        let refusal = Task { await model.checkArchiveStatus(threadID) }
        try await waitForEmailTestCondition { recovery.isWaiting }
        let replacing = Task { await model.reload(preserveOrder: true) }
        try await waitForEmailTestCondition { replacement.isWaiting }
        // Its caller no longer counts, so it changes nothing: the refusal still waits, and no list read is out.
        replacing.cancel()
        await replacing.value
        #expect(!model.isReadingList)
        #expect(model.hasMore)
        let before = pages()
        await model.loadMore()
        #expect(pages() == before, "More mail asks nothing while the refusal waits")
        #expect(!model.items.contains { $0.id == tailID })
        // A list read asked after the refusal answers, and More mail works again.
        await model.reload(preserveOrder: true)
        #expect(model.items.contains { $0.id == arrivalID })
        await model.loadMore()
        #expect(pages() == before + 1)
        #expect(model.items.contains { $0.id == tailID })
        recovery.release()
        replacement.release()
        await refusal.value
    }

    /// A page asked for before a refusal can answer after it, as a list read can. The refusal
    /// took the list's request, which retires the page: its row is not added, and its cursor
    /// does not replace the one the list read after the refusal brings.
    @Test func testPageAskedBeforeARefusalIsNotShownAfterIt() async throws {
        let afterRefusal = EmailRequestRecorder()
        let page = EmailArchiveResponseGate()
        let recovery = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let tailID = UUID(uuidString: "00000000-0000-0000-0000-000000002099")!
        let row = { (id: String) in
            self.archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(self.threadID)", with: id)
        }
        let fillers = (0..<100).map { row(String(format: "00000000-0000-0000-0000-%012d", 2000 + $0)) }
        let work = archiveThreadJSON(account: "work", inInbox: true, status: "pending")
        let model = try makeArchiveRaceModel { request in
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard path.hasSuffix("threads") else {
                return (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
            }
            // The second page, asked for before the refusal, is held: one last row, and no more.
            if request.url?.query?.contains("cursor=100") == true {
                return (200, "{\"items\":[\(fillers[99])],\"next_cursor\":null}", page)
            }
            let first = ([work] + fillers.prefix(99)).joined(separator: ",")
            guard revoked.isSet else { return (200, "{\"items\":[\(first)],\"next_cursor\":\"100\"}", nil) }
            afterRefusal.append(request)
            // The list read the refusal asks for is held too. Work is gone from it, and it has more.
            return (200, "{\"items\":[\(fillers.prefix(99).joined(separator: ","))],\"next_cursor\":\"99\"}",
                    afterRefusal.snapshot.count == 1 ? recovery : nil)
        }
        defer { page.release(); recovery.release(); model.resetConnection() }
        await model.reload()
        #expect(model.items.count == 99, "A hundred rows are loaded, one of them hidden by its pending archive")
        let paging = Task { await model.loadMore() }
        try await waitForEmailTestCondition { page.isWaiting }
        revoked.set()
        let refusal = Task { await model.checkArchiveStatus(threadID) }
        try await waitForEmailTestCondition { recovery.isWaiting }
        // The page answers now, and ends, while the list read asked after the refusal is still out.
        page.release()
        await paging.value
        #expect(!model.items.contains { $0.id == tailID }, "The page asked before the refusal is not added")
        recovery.release()
        await refusal.value
        #expect(model.items.count == 99)
        #expect(!model.items.contains { $0.id == tailID })
        #expect(model.hasMore, "The cursor is the one the list read after the refusal brought")
    }

    /// The one-second poll is usually the first to learn that Gmail confirmed an archive.
    /// Applying that outcome ends the poll, and the list is still read afterwards, so the mail
    /// that has arrived meanwhile is shown.
    @Test func testPollThatLearnsOfAConfirmedArchiveStillReadsTheList() async throws {
        let requests = EmailRequestRecorder()
        let confirmed = EmailTestFlag()
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let arrival = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(arrivalID)")
        let model = try makeModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if path.hasSuffix("refresh") || path.contains("/operations/") {
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            guard path.hasSuffix("threads") else {
                return (200, self.archiveThreadJSON(
                    inInbox: !confirmed.isSet, status: confirmed.isSet ? "completed" : "pending"))
            }
            // Once Gmail has confirmed, the list holds new mail in place of the archived conversation.
            let row = confirmed.isSet ? arrival : self.archiveThreadJSON(inInbox: true, status: "pending")
            return (200, "{\"items\":[\(row)],\"next_cursor\":null}")
        }
        defer { model.resetConnection() }
        model.setActive(true)
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing && !model.isReadingList
        }
        #expect(model.items.isEmpty, "The archive is pending, so its row is hidden")
        confirmed.set()
        // The poll reads the status a second after the archive was first seen, and then the list.
        try await waitForEmailTestCondition { model.items.map(\.id) == [arrivalID] }
        #expect(model.items.map(\.id) == [arrivalID], "The list was read after the confirmation")
    }

    /// The poll learns that an archive was confirmed and reads the list, which shows a newer
    /// archive of the same conversation and starts a poll for it. The first poll then ends, and
    /// must leave the newer one in place: when the newer archive is confirmed in its turn, that
    /// poll is stopped, and nothing reads the conversation's status or the list any more.
    @Test func testPollThatEndsLeavesTheNewerArchivesPollInPlace() async throws {
        let requests = EmailRequestRecorder()
        let newerOperation = UUID(uuidString: "00000000-0000-0000-0000-000000000877")!
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let arrival = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(arrivalID)")
        let statusReads = { requests.snapshot.filter { $0.url!.path.contains("/threads/") }.count }
        let listReads = { requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count }
        let model = try makeModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if path.hasSuffix("refresh") || path.contains("/operations/") {
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            guard path.hasSuffix("threads") else {
                // The first status read confirms the first archive, the second the newer one.
                return (200, self.archiveThreadJSON(
                    inInbox: false, status: "completed", operationID: statusReads() == 1 ? nil : newerOperation))
            }
            // The list follows: the first archive pending, then the conversation back in the
            // Inbox with the newer archive pending, then new mail in its place.
            switch statusReads() {
            case 0: return (200, "{\"items\":[\(self.archiveThreadJSON(inInbox: true, status: "pending"))],\"next_cursor\":null}")
            case 1: return (200, "{\"items\":[\(self.archiveThreadJSON(inInbox: true, status: "pending", operationID: newerOperation))],\"next_cursor\":null}")
            default: return (200, "{\"items\":[\(arrival)],\"next_cursor\":null}")
            }
        }
        defer { model.resetConnection() }
        model.setActive(true)
        // Each poll reads its conversation's status a second after it began.
        try await waitForEmailTestCondition {
            statusReads() == 2 && model.items.map(\.id) == [arrivalID] && !model.isReadingList
        }
        #expect(statusReads() == 2, "Each archive was followed by a poll of its own")
        let before = (statusReads(), listReads())
        // A poll that had not been stopped would read again within a second.
        try await Task.sleep(nanoseconds: 1_600_000_000)
        #expect(statusReads() == before.0, "The newer archive's poll stopped when its archive was confirmed")
        #expect(listReads() == before.1)
    }

    /// A poll's status read is refused and the list is asked. If the list then still returns
    /// the conversation with its archive pending, a poll is started for it again. The refused
    /// read's own poll has ended, and ending it must not stop the new one.
    @Test func testRefusedPollLeavesAPollStartedByTheListReadInPlace() async throws {
        let requests = EmailRequestRecorder()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let statusReads = { requests.snapshot.filter { $0.url!.path.contains("/threads/") }.count }
        let model = try makeModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if path.hasSuffix("refresh") || path.contains("/operations/") {
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            guard path.hasSuffix("threads") else {
                // Refused once; by the next read the account reads again and Gmail has confirmed.
                return statusReads() == 1
                    ? (403, Self.error("permission_denied", "Work mail can no longer be read."))
                    : (200, self.archiveThreadJSON(account: "work", inInbox: false, status: "completed"))
            }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            return (200, "{\"items\":[\(statusReads() < 2 ? "\(work),\(personal)" : personal)],\"next_cursor\":null}")
        }
        defer { model.resetConnection() }
        model.setActive(true)
        // The first poll is refused a second after it began; the poll the list read then
        // starts reads the status a second after that.
        try await waitForEmailTestCondition { statusReads() == 2 && !model.isReadingList }
        #expect(statusReads() == 2, "The archive the list still showed is followed by a poll of its own")
        #expect(model.items.map(\.id) == [personalID])
    }

    /// A refusal waits only while a list read asked after it is out. When the owner changes
    /// collection and that collection's list read cannot start, none is left to answer, and
    /// everything is cleared, as when the read fails.
    @Test func testRefusalClearsEverythingWhenTheReplacementListReadCannotStart() async throws {
        let recovery = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let closed = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let id = EmailArchiveRaceURLProtocol.register { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard request.url!.path.hasSuffix("threads") else {
                return (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
            }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            guard revoked.isSet else { return (200, "{\"items\":[\(work),\(personal)],\"next_cursor\":null}", nil) }
            // The list read asked for by the refusal is held.
            return (200, "{\"items\":[\(personal)],\"next_cursor\":null}", recovery)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [EmailArchiveRaceURLProtocol.self]
        configuration.httpAdditionalHeaders = ["X-Email-Archive-Race": id]
        let api = VeetbotAPIClient(transport: HTTPTransport(
            configuration: try ConnectionConfiguration(baseURLString: "https://email.test"),
            tokenStore: InMemoryTokenStore(token: "test-token"), session: URLSession(configuration: configuration)
        ))
        let model = EmailViewModel(makeAPIClient: { closed.isSet ? nil : api })
        defer { recovery.release(); model.resetConnection() }
        await model.reload()
        #expect(model.items.map(\.id) == [personalID])
        revoked.set()
        let refusal = Task { await model.checkArchiveStatus(threadID) }
        try await waitForEmailTestCondition { recovery.isWaiting }
        // From here no client is handed out, so the new collection's list read cannot start.
        closed.set()
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.accounts.isEmpty }
        #expect(model.items.isEmpty)
        #expect(model.errorMessage != nil)
        recovery.release()
        await refusal.value
        #expect(model.accounts.isEmpty)
    }

    /// A caller that no longer counts, here one whose task was cancelled, asks nothing and
    /// says nothing about whether the list can be read. While a refusal waits, it clears
    /// nothing: the list read that is out for the refusal still answers, and its answer keeps
    /// the other accounts' mail.
    @Test func testCancelledCallerClearsNothingWhileARefusalWaitsForItsList() async throws {
        let recovery = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let personalRow = { (id: UUID) in
            self.archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(self.threadID)", with: "\(id)")
        }
        let model = try makeArchiveRaceModel { request in
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard path.hasSuffix("threads") else {
                return (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
            }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            guard revoked.isSet else {
                return (200, "{\"items\":[\(work),\(personalRow(personalID))],\"next_cursor\":null}", nil)
            }
            // The list read asked for by the refusal is held. It brings a row of its own.
            return (200, "{\"items\":[\(personalRow(arrivalID)),\(personalRow(personalID))],\"next_cursor\":null}", recovery)
        }
        defer { recovery.release(); model.resetConnection() }
        await model.reload()
        #expect(model.items.map(\.id) == [personalID], "The Work archive is pending, so its row is hidden")
        revoked.set()
        let refusal = Task { await model.checkArchiveStatus(threadID) }
        try await waitForEmailTestCondition { recovery.isWaiting }
        let cancelled = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            await model.reload(preserveOrder: true)
        }
        await cancelled.value
        #expect(model.items.map(\.id) == [personalID], "A caller that no longer counts clears nothing")
        #expect(model.accounts.count == 2)
        recovery.release()
        await refusal.value
        #expect(Set(model.items.map(\.id)) == [personalID, arrivalID], "The list read out for the refusal still answers")
        #expect(model.accounts.count == 2)
        #expect(model.errorMessage == "Work mail can no longer be read.")
    }

    /// A list read asked before a refusal can answer after it. Its answer is older than the
    /// refusal and may still hold the refused account's mail. Nothing it brings is shown: not
    /// the refused account's other row, not a row of another account that only it holds, and
    /// not its copy of a row that is on screen. It does not stand in for the list read that has
    /// to answer after the refusal either. Here that read fails, so everything is cleared.
    /// The refusal hands the list to the read it asks for in the same step, so the test can
    /// hold that read open, deliver the earlier answer, and look.
    @Test func testListReadAskedBeforeARefusalIsNotShownAfterIt() async throws {
        let requests = EmailRequestRecorder()
        let earlier = EmailArchiveResponseGate()
        let recovery = EmailArchiveResponseGate()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let otherWorkID = UUID(uuidString: "00000000-0000-0000-0000-000000000897")!
        let laterPersonalID = UUID(uuidString: "00000000-0000-0000-0000-000000000896")!
        let personalRow = { (id: UUID) in
            self.archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(self.threadID)", with: "\(id)")
        }
        let otherWork = archiveThreadJSON(account: "work", inInbox: true)
            .replacingOccurrences(of: "\(threadID)", with: "\(otherWorkID)")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            guard path.hasSuffix("threads") else {
                return (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
            }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            switch requests.snapshot.filter({ $0.url!.path.hasSuffix("threads") }).count {
            case 1: return (200, "{\"items\":[\(work),\(otherWork),\(personalRow(personalID))],\"next_cursor\":null}", nil)
            // Asked before the refusal and answered after it: Work's mail as it stood, a
            // Personal row only this answer holds, and the listed Personal row under a new subject.
            case 2:
                let changed = personalRow(personalID).replacingOccurrences(of: "Board discussion", with: "Changed since")
                return (200, "{\"items\":[\(work),\(otherWork),\(changed),\(personalRow(laterPersonalID))],"
                    + "\"next_cursor\":null}", earlier)
            // Asked after the refusal.
            default: return (503, Self.error("unavailable", "Try again."), recovery)
            }
        }
        defer { earlier.release(); recovery.release(); model.resetConnection() }
        await model.reload()
        #expect(model.items.map(\.id) == [otherWorkID, personalID], "The Work archive is pending, so its row is hidden")
        let before = Task { await model.reload(preserveOrder: true) }
        try await waitForEmailTestCondition { earlier.isWaiting }
        let refusal = Task { await model.checkArchiveStatus(threadID) }
        // The list read the refusal asks for is out once the refusal has been taken up.
        try await waitForEmailTestCondition { recovery.isWaiting }
        #expect(model.items.map(\.id) == [personalID], "Work's other row went with the refusal")
        // The earlier list read answers now, and ends, while the one asked after the refusal is still out.
        earlier.release()
        await before.value
        #expect(model.items.map(\.id) == [personalID],
                "Neither Work's other row nor the Personal row only the earlier answer holds is shown")
        #expect(model.items.first?.subject == "Board discussion", "The row on screen is not replaced by the earlier answer's copy")
        #expect(model.accounts.count == 2, "The list read asked after the refusal has not answered yet")
        recovery.release()
        await refusal.value
        #expect(model.items.isEmpty, "The list read asked after the refusal failed, and the earlier one did not stand in for it")
        #expect(model.accounts.isEmpty)
        #expect(model.errorMessage == "Work mail can no longer be read.")
    }

    /// A refusal can still be waiting when a regular refresh comes round: the list read asked
    /// for it has not answered. A refresh reads the list only once the server has admitted
    /// it, and the refusal does not wait on that. The list is asked first, and that read
    /// decides: its answer keeps the other accounts' mail, and its failure clears everything.
    @Test(arguments: [200, 503])
    func testRegularRefreshAsksTheListFirstWhileARefusalStillWaits(list: Int) async throws {
        let requests = EmailRequestRecorder()
        let afterRefusal = EmailRequestRecorder()
        let recovery = EmailArchiveResponseGate()
        let revoked = EmailTestFlag()
        let personalID = UUID(uuidString: "00000000-0000-0000-0000-000000000899")!
        let personal = archiveThreadJSON(inInbox: true).replacingOccurrences(of: "\(threadID)", with: "\(personalID)")
        let model = try makeArchiveRaceModel(refreshNanoseconds: 1_500_000_000) { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            // No refresh is admitted, so no refresh reads the list.
            if path.hasSuffix("refresh") { return (500, Self.error("internal", "Try again."), nil) }
            let work = self.archiveThreadJSON(account: "work", inInbox: true, status: "pending")
            guard path.hasSuffix("threads") else {
                return revoked.isSet
                    ? (403, Self.error("permission_denied", "Work mail can no longer be read."), nil)
                    : (200, work, nil)
            }
            guard revoked.isSet else { return (200, "{\"items\":[\(work),\(personal)],\"next_cursor\":null}", nil) }
            afterRefusal.append(request)
            // The list read the refusal asks for is never answered. The next one is.
            if afterRefusal.snapshot.count == 1 { return (200, "{\"items\":[\(personal)],\"next_cursor\":null}", recovery) }
            return list == 200 ? (200, "{\"items\":[\(personal)],\"next_cursor\":null}", nil)
                : (503, Self.error("unavailable", "Try again."), nil)
        }
        defer { recovery.release(); model.resetConnection() }
        model.setActive(true)
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing && !model.isReadingList
        }
        #expect(model.items.map(\.id) == [personalID], "The Work archive is pending, so its row is hidden")
        revoked.set()
        // The poll reads the hidden conversation's status a second after it was first seen.
        try await waitForEmailTestCondition { recovery.isWaiting }
        // The next regular refresh comes round within a second and a half.
        try await waitForEmailTestCondition {
            list == 200 ? afterRefusal.snapshot.count >= 2 && !model.isReadingList : model.accounts.isEmpty
        }
        guard list == 200 else {
            #expect(model.items.isEmpty, "The list could not be read, so nothing confirmed what may still be read")
            #expect(model.errorMessage != nil)
            return
        }
        // Leaving Email clears everything while a refusal still waits, and nothing once it was answered.
        model.setActive(false)
        #expect(model.items.map(\.id) == [personalID], "The list answered, so the refusal no longer waits")
        #expect(model.accounts.count == 2)
    }

    /// The statuses a list read asks for belong to the connection it began on. When the
    /// connection changes while one of them is out, the rest are not sent: they would name the
    /// old connection's conversations to the new one.
    @Test func testStatusReadsStopWhenTheConnectionChanges() async throws {
        let requests = EmailRequestRecorder()
        let heldStatus = EmailArchiveResponseGate()
        let secondID = UUID(uuidString: "00000000-0000-0000-0000-000000000898")!
        let statusReads = { requests.snapshot.filter { $0.url!.path.contains("/threads/") }.count }
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            let path = request.url!.path
            if path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            let first = self.archiveThreadJSON(inInbox: true, status: "pending")
            let second = first.replacingOccurrences(of: "\(self.threadID)", with: "\(secondID)")
            guard path.hasSuffix("threads") else {
                // The third status read, the first of the second batch, is the one held.
                return (200, path.contains("\(secondID)") ? second : first, statusReads() == 3 ? heldStatus : nil)
            }
            let listed = request.url?.query?.contains("view=handled") != true
            return (200, "{\"items\":[\(listed ? "\(first),\(second)" : "")],\"next_cursor\":null}", nil)
        }
        defer { heldStatus.release(); model.resetConnection() }
        await model.reload()
        #expect(model.items.isEmpty, "Both archives are pending, so both rows are hidden")
        // Handled lists neither, so each read of it asks for the status of both, one after the other.
        model.setCollection(.handled)
        try await waitForEmailTestCondition { statusReads() == 2 && !model.isReadingList }
        let reading = Task { await model.reload(preserveOrder: true) }
        try await waitForEmailTestCondition { heldStatus.isWaiting }
        model.resetConnection()
        heldStatus.release()
        // The read takes the held answer and ends.
        await reading.value
        #expect(statusReads() == 3, "The other conversation of the old connection is never asked for again")
    }

    /// A failed archive can leave a count lowered for what the conversation has left since:
    /// here its need for a reply. Archiving it again before the list is read hides it once
    /// more, and each count is still lowered for it once only.
    @Test func testRetriedArchiveIsTakenOffEachCountOnce() async throws {
        let requests = EmailRequestRecorder()
        let retry = EmailArchiveResponseGate()
        let failed = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") {
                let first = requests.snapshot.filter { $0.url!.path.hasSuffix("archive") }.count == 1
                return (202, self.operationJSON(status: "QUEUED"), first ? nil : retry)
            }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            // The archive fails after feedback has ended the conversation's need for a reply.
            let row = self.archiveThreadJSON(
                inInbox: true, status: !requested ? nil : failed.isSet ? "failed" : "pending")
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!failed.isSet)")
            guard request.url!.path.hasSuffix("threads") else { return (200, row, nil) }
            return (200, "{\"items\":[\(row)],\"next_cursor\":null,"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: true)),\"total_count\":7}", nil)
        }
        defer { retry.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        await model.setThreadArchived(row, archived: true)
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        failed.set()
        await model.checkArchiveStatus(threadID)
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 23, needsYou: 8, handled: 0, priority: 7, suspectedSpam: 2),
            "It is back in the Inbox and has left Needs you")
        let shown = try #require(model.items.first)
        let retrying = Task { await model.setThreadArchived(shown, archived: true) }
        try await waitForEmailTestCondition { retry.isWaiting }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(), "Needs you is lowered for it once, not twice")
        retry.release()
        await retrying.value
    }

    /// The server counts mail by its Inbox flag alone, except in Suspected spam. A hidden
    /// conversation that also has messages in Spam is still in the Inbox counts and comes off them.
    @Test func testHiddenConversationAlsoInSpamComesOffTheInboxCounts() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            let row = self.archiveThreadJSON(inInbox: true, status: "pending")
                .replacingOccurrences(of: "\"in_inbox\":true", with: "\"in_inbox\":true,\"in_spam\":true")
                .replacingOccurrences(of: "\"priority\":0.95", with: "\"priority\":0.4")
            guard request.url!.path.hasSuffix("threads") else { return (200, row) }
            return (200, "{\"items\":[\(row)],\"next_cursor\":null,"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: true)),\"total_count\":40}")
        }
        defer { model.resetConnection() }
        model.setListView("other")
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        #expect(model.items.isEmpty, "Its archive is pending, so its row is hidden")
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 22, needsYou: 8, handled: 0, priority: 7, suspectedSpam: 2),
            "It is in the Inbox awaiting a reply; that it is also in Spam changes neither count")
        #expect(model.listTotal == 39)
    }

    /// A long list is read in pages, and Gmail can confirm an archive between them. The earlier
    /// page still returns the conversation. It is not put back in the list.
    @Test func testArchiveConfirmedBetweenPagesStaysOutOfTheList() async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let armed = EmailTestFlag()
        let confirmed = EmailTestFlag()
        let fillers = (0..<100).map { index in
            archiveThreadJSON(inInbox: true).replacingOccurrences(
                of: "\(threadID)", with: String(format: "00000000-0000-0000-0000-%012d", 2000 + index))
        }
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            let wasConfirmed = confirmed.isSet
            let row = self.archiveThreadJSON(inInbox: !wasConfirmed, status: wasConfirmed ? "completed" : nil)
            guard request.url!.path.hasSuffix("threads") else { return (200, row, nil) }
            let second = request.url?.query?.contains("cursor=100") == true
            // Gmail confirms once page one of the armed read has answered.
            if armed.isSet, !second { confirmed.set() }
            let first = (wasConfirmed ? Array(fillers.prefix(99)) : [row] + fillers.prefix(99)).joined(separator: ",")
            return (200, "{\"items\":[\(second ? fillers[99] : first)],\"next_cursor\":\(second ? "null" : "\"100\""),"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: !wasConfirmed, inbox: 101)),"
                + "\"total_count\":\(wasConfirmed ? 100 : 101)}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.items.count == 100 && !model.isReadingList }
        await model.loadMore()
        #expect(model.items.count == 101)
        let row = try #require(model.items.first { $0.id == threadID })
        armed.set()
        // Archiving a row from an earlier page reads the list again: two pages for 101 rows.
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting && confirmed.isSet && !model.isReadingList }
        #expect(!model.items.contains { $0.id == threadID }, "Gmail confirmed the archive; page one's copy is older")
        // The archive has not been admitted, so nothing but the list read can have asked.
        #expect(requests.snapshot.contains { $0.httpMethod == "GET" && $0.url!.path.contains("/threads/") },
                "The list read asked for the conversation's own status, which is how it learnt of the confirmation")
        admission.release()
        await archiving.value
        #expect(!model.items.contains { $0.id == threadID })
    }

    /// A refused save reads the draft again. If the connection changes while that read is out,
    /// the old connection's error is not reported on the new one.
    @Test func testRefusedSaveReportsNothingOnceTheConnectionHasChanged() async throws {
        let requests = EmailRequestRecorder()
        let draftRead = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.httpMethod == "PUT" { return (409, Self.error("invalid_state", "Draft changed on another device."), nil) }
            if request.url!.path.contains("/drafts/") {
                // Only the read that follows the refused save is held.
                let refused = requests.snapshot.contains { $0.httpMethod == "PUT" }
                return (200, self.draftJSON(revision: 2, body: "Other device edit"), refused ? draftRead : nil)
            }
            return (200, self.threadJSON(draft: self.draftJSON()), nil)
        }
        defer { draftRead.release(); model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My unfinished edit")
        let saving = Task { await model.saveDraft() }
        try await waitForEmailTestCondition { draftRead.isWaiting }
        model.resetConnection()
        draftRead.release()
        _ = await saving.value
        #expect(model.draftError == nil)
        #expect(model.errorMessage == nil)
    }

    /// A send approval that fails reads the approval again. If the connection changes while
    /// that read is out, the old connection's error is not reported on the new one.
    @Test func testFailedApprovalReportsNothingOnceTheConnectionHasChanged() async throws {
        let requests = EmailRequestRecorder()
        let approvalRead = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.url!.path.contains("/approvals/") {
                if request.httpMethod == "POST" { return (500, Self.error("internal", "Try again."), nil) }
                let resolving = requests.snapshot.contains { $0.httpMethod == "POST" }
                return (200, self.approvalJSON(body: "Thanks, I will review the agenda."), resolving ? approvalRead : nil)
            }
            return (200, self.threadJSON(draft: self.draftJSON(status: "awaiting_approval")), nil)
        }
        defer { approvalRead.release(); model.resetConnection() }
        await model.openThread(threadID)
        await model.loadReview()
        #expect(model.review?.id == approvalID)
        let resolving = Task { await model.resolveSend(.approveOnce) }
        try await waitForEmailTestCondition { approvalRead.isWaiting }
        model.resetConnection()
        approvalRead.release()
        await resolving.value
        #expect(model.draftError == nil)
        #expect(model.errorMessage == nil)
    }

    /// A draft's attached file is read in two requests. If the connection changes after the
    /// first, the second is not sent and nothing is handed back. On an unchanged connection the
    /// same two requests return the file.
    @Test(arguments: [false, true])
    func testDraftAttachmentIsNotReadAcrossAConnectionChange(changing: Bool) async throws {
        let requests = EmailRequestRecorder()
        let metadata = EmailArchiveResponseGate()
        let artifactID = UUID(uuidString: "00000000-0000-0000-0000-000000000890")!
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("content") { return (200, "notes", nil) }
            return (200, "{\"id\":\"\(artifactID)\",\"session_id\":\"00000000-0000-0000-0000-00000000a001\","
                + "\"run_id\":null,\"name\":\"notes.md\",\"media_type\":\"text/markdown\",\"sha256\":\"abc\","
                + "\"size_bytes\":5,\"metadata\":{\"attachment\":{\"kind\":\"text\"}},"
                + "\"created_at\":\"2026-09-23T00:00:00Z\"}", metadata)
        }
        defer { metadata.release(); model.resetConnection() }
        let loading = Task { try? await model.loadDraftAttachment(artifactID).data }
        try await waitForEmailTestCondition { metadata.isWaiting }
        if changing { model.resetConnection() }
        metadata.release()
        #expect(await loading.value == (changing ? nil : Data("notes".utf8)),
                "The file is handed back on the connection that asked for it, and on no other")
        #expect(requests.snapshot.contains { $0.url!.path.hasSuffix("content") } == !changing,
                "The file itself is not asked for on a connection that has changed")
    }

    /// A hidden conversation whose status could not be read is shown again with its error and
    /// left in the counts. Once its status reads again it is hidden once more, and the counts
    /// are read again so that they answer for it.
    @Test func testHiddenConversationIsTakenOffAgainOnceItsStatusReadsAgain() async throws {
        let admission = EmailArchiveResponseGate()
        let failing = EmailTestFlag()
        let recovered = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            guard request.url!.path.hasSuffix("threads") else {
                return failing.isSet && !recovered.isSet ? (500, Self.error("internal", "Try again."), nil)
                    : (200, self.archiveThreadJSON(inInbox: true, status: "pending"), nil)
            }
            let listed = request.url?.query?.contains("view=handled") != true
            return (200, "{\"items\":[\(listed ? self.archiveThreadJSON(inInbox: true) : "")],\"next_cursor\":null,"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: true)),"
                + "\"total_count\":\(listed ? 7 : 0)}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        failing.set()
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil && !model.isReadingList }
        #expect(model.archiveErrors[threadID] != nil)
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 23, needsYou: 9, handled: 0, priority: 7, suspectedSpam: 2),
            "Its status could not be read, so it is left in the counts as the server sent them")
        recovered.set()
        admission.release()
        await archiving.value
        #expect(model.archiveErrors[threadID] == nil, "Admission answered and its status read again")
        try await waitForEmailTestCondition { model.collectionCounts == Self.archiveCountsWithoutIt() && !model.isReadingList }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
    }

    /// A read of more than a hundred rows takes several pages and keeps the last page's counts.
    /// A hidden row from an earlier page did not arrive with those counts, so it is read on its
    /// own after them like any conversation their page left out. Here its need for a reply ends
    /// between the two pages.
    @Test func testHiddenRowFromAnEarlierPageIsCheckedAgainstTheLastPagesCounts() async throws {
        let admission = EmailArchiveResponseGate()
        let armed = EmailTestFlag()
        let answered = EmailTestFlag()
        let fillers = (0..<100).map { index in
            archiveThreadJSON(inInbox: true).replacingOccurrences(
                of: "\(threadID)", with: String(format: "00000000-0000-0000-0000-%012d", 2000 + index))
        }
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            let wasAnswered = answered.isSet
            let row = self.archiveThreadJSON(inInbox: true)
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(!wasAnswered)")
            guard request.url!.path.hasSuffix("threads") else { return (200, row, nil) }
            let second = request.url?.query?.contains("cursor=100") == true
            // The reply need ends once page one of the armed read has answered.
            if armed.isSet, !second { answered.set() }
            let items = second ? fillers[99] : ([row] + fillers.prefix(99)).joined(separator: ",")
            return (200, "{\"items\":[\(items)],\"next_cursor\":\(second ? "null" : "\"100\""),"
                + "\"collection_counts\":{\"priority\":7,\"inbox\":101,\"needs_you\":\(wasAnswered ? 8 : 9),"
                + "\"handled\":0,\"suspected_spam\":2},\"total_count\":101}", nil)
        }
        defer { admission.release(); model.resetConnection() }
        model.setCollection(.inbox)
        try await waitForEmailTestCondition { model.items.count == 100 && !model.isReadingList }
        await model.loadMore()
        #expect(model.items.count == 101)
        let row = try #require(model.items.first { $0.id == threadID })
        armed.set()
        // Archiving a row from an earlier page reads the list again: two pages for 101 rows.
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting && answered.isSet && !model.isReadingList }
        #expect(model.items.count == 100)
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 100, needsYou: 8, handled: 0, priority: 6, suspectedSpam: 2),
            "Needs you is 8 on the server once the reply need ended; it is not 7")
        admission.release()
        await archiving.value
    }

    /// Switching collections straight after archiving puts a status read on the wire before the
    /// archive is admitted. Admission overtakes it, so it no longer decides the archive's
    /// outcome, but it still shows the conversation in the Inbox the counts describe.
    @Test func testStatusReadOvertakenByAdmissionStillPlacesTheConversationInTheCounts() async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let overtaken = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            guard request.url!.path.hasSuffix("threads") else {
                let first = Self.statusReads(requests) == 1
                return (200, self.archiveThreadJSON(inInbox: true, status: first ? nil : "pending"), first ? overtaken : nil)
            }
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
            let listed = request.url?.query?.contains("view=handled") != true
            return (200, "{\"items\":[\(listed ? row : "")],\"next_cursor\":null,"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: true)),"
                + "\"total_count\":\(listed ? 7 : 0)}", nil)
        }
        defer { admission.release(); overtaken.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { overtaken.isWaiting }
        admission.release()
        await archiving.value
        #expect(model.collectionCounts == nil, "Handled's counts wait for the conversation's status")
        overtaken.release()
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
    }

    /// Once the status read shows Gmail removed the conversation, the list read that follows
    /// no longer counts it: the adjustment ends with those counts, not a moment after them.
    @Test func testConfirmedArchiveEndsItsCountAdjustmentWithTheReadThatFollows() async throws {
        let requests = EmailRequestRecorder()
        let admission = EmailArchiveResponseGate()
        let accounts = EmailArchiveResponseGate()
        let confirmed = EmailTestFlag()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") {
                return (200, Self.archiveAccountsJSON, confirmed.isSet ? accounts : nil)
            }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), admission) }
            let row = self.archiveThreadJSON(inInbox: !confirmed.isSet, status: confirmed.isSet ? "completed" : nil)
            guard request.url!.path.hasSuffix("threads") else { return (200, row, nil) }
            let listed = !confirmed.isSet && request.url?.query?.contains("view=handled") != true
            return (200, "{\"items\":[\(listed ? row : "")],\"next_cursor\":null,"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: !confirmed.isSet)),"
                + "\"total_count\":\(listed ? 7 : 0)}", nil)
        }
        defer { admission.release(); accounts.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        confirmed.set()
        admission.release()
        // The list read after the confirmation has been applied; its account read is still out.
        try await waitForEmailTestCondition { accounts.isWaiting && !model.isReadingList }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt(),
                "The confirmed counts already leave the conversation out")
        accounts.release()
        await archiving.value
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
    }

    /// The adjustment belongs to the counts that include the conversation. Another account's
    /// counts are shown as read, and so is a search the conversation does not match; a search
    /// it does match, whatever the case, counts it like any other read.
    @Test func testPendingArchiveCountAdjustmentStaysWithinItsAccountAndSearch() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let workCounts = "{\"priority\":3,\"inbox\":10,\"needs_you\":2,\"handled\":0,\"suspected_spam\":0}"
        let matchCounts = "{\"priority\":1,\"inbox\":1,\"needs_you\":1,\"handled\":0,\"suspected_spam\":0}"
        let otherCounts = "{\"priority\":1,\"inbox\":2,\"needs_you\":1,\"handled\":0,\"suspected_spam\":0}"
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            // The conversation is personal mail whose summary reads "Review the board agenda."
            let query = request.url?.query ?? ""
            let scoped = query.contains("account_id=work") ? workCounts
                : query.contains("text=AGENDA") ? matchCounts : query.contains("text=") ? otherCounts : nil
            let listed = scoped == nil && query.contains("view=priority")
            return (200, "{\"items\":[\(listed ? row : "")],\"next_cursor\":null,"
                + "\"collection_counts\":\(scoped ?? Self.archiveCountsJSON(counted: true)),"
                + "\"total_count\":\(listed ? 7 : 0)}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        model.setAccount("work")
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 10, needsYou: 2, handled: 0, priority: 3, suspectedSpam: 0))
        model.setAccount(nil)
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        model.setSearchText("invoice")
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 2, needsYou: 1, handled: 0, priority: 1, suspectedSpam: 0))
        model.setSearchText("AGENDA")
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts == EmailCollectionCounts(
            inbox: 0, needsYou: 0, handled: 0, priority: 0, suspectedSpam: 0),
            "The search matches the hidden conversation, so the server counted it")
        statusRead.release()
        await archiving.value
    }

    /// The server's search rule, which decides whether searched counts hold a conversation the
    /// search's page did not return: the text, case-folded, within subject, senders and summary.
    @Test func testSearchMatchFollowsTheServersRule() throws {
        let row = try JSONDecoder.server.decode(EmailThreadView.self, from: Data(threadJSON().utf8))
        #expect(EmailViewModel.matchesSearch(row, text: ""))
        #expect(EmailViewModel.matchesSearch(row, text: "BOARD DISCUSSION"))
        #expect(EmailViewModel.matchesSearch(row, text: "discussion alex@example.test review"),
                "The three fields are searched as one text joined by spaces")
        #expect(!EmailViewModel.matchesSearch(row, text: "invoice"))
        #expect(!EmailViewModel.matchesSearch(row, text: "board  discussion"))
        let german = try JSONDecoder.server.decode(EmailThreadView.self, from: Data(
            threadJSON().replacingOccurrences(of: "Board discussion", with: "Baustelle Hauptstraße").utf8))
        #expect(EmailViewModel.matchesSearch(german, text: "HAUPTSTRASSE"), "Case folding, not lowercasing")
    }

    /// The adjustment does not wait on a status read happening to be in flight: a list read
    /// that cannot show the hidden conversation reads its status itself. Email is not in the
    /// foreground here, which leaves nothing following the archive once the first read returns.
    @Test func testListReadAsksAfterAHiddenArchiveNothingWasFollowing() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            let listed = request.url?.query?.contains("view=handled") != true
            return (200, "{\"items\":[\(listed ? row : "")],\"next_cursor\":null,"
                + "\"collection_counts\":\(Self.archiveCountsJSON(counted: true)),"
                + "\"total_count\":\(listed ? 7 : 0)}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        let archiving = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        statusRead.release()
        await archiving.value
        #expect(model.items.isEmpty)
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        #expect(Self.statusReads(requests) == 1)
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        #expect(Self.statusReads(requests) == 2)
    }

    /// Archived from the reader while the list shows a collection without its row, the
    /// conversation still leaves the counts: they are lowered by it as the reader last read it.
    @Test func testArchiveFromTheReaderLowersCountsWhileItsRowIsNotListed() async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let row = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
            guard request.url!.path.hasSuffix("threads") else {
                return (200, row, Self.statusReads(requests) == 1 ? statusRead : nil)
            }
            return (200, "{\"items\":[],\"next_cursor\":null,\"collection_counts\":"
                + "\(Self.archiveCountsJSON(counted: true)),\"total_count\":0}", nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        await model.openThread(threadID)
        let open = try #require(model.thread)
        #expect(model.collectionCounts?.inbox == 23)
        let archiving = Task { await model.setThreadArchived(open, archived: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
        statusRead.release()
        await archiving.value
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
    }

    /// An archive begun on another device can first be seen in the reader. The lists hide the
    /// conversation from that moment, so the counts leave it out from that moment too.
    @Test func testArchiveFirstSeenInTheReaderLowersTheCounts() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            guard request.url!.path.hasSuffix("threads") else {
                return (200, self.archiveThreadJSON(inInbox: true, status: "pending"))
            }
            return (200, "{\"items\":[],\"next_cursor\":null,\"collection_counts\":"
                + "\(Self.archiveCountsJSON(counted: true)),\"total_count\":0}")
        }
        defer { model.resetConnection() }
        model.setCollection(.handled)
        try await waitForEmailTestCondition { model.collectionCounts != nil }
        #expect(model.collectionCounts?.inbox == 23)
        await model.openThread(threadID)
        #expect(model.collectionCounts == Self.archiveCountsWithoutIt())
    }

    /// Teach Veetbot's unapplied input belongs to the open thread, not to the card that Reply
    /// hides: a refresh of that thread keeps it, and it ends when the selection does.
    @Test func testUnappliedFeedbackStaysWithTheOpenThread() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.feedbackDraft.isExpanded = true
        model.feedbackDraft.target = .person
        model.feedbackDraft.targetValue = "alex@example.test"
        model.feedbackDraft.explanation = "Board mail comes first"
        let entered = model.feedbackDraft
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.feedbackDraft == entered)
        model.clearSelection()
        #expect(model.feedbackDraft == EmailFeedbackDraft())
    }

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
        #expect(model.edits[draftID]?.body == "Preserve my unfinished reply")
        #expect(model.selectedThreadID == nil)
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

    /// A recovered pending row stays hidden while foreground polling resumes without a second mutation.
    @Test(arguments: [false, true])
    func testArchivePendingProjectionRecoversAcrossVisits(versioned: Bool) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON) }
            if request.url!.path.hasSuffix("refresh") { return (202, self.operationJSON(status: "RUNNING")) }
            if request.url!.path.contains("/operations/") { return (200, self.operationJSON(status: "RUNNING")) }
            let recovered = requests.snapshot.contains { $0.url!.path == "/v1/email/threads/\(self.threadID.uuidString)" }
            let thread = self.archiveThreadJSON(inInbox: !recovered, status: recovered ? "completed" : "pending")
            if request.url!.path.hasSuffix("threads") {
                let version = versioned ? "\"\(recovered ? "finished" : "pending")\"" : "null"
                return (200, "{\"items\":[\(recovered ? "" : thread)],\"next_cursor\":null,\"snapshot_version\":\(version)}")
            }
            return (200, thread)
        }
        defer { model.resetConnection() }
        await model.reload()
        let pending = try JSONDecoder.server.decode(EmailThreadView.self,
            from: Data(archiveThreadJSON(inInbox: true, status: "pending").utf8))
        #expect(!pending.isArchived)
        #expect(!model.canArchive(pending))
        #expect(model.items.isEmpty)
        #expect(model.archiveMessage(for: pending) == nil)
        model.setActive(true)
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.url!.path == "/v1/email/threads/\(threadID.uuidString)" }
        }
        try await waitForEmailTestCondition { model.items.isEmpty }
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
        #expect(model.items.count == (status == "pending" ? 0 : 1))
        #expect((model.archiveMessage(for: thread) == nil) == (status == "pending"))
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
        #expect(model.items.count == 1)
        let pending = try #require(model.items.first)
        await model.checkArchiveStatus(threadID)
        #expect(model.archiveErrors[threadID] == nil)
        #expect(model.archiveMessage(for: pending) == nil)
        #expect(model.items.isEmpty)
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
        #expect(model.thread == nil)
        // The list still projects the stale Inbox state; the unresolved request keeps the detail's newer state.
        await model.reload(preserveOrder: true)
        let row = try #require(model.items.first { $0.id == threadID })
        #expect(row.archiveOperation?.status == "pending")
        #expect(row.isArchived == true)
        #expect(row.inInbox == false)
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
        let admission = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.archiveAccountsJSON, nil) }
            if request.url!.path.hasSuffix("archive") {
                // The first admission's answer is held until the second gesture has been made.
                let first = requests.snapshot.filter { $0.url!.path.hasSuffix("archive") }.count == 1
                return (202, self.operationJSON(status: "RUNNING"), first ? admission : nil)
            }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("archive") }
            let thread = self.archiveThreadJSON(inInbox: true, status: requested ? "pending" : nil)
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(thread)],\"next_cursor\":null}", nil) }
            return (200, thread, nil)
        }
        defer { admission.release(); model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        let row = try #require(model.items.first)
        let first = Task { await model.setThreadArchived(row, archived: true) }
        try await waitForEmailTestCondition { admission.isWaiting }
        await model.setThreadArchived(row, archived: false)
        admission.release()
        await first.value
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
        #expect(model.items.isEmpty)
        #expect(model.selectedThreadID == nil)
        await model.openThread(threadID)
        #expect(model.thread?.archiveOperation?.targetArchived == true)
        #expect(model.thread?.inInbox == true)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
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

    /// Report spam is Archive's gesture with its own route: the row leaves at once and only a
    /// confirmed report keeps it away; failed, uncertain and unreadable outcomes restore it.
    @Test(arguments: ["completed", "failed", "uncertain", "unreadable"])
    func testReportSpamRestoresOnlyUnconfirmedOutcomes(outcome: String) async throws {
        let requests = EmailRequestRecorder()
        let statusRead = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.spamAccountsJSON, nil) }
            if request.url!.path.hasSuffix("/spam") { return (202, self.operationJSON(status: "QUEUED"), nil) }
            let requested = requests.snapshot.contains { $0.url!.path.hasSuffix("/spam") }
            let reported = requested && outcome == "completed"
            let row = self.spamThreadJSON(inInbox: !reported, inSpam: reported, suspected: true,
                status: requested ? outcome : nil)
            if request.url!.path.hasSuffix("threads") {
                return (200, "{\"items\":[\(reported ? "" : row)],\"next_cursor\":null}", nil)
            }
            if requested && outcome == "unreadable" {
                return (400, Self.error("malformed_request", "Could not check Gmail status."), statusRead)
            }
            return (200, row, requested ? statusRead : nil)
        }
        defer { statusRead.release(); model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        #expect(row.isSuspectedSpam)
        #expect(model.spamSupported(for: row.accountID))
        #expect(model.canReportSpam(row))
        let reporting = Task { await model.setThreadSpam(row, spam: true) }
        try await waitForEmailTestCondition { statusRead.isWaiting }
        #expect(model.items.isEmpty)
        statusRead.release()
        await reporting.value
        #expect(model.items.count == (outcome == "completed" ? 0 : 1))
        #expect((model.items.first.flatMap(model.archiveMessage(for:)) == nil) == (outcome == "completed"))
        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(posts.map { $0.url!.path } == ["/v1/email/threads/\(threadID.uuidString)/spam"])
        let body = try Self.archiveRequestBody(try #require(posts.first))
        #expect(body["spam"] as? Bool == true)
        #expect(body["expected_revision"] as? Int == 1)
        #expect((body["idempotency_key"] as? String)?.isEmpty == false)
        #expect(posts.first?.value(forHTTPHeaderField: "Idempotency-Key") == body["idempotency_key"] as? String)
    }

    /// Spam actions need the thread's own account to advertise them, and a missing route never becomes Archive.
    @Test func testSpamActionsNeedTheThreadsOwnAccountSupport() async throws {
        let requests = EmailRequestRecorder()
        var supported = false
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") {
                return (200, supported ? Self.spamAccountsJSON : Self.archiveAccountsJSON)
            }
            if request.httpMethod == "POST" { return (404, Self.error("not_found", "Not found")) }
            let row = self.spamThreadJSON(inInbox: true, inSpam: false, suspected: true)
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(row)],\"next_cursor\":null}") }
            return (200, row)
        }
        defer { model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        #expect(!model.spamSupported(for: row.accountID))
        #expect(!model.canReportSpam(row))
        await model.setThreadSpam(row, spam: true)
        await model.clearSpamFlag(row)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.isEmpty)
        #expect(model.archiveErrors[row.id] != nil)
        #expect(model.spamFlagErrors[row.id] != nil)
        supported = true
        model.resetConnection()
        await model.reload()
        let current = try #require(model.items.first)
        #expect(model.spamSupported(for: current.accountID))
        await model.setThreadSpam(current, spam: true)
        #expect(!model.spamSupported(for: current.accountID), "A missing route marks the account unsupported")
        #expect(model.items.map(\.id) == [current.id], "The row returns with an actionable error")
        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }.map { $0.url!.path }
        #expect(posts == ["/v1/email/threads/\(threadID.uuidString)/spam"], "Never falls back to Archive")
    }

    /// Not spam on a flagged conversation clears the flag and writes nothing to Gmail.
    @Test func testNotSpamClearsTheFlagWithoutAMailboxWrite() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.spamAccountsJSON) }
            let cleared = requests.snapshot.contains { $0.url!.path.hasSuffix("spam-flag") }
            let row = self.spamThreadJSON(inInbox: true, inSpam: false, suspected: !cleared, cleared: cleared)
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(row)],\"next_cursor\":null}") }
            return (200, row)
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        let row = try #require(model.items.first)
        #expect(row.isSuspectedSpam)
        await model.clearSpamFlag(row)
        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(posts.map { $0.url!.path } == ["/v1/email/threads/\(threadID.uuidString)/spam-flag"])
        #expect(try Self.archiveRequestBody(try #require(posts.first)) as NSDictionary == ["cleared": true])
        #expect(model.items.first?.isSuspectedSpam == false)
        #expect(model.items.first?.spamCleared == true)
        #expect(model.thread?.isSuspectedSpam == false)
        #expect(model.spamFlagErrors[row.id] == nil)
    }

    /// A conversation in Spam returns through Not spam, never through Move to Inbox.
    @Test func testReportedThreadRestoresThroughNotSpam() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.spamAccountsJSON) }
            if request.url!.path.hasSuffix("/spam") { return (202, self.operationJSON(status: "QUEUED")) }
            let restored = requests.snapshot.contains { $0.url!.path.hasSuffix("/spam") }
            let row = self.spamThreadJSON(inInbox: restored, inSpam: !restored, suspected: false,
                status: restored ? "completed" : nil, targetArchived: false)
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(row)],\"next_cursor\":null}") }
            return (200, row)
        }
        defer { model.resetConnection() }
        await model.reload()
        let row = try #require(model.items.first)
        #expect(row.isInSpam && !row.isSuspectedSpam)
        #expect(!model.canArchive(row))
        #expect(!model.canReportSpam(row, spam: true))
        #expect(model.canReportSpam(row, spam: false))
        await model.setThreadSpam(row, spam: false)
        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(posts.map { $0.url!.path } == ["/v1/email/threads/\(threadID.uuidString)/spam"])
        #expect(try Self.archiveRequestBody(try #require(posts.first))["spam"] as? Bool == false)
        #expect(model.items.first?.isInSpam == false)
        #expect(model.items.first?.inInbox == true)
    }

    /// Advertises Report spam beside Archive on both accounts.
    private static var spamAccountsJSON: String {
        archiveAccountsJSON.replacingOccurrences(of: "\"archive_supported\":true",
            with: "\"archive_supported\":true,\"spam_supported\":true")
    }

    /// Supplies the spam projection and a spam operation through the real JSON decoder.
    private func spamThreadJSON(inInbox: Bool, inSpam: Bool, suspected: Bool, cleared: Bool = false,
        status: String? = nil, targetArchived: Bool = true) -> String {
        let operation = status.map { "{\"operation_id\":\"\(threadID)\",\"run_id\":\"\(runID)\",\"target_archived\":\(targetArchived),\"target_spam\":true,\"status\":\"\($0)\",\"error\":null}" } ?? "null"
        return threadJSON().replacingOccurrences(of: "\"account_id\":\"personal\"",
            with: "\"account_id\":\"personal\",\"in_inbox\":\(inInbox),\"in_spam\":\(inSpam),\"suspected_spam\":\(suspected),\"spam_cleared\":\(cleared),\"archive_operation\":\(operation)")
    }

    /// Builds an isolated transport whose selected response can be delivered after an independent request finishes.
    private func makeArchiveRaceModel(
        refreshNanoseconds: UInt64 = 60_000_000_000,
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
        return EmailViewModel(makeAPIClient: { api }, refreshNanoseconds: refreshNanoseconds)
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

    /// Excluding a thread drops its parsed conversation with the selection, so
    /// no copy of the excluded mail stays in the client (ADR-0301).
    @Test func testExcludingAThreadDropsItsCachedConversation() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("exclude") { return (200, "{}") }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            return (200, self.threadJSON())
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        let thread = try #require(model.thread)
        _ = try await EmailConversationCache.shared.conversation(for: thread, ownerAddress: nil)
        #expect(EmailConversationCache.shared.parses(of: threadID) == 1)
        await model.excludeSelectedThread()
        #expect(model.thread == nil)
        #expect(EmailConversationCache.shared.parses(of: threadID) == 0)
    }

    /// An accepted exclusion removes the thread's row even when the list
    /// reload after it fails, so its summary never stays in view.
    @Test func testExcludedThreadLeavesTheListWhenTheReloadFails() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("exclude") { return (200, "{}") }
            let excluded = requests.snapshot.contains { $0.url!.path.hasSuffix("exclude") }
            if request.url!.path.hasSuffix("threads") {
                return excluded ? (503, Self.error("service_unavailable", "Try again.")) : (200, self.pageJSON())
            }
            return (200, self.threadJSON())
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        #expect(model.items.contains { $0.id == threadID })
        await model.excludeSelectedThread()
        let afterExclusion = requests.snapshot.drop { !$0.url!.path.hasSuffix("exclude") }.dropFirst()
        #expect(afterExclusion.contains { $0.url!.path.hasSuffix("threads") }, "The reload ran and failed")
        #expect(model.items.contains { $0.id == threadID } == false)
    }

    /// Counts come from the whole server collection, never the visible first page.
    @Test func testCollectionCountsAndSwitchPreserveUnsentDraft() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") {
                return (200, self.pageJSON().replacingOccurrences(of: "\"next_cursor\":null",
                    with: "\"next_cursor\":\"5\",\"collection_counts\":{\"inbox\":23,\"needs_you\":9,\"handled\":4}"))
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.reload()
        #expect(model.collectionCounts?.count(for: .inbox) == 23)
        #expect(model.collectionCounts?.count(for: .needsYou) == 9)
        #expect(model.items.count == 1)
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Keep my words")
        model.setCollection(.handled)
        for _ in 0..<100 {
            if requests.snapshot.contains(where: { $0.url?.query?.contains("view=handled") == true }) { break }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
        #expect(requests.snapshot.contains { $0.url?.query?.contains("view=handled") == true })
        #expect(model.collection == .handled)
        #expect(model.currentEdit?.body == "Keep my words")
        model.resetConnection()
        #expect(model.collectionCounts == nil)
    }

    /// A same-query refresh retains confirmed totals while waiting and after a transient failure.
    @Test(arguments: [false, true])
    func testCollectionCountsDuringFailedReload(preserveOrder: Bool) async throws {
        let requests = EmailRequestRecorder()
        let gate = DispatchSemaphore(value: 0)
        defer { gate.signal() }
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let reads = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
            if reads > 1 {
                _ = gate.wait(timeout: .now() + 5)
                return (502, "Temporary failure")
            }
            return (200, self.pageJSON().replacingOccurrences(of: "\"next_cursor\":null",
                with: "\"next_cursor\":null,\"collection_counts\":{\"inbox\":23,\"needs_you\":9,\"handled\":4}"))
        }
        await model.reload()
        #expect(model.collectionCounts?.count(for: .inbox) == 23)
        let refresh = Task { await model.reload(preserveOrder: preserveOrder) }
        try await waitForEmailTestCondition {
            requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count == 2
        }
        #expect(model.collectionCounts?.count(for: .inbox) == (preserveOrder ? 23 : nil))
        gate.signal()
        await refresh.value
        #expect(model.collectionCounts?.count(for: .inbox) == (preserveOrder ? 23 : nil))
    }

    /// Search stays in the selected collection and old servers never imply zero counts.
    @Test(arguments: ["needs_you", "priority", "suspected_spam"])
    func testCollectionSearchAndUnknownCounts(view: String) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            return (200, self.pageJSON())
        }
        model.setListView(view)
        model.setSearchText("board")
        await model.reload()
        // Switching the collection also schedules a reload. An explicit reload can
        // be superseded, so synchronize on the transport boundary we assert below.
        try await waitForEmailTestCondition {
            requests.snapshot.contains {
                $0.url?.query?.contains("view=\(view)") == true && $0.url?.query?.contains("text=board") == true
            }
        }
        #expect(requests.snapshot.contains {
            $0.url?.query?.contains("view=\(view)") == true && $0.url?.query?.contains("text=board") == true
        })
        #expect(model.collectionCounts == nil)
        model.resetConnection()
        #expect(model.listView == "priority")
        #expect(model.searchText.isEmpty)
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

    /// Handled confirms with a toast whose Undo sends the reversible command back.
    @Test func testHandledConfirmsWithAToastWhoseUndoReopensTheThread() async throws {
        let requests = EmailRequestRecorder()
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.reload()
        let row = try #require(model.items.first)

        await model.setThreadHandled(row, handled: true)
        #expect(toasts.current?.text == "Marked handled.")
        #expect(toasts.current?.undo?.label == "Undo")

        await toasts.performUndo()
        let commands = requests.snapshot.filter { $0.url!.path.hasSuffix("dismiss") }
        #expect(commands.count == 2)
        let body = try Self.archiveRequestBody(try #require(commands.last))
        #expect(body["dismissed"] as? Bool == false)
        #expect(toasts.current?.text == "Marked not handled.")
    }

    /// A rejected inbox checkbox leaves the row unchanged and exposes a recoverable
    /// inline error; it raises no toast.
    @Test func testFailedHandledActionKeepsThreadAndReportsError() async throws {
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
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
        #expect(toasts.current == nil)
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

    /// A stale save response explains why Review & Send cannot create a proposal.
    @Test func testReviewSendExplainsDraftBecomingStaleDuringSave() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" {
                return (200, self.draftJSON(revision: 2, body: "Changed body").replacingOccurrences(of: "\"stale\":false", with: "\"stale\":true"))
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Changed body")
        await model.prepareSend()
        #expect(model.draft?.stale == true)
        #expect(model.draftActionMessage == "This draft is out of date. Refresh the thread and review the draft again.")
        #expect(!requests.snapshot.contains { $0.url!.path.hasSuffix("send-proposal") })
    }

    @Test(arguments: ["none", "ready", "discarded", "sent", "sent_manual"])
    func testReplySelectsBlankManualCompositionWithoutGeneration(status: String) async throws {
        let requests = EmailRequestRecorder()
        let blank = draftJSON(body: "")
            .replacingOccurrences(of: "\"revision\":1", with: "\"composition\":\"manual\",\"revision\":2")
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("/reply") { return (200, blank) }
            if request.url!.path.hasSuffix("/revisions") { return (200, "{\"items\":[],\"next_cursor\":null}") }
            let existing = status == "sent_manual"
                ? self.draftJSON(status: "sent").replacingOccurrences(of: "\"revision\":1", with: "\"composition\":\"manual\",\"revision\":1")
                : self.draftJSON(status: status)
            let detail = self.threadJSON(draft: status == "none" ? "null" : existing)
            return (200, status != "none" ? detail : detail.replacingOccurrences(
                of: "\"draft_id\":\"\(self.draftID)\"", with: "\"draft_id\":null"))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        #expect(await model.selectReply(manual: true))
        #expect(model.draft?.isManual == true)
        #expect(model.currentEdit?.body == "")
        #expect(!model.canReview, "A blank reply is editable but cannot be sent")
        let posts = requests.snapshot.filter { $0.httpMethod == "POST" }
        #expect(posts.count == 1)
        #expect(posts.first?.url?.path.hasSuffix("/reply") == true)
        #expect(!model.isSelectingReply)
    }

    @Test func testReplySelectionWaitsForGeneration() async throws {
        let gate = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.httpMethod == "POST" {
                return (200, "{\"draft\":\(self.draftJSON()),\"run_id\":\"\(self.runID)\",\"status\":\"COMPLETED\"}", gate)
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "COMPLETED"), nil) }
            return (200, self.threadJSON(draft: self.draftJSON()), nil)
        }
        defer { gate.release(); model.resetConnection() }
        await model.openThread(threadID)
        let generating = Task { await model.generateDraft() }
        try await waitForEmailTestCondition { gate.isWaiting }
        #expect(model.isGeneratingDraft)
        #expect(!model.canSelectReply)
        gate.release()
        await generating.value
    }

    @Test func testOnlyExplicitVeetbotChoiceGeneratesAMissingSuggestion() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("/reply") { return (404, Self.error("not_found", "No Veetbot draft")) }
            if request.httpMethod == "POST" {
                return (200, "{\"draft\":\(self.draftJSON()),\"run_id\":\"\(self.runID)\",\"status\":\"COMPLETED\"}")
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "COMPLETED")) }
            let generated = requests.snapshot.contains { $0.httpMethod == "POST" && $0.url!.path.hasSuffix("/drafts") }
            let detail = self.threadJSON(draft: generated ? self.draftJSON() : "null")
            return (200, generated ? detail : detail.replacingOccurrences(
                of: "\"draft_id\":\"\(self.draftID)\"", with: "\"draft_id\":null"))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        #expect(await model.selectReply(manual: false))
        #expect(model.draft?.isManual == false)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.map { $0.url!.lastPathComponent } == ["reply", "drafts"])
        #expect(!model.isGeneratingDraft && !model.isSelectingReply)
    }

    @Test func testManualReplyDoesNotDisplayVeetbotPreparationFailure() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let manual = self.draftJSON().replacingOccurrences(of: "\"revision\":1", with: "\"composition\":\"manual\",\"revision\":1")
            return (200, self.threadJSON(draft: manual).replacingOccurrences(of: "\"summary\":", with: "\"preparation_issue\":\"Missing files\",\"summary\":"))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        #expect(model.thread?.preparationIssue == "Missing files")
        #expect(model.draftActionMessage == nil)
    }

    @Test func testManualReplyFailureNeverFallsBackToGeneration() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "POST" { return (404, Self.error("not_found", "Reply unavailable")) }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        #expect(!(await model.selectReply(manual: true)))
        #expect(model.currentEdit?.body == "Thanks, I will review the agenda.")
        #expect(model.draftActionMessage == "Reply unavailable")
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
    }

    /// A terminal generation failure must be visible even before the first draft exists.
    @Test(arguments: ["FAILED", "CANCELLED"], [false, true])
    func testDraftGenerationReportsTerminalFailure(status: String, existingDraft: Bool) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "POST" {
                return (202, "{\"run_id\":\"\(self.runID)\",\"draft\":null,\"status\":\"QUEUED\"}")
            }
            if request.url!.path.contains("/runs/") {
                return (200, """
                {"id":"\(self.runID)","session_id":"\(self.threadID)","status":"\(status)","step_count":1,"model_call_count":1,"tool_call_count":0,"usage":{"input_tokens":1,"output_tokens":1,"cost_usd":"0"},"limits":{"max_steps":8},"created_at":"2026-09-11T00:00:00Z","updated_at":"2026-09-11T00:00:00Z"}
                """)
            }
            let thread = self.threadJSON(draft: existingDraft ? self.draftJSON() : "null")
            return (200, existingDraft ? thread : thread.replacingOccurrences(
                of: "\"draft_id\":\"\(self.draftID)\"", with: "\"draft_id\":null"))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.generateDraft()
        #expect(model.draftActionMessage == (status == "FAILED"
            ? "The email operation failed. Try again."
            : "The email operation was cancelled."))
        #expect(model.threadReadMessage == nil)
        #expect(!model.isPerformingAction)
        #expect((model.draft != nil) == existingDraft)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
    }

    /// A failed owner action is reported separately from a failed thread read, so the
    /// composer can show a refusal beside the button that caused it instead of only at
    /// the top of the reading pane, far above the action.
    @Test func testFailedOwnerActionIsReportedApartFromThreadReadFailures() async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("send-proposal") {
                return (400, Self.error("invalid_argument", "Review could not be prepared."))
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.prepareSend()

        #expect(model.draftActionMessage == "Review could not be prepared.")
        #expect(model.threadReadMessage == nil)
        #expect(model.draftError == "Review could not be prepared.")
    }

    /// Typing while Review & Send's own save is in flight leaves the edit dirty even
    /// though that save succeeded. The proposal must still reach the server carrying
    /// the latest text, rather than the click doing nothing at all.
    @Test func testReviewSendProposesAfterAnEditArrivesDuringItsOwnSave() async throws {
        let requests = EmailRequestRecorder()
        let saveGate = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.httpMethod == "PUT" {
                let saves = requests.snapshot.filter { $0.httpMethod == "PUT" }.count
                // Hold only the first save open, so a retry can settle the later edit.
                return (200, self.draftJSON(revision: saves + 1), saves == 1 ? saveGate : nil)
            }
            if request.url!.path.hasSuffix("send-proposal") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"draft\":\(self.draftJSON(revision: 3, status: "awaiting_approval"))}", nil)
            }
            if request.url!.path.contains("/runs/") {
                return (200, "{\"id\":\"\(self.runID)\",\"status\":\"WAITING_FOR_APPROVAL\"}", nil)
            }
            return (200, self.threadJSON(draft: self.draftJSON()), nil)
        }
        defer { saveGate.release(); model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "First edit")
        let sending = Task { await model.prepareSend() }
        try await waitForEmailTestCondition { saveGate.isWaiting }
        model.changeEdit(\.body, to: "Typed while the save was still in flight")
        saveGate.release()
        await sending.value

        #expect(requests.snapshot.contains { $0.url!.path.hasSuffix("send-proposal") })
        #expect(model.currentEdit?.isDirty == false)
    }

    /// An autosave in flight must not grey out Review & Send: the proposal waits for
    /// that save and then carries the settled revision, with no second save.
    @Test func testReviewSendStaysAvailableWhileAnAutosaveIsInFlight() async throws {
        let requests = EmailRequestRecorder()
        let saveGate = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.httpMethod == "PUT" {
                let saves = requests.snapshot.filter { $0.httpMethod == "PUT" }.count
                return (200, self.draftJSON(revision: saves + 1, body: "Edited body"), saves == 1 ? saveGate : nil)
            }
            if request.url!.path.hasSuffix("send-proposal") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.draftJSON(revision: 2, body: "Edited body", status: "awaiting_approval"))}", nil)
            }
            if request.url!.path.contains("/runs/") {
                return (200, "{\"id\":\"\(self.runID)\",\"status\":\"WAITING_FOR_APPROVAL\"}", nil)
            }
            return (200, self.threadJSON(draft: self.draftJSON()), nil)
        }
        defer { saveGate.release(); model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited body")
        let saving = Task { await model.saveDraft() }
        try await waitForEmailTestCondition { saveGate.isWaiting }
        #expect(model.isSaving)
        #expect(model.canReview, "a save in flight must not disable Review & Send")
        let sending = Task { await model.prepareSend() }
        saveGate.release()
        _ = await saving.value
        await sending.value

        #expect(requests.snapshot.filter { $0.url!.path.hasSuffix("send-proposal") }.count == 1)
        #expect(requests.snapshot.filter { $0.httpMethod == "PUT" }.count == 1)
        #expect(model.currentEdit?.isDirty == false)
    }

    /// A send the server ends because new mail arrived must say so beside the draft,
    /// not just close the review and leave the draft looking untouched.
    @Test func testSendEndedByNewMailIsReportedBesideTheDraft() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("send-proposal") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.draftJSON(status: "awaiting_approval"))}")
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "COMPLETED")) }
            let proposed = requests.snapshot.contains { $0.url!.path.hasSuffix("send-proposal") }
            return (200, proposed
                ? self.newMailThreadJSON(draft: self.staleDraftJSON())
                : self.threadJSON(draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.prepareSend()

        #expect(model.draft?.stale == true)
        #expect(model.review == nil)
        #expect(model.draftActionMessage == EmailViewModel.sendEndedByNewMailMessage)
    }

    /// The new-mail notice names who wrote and quotes them, so the owner can decide
    /// without hunting through the thread for what changed.
    @Test func testStaleDraftNoticeNamesTheNewMail() async throws {
        let now = ISO8601DateFormatter().date(from: "2026-09-11T00:10:00Z")!
        let model = try makeModel(now: { now }) { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let messages = [
                self.messageJSON(id: "m1", sender: "Alex Chen <alex@example.test>",
                                 body: "Here is the agenda.", sentAt: "2026-09-10T23:00:00Z"),
                self.messageJSON(id: "m2", sender: "Dana Lee <dana@example.test>",
                                 body: "Thursday works better for me, can we move it?", sentAt: "2026-09-11T00:07:00Z"),
            ].joined(separator: ",")
            return (200, self.newMailThreadJSON(draft: self.staleDraftJSON())
                .replacingOccurrences(of: "\"messages\":[]", with: "\"messages\":[\(messages)]"))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)

        #expect(model.newMailSinceDraft?.id == "m2")
        #expect(model.staleMailMessage(locale: Locale(identifier: "en_US")) ==
            "Dana Lee wrote 3 minutes ago: “Thursday works better for me, can we move it?” Read it above, then confirm your reply still fits.")
    }

    /// When the send proposal outlasts the local wait, the owner sees that it is still
    /// being prepared, and the review opens from a later refresh without another press.
    @Test func testLongSendPreparationIsVisibleAndReviewOpensWhenReady() async throws {
        let ready = EmailTestFlag()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/approvals/") {
                return (200, self.approvalJSON(body: "Thanks, I will review the agenda."))
            }
            // Admission marks the draft awaiting approval; the approval itself comes later.
            let queued = self.draftJSON(status: "awaiting_approval").replacingOccurrences(
                of: "\"approval_id\":\"\(self.approvalID)\"", with: "\"approval_id\":null")
            if request.url!.path.hasSuffix("send-proposal") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(queued)}")
            }
            if request.url!.path.contains("/runs/") {
                return (200, self.runJSON(status: ready.isSet ? "WAITING_FOR_APPROVAL" : "QUEUED"))
            }
            return (200, self.threadJSON(draft: ready.isSet ? self.draftJSON(status: "awaiting_approval") : queued))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.prepareSend()

        #expect(model.review == nil)
        #expect(model.draftActionMessage == nil)
        #expect(model.sendProgressMessage == EmailViewModel.stillPreparingSendMessage)
        ready.set()
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.review?.id == approvalID)
        #expect(model.sendProgressMessage == nil)
    }

    /// One press on Send consents to the exact revision at the thread revision on screen:
    /// no proposal, no sheet, and an Undo window the draft carries (ADR-0279).
    @Test func testSendIsOnePressWithAnUndoWindow() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(pollNanoseconds: 1_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "QUEUED")) }
            let sent = requests.snapshot.contains { $0.url!.path.hasSuffix("/send") }
            return (200, self.threadJSON(draft: sent ? self.parkedDraftJSON() : self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        await model.sendNow()

        let send = try #require(requests.snapshot.first { $0.url!.path.hasSuffix("/send") })
        let body = try Self.archiveRequestBody(send)
        #expect(body["expected_revision"] as? Double == 2)
        #expect(body["source_revision"] as? Double == 1)
        #expect((body["idempotency_key"] as? String)?.isEmpty == false)
        #expect(!requests.snapshot.contains { $0.url!.path.hasSuffix("send-proposal") })
        #expect(model.review == nil)
        #expect(model.draft?.status == "sending")
        #expect(model.canUndoSend)
        #expect(model.draftActionMessage == nil)
    }

    /// Undo inside the window takes the send back and returns the editor with the text intact.
    @Test func testUndoReturnsTheParkedSendToEditing() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(pollNanoseconds: 1_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.hasSuffix("/send-undo") {
                return (200, self.draftJSON(revision: 2, body: "Edited before sending").replacingOccurrences(
                    of: "\"approval_id\":\"\(self.approvalID)\"", with: "\"approval_id\":null"))
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "QUEUED")) }
            let undone = requests.snapshot.contains { $0.url!.path.hasSuffix("/send-undo") }
            let sent = requests.snapshot.contains { $0.url!.path.hasSuffix("/send") }
            return (200, self.threadJSON(draft: undone ? self.draftJSON(revision: 2, body: "Edited before sending") : sent ? self.parkedDraftJSON() : self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        await model.sendNow()
        #expect(model.canUndoSend)
        await model.undoSend()

        let undo = try #require(requests.snapshot.first { $0.url!.path.hasSuffix("/send-undo") })
        #expect(try Self.archiveRequestBody(undo)["expected_revision"] as? Double == 2)
        #expect(try Self.archiveRequestBody(undo)["run_id"] as? String == runID.uuidString, "Undo names the send it takes back")
        #expect(model.draft?.status == "ready")
        #expect(model.draft?.canEdit == true)
        #expect(!model.canUndoSend)
        #expect(model.currentEdit?.body == "Edited before sending")
        #expect(model.draftActionMessage == nil)
    }

    /// The revision survives an Undo, so a second press must carry a new idempotency key;
    /// replaying the cancelled send's key would be refused as already done.
    @Test func testSendAfterUndoUsesANewIdempotencyKey() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(pollNanoseconds: 1_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.hasSuffix("/send-undo") {
                return (200, self.draftJSON(revision: 2, body: "Edited before sending").replacingOccurrences(
                    of: "\"approval_id\":\"\(self.approvalID)\"", with: "\"approval_id\":null"))
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "QUEUED")) }
            let undone = requests.snapshot.contains { $0.url!.path.hasSuffix("/send-undo") }
            let sent = requests.snapshot.contains { $0.url!.path.hasSuffix("/send") }
            return (200, self.threadJSON(draft: undone
                ? self.draftJSON(revision: 2, body: "Edited before sending")
                : sent ? self.parkedDraftJSON() : self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        await model.sendNow()
        #expect(model.canUndoSend)
        await model.undoSend()
        #expect(model.draft?.status == "ready")
        await model.sendNow()

        let sends = requests.snapshot.filter { $0.url!.path.hasSuffix("/send") }
        #expect(sends.count == 2)
        let keys = try sends.map { try #require(Self.archiveRequestBody($0)["idempotency_key"] as? String) }
        #expect(keys.count == 2 && keys[0] != keys[1])
    }

    /// A send that failed must not keep its idempotency key: "try again" is a new
    /// admission, not a replay of the dead operation.
    @Test func testSendAfterAFailedSendUsesANewIdempotencyKey() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(pollNanoseconds: 1_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "FAILED")) }
            let sent = requests.snapshot.contains { $0.url!.path.hasSuffix("/send") }
            return (200, self.threadJSON(draft: sent
                ? self.draftJSON(revision: 2, body: "Edited before sending", status: "failed")
                : self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        await model.sendNow()
        #expect(model.draft?.status == "failed")
        await model.sendNow()

        let sends = requests.snapshot.filter { $0.url!.path.hasSuffix("/send") }
        #expect(sends.count == 2)
        let keys = try sends.map { try #require(Self.archiveRequestBody($0)["idempotency_key"] as? String) }
        #expect(keys.count == 2 && keys[0] != keys[1])
    }

    /// A known failure clears the key even when the thread refresh that would show the
    /// failed draft does not land; the next press after a later refresh is a new request.
    @Test func testKnownFailureClearsTheSendKeyWithoutADraftRefresh() async throws {
        let requests = EmailRequestRecorder()
        let refreshWorks = EmailTestFlag()
        let model = try makeModel(pollNanoseconds: 1_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "FAILED")) }
            let sent = requests.snapshot.contains { $0.url!.path.hasSuffix("/send") }
            guard sent else { return (200, self.threadJSON(draft: self.draftJSON())) }
            // The refresh that would show the failed draft is down until the test allows it.
            guard refreshWorks.isSet else { return (503, Self.error("unavailable", "Mail is briefly unavailable.")) }
            return (200, self.threadJSON(draft: self.draftJSON(revision: 2, body: "Edited before sending", status: "failed")))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        await model.sendNow()
        #expect(model.draft?.status == "sending", "the refresh never landed, so the draft still looks parked")
        refreshWorks.set()
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.draft?.status == "failed")
        await model.sendNow()

        let sends = requests.snapshot.filter { $0.url!.path.hasSuffix("/send") }
        #expect(sends.count == 2)
        let keys = try sends.map { try #require(Self.archiveRequestBody($0)["idempotency_key"] as? String) }
        #expect(keys.count == 2 && keys[0] != keys[1])
    }

    /// A watcher left over from an earlier send must not clear the key of, or report
    /// its failure against, a newer send the owner started after a refresh showed the
    /// earlier one had failed.
    @Test func testAnEarlierSendsWatcherLeavesANewerSendAlone() async throws {
        let requests = EmailRequestRecorder()
        let secondRunID = UUID(uuidString: "00000000-0000-0000-0000-000000000903")!
        let firstFailed = EmailTestFlag()
        let model = try makeModel(pollNanoseconds: 100_000_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            let sends = requests.snapshot.filter { $0.url!.path.hasSuffix("/send") }.count
            if request.url!.path.hasSuffix("/send") {
                let run = sends == 1 ? self.runID : secondRunID
                return (200, "{\"run_id\":\"\(run)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON(runID: run))}")
            }
            if request.url!.path.contains("/runs/\(self.runID)") {
                return (200, self.runJSON(status: firstFailed.isSet ? "FAILED" : "QUEUED"))
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "QUEUED", id: secondRunID)) }
            // After the first send, the thread shows it failed; after the second, the new parked send.
            if sends >= 2 { return (200, self.threadJSON(draft: self.parkedDraftJSON(runID: secondRunID))) }
            if sends == 1 {
                return (200, self.threadJSON(draft: self.draftJSON(revision: 2, body: "Edited before sending", status: "failed")))
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        // The first send's watcher keeps polling (run queued) while the thread already shows it failed.
        let firstSend = Task { await model.sendNow() }
        try await waitForEmailTestCondition { model.draft?.status == "failed" }
        let secondSend = Task { await model.sendNow() }
        try await waitForEmailTestCondition { model.draft?.runID == secondRunID }
        #expect(model.canUndoSend)
        // Now the first run reports its failure to the leftover watcher.
        firstFailed.set()
        await firstSend.value
        secondSend.cancel()

        #expect(model.draft?.runID == secondRunID)
        #expect(model.draftActionMessage == nil, "the earlier send's failure is not the newer send's")
        #expect(model.canUndoSend)
    }

    /// A press whose response was lost retries with the same key, so the server can
    /// replay the admission instead of sending twice.
    @Test func testALostSendResponseRetriesWithTheSameKey() async throws {
        let requests = EmailRequestRecorder()
        let admitted = EmailTestFlag()
        let model = try makeModel(pollNanoseconds: 1_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                // The first press never gets its answer, through the transport's own
                // retry as well; the next press carries the same key and is admitted.
                let sends = requests.snapshot.filter { $0.url!.path.hasSuffix("/send") }.count
                if sends <= 2 { throw URLError(.networkConnectionLost) }
                admitted.set()
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "QUEUED")) }
            return (200, self.threadJSON(draft: admitted.isSet
                ? self.parkedDraftJSON() : self.draftJSON(revision: 2, body: "Edited before sending")))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        await model.sendNow()
        #expect(model.draftActionMessage?.isEmpty == false)
        await model.sendNow()

        let sends = requests.snapshot.filter { $0.url!.path.hasSuffix("/send") }
        let keys = try sends.map { try #require(Self.archiveRequestBody($0)["idempotency_key"] as? String) }
        #expect(Set(keys).count == 1, "every attempt of the same press carries one key")
        #expect(model.draft?.status == "sending")
    }

    /// Undo that succeeded on the server but whose response was lost (or an Undo from
    /// another device) leaves a restored draft; the next press is a new request.
    @Test func testSendAfterALostUndoResponseUsesANewIdempotencyKey() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(pollNanoseconds: 1_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.hasSuffix("/send-undo") { throw URLError(.networkConnectionLost) }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "CANCELLED")) }
            let undone = requests.snapshot.contains { $0.url!.path.hasSuffix("/send-undo") }
            let sent = requests.snapshot.contains { $0.url!.path.hasSuffix("/send") }
            // After the lost Undo, the server shows the restored draft with no run.
            let restored = self.draftJSON(revision: 2, body: "Edited before sending")
                .replacingOccurrences(of: "\"run_id\":\"\(self.runID)\"", with: "\"run_id\":null")
                .replacingOccurrences(of: "\"approval_id\":\"\(self.approvalID)\"", with: "\"approval_id\":null")
            return (200, self.threadJSON(draft: undone ? restored : sent ? self.parkedDraftJSON() : self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        await model.sendNow()
        await model.undoSend()
        try await waitForEmailTestCondition { model.draft?.status == "ready" && model.draft?.runID == nil }
        await model.sendNow()

        let sends = requests.snapshot.filter { $0.url!.path.hasSuffix("/send") }
        #expect(sends.count == 2)
        let keys = try sends.map { try #require(Self.archiveRequestBody($0)["idempotency_key"] as? String) }
        #expect(keys.count == 2 && keys[0] != keys[1])
    }

    /// A press whose response was lost keeps its key only until a refresh proves the
    /// press landed or the draft moved on; a failed draft at the same revision then gets
    /// a new key on the next press.
    @Test func testARefreshThatShowsTheLostPressFailedRetiresItsKey() async throws {
        let requests = EmailRequestRecorder()
        let failed = EmailTestFlag()
        let model = try makeModel(pollNanoseconds: 1_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                let sends = requests.snapshot.filter { $0.url!.path.hasSuffix("/send") }.count
                if sends <= 2 { throw URLError(.networkConnectionLost) }
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "QUEUED")) }
            return (200, self.threadJSON(draft: failed.isSet
                ? self.draftJSON(revision: 2, body: "Edited before sending", status: "failed")
                : self.draftJSON(revision: 2, body: "Edited before sending")))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        await model.sendNow()
        #expect(model.draftActionMessage?.isEmpty == false)
        // The server had admitted that press after all; a refresh shows it failed.
        failed.set()
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.draft?.status == "failed")
        await model.sendNow()

        let sends = requests.snapshot.filter { $0.url!.path.hasSuffix("/send") }
        let keys = try sends.map { try #require(Self.archiveRequestBody($0)["idempotency_key"] as? String) }
        #expect(keys.count == 3)
        #expect(keys[0] == keys[1] && keys[1] != keys[2])
    }

    /// A press belongs to the draft it was made on. Moving to another conversation
    /// while that press's save settles must not send the newly opened draft.
    @Test func testSendPressedOnOneDraftNeverSendsAnotherOpenedDuringItsSave() async throws {
        let requests = EmailRequestRecorder()
        let saveGate = EmailArchiveResponseGate()
        let otherThreadID = UUID(uuidString: "00000000-0000-0000-0000-000000000811")!
        let otherDraftID = UUID(uuidString: "00000000-0000-0000-0000-000000000812")!
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.httpMethod == "PUT" {
                return (200, self.draftJSON(revision: 2, body: "Edited on the first draft"), saveGate)
            }
            if request.url!.path.hasSuffix("/send") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}", nil)
            }
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: "QUEUED"), nil) }
            if request.url!.path.hasSuffix(otherThreadID.uuidString) {
                let other = self.threadJSON(draft: self.draftJSON(body: "A clean reply on the other thread"))
                    .replacingOccurrences(of: self.threadID.uuidString, with: otherThreadID.uuidString)
                    .replacingOccurrences(of: self.draftID.uuidString, with: otherDraftID.uuidString)
                return (200, other, nil)
            }
            return (200, self.threadJSON(draft: self.draftJSON()), nil)
        }
        defer { saveGate.release(); model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited on the first draft")
        let pressed = Task { await model.sendNow() }
        try await waitForEmailTestCondition { saveGate.isWaiting }
        #expect(!model.canSelectReply)
        await model.openThread(otherThreadID)
        #expect(model.draft?.id == otherDraftID)
        saveGate.release()
        await pressed.value

        #expect(!requests.snapshot.contains { $0.url!.path.hasSuffix("/send") }, "nothing may be sent from a press made on another draft")
        #expect(model.draft?.id == otherDraftID)
        #expect(model.draft?.status == "ready")
    }

    /// A refused Undo (the send was already claimed) keeps watching that send so its
    /// outcome is still reported, instead of leaving a silent "sending" draft.
    @Test func testRefusedUndoKeepsWatchingTheSend() async throws {
        let requests = EmailRequestRecorder()
        let failed = EmailTestFlag()
        // A 20 ms poll keeps the restarted watcher alive (120 polls, about 2.4 s)
        // well past the refusal assertion, instead of spending its attempts at once.
        let model = try makeModel(pollNanoseconds: 20_000_000, now: { ISO8601DateFormatter().date(from: "2026-09-11T00:00:00Z")! }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.hasSuffix("/send-undo") {
                return (409, Self.error("invalid_state", "This reply is already on its way and can't be undone."))
            }
            // The run stays queued until the test releases its failure, so the refusal
            // is asserted before the restarted watcher can report anything.
            if request.url!.path.contains("/runs/") { return (200, self.runJSON(status: failed.isSet ? "FAILED" : "QUEUED")) }
            return (200, self.threadJSON(draft: failed.isSet
                ? self.draftJSON(revision: 2, body: "Edited before sending", status: "failed")
                : self.parkedDraftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Edited before sending")
        await model.sendNow()
        await model.undoSend()
        #expect(model.draftActionMessage == "This reply is already on its way and can't be undone.")
        // The restarted watcher reports the send's own outcome once it lands.
        failed.set()
        try await waitForEmailTestCondition { model.draftActionMessage == EmailViewModel.sendFailedMessage }
        #expect(model.draft?.status == "failed")
    }

    /// A press the server refuses because mail arrived shows that mail, in the server's words.
    @Test func testSendRefusedForUnseenMailShowsTheNewMail() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                return (409, Self.error("invalid_state", "New mail arrived on this thread. Read it, then send again."))
            }
            let refused = requests.snapshot.contains { $0.url!.path.hasSuffix("/send") }
            return (200, refused ? self.newMailThreadJSON(draft: self.staleDraftJSON()) : self.threadJSON(draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.sendNow()

        #expect(model.draftActionMessage == "New mail arrived on this thread. Read it, then send again.")
        #expect(model.draft?.stale == true)
        #expect(!model.canUndoSend)
    }

    /// Once the window closes and the run completes, the draft reads Sent.
    @Test func testSendReportsSentWhenTheRunCompletes() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.httpMethod == "PUT" { return (200, self.draftJSON(revision: 2, body: "Edited before sending")) }
            if request.url!.path.hasSuffix("/send") {
                return (200, "{\"run_id\":\"\(self.runID)\",\"status\":\"QUEUED\",\"draft\":\(self.parkedDraftJSON())}")
            }
            if request.url!.path.contains("/runs/") {
                let polls = requests.snapshot.filter { $0.url!.path.contains("/runs/") }.count
                return (200, self.runJSON(status: polls >= 3 ? "COMPLETED" : "QUEUED"))
            }
            let polls = requests.snapshot.filter { $0.url!.path.contains("/runs/") }.count
            let sent = requests.snapshot.contains { $0.url!.path.hasSuffix("/send") }
            return (200, self.threadJSON(draft: polls >= 3 ? self.draftJSON(revision: 2, status: "sent") : sent ? self.parkedDraftJSON() : self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.sendNow()

        #expect(model.draft?.status == "sent")
        #expect(!model.canUndoSend)
        #expect(model.draftActionMessage == nil)
    }

    /// A reply over the approval view's 512-character ceiling arrives truncated. The
    /// published digest still proves the frozen body is exactly the displayed draft,
    /// so review opens instead of silently refusing every long email.
    @Test func testReviewAcceptsATruncatedApprovalBodyWithAMatchingDigest() async throws {
        let body = String(repeating: "b", count: 600)
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/approvals/") {
                return (200, self.approvalJSON(
                    body: Self.truncatedView(body),
                    digests: "{\"body\":\"\(Self.sha256Hex(body))\"}"))
            }
            return (200, self.threadJSON(draft: self.draftJSON(body: body, status: "awaiting_approval")))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.loadReview()
        #expect(model.review != nil)
        #expect(model.reviewDraft?.body == body)
        #expect(model.draftError == nil)
    }

    /// The digest is the verification, not a bypass: a frozen body that differs is
    /// still refused even though the truncated prefix matches; a truncated body with no
    /// digest at all cannot be verified either, so it must not be presented.
    @Test(arguments: [true, false])
    func testReviewRefusesATruncatedApprovalBodyWithoutAMatchingDigest(carriesADigest: Bool) async throws {
        let body = String(repeating: "b", count: 600)
        let tampered = body.dropLast() + "X"
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.contains("/approvals/") {
                guard carriesADigest else { return (200, self.approvalJSON(body: Self.truncatedView(body))) }
                return (200, self.approvalJSON(
                    body: Self.truncatedView(body),
                    digests: "{\"body\":\"\(Self.sha256Hex(String(tampered)))\"}"))
            }
            return (200, self.threadJSON(draft: self.draftJSON(body: body, status: "awaiting_approval")))
        }
        defer { model.resetConnection() }
        await model.openThread(threadID)
        await model.loadReview()
        #expect(model.review == nil)
        #expect(model.draftError != nil)
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
        let late = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { _ in
            (200, self.threadJSON(draft: self.draftJSON()), late)
        }
        defer { late.release() }
        let opening = Task { await model.openThread(threadID) }
        // The conversation's answer is held until the connection has been forgotten.
        try await waitForEmailTestCondition { late.isWaiting }
        model.resetConnection()
        late.release()
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

    /// The conflict itself tells the owner another device changed the draft; the
    /// refused save must not repeat the server's 409 text as a second, red error,
    /// including when saving the local version meets a still newer edit.
    @Test func testConflictingSaveShowsOnlyTheConflictNotTheRefusedSaveError() async throws {
        let serverRevision = EmailRequestRecorder()
        let model = try makeModel { request in
            serverRevision.append(request)
            if request.httpMethod == "PUT" { return (409, Self.error("invalid_state", "Draft changed on another device.")) }
            if request.url!.path.contains("/drafts/") {
                let fetches = serverRevision.snapshot.filter { $0.httpMethod == "GET" && $0.url!.path.contains("/drafts/") }
                return (200, self.draftJSON(revision: 1 + fetches.count, body: "Other device edit \(fetches.count)"))
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My unfinished edit")
        #expect(await model.saveDraft() == false)
        #expect(model.conflict?.body == "Other device edit 1")
        #expect(model.draftActionMessage == nil)

        await model.keepLocalDraft()
        #expect(model.conflict?.body == "Other device edit 2")
        #expect(model.currentEdit?.body == "My unfinished edit")
        #expect(model.draftActionMessage == nil)
    }

    /// New mail marks a draft stale at its current revision, and the server refuses edits until the
    /// owner confirms the updated thread. That refusal is not another device's edit: reporting it as a
    /// conflict disabled the confirmation, leaving Review & Send unreachable.
    @Test func testStaleDraftRefusalIsNotAConflictAndReviewConfirmationSavesTheEdit() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "PUT" { return try self.staleDraftSave(request, revision: 2) }
            if request.url!.path.contains("/drafts/") { return (200, self.staleDraftJSON()) }
            return (200, self.newMailThreadJSON(draft: self.staleDraftJSON()))
        }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My reply")
        #expect(await model.saveDraft() == false)
        #expect(model.conflict == nil)
        #expect(model.currentEdit?.body == "My reply")
        await model.confirmCurrentSourceReviewed()
        #expect(model.conflict == nil)
        #expect(model.draft?.stale == false)
        #expect(model.currentEdit?.isDirty == false)
        #expect(model.canReview)
    }

    /// The owner's reported path: a real edit conflict on a stale draft. Keeping the local version
    /// must leave the review confirmation usable instead of reopening the same conflict.
    @Test func testSavingMyVersionOfAStaleConflictLeavesReviewConfirmationUsable() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.httpMethod == "PUT" { return try self.staleDraftSave(request, revision: 3) }
            let other = self.staleDraftJSON(revision: 2, body: "Other device edit")
            if request.url!.path.contains("/drafts/") { return (200, other) }
            let threadReads = requests.snapshot.filter { $0.url!.path.hasSuffix(self.threadID.uuidString) }.count
            return (200, threadReads > 1 ? self.newMailThreadJSON(draft: other) : self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "My reply")
        #expect(await model.saveDraft() == false)
        #expect(model.conflict?.body == "Other device edit")
        await model.keepLocalDraft()
        #expect(model.conflict == nil)
        #expect(model.currentEdit?.body == "My reply")
        await model.confirmCurrentSourceReviewed()
        #expect(model.conflict == nil)
        #expect(model.draft?.revision == 3)
        #expect(model.canReview)
        let save = try #require(requests.snapshot.last { $0.httpMethod == "PUT" })
        let body = try Self.archiveRequestBody(save)
        #expect(body["expected_revision"] as? Double == 2)
        #expect(body["source_revision"] as? Double == 2)
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

    /// Resume automatic admission after the pause expires and clear it on connection replacement.
    @Test func testBudgetPauseExpiresAndConnectionResetClearsItsState() async throws {
        let requests = EmailRequestRecorder()
        var current = Date(timeIntervalSince1970: 1_789_344_000)
        let model = try makeModel(refreshNanoseconds: 10_000_000, now: { current }) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                return (402, Self.error("budget_exceeded", "Budget paused."))
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.setActive(false) }
        try await waitForEmailTestCondition { model.budgetRetryAt != nil && !model.isRefreshing }
        #expect(model.budgetPauseMessage == "Processing budget is in use.")
        #expect(model.budgetPauseDetails.isEmpty)
        #expect(model.errorMessage == nil)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
        current = try #require(model.budgetRetryAt).addingTimeInterval(1)
        try await waitForEmailTestCondition {
            requests.snapshot.filter { $0.httpMethod == "POST" }.count == 2 && !model.isRefreshing
        }
        model.setActive(false)
        model.resetConnection()
        #expect(model.budgetPauseMessage == nil)
        #expect(model.budgetPauseDetails.isEmpty)
        #expect(model.budgetRetryAt == nil)
    }

    /// The sidebar summarizes the limiting window without repeating accounting diagnostics.
    @Test(arguments: [false, true])
    func testBudgetPauseUsesConciseReason(monthly: Bool) async throws {
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                let spent = monthly ? "399.50" : "16.2966295"
                let daily = monthly ? "0" : spent
                let held = monthly ? "0" : "23"
                return (402, """
                {"error":{"code":"budget_exceeded","message":"A long paragraph with accounting numbers.",
                "details":{"daily_spent":"\(daily)","daily_reserved":"\(held)","daily_limit":"40",
                "monthly_spent":"\(monthly ? spent : "347.5703420")","monthly_reserved":"\(held)",
                "monthly_limit":"400","next_reservation":"1","retry_at":"2099-01-01T00:00:00Z"},
                "request_id":"budget-test"}}
                """)
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.setActive(false) }
        try await waitForEmailTestCondition { model.budgetRetryAt != nil && !model.isRefreshing }
        #expect(model.budgetPauseMessage == (monthly
            ? "Monthly processing budget is in use." : "Daily processing budget is in use."))
        #expect(model.budgetPauseDetails.count == 4)
        #expect(model.budgetPauseDetails[0].hasPrefix("Today: "))
        #expect(model.budgetPauseDetails[1].hasPrefix("Past 30 days: "))
        #expect(model.budgetPauseDetails[2].hasPrefix("Pending costs: "))
        #expect(model.budgetPauseDetails[3].hasPrefix("Next batch: "))
        #expect(!model.budgetPauseDetails.joined().contains("2966295"))
        #expect(!model.budgetPauseDetails.joined().contains("5703420"))
    }

    @Test(arguments: ["0", "1.25"])
    func testBudgetPauseLabelsEstimatedSpending(estimated: String) async throws {
        let monthlyEstimated = estimated == "0" ? "0" : "2.50"
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                return (402, """
                {"error":{"code":"budget_exceeded","message":"Budget in use",
                "details":{"daily_spent":"4","daily_limit":"4","daily_estimated":"\(estimated)",
                "monthly_spent":"8","monthly_limit":"8","monthly_estimated":"\(monthlyEstimated)",
                "retry_at":"2099-01-01T00:00:00Z"},"request_id":"budget-estimate"}}
                """)
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.setActive(false) }
        try await waitForEmailTestCondition { model.budgetRetryAt != nil && !model.isRefreshing }
        #expect(model.budgetPauseDetails.count == 2)
        for (index, detail) in model.budgetPauseDetails.enumerated() {
            #expect(detail.contains("estimated") == (estimated != "0"))
            if estimated != "0" { #expect(detail.contains(index == 0 ? "1.25" : "2.50")) }
            #expect(detail.contains(" of "))
        }
    }

    /// Keep budget pauses across mode visits while allowing an explicit refresh to recover.
    @Test func testBudgetCeilingStopsTimerAcrossVisitsAndManualRefreshRecovers() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(refreshNanoseconds: 10_000_000) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("refresh") {
                if requests.snapshot.filter({ $0.httpMethod == "POST" }).count == 1 {
                    return (402, "{\"error\":{\"code\":\"budget_exceeded\",\"message\":\"Automatic email work has reached its cost ceiling.\",\"details\":{\"reason\":\"email_aggregate_cost\",\"retry_at\":\"2099-01-01T00:00:00+00:00\"},\"request_id\":\"budget-test\"}}")
                }
                return (200, self.operationJSON(status: "COMPLETED"))
            }
            return (200, self.pageJSON())
        }
        model.setActive(true)
        defer { model.setActive(false) }
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing
        }
        try await Task.sleep(nanoseconds: 50_000_000)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
        model.setActive(false)
        model.setActive(true)
        try await Task.sleep(nanoseconds: 50_000_000)
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 1)
        #expect(model.items.count == 1, "cached mail stays available during the pause")
        await model.refresh()
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 2)
        #expect(model.errorMessage == nil)
        #expect(model.budgetPauseMessage == nil)
        #expect(model.budgetPauseDetails.isEmpty)
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

    /// Whatever the server ranked first, the list reads newest first.
    @Test func testRowsReadNewestFirstWhateverTheServerRanks() async throws {
        let newerID = UUID(uuidString: "00000000-0000-0000-0000-000000000805")!
        let older = threadJSON()
        let newer = older.replacingOccurrences(of: threadID.uuidString, with: newerID.uuidString)
            .replacingOccurrences(of: "\"updated_at\":\"2026-09-11T00:00:00Z\"", with: "\"updated_at\":\"2026-09-12T00:00:00Z\"")
        let model = try makeModel { request in
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            return (200, "{\"items\":[\(older),\(newer)],\"next_cursor\":null}")
        }
        await model.reload()
        #expect(model.items.map(\.id) == [newerID, threadID])
    }

    /// New mail in a conversation moves it to the top on the next refresh; the others keep their places.
    @Test func testRefreshMovesAConversationWithNewMailToTheTop() async throws {
        let requests = EmailRequestRecorder()
        let otherID = UUID(uuidString: "00000000-0000-0000-0000-000000000805")!
        let first = threadJSON()
        let second = first.replacingOccurrences(of: threadID.uuidString, with: otherID.uuidString)
            .replacingOccurrences(of: "\"updated_at\":\"2026-09-11T00:00:00Z\"", with: "\"updated_at\":\"2026-09-10T00:00:00Z\"")
        let updated = second.replacingOccurrences(of: "\"updated_at\":\"2026-09-10T00:00:00Z\"", with: "\"updated_at\":\"2026-09-12T00:00:00Z\"")
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let count = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
            return (200, "{\"items\":[\(first),\(count == 1 ? second : updated)],\"next_cursor\":null}")
        }
        await model.reload()
        #expect(model.items.map(\.id) == [threadID, otherID])
        await model.reload(preserveOrder: true)
        #expect(model.items.map(\.id) == [otherID, threadID])
    }

    /// Coming back to Email refreshes the rows in place: nothing disappears and no loading indicator restarts.
    @Test func testReturningToEmailKeepsTheLoadedRowsWhileItRefreshes() async throws {
        let requests = EmailRequestRecorder()
        let gate = EmailArchiveResponseGate()
        let arrivalID = UUID(uuidString: "00000000-0000-0000-0000-000000000805")!
        let first = threadJSON()
        let arrival = first.replacingOccurrences(of: threadID.uuidString, with: arrivalID.uuidString)
            .replacingOccurrences(of: "\"updated_at\":\"2026-09-11T00:00:00Z\"", with: "\"updated_at\":\"2026-09-12T00:00:00Z\"")
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.url!.path.hasSuffix("refresh") { return (202, self.operationJSON(status: "COMPLETED"), nil) }
            let count = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
            if count == 1 { return (200, "{\"items\":[\(first)],\"next_cursor\":null}", nil) }
            return (200, "{\"items\":[\(arrival),\(first)],\"next_cursor\":null}", count == 2 ? gate : nil)
        }
        defer {
            gate.release()
            model.setActive(false)
            model.resetConnection()
        }
        await model.reload()
        #expect(model.items.map(\.id) == [threadID])
        model.setActive(true)
        try await waitForEmailTestCondition { gate.isWaiting }
        #expect(model.items.map(\.id) == [threadID], "The rows already on screen stay while the new visit reads")
        #expect(!model.isLoading, "A refresh in place restarts no initial loading indicator")
        gate.release()
        try await waitForEmailTestCondition { model.items.count == 2 }
        #expect(model.items.map(\.id) == [arrivalID, threadID])
    }

    /// A list read abandoned by a connection reset or a new list leaves paging free: the
    /// abandoned read can no longer clear its own flag (ADR-0303).
    @Test(arguments: [true, false]) func testAbandonedListReadDoesNotHoldPagingOff(reset: Bool) async throws {
        let requests = EmailRequestRecorder()
        let gate = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            let reads = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
            return (200, "{\"items\":[\(self.threadJSON())],\"next_cursor\":null}", reads == 1 ? gate : nil)
        }
        defer {
            gate.release()
            model.resetConnection()
        }
        let read = Task { await model.reload() }
        try await waitForEmailTestCondition { gate.isWaiting }
        #expect(model.isReadingList)
        if reset { model.resetConnection() } else { model.setListView("handled") }
        #expect(!model.isReadingList, "The abandoned read must not hold paging off")
        gate.release()
        await read.value
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

    /// A refresh that succeeds while this client is not watching its operation must still clear the
    /// banner. A visit that ends, or a later admission that supersedes the poll, leaves advancing
    /// account freshness as the only evidence the success happened.
    @Test func testLaterRefreshSuccessClearsTheBannerWithoutATerminalOperationRead() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            let posts = requests.snapshot.filter { $0.httpMethod == "POST" }.count
            if request.url!.path.hasSuffix("accounts") {
                return (200, Self.accountsJSON(synced: posts >= 2 ? "2026-09-11T00:05:00Z" : "2026-09-11T00:00:00Z"))
            }
            if request.url!.path.hasSuffix("refresh") {
                return (200, self.operationJSON(status: posts == 1 ? "FAILED" : "RUNNING"))
            }
            if request.url!.path.contains("/operations/") { return (200, self.operationJSON(status: "RUNNING")) }
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
        #expect(model.errorMessage != nil, "a cached read without newer account freshness keeps the failure visible")
        await model.refresh()
        #expect(model.errorMessage == nil, "an account synced after the failure proves a later refresh succeeded")
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
        let backoff = StatusBackoffRecorder()
        let model = try makeModel(statusBackoff: { backoff.append($0) }) { request in
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
        // Backoff completes at once here, so a fourth attempt would follow the
        // third failure within milliseconds instead of sixteen seconds later.
        try await Task.sleep(nanoseconds: 1_000_000_000)
        #expect(requests.snapshot.filter { $0.url!.path.contains("/operations/") }.count == 3)
        #expect(backoff.snapshot == [2, 4, 8].map { $0 * 1_000_000_000 })
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
        let admission = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.url!.path.hasSuffix("refresh") {
                // The manual refresh's admission is held until Email has been hidden.
                let manual = requests.snapshot.filter { $0.httpMethod == "POST" }.count == 2
                return (200, self.operationJSON(status: "COMPLETED"), manual ? admission : nil)
            }
            return (200, self.pageJSON(), nil)
        }
        model.setActive(true)
        defer { admission.release(); model.setActive(false) }
        try await waitForEmailTestCondition {
            requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing
        }
        #expect(!model.isRefreshing)
        let manualRefresh = Task { await model.refresh() }
        try await waitForEmailTestCondition { admission.isWaiting }
        #expect(requests.snapshot.filter { $0.httpMethod == "POST" }.count == 2)
        model.setActive(false)
        let readsAtExit = requests.snapshot.filter { $0.httpMethod == "GET" }.count
        admission.release()
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
        let firstPage = EmailArchiveResponseGate()
        defer { firstPage.release() }
        let model = try makeArchiveRaceModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") {
                let count = requests.snapshot.filter { $0.url!.path.hasSuffix("accounts") }.count
                if count == (initial ? 1 : 3) {
                    Thread.sleep(forTimeInterval: 0.15)
                    return (status, status == 200
                        ? Self.accountsJSON.replacingOccurrences(of: "Personal", with: "Obsolete account")
                        : Self.error(status == 403 ? "permission_denied" : "not_found", "Obsolete projection failure"), nil)
                }
                return (200, Self.accountsJSON, nil)
            }
            if request.url!.path.hasSuffix("refresh") {
                return (200, self.operationJSON(status: polling ? "RUNNING" : "COMPLETED"), nil)
            }
            if request.url!.path.contains("/operations/") { return (200, self.operationJSON(status: "COMPLETED"), nil) }
            // Hold the projection itself: rows no longer wait for account metadata.
            return (200, self.pageJSON(), initial ? firstPage : nil)
        }
        model.setActive(true)
        defer { model.resetConnection() }
        if !initial {
            try await waitForEmailTestCondition { requests.snapshot.contains { $0.httpMethod == "POST" } && !model.isRefreshing }
        }
        let manual = (initial || polling) ? nil : Task { await model.refresh() }
        try await waitForEmailTestCondition { requests.snapshot.filter { $0.url!.path.hasSuffix("accounts") }.count == (initial ? 1 : 3) }
        if initial {
            try await waitForEmailTestCondition { firstPage.isWaiting }
            #expect(model.items.isEmpty)
        }
        model.setActive(false)
        firstPage.release()
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

    /// Missing, ambiguous and stale topic selections must never send an invalid feedback command.
    @Test(arguments: ["", "Unlisted topic"], [false, true])
    func testTopicFeedbackRequiresAnAvailableSelection(targetValue: String, hasTopics: Bool) async throws {
        let requests = EmailRequestRecorder()
        let thread = hasTopics
            ? threadJSON().replacingOccurrences(of: "\"senders\":", with: "\"topics\":[\"Board\",\"Hiring\"],\"senders\":")
            : threadJSON()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("/feedback") {
                return (400, Self.error("malformed_request", "The HTTP request is not supported."))
            }
            return (200, thread)
        }
        await model.openThread(threadID)
        await model.giveFeedback(target: .topic, judgment: "important", targetValue: targetValue)
        #expect(!requests.snapshot.contains { $0.httpMethod == "POST" })
        #expect(model.draftError == "Choose an available content topic, or apply feedback to This thread.")
    }

    /// Sends exactly the server topic the owner selected and names it in the reversible acknowledgment.
    @Test(arguments: ["important", "less_important"])
    func testTopicFeedbackSendsSelectedTopic(judgment: String) async throws {
        let requests = EmailRequestRecorder()
        let thread = threadJSON().replacingOccurrences(of: "\"senders\":", with: "\"topics\":[\"Board\",\"Hiring\"],\"senders\":")
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("/feedback") {
                return (200, "{\"feedback_id\":\"\(self.threadID)\",\"thread\":\(thread)}")
            }
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, "{\"items\":[\(thread)],\"next_cursor\":null}") }
            if request.url!.path.contains("/drafts/") { return (200, self.draftJSON()) }
            return (200, thread)
        }
        await model.openThread(threadID)
        await model.giveFeedback(target: .topic, judgment: judgment, targetValue: "Board")
        let command = try #require(requests.snapshot.first { $0.httpMethod == "POST" })
        let body = try Self.archiveRequestBody(command)
        #expect(body["target"] as? String == "topic")
        #expect(body["target_value"] as? String == "Board")
        #expect(body["judgment"] as? String == judgment)
        #expect(body["expected_revision"] as? Int == 1)
        #expect(model.feedbackMessage == "Marked Board as \(judgment.replacingOccurrences(of: "_", with: " ")).")
        #expect(model.feedbackID == threadID)
        #expect(model.draftError == nil)
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

    /// Importance and reply need are separate judgments: giving one keeps the other chosen,
    /// each with its own Undo.
    @Test func testImportanceAndReplyNeedAreHeldTogether() async throws {
        let requests = EmailRequestRecorder()
        let importanceID = UUID(), replyID = UUID()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("/feedback") {
                let judgment = (try? Self.archiveRequestBody(request))?["judgment"] as? String
                let id = judgment == "important" ? importanceID : replyID
                return (200, "{\"feedback_id\":\"\(id)\",\"thread\":\(self.threadJSON())}")
            }
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON()) }
            return (200, self.threadJSON())
        }
        await model.openThread(threadID)
        await model.giveFeedback(target: .thread, judgment: "important")
        await model.giveFeedback(target: .thread, judgment: "needs_reply")
        #expect(model.feedbackGiven[.importance]?.judgment == "important")
        #expect(model.feedbackGiven[.importance]?.message == "Marked this thread as important.")
        #expect(model.feedbackGiven[.importance]?.id == importanceID)
        #expect(model.feedbackGiven[.replyNeed]?.judgment == "needs_reply")
        #expect(model.feedbackGiven[.replyNeed]?.message == "This thread needs a reply.")
        #expect(model.feedbackGiven[.replyNeed]?.id == replyID)

        await model.undoFeedback(.importance)
        let undone = requests.snapshot.filter { $0.httpMethod == "DELETE" }.map { $0.url!.lastPathComponent.lowercased() }
        #expect(undone == [importanceID.uuidString.lowercased()])
        #expect(model.feedbackGiven[.importance] == nil)
        #expect(model.feedbackGiven[.replyNeed]?.id == replyID)

        model.clearSelection()
        #expect(model.feedbackGiven.isEmpty)
    }

    /// A judgment answered after the owner opened another thread must not appear on that thread.
    @Test func testFeedbackAnsweredAfterAnotherThreadOpensStaysOffThatThread() async throws {
        let other = UUID()
        let gate = EmailArchiveResponseGate()
        let model = try makeArchiveRaceModel { request in
            if request.url!.path.hasSuffix("/feedback") {
                return (200, "{\"feedback_id\":\"\(self.threadID)\",\"thread\":\(self.threadJSON())}", gate)
            }
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON, nil) }
            if request.url!.path.hasSuffix("threads") { return (200, self.pageJSON(), nil) }
            if request.url!.path.hasSuffix(other.uuidString) {
                return (200, self.threadJSON().replacingOccurrences(of: self.threadID.uuidString, with: other.uuidString), nil)
            }
            return (200, self.threadJSON(), nil)
        }
        defer { gate.release(); model.resetConnection() }
        await model.openThread(threadID)
        let pending = Task { await model.giveFeedback(target: .thread, judgment: "important") }
        try await waitForEmailTestCondition { gate.isWaiting }
        await model.openThread(other)
        gate.release()
        await pending.value
        #expect(model.selectedThreadID == other)
        #expect(model.feedbackGiven.isEmpty, "Another thread must not show this judgment or its Undo")
        #expect(model.feedbackMessage == nil)
        #expect(model.feedbackID == nil)
    }

    /// Foreground arrivals appear immediately without reordering surviving rows or disturbing edits.
    @Test func testRefreshInsertsNewThreadsWithoutChangingSelection() async throws {
        let requests = EmailRequestRecorder()
        let second = UUID()
        let arrival = UUID()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            if request.url!.path.hasSuffix("threads") {
                let reads = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
                let ids = reads == 1 ? [self.threadID, second] : [arrival, second, self.threadID]
                let rows = ids.map { id in
                    self.threadJSON().replacingOccurrences(of: self.threadID.uuidString, with: id.uuidString)
                }.joined(separator: ",")
                return (200, "{\"items\":[\(rows)],\"next_cursor\":null}")
            }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        defer { model.resetConnection() }
        await model.reload()
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Keep my reply")
        await model.reload(preserveOrder: true)
        #expect(model.items.map(\.id) == [threadID, second, arrival])
        #expect(model.selectedThreadID == threadID)
        #expect(model.thread?.id == threadID)
        #expect(model.edits[draftID]?.body == "Keep my reply")
        await model.reload(preserveOrder: true)
        #expect(model.items.map(\.id) == [threadID, second, arrival], "Repeated refreshes must not duplicate or reorder arrivals")
    }

    @Test func testThreadRefreshReusesValidatedContentAndKeepsEdits() async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel(responseHeaders: ["ETag": "W/\"retained-thread\""]) { request in
            requests.append(request)
            if request.value(forHTTPHeaderField: "If-None-Match") != nil { return (304, "") }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Unsent local words")
        await model.openThread(threadID, refreshOnly: true)
        #expect(requests.snapshot.last?.value(forHTTPHeaderField: "If-None-Match") == "W/\"retained-thread\"")
        #expect(model.thread?.id == threadID)
        #expect(model.edits[draftID]?.body == "Unsent local words")
        #expect(model.draftError == nil)
        model.clearSelection()
        await model.openThread(threadID)
        #expect(requests.snapshot.last?.value(forHTTPHeaderField: "If-None-Match") == nil)
        model.resetConnection()
    }

    /// A successful unchanged refresh restores automatic paging after a failed page request.
    @Test func testUnchangedSnapshotRecoversPagingAfterFailure() async throws {
        let server = AttachmentSaveServer()
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let cursor = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?
                .queryItems?.first { $0.name == "cursor" }?.value
            if cursor != nil && !server.recovered { return (500, Self.error("internal_error", "Page failed")) }
            let page = self.pageJSON(cursor: cursor == nil ? "next" : nil)
                .replacingOccurrences(of: "\"items\":", with: "\"snapshot_version\":\"stable\",\"items\":")
            return (200, page)
        }
        await model.reload()
        await model.loadMore()
        #expect(model.pagingFailed)
        #expect(model.hasMore)
        server.recover()
        let before = requests.snapshot.count
        await model.reload(preserveOrder: true)
        #expect(!model.pagingFailed)
        #expect(model.errorMessage == nil)
        #expect(model.items.count == 1)
        #expect(model.hasMore)
        #expect(requests.snapshot.dropFirst(before).filter { $0.url!.path.hasSuffix("threads") }.count == 1)
        await model.loadMore()
        #expect(requests.snapshot.last?.url?.query?.contains("cursor=next") == true)
        #expect(!model.hasMore)
    }

    @Test(arguments: [true, false], [300, 1800])
    func testUnchangedSnapshotRefreshReadsOnlyOnePage(versioned: Bool, count: Int) async throws {
        let requests = EmailRequestRecorder()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems ?? []
            let offset = Int(query.first { $0.name == "cursor" }?.value ?? "0")!
            let rows = (offset..<min(offset + 100, count)).map { i in
                self.threadJSON().replacingOccurrences(of: self.threadID.uuidString,
                    with: String(format: "00000000-0000-0000-0000-%012d", 1000 + i))
            }.joined(separator: ",")
            let version = versioned ? ",\"snapshot_version\":\"stable\"" : ""
            return (200, "{\"items\":[\(rows)],\"next_cursor\":\(offset + 100 < count ? "\"\(offset + 100)\"" : "null")\(version)}")
        }
        await model.reload()
        for _ in 1..<(count / 100) { await model.loadMore() }
        #expect(model.items.count == count)
        let before = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
        await model.reload(preserveOrder: true)
        let after = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
        #expect(after - before == (versioned ? 1 : count / 100))
        #expect(model.items.count == count)
        #expect(!model.hasMore)
    }

    @Test(arguments: [false, true])
    func testChangedAndMixedSnapshotsRefreshEveryLoadedPage(mixed: Bool) async throws {
        let requests = EmailRequestRecorder()
        let server = AttachmentSaveServer()
        let model = try makeModel { request in
            requests.append(request)
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)!.queryItems ?? []
            let offset = Int(query.first { $0.name == "cursor" }?.value ?? "0")!
            let rows = (offset..<min(offset + 100, 300)).map { i in
                self.threadJSON().replacingOccurrences(of: self.threadID.uuidString,
                    with: String(format: "00000000-0000-0000-0000-%012d", 1000 + i))
            }.joined(separator: ",")
            let version = server.recovered ? (mixed && offset > 0 ? "third" : "second") : "first"
            return (200, "{\"items\":[\(rows)],\"next_cursor\":\(offset < 200 ? "\"\(offset + 100)\"" : "null"),\"snapshot_version\":\"\(version)\"}")
        }
        await model.reload()
        await model.loadMore()
        await model.loadMore()
        server.recover()
        let before = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
        await model.reload(preserveOrder: true)
        let changed = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
        #expect(changed - before == 3)
        await model.reload(preserveOrder: true)
        let after = requests.snapshot.filter { $0.url!.path.hasSuffix("threads") }.count
        #expect(after - changed == (mixed ? 3 : 1), "Mixed pages cannot establish a reusable snapshot")
        #expect(model.items.count == 300)
    }

    @Test func testValidatedThreadRefreshStillClearsMailOnRevokedAccess() async throws {
        let server = AttachmentSaveServer()
        let model = try makeModel(responseHeaders: ["ETag": "W/\"private-thread\""]) { request in
            if server.recovered { return (403, Self.error("forbidden", "Email access was revoked.")) }
            return (200, self.threadJSON(draft: self.draftJSON()))
        }
        await model.openThread(threadID)
        model.changeEdit(\.body, to: "Unsent words")
        server.recover()
        await model.openThread(threadID, refreshOnly: true)
        #expect(model.thread == nil)
        #expect(model.edits.isEmpty)
        #expect(model.draftError != nil)
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

    /// A learning reset shows while it runs and confirms when done; the sheet barely changes otherwise.
    @Test func testResettingLearningShowsProgressAndConfirms() async throws {
        let requests = EmailRequestRecorder()
        let gate = DispatchSemaphore(value: 0)
        defer { gate.signal() }
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let model = try makeModel(toasts: toasts) { request in
            requests.append(request)
            if request.url!.path.hasSuffix("learning/reset") {
                _ = gate.wait(timeout: .now() + 5)
                return (200, "{\"paused\":false,\"profile_revision\":3,\"excluded_sources\":0,\"style_examples\":0,\"history_processed\":25,\"history_complete\":false}")
            }
            if request.url!.path.hasSuffix("accounts") { return (200, Self.accountsJSON) }
            return (200, self.pageJSON())
        }
        let reset = Task { await model.resetLearning(scope: "preferences") }
        try await waitForEmailTestCondition { requests.snapshot.contains { $0.url!.path.hasSuffix("learning/reset") } }
        #expect(model.isResettingLearning)
        gate.signal()
        await reset.value
        #expect(!model.isResettingLearning)
        #expect(toasts.current?.text == "Importance preferences reset.")
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
    /// Five rows as a real inbox reads: named senders, several accounts, a draft,
    /// dates spread across the week, newest first. Their IDs are 801 through 805.
    private func variedInboxRows() -> String {
        let rows: [(senders: String, subject: String, summary: String, updated: String, needsReply: Bool, draft: String,
                    account: String)] = [
            ("\"Max Abram <max@example.test>\"", "Board discussion", "Asks for your view on the agenda before Friday.",
             "2026-09-11T16:30:00Z", true, "null", "personal"),
            ("\"Claire Yoon <claire@example.test>\",\"Sid Ramakrishnan <sid@example.test>\"",
             "Design review for the autumn release", "Two open questions on the onboarding flow.",
             "2026-09-11T09:05:00Z", true, "null", "work"),
            ("\"alex@example.test\"", "Friday planning notes", "Notes from the planning call, nothing to answer.",
             "2026-09-10T18:00:00Z", false, "null", "personal"),
            ("\"\\\"Docusign\\\" <dse@example.test>\"", "Please sign: closing documents",
             "A signature request; a reply is drafted.", "2026-09-08T12:00:00Z", true, draftJSON(), "work"),
            ("\"Wendy Oropeza <wendy@example.test>\"", "Travel details for next week",
             "Confirms the hotel and asks about your arrival time.", "2026-08-30T08:00:00Z", true, "null", "personal"),
        ]
        return rows.enumerated().map { index, row in
            threadJSON(draft: row.draft)
                .replacingOccurrences(of: threadID.uuidString, with: "00000000-0000-0000-0000-00000000080\(index + 1)")
                .replacingOccurrences(of: "\"summary\":\"Review the board agenda.\"", with: "\"summary\":\"\(row.summary)\"")
                .replacingOccurrences(of: row.draft == "null" ? "\"draft_id\":\"\(draftID)\"" : "\"draft_id\":\"-\"",
                                      with: "\"draft_id\":null")
                .replacingOccurrences(of: "\"senders\":[\"alex@example.test\"]", with: "\"senders\":[\(row.senders)]")
                .replacingOccurrences(of: "\"subject\":\"Board discussion\"", with: "\"subject\":\"\(row.subject)\"")
                .replacingOccurrences(of: "\"updated_at\":\"2026-09-11T00:00:00Z\",\"revision\"",
                                      with: "\"updated_at\":\"\(row.updated)\",\"revision\"")
                .replacingOccurrences(of: "\"needs_reply\":true", with: "\"needs_reply\":\(row.needsReply)")
                .replacingOccurrences(of: "\"account_id\":\"personal\"", with: "\"account_id\":\"\(row.account)\"")
        }.joined(separator: ",")
    }

    // MARK: - Saving an attachment (ADR-0296)

    private func saveOperationJSON(_ status: String) -> String {
        "{\"operation_id\":\"\(threadID)\",\"run_id\":\"\(runID)\",\"status\":\"\(status)\",\"replayed\":false}"
    }

    @Test func testSavingAnAttachmentNamesItsPositionAndFollowsTheOperationToSaved() async throws {
        let server = AttachmentSaveServer()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            if request.url!.path.hasSuffix("/attachments/save") {
                server.posted(try Self.archiveRequestBody(request))
                return (200, self.saveOperationJSON("QUEUED"))
            }
            return (200, self.saveOperationJSON(server.read() == 1 ? "RUNNING" : "COMPLETED"))
        }
        let key = EmailAttachmentKey(threadID: threadID, messageID: "m1", index: 2)
        #expect(model.attachmentSave(key) == nil)

        await model.saveAttachment(key, filename: "invoice.pdf")

        #expect(model.attachmentSave(key) == .saved)
        let bodies = server.bodies
        #expect(bodies.count == 1)
        let body = try #require(bodies.first)
        #expect(body["message_id"] as? String == "m1")
        #expect(body["attachment_index"] as? Int == 2)
        #expect(body["filename"] as? String == "invoice.pdf")
        #expect((body["idempotency_key"] as? String)?.isEmpty == false)
        #expect(server.reads == 2)
    }

    @Test func testAFailedSaveSaysSoAndAPressAfterItIsANewRequest() async throws {
        let server = AttachmentSaveServer()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            if request.url!.path.hasSuffix("/attachments/save") {
                server.posted(try Self.archiveRequestBody(request))
                return (200, self.saveOperationJSON("QUEUED"))
            }
            return (200, self.saveOperationJSON(server.bodies.count == 1 ? "FAILED" : "COMPLETED"))
        }
        let key = EmailAttachmentKey(threadID: threadID, messageID: "m1", index: 0)

        await model.saveAttachment(key, filename: "invoice.pdf")
        guard case .failed(let message) = model.attachmentSave(key) else {
            Issue.record("A failed run must read as failed")
            return
        }
        #expect(message.contains("could not be saved"))

        await model.saveAttachment(key, filename: "invoice.pdf")
        #expect(model.attachmentSave(key) == .saved)
        #expect(Set(server.bodies.compactMap { $0["idempotency_key"] as? String }).count == 2)
    }

    @Test func testARefusedSaveShowsTheServersReasonAndASavedFileIsNotSavedAgain() async throws {
        let server = AttachmentSaveServer()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            if request.url!.path.hasSuffix("/attachments/save") {
                server.posted(try Self.archiveRequestBody(request))
                if server.bodies.count == 1 {
                    return (409, "{\"error\":{\"code\":\"conflict\",\"message\":\"Veetbot is still working on this thread. Try again in a moment.\",\"details\":{}}}")
                }
            }
            return (200, self.saveOperationJSON("COMPLETED"))
        }
        let key = EmailAttachmentKey(threadID: threadID, messageID: "m1", index: 0)
        await model.saveAttachment(key, filename: "invoice.pdf")
        guard case .failed(let message) = model.attachmentSave(key) else {
            Issue.record("A refusal must read as failed")
            return
        }
        #expect(message.contains("still working"))

        await model.saveAttachment(key, filename: "invoice.pdf")
        #expect(model.attachmentSave(key) == .saved)
        await model.saveAttachment(key, filename: "invoice.pdf")
        #expect(server.bodies.count == 2, "A saved file needs no second copy")
    }

    @Test func testARetryAfterALostAnswerRepeatsTheSameRequest() async throws {
        let server = AttachmentSaveServer()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            if request.url!.path.hasSuffix("/attachments/save") {
                server.posted(try Self.archiveRequestBody(request))
                // The transport retries a lost request, so the whole press must lose it.
                if !server.recovered { throw URLError(.networkConnectionLost) }
                return (200, self.saveOperationJSON("COMPLETED"))
            }
            return (200, self.saveOperationJSON("COMPLETED"))
        }
        let key = EmailAttachmentKey(threadID: threadID, messageID: "m1", index: 0)
        await model.saveAttachment(key, filename: "invoice.pdf")
        guard case .failed = model.attachmentSave(key) else {
            Issue.record("A lost answer must offer Retry")
            return
        }
        server.recover()
        await model.saveAttachment(key, filename: "invoice.pdf")
        #expect(model.attachmentSave(key) == .saved)
        #expect(server.bodies.count >= 2)
        #expect(Set(server.bodies.compactMap { $0["idempotency_key"] as? String }).count == 1)
    }

    @Test func testARetryAfterALostStatusFollowsTheAdmittedOperation() async throws {
        let server = AttachmentSaveServer()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            if request.url!.path.hasSuffix("/attachments/save") {
                server.posted(try Self.archiveRequestBody(request))
                return (200, self.saveOperationJSON("QUEUED"))
            }
            // The transport retries reads, so every read in the first press fails.
            if server.bodies.count == 1, !server.recovered { throw URLError(.networkConnectionLost) }
            return (200, self.saveOperationJSON("COMPLETED"))
        }
        let key = EmailAttachmentKey(threadID: threadID, messageID: "m1", index: 0)
        await model.saveAttachment(key, filename: "invoice.pdf")
        guard case .failed = model.attachmentSave(key) else {
            Issue.record("A lost status must offer Retry")
            return
        }
        server.recover()
        await model.saveAttachment(key, filename: "invoice.pdf")
        #expect(model.attachmentSave(key) == .saved)
        #expect(server.bodies.count == 1, "The admitted operation needs no second request")
    }

    @Test func testARefusedStatusReadKeepsFollowingTheAdmittedOperation() async throws {
        let server = AttachmentSaveServer()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            if request.url!.path.hasSuffix("/attachments/save") {
                server.posted(try Self.archiveRequestBody(request))
                return (200, self.saveOperationJSON("QUEUED"))
            }
            if !server.recovered {
                return (404, "{\"error\":{\"code\":\"not_found\",\"message\":\"Not found.\",\"details\":{}}}")
            }
            return (200, self.saveOperationJSON("COMPLETED"))
        }
        let key = EmailAttachmentKey(threadID: threadID, messageID: "m1", index: 0)
        await model.saveAttachment(key, filename: "invoice.pdf")
        guard case .failed = model.attachmentSave(key) else {
            Issue.record("A refused read must offer Retry")
            return
        }
        server.recover()
        await model.saveAttachment(key, filename: "invoice.pdf")
        #expect(model.attachmentSave(key) == .saved)
        #expect(server.bodies.count == 1, "A retained operation is never admitted again")
    }

    @Test func testAConnectionResetKeepsFollowingAnAdmittedSave() async throws {
        let server = AttachmentSaveServer()
        let model = try makeModel(pollNanoseconds: 1_000) { request in
            if request.url!.path.hasSuffix("/attachments/save") {
                server.posted(try Self.archiveRequestBody(request))
                return (200, self.saveOperationJSON("QUEUED"))
            }
            if !server.recovered { throw URLError(.networkConnectionLost) }
            return (200, self.saveOperationJSON("COMPLETED"))
        }
        let key = EmailAttachmentKey(threadID: threadID, messageID: "m1", index: 0)
        await model.saveAttachment(key, filename: "invoice.pdf")
        // Signing in again resets the connection, for the same account and the same file.
        model.resetConnection()
        #expect(model.attachmentSave(key) == nil)
        server.recover()
        await model.saveAttachment(key, filename: "invoice.pdf")
        #expect(model.attachmentSave(key) == .saved)
        #expect(server.bodies.count == 1, "A second press must not save a second copy")
    }

    @Test func testAConnectionResetForgetsSaveState() async throws {
        let model = try makeModel(pollNanoseconds: 1_000) { _ in (200, self.saveOperationJSON("COMPLETED")) }
        let key = EmailAttachmentKey(threadID: threadID, messageID: "m1", index: 0)
        await model.saveAttachment(key, filename: "invoice.pdf")
        #expect(model.attachmentSave(key) == .saved)
        model.resetConnection()
        #expect(model.attachmentSave(key) == nil)
    }

    @Test func testAFileOverTheLimitCannotBeSaved() {
        #expect(EmailAttachmentView.canSave(size: nil))
        #expect(EmailAttachmentView.canSave(size: 32 * 1024 * 1024))
        #expect(!EmailAttachmentView.canSave(size: 32 * 1024 * 1024 + 1))
    }

    private func makeModel(
        refreshNanoseconds: UInt64 = 60_000_000_000,
        pollNanoseconds: UInt64 = 1_000_000_000,
        now: @escaping () -> Date = Date.init,
        statusBackoff: @escaping @Sendable (UInt64) async throws -> Void = { try await Task.sleep(nanoseconds: $0) },
        toasts: ToastCenter? = nil,
        responseHeaders: [String: String] = [:],
        handler: @escaping (URLRequest) throws -> (Int, String)
    ) throws -> EmailViewModel {
        let id = EmailTestURLProtocol.register { request in
            let (status, body) = try handler(request)
            return (status, body, responseHeaders)
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [EmailTestURLProtocol.self]
        configuration.httpAdditionalHeaders = ["X-Email-Test": id]
        let api = VeetbotAPIClient(transport: HTTPTransport(
            configuration: try ConnectionConfiguration(baseURLString: "https://email.test"),
            tokenStore: InMemoryTokenStore(token: "test-token"), session: URLSession(configuration: configuration)
        ))
        return EmailViewModel(
            makeAPIClient: { api }, refreshNanoseconds: refreshNanoseconds, pollNanoseconds: pollNanoseconds,
            now: now, statusBackoff: statusBackoff,
            toasts: toasts ?? ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        )
    }

    private static let accountsJSON = """
        {"items":[{"id":"personal","label":"Personal","email_address":"owner@example.test","status":"ready","last_synced_at":"2026-09-11T00:00:00Z","history_complete":false,"history_processed":25,"read_server_id":"gmail_read","send_server_id":"gmail_send"},{"id":"work","label":"Work","email_address":"owner@work.test","status":"ready","last_synced_at":null,"history_complete":false,"history_processed":0,"read_server_id":"gmail_work_read","send_server_id":"gmail_work_send"}],"next_cursor":null}
        """
    private static func accountsJSON(synced: String) -> String {
        """
        {"items":[{"id":"personal","label":"Personal","email_address":"owner@example.test","status":"ready","last_synced_at":"\(synced)","history_complete":false,"history_processed":25,"read_server_id":"gmail_read","send_server_id":"gmail_send"}],"next_cursor":null}
        """
    }
    /// Mirrors the server's approval view: the first 512 characters plus the marker.
    private static func truncatedView(_ value: String) -> String {
        "\(value.prefix(512))…[TRUNCATED]"
    }
    private static func sha256Hex(_ value: String) -> String {
        SHA256.hash(data: Data(value.utf8)).map { String(format: "%02x", $0) }.joined()
    }
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
    /// The draft the server returns while a one-press send is parked in its undo window.
    private func parkedDraftJSON(runID: UUID? = nil) -> String {
        draftJSON(revision: 2, body: "Edited before sending", status: "sending")
            .replacingOccurrences(of: "\"approval_id\":\"\(approvalID)\"", with: "\"approval_id\":null")
            .replacingOccurrences(of: "\"run_id\":\"\(self.runID)\"", with: "\"run_id\":\"\(runID ?? self.runID)\"")
            .replacingOccurrences(of: "\"updated_at\":\"2026-09-11T00:00:00Z\"",
                                  with: "\"updated_at\":\"2026-09-11T00:00:00Z\",\"send_undo_until\":\"2026-09-11T00:00:10Z\"")
    }
    private func runJSON(status: String, id: UUID? = nil) -> String {
        """
        {"id":"\(id ?? runID)","session_id":"\(threadID)","status":"\(status)","step_count":1,"model_call_count":0,"tool_call_count":0,"usage":{"input_tokens":0,"output_tokens":0,"cost_usd":"0"},"limits":{"max_steps":8},"created_at":"2026-09-11T00:00:00Z","updated_at":"2026-09-11T00:00:00Z"}
        """
    }
    private func messageJSON(id: String, sender: String, body: String, sentAt: String) -> String {
        """
        {"id":"\(id)","sender":"\(sender)","to":["owner@example.test"],"cc":[],"subject":"Board discussion","body":"\(body)","sent_at":"\(sentAt)","complete":true}
        """
    }
    /// The thread after new mail arrived: its revision advanced past the draft's source revision.
    private func newMailThreadJSON(draft: String) -> String {
        threadJSON(draft: draft).replacingOccurrences(of: "\"revision\":1,\"summary\"", with: "\"revision\":2,\"summary\"")
    }
    private func staleDraftJSON(revision: Int = 1, body: String = "Thanks, I will review the agenda.") -> String {
        draftJSON(revision: revision, body: body).replacingOccurrences(of: "\"stale\":false", with: "\"stale\":true")
    }
    /// Mirrors the server: a stale draft accepts an edit only with the reviewed thread revision.
    private func staleDraftSave(_ request: URLRequest, revision: Int) throws -> (Int, String) {
        let body = try Self.archiveRequestBody(request)
        guard body["source_revision"] as? Double == 2 else {
            return (409, Self.error("invalid_state", "review the changed thread before updating this draft"))
        }
        let saved = draftJSON(revision: revision, body: body["body"] as? String ?? "")
        return (200, saved.replacingOccurrences(of: "\"source_revision\":1", with: "\"source_revision\":2"))
    }
    private func approvalJSON(body: String, digests: String = "{}") -> String {
        """
        {"id":"\(approvalID)","run_id":"\(runID)","session_id":"\(threadID)","status":"PENDING","tool_name":"mcp.gmail_send.send_message","action_summary":"Send reply","arguments":{"thread_id":"provider-thread","to":["alex@example.test"],"cc":[],"bcc":[],"subject":"Re: Board discussion","body":"\(body)"},"argument_digests":\(digests),"risk":"HIGH","policy_reason":"Approval required","expires_at":null,"created_at":"2026-09-11T00:00:00Z","resolved_at":null,"resolved_by":null,"decision":null}
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

/// A switch the test flips while the handler keeps answering on URLSession's queue.
private final class EmailTestFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false
    func set() { lock.withLock { value = true } }
    var isSet: Bool { lock.withLock { value } }
}

/// Shares the row moved by the fixture server with its main-actor rendered test.
private final class EmailTestAnchor: @unchecked Sendable {
    private let lock = NSLock()
    private var value: UUID?
    func set(_ id: UUID) { lock.withLock { value = id } }
    var id: UUID? { lock.withLock { value } }
}

private final class EmailRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [URLRequest] = []
    func append(_ value: URLRequest) { lock.withLock { values.append(value) } }
    var snapshot: [URLRequest] { lock.withLock { values } }
}

/// Records each requested status-read wait and returns at once, keeping backoff in virtual time.
private final class StatusBackoffRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [UInt64] = []
    func append(_ value: UInt64) { lock.withLock { values.append(value) } }
    var snapshot: [UInt64] { lock.withLock { values } }
}

/// What the attachment tests' fake server saw; URL loading runs off the main actor.
private final class AttachmentSaveServer: @unchecked Sendable {
    private let lock = NSLock()
    private var recorded: [[String: Any]] = []
    private var readCount = 0
    private var isRecovered = false
    var recovered: Bool { lock.withLock { isRecovered } }
    func recover() { lock.withLock { isRecovered = true } }
    var bodies: [[String: Any]] { lock.withLock { recorded } }
    var reads: Int { lock.withLock { readCount } }
    func posted(_ body: [String: Any]) { lock.withLock { recorded.append(body) } }
    func read() -> Int { lock.withLock { readCount += 1; return readCount } }
}

private final class EmailTestURLProtocol: URLProtocol {
    private static let lock = NSLock()
    private static var handlers: [String: (URLRequest) throws -> (Int, String, [String: String])] = [:]
    /// Allocates a distinct fixture host so concurrent tests cannot replace each other's routes.
    static func register(_ handler: @escaping (URLRequest) throws -> (Int, String, [String: String])) -> String {
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
            let (status, body, headers) = try handler(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: headers.merging(["Content-Type": "application/json"]) { current, _ in current })!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }
    override func stopLoading() {}
}
