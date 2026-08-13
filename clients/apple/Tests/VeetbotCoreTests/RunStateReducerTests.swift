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
}
