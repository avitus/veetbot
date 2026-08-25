import Foundation
import Testing
@testable import VeetbotCore

@MainActor
@Suite struct RunStateReducerTests {
    @Test
    func testFailedRunPresentsThePublicMessageReasonAndLocation() {
        let reducer = RunStateReducer()

        reducer.reduce(
            SSEFrame(
                id: 9,
                event: "run.failed",
                data: [
                    "failure": .object([
                        "reason": .string("internal_error"),
                        "message": .string("The web search provider returned an invalid response."),
                        "step_number": .number(2),
                        "attempt_number": .number(1),
                        "occurred_at": .string("2026-08-24T22:06:00Z"),
                    ])
                ]
            )
        )

        #expect(reducer.runStatus == .failed)
        #expect(
            reducer.failure?.userFacingMessage
                == "The web search provider returned an invalid response."
        )
        #expect(reducer.failure?.diagnosticSummary == "Internal error · Step 2 · Attempt 1")
    }

    @Test
    func testChatRendersTheStructuredRunFailureInTheConversation() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/ChatView.swift"),
            encoding: .utf8
        )

        #expect(source.contains("if let failure = state.failure {"))
        #expect(source.contains("RunFailureCard(failure: failure)"))
        #expect(source.contains(".accessibilityIdentifier(\"conversation.failure\")"))
    }

    @Test
    func testTransientTextReconcilesToDurableAssistantMessage() {
        let reducer = RunStateReducer()
        reducer.reduce(
            SSEFrame(id: nil, event: "message.delta", data: ["text": .string("part")])
        )
        #expect(reducer.timeline.first?.text == "part")
        #expect(reducer.timeline.first?.isStreaming == true)

        reducer.reduce(
            SSEFrame(
                id: 12,
                event: "assistant.message.completed",
                data: [
                    "message": .object([
                        "kind": .string("assistant"),
                        "content": .array([
                            .object(["kind": .string("text"), "text": .string("complete")]),
                        ]),
                    ]),
                ]
            )
        )
        #expect(reducer.timeline.count == 1)
        #expect(reducer.timeline[0].text == "complete")
        #expect(!reducer.timeline[0].isStreaming)
    }

    @Test
    func testReasoningTextIsDiscardedAndOnlyIndicatorChanges() {
        let reducer = RunStateReducer()
        reducer.reduce(
            SSEFrame(
                id: nil,
                event: "reasoning.delta",
                data: ["text": .string("private chain of thought")]
            )
        )
        #expect(reducer.reasoningActive)
        #expect(reducer.timeline.isEmpty)
    }

    @Test
    func testWaitingQuestionUsesWorkingStateAndCancellationClearsPrompt() {
        let reducer = RunStateReducer()
        let runID = UUID()
        let questionID = UUID()
        reducer.begin(runID: runID, status: .running)
        reducer.reduce(
            SSEFrame(
                id: 3,
                event: "context.working_state.updated",
                data: [
                    "working_state": .object([
                        "objective": .string("Finish"),
                        "constraints": .array([]),
                        "tasks": .array([]),
                        "established_facts": .array([]),
                        "open_questions": .array([.string("Which environment?")]),
                        "next_action": .null,
                    ]),
                ]
            )
        )
        reducer.reduce(
            SSEFrame(
                id: 4,
                event: "run.waiting_for_user",
                data: [
                    "run_id": .string(runID.uuidString),
                    "question_id": .string(questionID.uuidString),
                ]
            )
        )
        #expect(reducer.clarifyingQuestion?.question == "Which environment?")
        #expect(reducer.runStatus == .waitingForUser)

        reducer.reduce(
            SSEFrame(
                id: 5,
                event: "run.cancelled",
                data: ["run_id": .string(runID.uuidString)]
            )
        )
        #expect(reducer.clarifyingQuestion == nil)
        #expect(reducer.runStatus == .cancelled)
    }

    @Test
    func testDuplicatePersistedSequenceIsReducedOnce() {
        let reducer = RunStateReducer()
        let frame = SSEFrame(
            id: 7,
            event: "user.message.created",
            data: [
                "content": .array([
                    .object(["type": .string("text"), "text": .string("hello")]),
                ]),
            ]
        )
        reducer.reduce(frame)
        reducer.reduce(frame)
        #expect(reducer.timeline.count == 1)
    }

    @Test
    func testApprovalBindsToMostRecentMatchingUnresolvedTool() {
        let reducer = RunStateReducer()
        for (sequence, callID) in [(1, "call-1"), (2, "call-2")] {
            reducer.reduce(
                SSEFrame(
                    id: sequence,
                    event: "model.response.completed",
                    data: [
                        "conversation_items": .array([
                            .object([
                                "kind": .string("tool_call"),
                                "call_id": .string(callID),
                                "name": .string("sandbox.run_command"),
                                "arguments": .object(["argv": .array([.string(callID)])]),
                            ]),
                        ]),
                    ]
                )
            )
        }
        let approval = ApprovalView(
            id: UUID(),
            runID: UUID(),
            sessionID: UUID(),
            status: .pending,
            toolName: "sandbox.run_command",
            actionSummary: "Run a command",
            arguments: ["argv": .array([.string("call-2")])],
            risk: "high",
            policyReason: "not displayed",
            expiresAt: nil,
            createdAt: Date(),
            resolvedAt: nil,
            resolvedBy: nil,
            decision: nil
        )
        reducer.mergeApproval(approval)

        #expect(reducer.tools.first { $0.callID == "call-1" }?.approvalID == nil)
        #expect(reducer.tools.first { $0.callID == "call-2" }?.approvalID == approval.id)
        #expect(reducer.tools.first { $0.callID == "call-2" }?.status == .awaitingApproval)
        #expect(reducer.needsApprovalIDs.contains(approval.id))
    }

    @Test
    func testToolActivityKeepsItsFirstSeenPositionBetweenMessages() {
        let reducer = RunStateReducer()
        reducer.reduce(
            SSEFrame(
                id: 1,
                event: "user.message.created",
                data: [
                    "content": .array([
                        .object(["type": .string("text"), "text": .string("Start")]),
                    ]),
                ]
            )
        )
        reducer.reduce(assistantMessageFrame(id: 2, text: "I will check."))
        reducer.reduce(
            SSEFrame(
                id: 3,
                event: "tool.call.proposed",
                data: [
                    "call_id": .string("call-1"),
                    "name": .string("workspace.read_text"),
                ]
            )
        )
        reducer.reduce(assistantMessageFrame(id: 4, text: "The check is complete."))

        #expect(
            reducer.activityTimeline.map(\.id) == [
                "message:event-1",
                "message:event-2",
                "tool:call-1",
                "message:event-4",
            ]
        )

        reducer.reduce(
            SSEFrame(
                id: 5,
                event: "tool.call.completed",
                data: [
                    "call_id": .string("call-1"),
                    "name": .string("workspace.read_text"),
                ]
            )
        )

        #expect(
            reducer.activityTimeline.map(\.id) == [
                "message:event-1",
                "message:event-2",
                "tool:call-1",
                "message:event-4",
            ]
        )
        let toolStatuses: [ToolActivityStatus] = reducer.activityTimeline.compactMap { item in
            guard case .tool(let activity) = item else { return nil }
            return activity.status
        }
        #expect(toolStatuses == [.completed])
    }

    @Test
    func testSuccessiveCompletedSearchesRenderAsSingleActivity() {
        let reducer = RunStateReducer()
        for index in 1...10 {
            reducer.reduce(
                SSEFrame(
                    id: index,
                    event: "tool.call.proposed",
                    data: [
                        "call_id": .string("search-\(index)"),
                        "name": .string("web.search"),
                        "arguments": .object(["query": .string("query \(index)")]),
                    ]
                )
            )
        }
        for index in 1...10 {
            reducer.reduce(
                SSEFrame(
                    id: index + 10,
                    event: "tool.call.completed",
                    data: [
                        "call_id": .string("search-\(index)"),
                        "name": .string("web.search"),
                        "result_item": .object([
                            "content": .array([
                                .object([
                                    "type": .string("text"),
                                    "text": .string("result \(index)"),
                                ]),
                            ]),
                            "is_error": .bool(false),
                            "trust": .string("external_untrusted"),
                        ]),
                    ]
                )
            )
        }

        #expect(reducer.tools.count == 10)
        #expect(reducer.activityTimeline.count == 1)
        guard case .toolBundle(let bundle) = reducer.activityTimeline[0] else {
            Issue.record("expected completed searches to render as a bundle")
            return
        }
        #expect(bundle.summary == "10 Web.Searches Completed")
        #expect(bundle.activities.map(\.callID) == (1...10).map { "search-\($0)" })
        #expect(bundle.activities[9].arguments["query"]?.stringValue == "query 10")
        #expect(bundle.activities[9].result?.content.first?.text == "result 10")
    }

    @Test
    func testBundlingRequiresAdjacentCompletedCallsWithTheSameName() {
        let reducer = RunStateReducer()
        for (sequence, callID, name) in [
            (1, "search-1", "web.search"),
            (2, "search-2", "web.search"),
            (3, "fetch-1", "web.fetch"),
            (4, "search-3", "web.search"),
            (5, "search-4", "web.search"),
        ] {
            reducer.reduce(toolFrame(id: sequence, callID: callID, name: name))
        }

        #expect(
            reducer.activityTimeline.map(\.id) == [
                "tool:search-1",
                "tool:fetch-1",
                "tool:search-3",
            ]
        )
        let summaries = reducer.activityTimeline.compactMap { activity -> String? in
            guard case .toolBundle(let bundle) = activity else { return nil }
            return bundle.summary
        }
        #expect(summaries == ["2 Web.Searches Completed", "2 Web.Searches Completed"])
    }

    @Test
    func testCompletedToolsDoNotNeedDotQualifiedNamesToBundle() {
        let reducer = RunStateReducer()
        reducer.reduce(toolFrame(id: 1, callID: "search-1", name: "search"))
        reducer.reduce(toolFrame(id: 2, callID: "search-2", name: "search"))

        guard case .toolBundle(let bundle) = reducer.activityTimeline.first else {
            Issue.record("expected undotted same-name tools to render as a bundle")
            return
        }
        #expect(bundle.activities.map(\.callID) == ["search-1", "search-2"])
        #expect(bundle.summary == "2 Searches Completed")
    }

    @Test
    func testUnknownToolNamesRemainStandalone() {
        let reducer = RunStateReducer()
        for index in 1...2 {
            reducer.reduce(
                SSEFrame(
                    id: index,
                    event: "tool.call.completed",
                    data: ["call_id": .string("unknown-\(index)")]
                )
            )
        }

        #expect(reducer.activityTimeline.map(\.id) == ["tool:unknown-1", "tool:unknown-2"])
        #expect(reducer.tools.map(\.name) == ["tool", "tool"])
    }

    @Test
    func testBundleReportsHighestRiskAcrossItsActivities() {
        let reducer = RunStateReducer()
        reducer.reduce(
            toolFrame(id: 1, callID: "search-1", name: "web.search", risk: .low)
        )
        reducer.reduce(
            toolFrame(id: 2, callID: "search-2", name: "web.search", risk: .critical)
        )

        guard case .toolBundle(let bundle) = reducer.activityTimeline.first else {
            Issue.record("expected completed searches to render as a bundle")
            return
        }
        #expect(bundle.highestRisk == .critical)
    }

    @Test
    func testMessagesAndFailuresBreakCompletedToolBundles() {
        let reducer = RunStateReducer()
        reducer.reduce(toolFrame(id: 1, callID: "search-1", name: "web.search"))
        reducer.reduce(toolFrame(id: 2, callID: "search-2", name: "web.search"))
        reducer.reduce(assistantMessageFrame(id: 3, text: "Checking another source."))
        reducer.reduce(toolFrame(id: 4, callID: "search-3", name: "web.search"))
        reducer.reduce(toolFrame(id: 5, callID: "search-4", name: "web.search"))
        reducer.reduce(
            toolFrame(
                id: 6,
                event: "tool.call.failed",
                callID: "search-failed",
                name: "web.search"
            )
        )
        reducer.reduce(toolFrame(id: 7, callID: "search-5", name: "web.search"))
        reducer.reduce(toolFrame(id: 8, callID: "search-6", name: "web.search"))

        #expect(
            reducer.activityTimeline.map(\.id) == [
                "tool:search-1",
                "message:event-3",
                "tool:search-3",
                "tool:search-failed",
                "tool:search-5",
            ]
        )
        let failedTools = reducer.activityTimeline.compactMap { activity -> ToolActivity? in
            guard case .tool(let tool) = activity, tool.status == .failed else { return nil }
            return tool
        }
        #expect(failedTools.map(\.callID) == ["search-failed"])
    }

    @Test
    func testRetryableArgumentFailureRendersAsCorrectedAfterSuccessfulRetry() {
        let reducer = RunStateReducer()
        let failedOutcome = retryableArgumentFailureOutcome
        reducer.reduce(retryableArgumentFailureFrame(id: 1, callID: "remember-portable"))

        #expect(reducer.tools[0].status.rawValue == "needs correction")

        reducer.reduce(
            toolFrame(
                id: 2,
                callID: "remember-contextual",
                name: "memory.remember"
            )
        )

        #expect(reducer.tools.map(\.status.rawValue) == ["corrected and retried", "completed"])
        #expect(reducer.tools[0].result?.content.first?.text == failedOutcome)
    }

    @Test
    func testLaterSameToolCallDoesNotClaimAnInterruptedCorrection() {
        let reducer = RunStateReducer()
        reducer.reduce(retryableArgumentFailureFrame(id: 1, callID: "remember-portable"))
        reducer.reduce(
            toolFrame(id: 2, callID: "time", name: "system.current_time")
        )
        reducer.reduce(
            toolFrame(
                id: 3,
                callID: "remember-unrelated",
                name: "memory.remember"
            )
        )

        #expect(reducer.tools[0].status.rawValue == "needs correction")
    }

    @Test
    func testDeniedAndUncertainToolsBreakCompletedToolBundles() {
        for (event, expectedStatus) in [
            ("tool.call.denied", ToolActivityStatus.denied),
            ("tool.call.uncertain", ToolActivityStatus.uncertain),
        ] {
            let reducer = RunStateReducer()
            reducer.reduce(toolFrame(id: 1, callID: "search-1", name: "web.search"))
            reducer.reduce(toolFrame(id: 2, callID: "search-2", name: "web.search"))
            reducer.reduce(
                toolFrame(id: 3, event: event, callID: "search-boundary", name: "web.search")
            )
            reducer.reduce(toolFrame(id: 4, callID: "search-3", name: "web.search"))
            reducer.reduce(toolFrame(id: 5, callID: "search-4", name: "web.search"))

            #expect(
                reducer.activityTimeline.map(\.id) == [
                    "tool:search-1",
                    "tool:search-boundary",
                    "tool:search-3",
                ]
            )
            guard case .tool(let boundary) = reducer.activityTimeline[1] else {
                Issue.record("expected \(event) to remain a standalone activity")
                continue
            }
            #expect(boundary.status == expectedStatus)
        }
    }

    @Test
    func testApprovalBoundCompletedToolBreaksCompletedToolBundles() {
        let reducer = RunStateReducer()
        reducer.reduce(toolFrame(id: 1, callID: "search-1", name: "web.search"))
        reducer.reduce(toolFrame(id: 2, callID: "search-2", name: "web.search"))
        reducer.reduce(
            toolFrame(
                id: 3,
                event: "tool.call.proposed",
                callID: "search-approved",
                name: "web.search"
            )
        )
        let approval = ApprovalView(
            id: UUID(),
            runID: UUID(),
            sessionID: UUID(),
            status: .approved,
            toolName: "web.search",
            actionSummary: "Search the web",
            arguments: ["query": .string("approved query")],
            risk: "high",
            policyReason: "explicit approval required",
            expiresAt: nil,
            createdAt: Date(),
            resolvedAt: Date(),
            resolvedBy: "test",
            decision: .approveOnce
        )
        reducer.mergeApproval(approval)
        reducer.reduce(toolFrame(id: 4, callID: "search-approved", name: "web.search"))
        reducer.reduce(toolFrame(id: 5, callID: "search-3", name: "web.search"))
        reducer.reduce(toolFrame(id: 6, callID: "search-4", name: "web.search"))

        #expect(
            reducer.activityTimeline.map(\.id) == [
                "tool:search-1",
                "tool:search-approved",
                "tool:search-3",
            ]
        )
        guard case .tool(let approved) = reducer.activityTimeline[1] else {
            Issue.record("expected approved completed tool to remain standalone")
            return
        }
        #expect(approved.status == .completed)
        #expect(approved.approvalID == approval.id)
    }

    @Test
    func testErrorResultBreaksCompletedToolBundles() {
        let reducer = RunStateReducer()
        reducer.reduce(toolFrame(id: 1, callID: "search-1", name: "web.search"))
        reducer.reduce(toolFrame(id: 2, callID: "search-2", name: "web.search"))
        reducer.reduce(
            toolFrame(
                id: 3,
                callID: "search-error",
                name: "web.search",
                resultIsError: true
            )
        )
        reducer.reduce(toolFrame(id: 4, callID: "search-3", name: "web.search"))
        reducer.reduce(toolFrame(id: 5, callID: "search-4", name: "web.search"))

        #expect(
            reducer.activityTimeline.map(\.id) == [
                "tool:search-1",
                "tool:search-error",
                "tool:search-3",
            ]
        )
        guard case .tool(let error) = reducer.activityTimeline[1] else {
            Issue.record("expected error result to remain standalone")
            return
        }
        #expect(error.status == .completed)
        #expect(error.result?.isError == true)
    }

    @Test
    func testDuplicateCompletedToolReplayDoesNotDuplicateBundleEntries() {
        let reducer = RunStateReducer()
        let first = toolFrame(id: 1, callID: "search-1", name: "web.search")
        reducer.reduce(first)
        reducer.reduce(first)
        reducer.reduce(toolFrame(id: 2, callID: "search-2", name: "web.search"))

        #expect(reducer.tools.map(\.callID) == ["search-1", "search-2"])
        guard case .toolBundle(let bundle) = reducer.activityTimeline.first else {
            Issue.record("expected replayed completed event to preserve the adjacent bundle")
            return
        }
        #expect(bundle.activities.map(\.callID) == ["search-1", "search-2"])
    }

    private func assistantMessageFrame(id: Int, text: String) -> SSEFrame {
        SSEFrame(
            id: id,
            event: "assistant.message.completed",
            data: [
                "message": .object([
                    "kind": .string("assistant"),
                    "content": .array([
                        .object(["kind": .string("text"), "text": .string(text)]),
                    ]),
                ]),
            ]
        )
    }

    private func toolFrame(
        id: Int,
        event: String = "tool.call.completed",
        callID: String,
        name: String,
        resultIsError: Bool? = nil,
        risk: RiskLevel? = nil
    ) -> SSEFrame {
        var data: [String: JSONValue] = [
            "call_id": .string(callID),
            "name": .string(name),
        ]
        if let resultIsError {
            data["result_item"] = .object([
                "content": .array([]),
                "is_error": .bool(resultIsError),
                "trust": .string("external_untrusted"),
            ])
        }
        if let risk {
            data["risk"] = .string(risk.rawValue)
        }
        return SSEFrame(
            id: id,
            event: event,
            data: data
        )
    }

    private var retryableArgumentFailureOutcome: String {
        #"{"status":"failed","action":"memory.remember","reason_code":"tool.invalid_arguments.portability_ceiling","message":"Use contextual portability.","retryable":true,"remediation":"modify_arguments"}"#
    }

    private func retryableArgumentFailureFrame(id: Int, callID: String) -> SSEFrame {
        SSEFrame(
            id: id,
            event: "tool.call.failed",
            data: [
                "call_id": .string(callID),
                "name": .string("memory.remember"),
                "result_item": .object([
                    "content": .array([
                        .object([
                            "type": .string("text"),
                            "text": .string(retryableArgumentFailureOutcome),
                        ]),
                    ]),
                    "is_error": .bool(true),
                    "trust": .string("internal_tool"),
                ]),
            ]
        )
    }
}
