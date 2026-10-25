import Foundation
import Testing
import WebKit

@testable import VeetbotCore

/// docs/plan/cockpit.md, gate 18 and gate 17's handler half: the scheme
/// handler answers only content hashes the current snapshot names, fetches
/// them natively, hands web content no credential, and answers 404 otherwise.
@MainActor
@Suite struct CockpitSchemeHandlerTests {
    final class FakeTask: NSObject, WKURLSchemeTask {
        let request: URLRequest
        var response: URLResponse?
        var body = Data()
        var finished = false
        var failure: Error?

        init(_ url: String) { request = URLRequest(url: URL(string: url)!) }

        func didReceive(_ response: URLResponse) { self.response = response }
        func didReceive(_ data: Data) { body.append(data) }
        func didFinish() { finished = true }
        func didFailWithError(_ error: Error) { failure = error }

        var status: Int? { (response as? HTTPURLResponse)?.statusCode }
        var headers: [String: String] {
            ((response as? HTTPURLResponse)?.allHeaderFields as? [String: String]) ?? [:]
        }
    }

    final class Fetches: @unchecked Sendable {
        private let lock = NSLock()
        private var _digests: [String] = []
        var digests: [String] { lock.withLock { _digests } }
        func record(_ digest: String) { lock.withLock { _digests.append(digest) } }
    }

    static let bundle = String(repeating: "d", count: 64)
    static let other = String(repeating: "9", count: 64)

    func handler(_ fetches: Fetches, delay: UInt64 = 0) -> CockpitSchemeHandler {
        let handler = CockpitSchemeHandler { digest in
            fetches.record(digest)
            if delay > 0 { try await Task.sleep(nanoseconds: delay) }
            return CockpitAssetData(content: Data("(()=>{})();".utf8), contentType: "text/javascript; charset=utf-8")
        }
        handler.allowed = [Self.bundle]
        return handler
    }

    func settle(_ condition: () -> Bool) async {
        for _ in 0..<1000 where !condition() { try? await Task.sleep(nanoseconds: 1_000_000) }
    }

    @Test
    func aNamedHashIsFetchedNativelyAndServedWithoutCredentials() async {
        let fetches = Fetches()
        let schemes = handler(fetches)
        let task = FakeTask("veetbot-cockpit://board/\(Self.bundle).js")
        schemes.start(task)
        await settle { task.finished }
        #expect(task.status == 200)
        #expect(task.body == Data("(()=>{})();".utf8))
        #expect(task.headers["Content-Type"] == "text/javascript; charset=utf-8")
        let names = Set(task.headers.keys.map { $0.lowercased() })
        #expect(names.isDisjoint(with: ["authorization", "cookie", "set-cookie", "access-control-allow-origin"]))
        #expect(fetches.digests == [Self.bundle])
        // Immutable by its hash: a second load is served from memory.
        let again = FakeTask("veetbot-cockpit://board/\(Self.bundle).js")
        schemes.start(again)
        await settle { again.finished }
        #expect(again.status == 200)
        #expect(fetches.digests == [Self.bundle])
    }

    @Test
    func anythingElseIs404AndNeverReachesTheServer() async {
        let fetches = Fetches()
        let schemes = handler(fetches)
        for url in [
            "veetbot-cockpit://board/\(Self.other).js",
            "veetbot-cockpit://board/v1/cockpit",
            "veetbot-cockpit://board/../v1/cockpit/assets/\(Self.bundle)",
            "veetbot-cockpit://board/\(Self.bundle).js?x=1",
        ] {
            let task = FakeTask(url)
            schemes.start(task)
            #expect(task.status == 404, "\(url)")
            #expect(task.finished, "\(url)")
        }
        #expect(fetches.digests.isEmpty)
    }

    @Test
    func aStoppedLoadReceivesNothingAfterItsStop() async {
        let fetches = Fetches()
        let schemes = handler(fetches, delay: 50_000_000)
        let task = FakeTask("veetbot-cockpit://board/\(Self.bundle).js")
        schemes.start(task)
        schemes.stop(task)
        try? await Task.sleep(nanoseconds: 150_000_000)
        #expect(task.response == nil)
        #expect(!task.finished)
    }
}
