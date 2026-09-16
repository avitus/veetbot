import Foundation
import Testing
@testable import VeetbotCore

@Suite @MainActor struct PeopleViewModelTests {
    @Test func commitmentCalendarPrecisionDoesNotInventAnExactDueDay() throws {
        let encoded = """
        {"id":"\(UUID())","revision":1,"debtor":{"kind":"owner"},"beneficiary":{"kind":"person","id":"\(UUID())"},"description":"Send a report","state":"open","due_at":"2026-08-01T00:00:00Z","due_precision":"month","source_timezone":"America/Los_Angeles","support_ids":[]}
        """
        let month = try JSONDecoder.server.decode(PersonCommitmentView.self, from: Data(encoded.utf8))
        // The stored instant is still July in the explicitly retained source zone.
        #expect(month.dueLabel?.contains("July") == true)
        #expect(month.dueLabel?.contains("31") == false)
        let unknown = try JSONDecoder.server.decode(PersonCommitmentView.self, from: Data(encoded.replacingOccurrences(of: "\"month\"", with: "\"unknown\"").utf8))
        #expect(unknown.dueLabel == "Due date precision unknown")
    }
    @Test func relationshipAndInteractionRetainCalendarPrecision() throws {
        let relationship = """
        {"id":"\(UUID())","revision":1,"subject":{"kind":"owner"},"object":{"kind":"person","id":"\(UUID())"},"predicate":"friend","qualifier":"","valid_from":"2026-08-01T00:00:00Z","precision":"month","source_timezone":"America/Los_Angeles","support_ids":[]}
        """
        let interaction = """
        {"id":"\(UUID())","revision":1,"channel":"chat","interaction_kind":"meeting","attribution":"owner_reported","direction":"reported","summary":"Met for coffee","occurred_at":"2026-08-01T00:00:00Z","precision":"month","source_timezone":"America/Los_Angeles","support_ids":[],"participants":[]}
        """
        let edge = try JSONDecoder.server.decode(PersonRelationshipView.self, from: Data(relationship.utf8))
        let event = try JSONDecoder.server.decode(PersonInteractionView.self, from: Data(interaction.utf8))
        #expect(edge.sinceLabel == "Since July 2026")
        #expect(event.dateLabel == "July 2026")
        let unknown = try JSONDecoder.server.decode(PersonInteractionView.self, from: Data(interaction.replacingOccurrences(of: "\"month\"", with: "\"unknown\"").utf8))
        #expect(unknown.dateLabel == "Date precision unknown")
        let offset = try JSONDecoder.server.decode(PersonInteractionView.self, from: Data(interaction.replacingOccurrences(of: "America/Los_Angeles", with: "GMT+0900").utf8))
        #expect(offset.dateLabel == "August 2026")
        for data in [try JSONEncoder().encode(edge), try JSONEncoder().encode(event)] {
            let fields = try #require(JSONSerialization.jsonObject(with: data) as? [String: Any])
            #expect(fields["source_timezone"] as? String == "America/Los_Angeles")
            #expect(fields["precision"] as? String == "month")
        }
    }
    @Test func inFlightDirectoryResponseCannotRestorePreviousConnectionData() async throws {
        let notifications = NotificationCenter(), id = UUID()
        let started = AsyncStream<Void>.makeStream()
        let release = DispatchSemaphore(value: 0)
        let client = try makePeopleClient { _ in
            started.continuation.yield(())
            guard release.wait(timeout: .now() + 5) == .success else { throw URLError(.timedOut) }
            return (200, "{\"items\":[\(personJSON(id))],\"next_cursor\":null}")
        }
        let people = PeopleViewModel(notifications: notifications, makeAPIClient: { client })
        let request = Task { await people.reload() }
        for await _ in started.stream { break }
        notifications.post(name: .peopleConnectionChanged, object: nil)
        try await Task.sleep(nanoseconds: 10_000_000)
        release.signal()
        await request.value
        #expect(people.items.isEmpty && !people.isLoading && people.errorMessage == nil)
    }

    @Test func connectionChangeClearsPeopleImportAndSourceState() async throws {
        let notifications = NotificationCenter()
        let id = UUID(), jobID = UUID(), auditID = UUID()
        let client = try makePeopleClient { request in
            if request.url?.path == "/v1/people" { return (200, "{\"items\":[\(personJSON(id))],\"next_cursor\":null}") }
            if request.url?.path.contains("/imports/") == true {
                return (200, """
                {"id":"\(jobID)","audit_session_id":"\(auditID)","scope":{},"revision":1,"state":"budget_paused","records_read":1,"records_processed":1,"records_excluded":0,"failures":0,"spent_usd":"1","reserved_usd":"0","source_read_complete":false,"analysis_complete":false,"coverage":"Owner history"}
                """)
            }
            return (200, """
            {"person":\(personJSON(id)),"aliases":[],"relationships":[],"history":[],"commitments":[],"facts":[],"fact_revisions":{},"truncated":false,"coverage":"Owner history"}
            """)
        }
        let source = try JSONDecoder.server.decode(PeopleEvidenceView.self, from: Data("""
        {"reference":"\(UUID())","source_kind":"owner","session_id":"\(auditID)","event_sequence":1,"evidence_at":"2026-08-01T00:00:00Z","owner_assertion":"Private source assertion"}
        """.utf8))
        let people = PeopleViewModel(notifications: notifications, makeAPIClient: { client })
        let detail = PeopleDetailViewModel(personID: id, notifications: notifications, makeAPIClient: { client })
        let imports = PeopleImportViewModel(notifications: notifications, makeAPIClient: { client })
        let evidence = PeopleEvidenceViewModel(source: source, notifications: notifications, makeAPIClient: { client })
        await people.reload(); await detail.reload(); await imports.restore(jobID); await evidence.load()
        #expect(people.items.count == 1 && detail.profile != nil && imports.job != nil && evidence.text != nil)
        notifications.post(name: .peopleConnectionChanged, object: nil)
        try await Task.sleep(nanoseconds: 10_000_000)
        #expect(people.items.isEmpty)
        #expect(detail.profile == nil && detail.history.isEmpty && detail.preview == nil && !detail.canRetrySave)
        #expect(imports.job == nil && imports.existingImports.isEmpty && !imports.canRetry)
        #expect(evidence.text == nil && evidence.heading == nil)
        await people.reload(); await detail.reload(); await imports.restore(jobID); await evidence.load()
        #expect(people.items.isEmpty && detail.profile == nil && imports.job == nil && evidence.text == nil)
    }

    @Test func identityEvidencePagingPreservesSelectionAndRetriesFailedPage() async throws {
        let id = UUID(), claimID = UUID(), mentionID = UUID(), sourceID = UUID()
        let lock = NSLock(); var attempts = 0
        let client = try makePeopleClient { request in
            #expect(request.url?.path == "/v1/people/\(id.uuidString)/identity-evidence")
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            if query.contains(URLQueryItem(name: "cursor", value: "next")) {
                let attempt = lock.withLock { attempts += 1; return attempts }
                if attempt == 1 { return (503, #"{"error":{"code":"unavailable","message":"Try again","request_id":"retry"}}"#) }
                return (200, """
                {"items":[{"id":"\(mentionID)","revision":4,"kind":"mention","label":"Subject mention","support_ids":["\(sourceID)"],"unresolved":false}],"next_cursor":null}
                """)
            }
            return (200, """
            {"items":[{"id":"\(claimID)","revision":2,"kind":"memory_link","label":"Alex enjoys cycling","support_ids":["\(sourceID)"],"unresolved":true}],"next_cursor":"next"}
            """)
        }
        let model = PeopleDetailViewModel(personID: id, makeAPIClient: { client })
        await model.loadIdentityEvidence()
        #expect(model.identityEvidence.map(\.id) == [claimID])
        await model.loadIdentityEvidence()
        #expect(model.identityEvidence.map(\.id) == [claimID])
        #expect(model.identityEvidenceError != nil)
        await model.loadIdentityEvidence()
        #expect(model.identityEvidence.map(\.id) == [claimID, mentionID])
        #expect(model.identityEvidenceError == nil)
    }

    @Test func removingOneFactKeepsThePersonAndTracksPendingCleanup() async throws {
        let id = UUID(), sessionID = UUID(), factID = UUID(), receiptID = UUID()
        let fact = """
        {"id":"\(factID)","subject":"Maya","statement":"Maya is my colleague","belief_type":"relationship","claim_kind":"relationship","derivation":"direct","longevity":"durable","status":"active","polarity":"assert","scope":"general","portability":"contextual","authority":"user","sensitivity":"sensitive","confidence":1,"corroboration_count":1,"flagged_for_review":false,"conflicts_with":[],"source_session_id":"\(sessionID)","source_event_ids":[1],"formation_run_id":"00000000-0000-0000-0000-000000000900","consolidation_policy_version":"formation@11","origin_scopes":[],"valid_from":"2026-08-01T00:00:00Z","last_evidence_at":"2026-08-01T00:00:00Z","last_reinforced_at":"2026-08-01T00:00:00Z","created_at":"2026-08-01T00:00:00Z","updated_at":"2026-08-01T00:00:00Z"}
        """
        let client = try makePeopleClient { request in
            if request.url?.path.hasSuffix("/corrections") == true {
                return (200, """
                {"person_revision":2,"belief":null,"removed":true,"erasure":{"id":"\(receiptID)","revision":1,"state":"cleanup_pending","counts":{},"scope":"One fact and its generated copies; original messages remain."}}
                """)
            }
            if request.url?.path.contains("/operations/") == true {
                return (200, """
                {"id":"\(receiptID)","revision":2,"state":"completed","counts":{},"scope":"One fact and its generated copies; original messages remain."}
                """)
            }
            return (200, """
            {"person":\(personJSON(id)),"aliases":[],"relationships":[],"history":[],"commitments":[],"facts":[\(fact)],"fact_revisions":{"\(factID.uuidString.lowercased())":7},"truncated":false,"coverage":"Recorded evidence only"}
            """)
        }
        let model = PeopleDetailViewModel(personID: id, makeAPIClient: { client })
        await model.reload()
        let recorded = try #require(model.profile?.facts.first)
        await model.correct(recorded, operation: "remove", statement: nil, sessionID: sessionID)
        #expect(model.profile != nil && !model.isForgotten && model.errorMessage == nil)
        #expect(model.receipt?.id == receiptID && model.receipt?.state == "cleanup_pending")
        await model.refreshReceipt()
        #expect(model.receipt?.state == "completed")
    }

    @Test func structuredRelationshipCorrectionKeepsEndpointsAndRevision() async throws {
        let id = UUID(), sessionID = UUID(), factID = UUID(), edgeID = UUID()
        let fact = """
        {"id":"\(factID)","subject":"Maya","statement":"Maya is my colleague","belief_type":"relationship","claim_kind":"relationship","derivation":"direct","longevity":"durable","status":"active","polarity":"assert","scope":"general","portability":"contextual","authority":"user","sensitivity":"sensitive","confidence":1,"corroboration_count":1,"flagged_for_review":false,"conflicts_with":[],"source_session_id":"\(sessionID)","source_event_ids":[1],"formation_run_id":"00000000-0000-0000-0000-000000000900","consolidation_policy_version":"formation@11","origin_scopes":[],"valid_from":"2026-08-01T00:00:00Z","last_evidence_at":"2026-08-01T00:00:00Z","last_reinforced_at":"2026-08-01T00:00:00Z","created_at":"2026-08-01T00:00:00Z","updated_at":"2026-08-01T00:00:00Z"}
        """
        let lock = NSLock()
        var writes = 0
        let edge = """
        {"id":"\(edgeID)","revision":3,"belief_id":"\(factID)","subject":{"kind":"person","id":"\(id)"},"object":{"kind":"owner"},"predicate":"colleague","qualifier":"","precision":"unknown","support_ids":[]}
        """
        let client = try makePeopleClient { request in
            if request.url?.path.hasSuffix("/corrections") == true {
                lock.withLock { writes += 1 }
                let body = try JSONSerialization.jsonObject(with: request.peopleBodyData!) as! [String: Any]
                let projection = body["projection"] as? [String: Any]
                #expect(projection?["predicate"] as? String == "friend")
                #expect(projection?["expected_revision"] as? Int == 3)
                #expect(projection?["id"] as? String == edgeID.uuidString)
                #expect((projection?["subject"] as? [String: Any])?["id"] as? String == id.uuidString)
                #expect((projection?["object"] as? [String: Any])?["kind"] as? String == "owner")
                return (200, #"{"person_revision":2,"belief":null,"removed":false}"#)
            }
            return (200, """
            {"person":\(personJSON(id)),"aliases":[],"relationships":[\(edge)],"history":[],"commitments":[],"facts":[\(fact)],"fact_revisions":{"\(factID.uuidString.lowercased())":7},"truncated":false,"coverage":"Recorded evidence only"}
            """)
        }
        let model = PeopleDetailViewModel(personID: id, makeAPIClient: { client })
        await model.reload()
        let recorded = try #require(model.profile?.facts.first)
        await model.correct(recorded, operation: "changed", statement: "Maya is my friend", sessionID: sessionID, relationshipPredicate: "friend")
        #expect(model.errorMessage == nil)
        #expect(lock.withLock { writes } == 1)
    }

    @Test func restoredImportResumesSavedScopeAndAuditSession() async throws {
        let auditID = UUID(), sourceID = UUID(), jobID = UUID()
        let lock = NSLock()
        var writes = 0
        let scope = """
        {"session_ids":["\(sourceID)"],"account_ids":[],"person_ids":[],"excluded_source_ids":[],"since":"2026-01-01T00:00:00Z","until":"2026-02-01T00:00:00Z","max_records":100,"max_cost_usd":"2.00"}
        """
        let client = try makePeopleClient { request in
            if request.httpMethod == "POST" {
                lock.withLock { writes += 1 }
                let body = try JSONSerialization.jsonObject(with: request.peopleBodyData!) as! [String: Any]
                #expect(body["phase"] as? String == "resume")
                #expect(body["operation_id"] as? String == jobID.uuidString)
                #expect(body["session_id"] as? String == auditID.uuidString)
                #expect(body["expected_revision"] as? Int == 7)
                #expect((body["scope"] as? [String: Any])?["session_ids"] as? [String] == [sourceID.uuidString])
            }
            return (200, """
            {"id":"\(jobID)","audit_session_id":"\(auditID)","scope":\(scope),"revision":7,"state":"budget_paused","records_read":20,"records_processed":18,"records_excluded":2,"failures":0,"spent_usd":"1.99","reserved_usd":"0","source_read_complete":false,"analysis_complete":false,"coverage":"Selected sources"}
            """)
        }
        let model = PeopleImportViewModel(makeAPIClient: { client })
        await model.restore(jobID)
        #expect(model.job?.id == jobID)
        await model.start(maxCost: "3.00")
        #expect(lock.withLock { writes } == 1)
        #expect(model.errorMessage == nil)
    }

    @Test func evidenceOpensTheExactMessageAcrossPages() async throws {
        let sessionID = UUID(), reference = UUID()
        let source = try JSONDecoder.server.decode(PeopleEvidenceView.self, from: Data("""
        {"reference":"\(reference)","source_kind":"owner","session_id":"\(sessionID)","event_sequence":44,"evidence_at":"2026-08-01T00:00:00Z"}
        """.utf8))
        let client = try makePeopleClient { request in
            #expect(request.url?.path == "/v1/sessions/\(sessionID.uuidString)/messages")
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            if query.contains(URLQueryItem(name: "cursor", value: "next-page")) {
                return (200, #"{"items":[{"sequence":44,"role":"user","content":[{"type":"text","text":"Maya is my sister."}]},{"sequence":45,"role":"assistant","content":[{"type":"text","text":"Later reply"}]}],"next_cursor":null}"#)
            }
            return (200, #"{"items":[{"sequence":1,"role":"user","content":[{"type":"text","text":"Earlier unrelated message"}]}],"next_cursor":"next-page"}"#)
        }
        let model = PeopleEvidenceViewModel(source: source, makeAPIClient: { client })
        await model.load()
        #expect(model.text == "Maya is my sister.")
        #expect(model.errorMessage == nil)
    }

    @Test func sourceReaderContinuesLongHistoryWithoutRepeatingEarlierPages() async throws {
        let source = try JSONDecoder.server.decode(PeopleEvidenceView.self, from: Data("""
        {"reference":"\(UUID())","source_kind":"owner","session_id":"\(UUID())","event_sequence":5000,"evidence_at":"2026-08-01T00:00:00Z"}
        """.utf8))
        let lock = NSLock(); var pages: [Int] = []
        let client = try makePeopleClient { request in
            let value = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "cursor" })?.value
            let page = value.flatMap(Int.init) ?? 0
            lock.lock(); pages.append(page); lock.unlock()
            if page == 10 {
                return (200, #"{"items":[{"sequence":5000,"role":"user","content":[{"type":"text","text":"The exact older source"}]}],"next_cursor":null}"#)
            }
            return (200, "{\"items\":[],\"next_cursor\":\"\(page + 1)\"}")
        }
        let model = PeopleEvidenceViewModel(source: source, makeAPIClient: { client })
        await model.load()
        #expect(model.text == nil && model.errorMessage == nil && model.hasMoreHistory)
        await model.loadMoreHistory()
        #expect(model.text == "The exact older source" && !model.hasMoreHistory)
        #expect(pages == Array(0...10))
    }

    @Test(arguments: [false, true]) func emailEvidenceKeepsAccountAndMessageBoundary(wrongAccount: Bool) async throws {
        let sessionID = UUID(), reference = UUID(), threadID = UUID()
        let source = try JSONDecoder.server.decode(PeopleEvidenceView.self, from: Data("""
        {"reference":"\(reference)","source_kind":"email","session_id":"\(sessionID)","event_sequence":44,"evidence_at":"2026-08-01T00:00:00Z","account_id":"personal","thread_id":"provider-thread","message_id":"exact-message","email_thread_id":"\(threadID)"}
        """.utf8))
        let client = try makePeopleClient { request in
            #expect(request.httpMethod == "GET")
            #expect(request.url?.path == "/v1/email/threads/\(threadID.uuidString)")
            return (200, """
            {"id":"\(threadID)","account_id":"\(wrongAccount ? "work" : "personal")","subject":"Source","senders":[],"updated_at":"2026-08-01T00:00:00Z","revision":1,"summary":"Retained","reason":"Retained","needs_reply":false,"priority":0,"complete":true,"messages":[{"id":"other-message","sender":"other@example.test","to":[],"cc":[],"subject":"Other","body":"Not this message","sent_at":"2026-08-01T00:00:00Z","complete":true},{"id":"exact-message","sender":"maya@example.test","to":[],"cc":[],"subject":"Source","body":"Exact source text","sent_at":"2026-08-01T00:00:00Z","complete":false}]}
            """)
        }
        let model = PeopleEvidenceViewModel(source: source, makeAPIClient: { client })
        await model.load()
        if wrongAccount {
            #expect(model.text == nil)
            #expect(model.errorMessage != nil)
        } else {
            #expect(model.text == "Exact source text")
            #expect(model.partial)
        }
    }

    @Test func directoryFiltersAreSentWithEveryPage() async throws {
        let client = try makePeopleClient { request in
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            #expect(query.contains(URLQueryItem(name: "state", value: "active")))
            #expect(query.contains(URLQueryItem(name: "relationship", value: "family")))
            #expect(query.contains(URLQueryItem(name: "pinned", value: "true")))
            #expect(query.contains(URLQueryItem(name: "cursor", value: "next")))
            return (200, #"{"items":[],"next_cursor":null}"#)
        }
        _ = try await client.listPeople(cursor: "next", state: "active", pinned: true, relationship: "family")
    }
    @Test(arguments: [false, true]) func emailOnlyImportRequiresExplicitAccountSelection(fetchMailbox: Bool) async throws {
        let auditID = UUID(), jobID = UUID()
        let client = try makePeopleClient { request in
            if request.url?.path == "/v1/sessions" {
                return (200, "{\"id\":\"\(auditID)\",\"status\":\"ACTIVE\",\"agent_id\":\"test\",\"agent_version\":\"1\",\"metadata\":{},\"created_at\":\"2026-09-01T00:00:00Z\",\"updated_at\":\"2026-09-01T00:00:00Z\"}")
            }
            let body = try JSONSerialization.jsonObject(with: request.peopleBodyData!) as! [String: Any]
            let scope = body["scope"] as! [String: Any]
            #expect(scope["account_ids"] as? [String] == ["work"])
            #expect(scope["email_source"] as? String == (fetchMailbox ? "mailbox" : "retained"))
            #expect((scope["session_ids"] as? [String])?.isEmpty == true)
            return (200, "{\"id\":\"\(jobID)\",\"revision\":1,\"state\":\"preview\",\"records_read\":0,\"records_processed\":0,\"records_excluded\":0,\"failures\":0,\"spent_usd\":\"0\",\"reserved_usd\":\"0\",\"source_read_complete\":false,\"analysis_complete\":false,\"coverage\":\"Selected retained mail only\"}")
        }
        let model = PeopleImportViewModel(makeAPIClient: { client })
        await model.preview(sessionIDs: [], accountIDs: ["work"], fetchMailbox: fetchMailbox, since: Date(timeIntervalSince1970: 0), until: Date(), maxRecords: 100, maxCost: "1")
        #expect(model.job?.id == jobID)
    }
    @Test func pausedImportResumesTheSameJobAndSourceScope() async throws {
        let auditID = UUID(), jobID = UUID()
        let lock = NSLock()
        var writes: [[String: Any]] = []
        let client = try makePeopleClient { request in
            if request.url?.path == "/v1/sessions" {
                return (200, "{\"id\":\"\(auditID)\",\"status\":\"ACTIVE\",\"agent_id\":\"test\",\"agent_version\":\"1\",\"metadata\":{},\"created_at\":\"2026-09-01T00:00:00Z\",\"updated_at\":\"2026-09-01T00:00:00Z\"}")
            }
            let body = try JSONSerialization.jsonObject(with: request.peopleBodyData!) as! [String: Any]
            let count = lock.withLock { writes.append(body); return writes.count }
            let state = count == 1 ? "preview" : count == 2 ? "budget_paused" : "completed"
            return (200, "{\"id\":\"\(jobID)\",\"revision\":\(count),\"state\":\"\(state)\",\"records_read\":0,\"records_processed\":0,\"records_excluded\":0,\"failures\":0,\"spent_usd\":\"0\",\"reserved_usd\":\"0\",\"source_read_complete\":false,\"analysis_complete\":false,\"coverage\":\"Selected sources only\"}")
        }
        let model = PeopleImportViewModel(makeAPIClient: { client })
        await model.preview(sessionIDs: [UUID()], since: Date(timeIntervalSince1970: 0), until: Date(), maxRecords: 100, maxCost: "1")
        await model.start()
        #expect(model.job?.state == "budget_paused")
        await model.start()
        #expect(model.job?.state == "completed")
        let bodies = lock.withLock { writes }
        #expect(bodies.count == 3)
        if bodies.count == 3 {
            #expect(bodies[2]["phase"] as? String == "resume")
            #expect(bodies[2]["operation_id"] as? String == jobID.uuidString)
            #expect(NSDictionary(dictionary: bodies[0]["scope"] as! [String: Any]) == NSDictionary(dictionary: bodies[2]["scope"] as! [String: Any]))
        }
    }
    @Test func cancellationRetryKeepsReviewedRevisionAfterRefresh() async throws {
        let auditID = UUID(), jobID = UUID()
        let lock = NSLock()
        var submissions = 0
        var cancellations: [String] = []
        func result(_ state: String, _ revision: Int) -> String {
            "{\"id\":\"\(jobID)\",\"revision\":\(revision),\"state\":\"\(state)\",\"records_read\":0,\"records_processed\":0,\"records_excluded\":0,\"failures\":0,\"spent_usd\":\"0\",\"reserved_usd\":\"0\",\"source_read_complete\":false,\"analysis_complete\":false,\"coverage\":\"Selected sources only\"}"
        }
        let client = try makePeopleClient { request in
            if request.url?.path == "/v1/sessions" {
                return (200, "{\"id\":\"\(auditID)\",\"status\":\"ACTIVE\",\"agent_id\":\"test\",\"agent_version\":\"1\",\"metadata\":{},\"created_at\":\"2026-09-01T00:00:00Z\",\"updated_at\":\"2026-09-01T00:00:00Z\"}")
            }
            if request.url?.path.hasSuffix("/cancel") == true {
                let body = request.peopleBodyData ?? Data()
                let count = lock.withLock {
                    cancellations.append((request.value(forHTTPHeaderField: "Idempotency-Key") ?? "") + String(decoding: body, as: UTF8.self))
                    return cancellations.count
                }
                if count <= 2 { return (503, #"{"error":{"code":"service_unavailable","message":"Retry","details":{},"request_id":"cancel-test"}}"#) }
                return (200, result("cancelled", 100))
            }
            if request.httpMethod == "GET" { return (200, result("running", 99)) }
            let count = lock.withLock { submissions += 1; return submissions }
            return (200, result(count == 1 ? "preview" : "running", count))
        }
        let model = PeopleImportViewModel(makeAPIClient: { client })
        await model.preview(sessionIDs: [UUID()], since: Date(timeIntervalSince1970: 0), until: Date(), maxRecords: 100, maxCost: "1")
        await model.start()
        await model.cancel()
        await model.refresh()
        await model.cancel()
        #expect(model.job?.state == "cancelled")
        #expect(lock.withLock { cancellations.count } == 3)
        #expect(lock.withLock { Set(cancellations).count } == 1)
    }
    @Test func importPreviewRetriesWithoutChangingItsIdempotencyKey() async throws {
        let sourceID = UUID(), auditID = UUID(), jobID = UUID()
        let lock = NSLock()
        var keys: [String] = []
        let client = try makePeopleClient { request in
            if request.url?.path == "/v1/sessions" {
                return (200, "{\"id\":\"\(auditID)\",\"status\":\"ACTIVE\",\"agent_id\":\"test\",\"agent_version\":\"1\",\"title\":null,\"metadata\":{},\"created_at\":\"2026-09-01T00:00:00Z\",\"updated_at\":\"2026-09-01T00:00:00Z\",\"active_run_id\":null,\"last_run_id\":null}")
            }
            let attempt = lock.withLock { keys.append(request.value(forHTTPHeaderField: "Idempotency-Key") ?? ""); return keys.count }
            if attempt <= 2 { return (503, #"{"error":{"code":"service_unavailable","message":"Try again","details":{},"request_id":"import-test"}}"#) }
            return (200, "{\"id\":\"\(jobID)\",\"revision\":1,\"state\":\"preview\",\"records_read\":0,\"records_processed\":0,\"records_excluded\":0,\"failures\":0,\"spent_usd\":\"0\",\"reserved_usd\":\"0\",\"source_read_complete\":false,\"analysis_complete\":false,\"known_records\":null,\"remaining_records\":null,\"coverage\":\"Selected conversations only\",\"error_code\":null}")
        }
        let model = PeopleImportViewModel(makeAPIClient: { client })
        await model.preview(sessionIDs: [sourceID], since: Date(timeIntervalSince1970: 0), until: Date(), maxRecords: 100, maxCost: "1.00")
        #expect(model.canRetry)
        await model.retry()
        #expect(model.job?.id == jobID)
        #expect(!model.canRetry)
        #expect(lock.withLock { keys.count } == 3)
        #expect(lock.withLock { Set(keys).count } == 1)
    }

    @Test func correspondentLookupPreservesOriginalAssignmentDate() async throws {
        let client = try makePeopleClient { request in
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            #expect(query.contains(URLQueryItem(name: "text", value: "maya@example.test")))
            #expect(query.first { $0.name == "as_of" }?.value == "2026-09-01T00:00:00.000Z")
            return (200, #"{"items":[],"next_cursor":null}"#)
        }
        _ = try await client.listPeople(text: "maya@example.test", asOf: ISO8601DateFormatter().date(from: "2026-09-01T00:00:00Z")!)
    }
    @Test func pagingPreservesLoadedPeopleOnFailureAndRetriesSameCursor() async throws {
        let id = UUID()
        let lock = NSLock()
        var pageAttempts = 0
        var cursors: [String?] = []
        let client = try makePeopleClient { request in
            let query = URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.queryItems ?? []
            #expect(query.contains(URLQueryItem(name: "ceiling", value: "restricted")))
            let cursor = query.first { $0.name == "cursor" }?.value
            lock.withLock { cursors.append(cursor) }
            if cursor == nil { return (200, "{\"items\":[\(personJSON(id))],\"next_cursor\":\"page2\"}") }
            let attempt = lock.withLock { pageAttempts += 1; return pageAttempts }
            if attempt == 1 { return (503, #"{"error":{"code":"service_unavailable","message":"Try again","details":{},"request_id":"people-test"}}"#) }
            return (200, "{\"items\":[\(personJSON(id))],\"next_cursor\":null}")
        }
        let model = PeopleViewModel(makeAPIClient: { client })
        await model.reload()
        #expect(model.items.map(\.id) == [id])
        await model.loadMore()
        #expect(model.items.map(\.id) == [id])
        #expect(model.errorMessage != nil)
        await model.loadMore()
        #expect(model.items.map(\.id) == [id])
        #expect(model.errorMessage == nil)
        #expect(lock.withLock { cursors } == [nil, "page2", "page2"])
    }
    @Test func uncertainSaveRetriesTheSameRevisionAndIdempotencyKey() async throws {
        let id = UUID(), sessionID = UUID()
        let lock = NSLock()
        var writes: [String] = []
        let client = try makePeopleClient { request in
            if request.httpMethod == "PATCH" {
                let count = lock.withLock { writes.append(request.value(forHTTPHeaderField: "Idempotency-Key") ?? ""); return writes.count }
                if count <= 2 { return (503, #"{"error":{"code":"service_unavailable","message":"Try again","details":{},"request_id":"save-test"}}"#) }
                return (200, personJSON(id))
            }
            return (200, "{\"person\":\(personJSON(id)),\"aliases\":[],\"relationships\":[],\"history\":[],\"commitments\":[],\"facts\":[],\"fact_revisions\":{},\"truncated\":false,\"coverage\":\"Recorded evidence only\"}")
        }
        let model = PeopleDetailViewModel(personID: id, makeAPIClient: { client })
        await model.reload()
        #expect(model.profile?.person.id == id)
        await model.saveName("Maya Smith", sessionID: sessionID)
        #expect(model.errorMessage != nil)
        await model.retrySave()
        #expect(model.errorMessage == nil)
        #expect(lock.withLock { writes.count } == 3)
        #expect(lock.withLock { Set(writes).count } == 1)
    }

    @Test func addingAliasUsesTheGovernedProfileWrite() async throws {
        let id = UUID(), sessionID = UUID()
        let lock = NSLock()
        var writes = 0
        let client = try makePeopleClient { request in
            if request.httpMethod == "PATCH" {
                lock.withLock { writes += 1 }
                #expect(request.value(forHTTPHeaderField: "Idempotency-Key") != nil)
                return (200, personJSON(id))
            }
            return (200, "{\"person\":\(personJSON(id)),\"aliases\":[],\"relationships\":[],\"history\":[],\"commitments\":[],\"facts\":[],\"fact_revisions\":{},\"truncated\":false,\"coverage\":\"Recorded evidence only\"}")
        }
        let model = PeopleDetailViewModel(personID: id, makeAPIClient: { client })
        await model.reload()
        await model.addAlias(value: "maya@example.test", kind: "email", context: "owner", sessionID: sessionID)
        #expect(lock.withLock { writes } == 1)
        #expect(model.errorMessage == nil)
    }
}

private func personJSON(_ id: UUID) -> String {
    "{\"id\":\"\(id)\",\"revision\":1,\"display_name\":\"Maya\",\"state\":\"active\",\"pinned\":false,\"sensitivity\":\"sensitive\",\"support_ids\":[]}"
}

private extension URLRequest {
    var peopleBodyData: Data? {
        if let httpBody { return httpBody }
        guard let stream = httpBodyStream else { return nil }
        stream.open(); defer { stream.close() }
        var data = Data(), bytes = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&bytes, maxLength: bytes.count)
            guard count > 0 else { break }
            data.append(bytes, count: count)
        }
        return data
    }
}

private func makePeopleClient(_ handler: @escaping (URLRequest) throws -> (Int, String)) throws -> VeetbotAPIClient {
    let configuration = try ConnectionConfiguration(baseURLString: "https://veetbot.test")
    let sessionConfiguration = URLSessionConfiguration.ephemeral
    let handlerID = PeopleTestURLProtocol.register(handler)
    sessionConfiguration.httpAdditionalHeaders = [PeopleTestURLProtocol.header: handlerID]
    sessionConfiguration.protocolClasses = [PeopleTestURLProtocol.self]
    let session = URLSession(configuration: sessionConfiguration)
    return VeetbotAPIClient(transport: HTTPTransport(configuration: configuration,
        tokenStore: InMemoryTokenStore(token: "test"), session: session))
}

private final class PeopleTestURLProtocol: URLProtocol {
    static let header = "X-People-Test"
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
        guard let id = request.value(forHTTPHeaderField: Self.header),
              let handler = Self.lock.withLock({ Self.handlers[id] }) else { return }
        do {
            let (status, body) = try handler(request)
            let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil,
                headerFields: ["Content-Type": "application/json"])!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}
