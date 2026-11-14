import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0324: a follow-up sent while a run works waits in the transcript, then
/// moves to where the run read it.
@MainActor
@Suite
struct RunSteeringReducerTests {
    private let first = UUID(uuidString: "00000000-0000-0000-0000-0000000000a1")!
    private let second = UUID(uuidString: "00000000-0000-0000-0000-0000000000a2")!

    private func userFrame(
        id: Int, text: String, extra: [String: JSONValue] = [:]
    ) -> SSEFrame {
        var data: [String: JSONValue] = [
            "content": .array([.object(["type": .string("text"), "text": .string(text)])]),
        ]
        data.merge(extra) { _, new in new }
        return SSEFrame(id: id, event: "user.message.created", data: data)
    }

    private func answerFrame(id: Int, text: String) -> SSEFrame {
        SSEFrame(
            id: id,
            event: "assistant.message.completed",
            data: [
                "message": .object([
                    "kind": .string("assistant"),
                    "content": .array([.object(["kind": .string("text"), "text": .string(text)])]),
                ]),
            ]
        )
    }

    private func messageIDs(_ reducer: RunStateReducer) -> [String] {
        reducer.activityTimeline.compactMap { activity in
            if case .message(let item) = activity { return item.id }
            return nil
        }
    }

    @Test
    func testAHeldFollowUpWaitsThenMovesToWhereTheRunReadIt() {
        let reducer = RunStateReducer()
        reducer.reduce(userFrame(id: 2, text: "How long is it?"))
        reducer.beginFollowUp(content: [.text("Use metric units.")], submissionID: "key-1")
        reducer.confirmFollowUp(submissionID: "key-1", pendingInputID: first)

        #expect(reducer.hasUnreadFollowUps)
        #expect(reducer.timeline.map(\.isAwaitingRead) == [false, true])
        #expect(reducer.timeline.last?.text == "Use metric units.")

        // Work the run did before reading it stays above the follow-up.
        reducer.reduce(answerFrame(id: 5, text: "Measuring…"))
        reducer.reduce(
            userFrame(
                id: 7, text: "Use metric units.",
                extra: ["pending_input_id": .string(first.uuidString.lowercased())]
            )
        )

        #expect(!reducer.hasUnreadFollowUps)
        #expect(messageIDs(reducer) == ["event-2", "event-5", "event-7"])
        #expect(reducer.timeline.allSatisfy { !$0.isAwaitingRead })
        #expect(reducer.timeline.filter { $0.text == "Use metric units." }.count == 1)
    }

    @Test
    func testAReadThatArrivesBeforeTheAcceptanceLeavesOneBubble() {
        let reducer = RunStateReducer()
        reducer.reduce(userFrame(id: 2, text: "How long is it?"))
        reducer.beginFollowUp(content: [.text("Use metric units.")], submissionID: "key-1")
        reducer.reduce(
            userFrame(
                id: 7, text: "Use metric units.",
                extra: ["pending_input_id": .string(first.uuidString.lowercased())]
            )
        )
        reducer.confirmFollowUp(submissionID: "key-1", pendingInputID: first)

        #expect(messageIDs(reducer) == ["event-2", "event-7"])
        #expect(!reducer.hasUnreadFollowUps)
    }

    @Test
    func testAnOrdinaryMessageNeverClaimsAWaitingFollowUp() {
        let reducer = RunStateReducer()
        reducer.beginFollowUp(content: [.text("Use metric units.")], submissionID: "key-1")
        reducer.confirmFollowUp(submissionID: "key-1", pendingInputID: first)
        reducer.reduce(userFrame(id: 9, text: "An answer to a question"))

        #expect(reducer.hasUnreadFollowUps)
        #expect(reducer.timeline.map(\.text).sorted() == ["An answer to a question", "Use metric units."])
    }

    @Test
    func testFollowUpsCarriedToTheNextRunBecomeItsOneMessage() {
        let reducer = RunStateReducer()
        reducer.reduce(userFrame(id: 2, text: "How long is it?"))
        reducer.beginFollowUp(content: [.text("Typed before Stop.")], submissionID: "key-1")
        reducer.confirmFollowUp(submissionID: "key-1", pendingInputID: first)
        reducer.beginFollowUp(content: [.text("Do this instead.")], submissionID: "key-2")
        reducer.confirmFollowUp(submissionID: "key-2", pendingInputID: second)

        reducer.reduce(
            userFrame(
                id: 12, text: "Do this instead.",
                extra: ["pending_input_ids": .array([.string(second.uuidString.lowercased())])]
            )
        )

        // The carried one is now the next run's message; the other still waits
        // for the view model to return it to the composer.
        #expect(messageIDs(reducer).contains("event-12"))
        #expect(reducer.timeline.filter { $0.text == "Do this instead." }.count == 1)
        #expect(reducer.dropUnreadFollowUps() == ["Typed before Stop."])
        #expect(messageIDs(reducer) == ["event-2", "event-12"])
        #expect(!reducer.hasUnreadFollowUps)
    }

    @Test
    func testARefusedFollowUpLeavesNoBubble() {
        let reducer = RunStateReducer()
        reducer.beginFollowUp(content: [.text("Use metric units.")], submissionID: "key-1")
        reducer.discardFollowUp(submissionID: "key-1")

        #expect(reducer.timeline.isEmpty)
        #expect(reducer.activityTimeline.isEmpty)
        #expect(!reducer.hasUnreadFollowUps)
    }

    @Test
    func testAFollowUpTheServerTurnedIntoANewRunBecomesItsPendingMessage() {
        let reducer = RunStateReducer()
        reducer.beginFollowUp(content: [.text("Use metric units.")], submissionID: "key-1")
        reducer.adoptFollowUpAsMessage(submissionID: "key-1")

        #expect(!reducer.hasUnreadFollowUps)
        #expect(reducer.timeline.map(\.isAwaitingRead) == [false])
        // The new run's first message replaces it, as for any optimistic bubble.
        reducer.reduce(userFrame(id: 3, text: "Use metric units."))
        #expect(messageIDs(reducer) == ["event-3"])
    }

    @Test
    func testWireShapesCarryTheFollowUpIdentifierAndTolerateOlderServers() throws {
        let held = try JSONDecoder.server.decode(
            SubmitResult.self,
            from: Data(
                #"{"run_id":"00000000-0000-0000-0000-000000000456","status":"RUNNING","pending_input_id":"00000000-0000-0000-0000-0000000000a1"}"#
                    .utf8)
        )
        #expect(held.pendingInputID == first)
        let started = try JSONDecoder.server.decode(
            SubmitResult.self,
            from: Data(#"{"run_id":"00000000-0000-0000-0000-000000000456","status":"QUEUED"}"#.utf8)
        )
        #expect(started.pendingInputID == nil)

        let run = #"{"id":"00000000-0000-0000-0000-000000000456","session_id":"00000000-0000-0000-0000-000000000123","parent_run_id":null,"status":"RUNNING","step_count":0,"model_call_count":0,"tool_call_count":0,"usage":{"input_tokens":0,"output_tokens":0,"cost_usd":"0"},"limits":{"max_steps":8,"deadline_at":null,"max_cost_usd":null},"failure":null,"cancel_requested_at":null,"created_at":"2026-10-07T15:33:00Z","updated_at":"2026-10-07T15:46:00Z""#
        let older = try JSONDecoder.server.decode(RunView.self, from: Data((run + "}").utf8))
        #expect(older.pendingInputs == nil)
        let waiting = try JSONDecoder.server.decode(
            RunView.self,
            from: Data(
                (run
                    + #","pending_inputs":[{"id":"00000000-0000-0000-0000-0000000000a1","content":[{"type":"text","text":"Use metric units."}],"created_at":"2026-10-07T15:40:00Z"}]}"#)
                    .utf8)
        )

        // A reopened chat shows what is still waiting, once.
        let reducer = RunStateReducer()
        reducer.seed(run: waiting)
        reducer.seed(run: waiting)
        #expect(reducer.timeline.map(\.text) == ["Use metric units."])
        #expect(reducer.timeline.first?.isAwaitingRead == true)
        #expect(reducer.hasUnreadFollowUps)
    }
}
