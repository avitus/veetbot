#if os(macOS)
import AppKit
import SwiftUI
import Testing

@testable import VeetbotCore

/// A docked card stays where the owner is about to tap while the run's
/// activity line comes and goes beneath it.
@Suite(.serialized) @MainActor struct ChatDockLayoutTests {
    /// Answering one approval of several resumes the run, which parks again on
    /// the next: the line appears and goes under the card still waiting.
    @Test
    func theActivityLineLeavesADockedApprovalWhereItIs() async throws {
        let (model, host) = try chat()
        let approval = try WireModelsTests.contractApproval("offered")
        model.runState.begin(runID: approval.runID, status: .waitingForApproval)
        model.runState.mergeApproval(approval)

        try await expectDockHolds(model: model, host: host, runID: approval.runID, parked: .waitingForApproval)
    }

    @Test
    func theActivityLineLeavesADockedQuestionWhereItIs() async throws {
        let (model, host) = try chat()
        let runID = UUID()
        model.runState.begin(runID: runID, status: .waitingForUser)
        model.runState.reduce(
            SSEFrame(
                id: 1,
                event: "run.waiting_for_user",
                data: [
                    "run_id": .string(runID.uuidString),
                    "question_id": .string(UUID().uuidString),
                    "question": .string("Which day?"),
                ]
            )
        )
        #expect(model.runState.clarifyingQuestion != nil)

        try await expectDockHolds(model: model, host: host, runID: runID, parked: .waitingForUser)
    }

    /// The row holds exactly the line's height for a docked card, and with
    /// nothing docked it takes no room or stack spacing while the line is away.
    @Test
    func theActivityRowOnlyHoldsItsPlaceForADockedCard() {
        let empty = dockHeight(ChatActivityRow(label: nil, holdsPlace: false))
        let held = dockHeight(ChatActivityRow(label: nil, holdsPlace: true))
        let working = dockHeight(ChatActivityRow(label: "Working…", holdsPlace: true))

        #expect(empty == 28)
        #expect(held > empty)
        #expect(held == working)
    }

    /// The row's height between two others, as the dock's stack lays it out.
    private func dockHeight(_ row: ChatActivityRow) -> CGFloat {
        NSHostingView(
            rootView: VStack(spacing: 8) {
                Color.clear.frame(height: 10)
                row
                Color.clear.frame(height: 10)
            }
            .frame(width: 400)
        ).fittingSize.height
    }

    private func expectDockHolds(
        model: ChatViewModel, host: NSHostingView<ChatView>, runID: UUID, parked: RunStatus
    ) async throws {
        await settle(host)
        let waiting = try #require(dockFrame(in: host))
        try #require(waiting.height > 40, "The card must be laid out: \(waiting)")

        model.runState.begin(runID: runID, status: .running)
        await settle(host)
        let working = try #require(dockFrame(in: host))
        #expect(abs(working.minY - waiting.minY) < 1 && abs(working.maxY - waiting.maxY) < 1,
                "The line appearing must not move the card: \(waiting) -> \(working)")

        model.runState.begin(runID: runID, status: parked)
        await settle(host)
        let parkedAgain = try #require(dockFrame(in: host))
        #expect(abs(parkedAgain.minY - waiting.minY) < 1 && abs(parkedAgain.maxY - waiting.maxY) < 1,
                "The line going must not move the card: \(working) -> \(parkedAgain)")
    }

    private func chat() throws -> (ChatViewModel, NSHostingView<ChatView>) {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore()
        )
        let host = NSHostingView(rootView: ChatView(model: model))
        host.frame = NSRect(x: 0, y: 0, width: 900, height: 1000)
        host.layoutSubtreeIfNeeded()
        return (model, host)
    }

    /// The dock measures its cards on a later main-queue turn.
    private func settle(_ host: NSHostingView<ChatView>) async {
        for _ in 0..<8 {
            await Task.yield()
            RunLoop.main.run(until: Date().addingTimeInterval(0.02))
            host.layoutSubtreeIfNeeded()
        }
    }

    /// Among the scroll views other than the composer's, the transcript is
    /// the tallest and the dock that hugs its cards the next.
    private func dockFrame(in host: NSView) -> NSRect? {
        let composerScroll = findComposer(in: host)?.enclosingScrollView
        let panes = scrollViews(in: host)
            .filter { $0 !== composerScroll }
            .sorted { $0.frame.height > $1.frame.height }
        guard panes.count > 1 else { return nil }
        return host.convert(panes[1].bounds, from: panes[1])
    }

    private func scrollViews(in view: NSView) -> [NSScrollView] {
        let nested = view.subviews.flatMap { scrollViews(in: $0) }
        if let scroll = view as? NSScrollView { return [scroll] + nested }
        return nested
    }

    private func findComposer(in view: NSView) -> ComposerNSTextView? {
        if let composer = view as? ComposerNSTextView { return composer }
        return view.subviews.lazy.compactMap { findComposer(in: $0) }.first
    }
}
#endif
