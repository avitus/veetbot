#if os(macOS)
import AppKit
import Foundation
import SwiftUI
import Testing

@testable import VeetbotCore

/// The Mac Memory browser shows every chosen profile in one detail column, and
/// SwiftUI keeps a view's state when only its arguments change in place. These
/// host the profile in a real window and change the person it was given.
@Suite(.serialized) @MainActor struct PeopleDetailViewTests {
    @Test func aProfileReusedForAnotherPersonLoadsThatPerson() async throws {
        let first = UUID(), second = UUID()
        let requests = PeopleRequestLog()
        let client = try makePeopleClient { request in
            let path = request.url?.path ?? ""
            requests.append(path)
            let id = path.hasSuffix(second.uuidString) ? second : first
            return (200, Self.profileJSON(id: id, name: id == first ? "Ada" : "Grace"))
        }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 640),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let host = NSHostingView(rootView: PeopleDetailView(personID: first, makeAPIClient: { client }))
        window.contentView = host
        window.orderFront(nil)
        defer {
            window.orderOut(nil)
            window.contentView = nil
        }
        #expect(await requests.eventually { $0.contains("/v1/people/\(first.uuidString)") })

        host.rootView = PeopleDetailView(personID: second, makeAPIClient: { client })

        #expect(
            await requests.eventually { $0.contains("/v1/people/\(second.uuidString)") },
            "The profile kept the first person's model after being given another person"
        )
    }

    private static func profileJSON(id: UUID, name: String) -> String {
        """
        {"person":{"id":"\(id)","revision":1,"display_name":"\(name)","state":"active","pinned":false,"sensitivity":"sensitive","support_ids":[]},"aliases":[],"relationships":[],"history":[],"commitments":[],"facts":[],"fact_revisions":{},"related_labels":{},"truncated":false,"coverage":"Owner history"}
        """
    }
}

/// Records request paths from the URL protocol's thread for the main-actor test.
final class PeopleRequestLog: @unchecked Sendable {
    private let lock = NSLock()
    private var paths: [String] = []

    func append(_ path: String) { lock.withLock { paths.append(path) } }

    /// Polls for up to three seconds while SwiftUI renders and loads.
    @MainActor func eventually(_ condition: ([String]) -> Bool) async -> Bool {
        for _ in 0..<60 {
            if condition(lock.withLock { paths }) { return true }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        return condition(lock.withLock { paths })
    }
}
#endif
