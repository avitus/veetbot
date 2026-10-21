import Foundation
@testable import VeetbotCore

/// Executes the app's actual UI-test transport with the normal launch argument.
@main
struct EmailCollectionProbe {
    @MainActor static func main() async throws {
        let api = ConversationNavigationUITestFixture.makeMemoryAPIClientIfRequested()!
        if ProcessInfo.processInfo.arguments.contains("--probe-persona") {
            let model = PersonaViewModel(makeAPIClient: { api })
            await model.load()
            precondition(model.version == 1 && model.errorMessage == nil, "Fixture must load Persona")
            precondition(model.toneSupported && !model.voice.isEmpty)
            precondition(model.boundaries.count == 2)
            precondition(model.nominations.isEmpty)
            print("Persona fixture probe passed")
            return
        }
        let filtered = try await api.emailThreads(accountID: "missing", view: "inbox")
        precondition(filtered.items.isEmpty && filtered.collectionCounts?.inbox == 0)
        if ProcessInfo.processInfo.arguments.contains("--ui-testing-email-full-inbox") {
            let first = try await api.emailThreads(view: "inbox", limit: 2)
            precondition(first.items.count == 2 && first.collectionCounts?.inbox == 5)
            precondition(first.nextCursor == "2")
            let second = try await api.emailThreads(view: "inbox", limit: 2, cursor: first.nextCursor)
            precondition(second.items.count == 2 && second.collectionCounts?.inbox == 5)
            precondition(Set(first.items.map(\.id)).isDisjoint(with: second.items.map(\.id)))
            print("Email collection fixture pagination probe passed")
            return
        }
        let count = ProcessInfo.processInfo.arguments.contains("--ui-testing-email-archive-next") ? 2 : 1
        let inbox = try await api.emailThreads(view: "inbox")
        precondition(inbox.items.count == count)
        precondition(inbox.collectionCounts?.inbox == count, "Fixture must return collection counts")
        let row = inbox.items[0]
        _ = try await api.dismissEmailThread(row, idempotencyKey: "handle")
        let handled = try await api.emailThreads(view: "handled")
        precondition(handled.items.map(\.id) == [row.id])
        precondition(handled.collectionCounts?.handled == 1)
        let stillInInbox = try await api.emailThreads(view: "inbox")
        precondition(stillInInbox.items.count == count, "Handled is not archived")
        let needs = try await api.emailThreads(view: "needs_you")
        precondition(needs.items.isEmpty)
        _ = try await api.dismissEmailThread(row, dismissed: false, idempotencyKey: "undo")
        let reopened = try await api.emailThreads(view: "needs_you")
        precondition(reopened.items.count == 1)
        print("Email collection fixture probe passed")
    }
}
