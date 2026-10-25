import CryptoKit
import Foundation
import Testing

@testable import VeetbotCore

/// The Cockpit snapshot the server renders (tests/fixtures/api/cockpit_snapshot_contract.json).
@Suite struct CockpitWireTests {
    /// SHA-256 of `tests/fixtures/api/cockpit_snapshot_contract.json`. The
    /// Swift copy must be that file byte for byte; update both together.
    static let snapshotContractChecksum =
        "52b8064295b30acafb962bd679a81573c8c5b1160e81b7de6afe09d6dac113cc"

    static func contract() throws -> Data {
        let url = try #require(Bundle.module.url(
            forResource: "cockpit_snapshot_contract", withExtension: "json", subdirectory: "Fixtures"
        ))
        return try Data(contentsOf: url)
    }

    static func sha256(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    @Test
    func theFixtureIsTheServersCopy() throws {
        #expect(Self.sha256(try Self.contract()) == Self.snapshotContractChecksum)
        let repositoryCopy = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/api/cockpit_snapshot_contract.json")
        if let canonical = try? Data(contentsOf: repositoryCopy) {
            #expect(Self.sha256(canonical) == Self.snapshotContractChecksum)
        }
    }

    @Test
    func theRenderedSnapshotDecodesIntoTheBoard() throws {
        let snapshot = try JSONDecoder.server.decode(CockpitSnapshot.self, from: try Self.contract())
        let serverTime = try #require(snapshot.serverTime)
        #expect(serverTime == ISO8601DateFormatter().date(from: "2026-10-24T17:00:00Z"))
        let runtime = try #require(snapshot.runtime)
        #expect(runtime.kitAPI == 1)
        #expect(runtime.entry == runtime.assets["host.html"])
        let widget = try #require(snapshot.widgets.first)
        #expect(widget.slug == "weather")
        #expect(widget.title == "Weather")
        #expect(widget.sizes == ["sm", "md"])
        #expect(widget.declaredFeeds == [CockpitDeclaredFeed(id: "agent.weather", history: 2)])
        #expect(widget.settings == ["place": .string("Example City")])
        #expect(widget.changedAt == serverTime)
        #expect(snapshot.layouts[.wide] == CockpitLayout(revision: 1, placements: [CockpitPlacement(slug: "weather", size: "md")]))
        let feed = try #require(snapshot.feeds["agent.weather"])
        #expect(feed.trust == "untrusted")
        #expect(feed.current?.data == .object(["temperature": .number(62)]))
        #expect(feed.history.count == 2)
        #expect(feed.current?.freshness(at: serverTime.addingTimeInterval(20 * 60)) == .live)
        #expect(feed.current?.freshness(at: serverTime.addingTimeInterval(30 * 60)) == .aging)
        #expect(feed.current?.freshness(at: serverTime.addingTimeInterval(50 * 60)) == .stale)
        #expect(CockpitBoard.allowedAssets(snapshot).count == 4)
    }
}
