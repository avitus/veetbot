import Foundation
import Testing
@testable import VeetbotCore

@MainActor
@Suite struct RunStateReducerTests {
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
        name: String
    ) -> SSEFrame {
        SSEFrame(
            id: id,
            event: event,
            data: [
                "call_id": .string(callID),
                "name": .string(name),
            ]
        )
    }
}
