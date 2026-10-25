import Foundation
import Testing

@testable import VeetbotCore

/// docs/plan/cockpit.md, `CockpitViewModel`: the snapshot, then the stream,
/// held only while Cockpit is visible (gate 21), reconnecting without a give-up
/// limit, refetching on every change and on `stream.reset`.
@MainActor
@Suite(.serialized) struct CockpitViewModelTests {
    final class FakeClient: CockpitBoardClient, @unchecked Sendable {
        private let lock = NSLock()
        private var _snapshots: [Result<CockpitSnapshot, Error>] = []
        private var _streams: [[Result<SSEFrame, Error>]] = []
        private var _openedAfter: [Int?] = []
        private var _snapshotLoads = 0
        private var _openStreams = 0
        var holdStreamOpen = true

        func queueSnapshot(_ result: Result<CockpitSnapshot, Error>) { lock.withLock { _snapshots.append(result) } }
        func queueStream(_ frames: [Result<SSEFrame, Error>]) { lock.withLock { _streams.append(frames) } }
        var openedAfter: [Int?] { lock.withLock { _openedAfter } }
        var snapshotLoads: Int { lock.withLock { _snapshotLoads } }
        var openStreams: Int { lock.withLock { _openStreams } }

        func cockpitSnapshot() async throws -> CockpitSnapshot {
            let next: Result<CockpitSnapshot, Error> = lock.withLock {
                _snapshotLoads += 1
                return _snapshots.count > 1 ? _snapshots.removeFirst() : (_snapshots.first ?? .failure(URLError(.badServerResponse)))
            }
            return try next.get()
        }

        func cockpitEvents(after cursor: Int?) -> AsyncThrowingStream<SSEFrame, Error> {
            let frames: [Result<SSEFrame, Error>] = lock.withLock {
                _openedAfter.append(cursor)
                return _streams.isEmpty ? [] : _streams.removeFirst()
            }
            let hold = holdStreamOpen && frames.allSatisfy { if case .success = $0 { true } else { false } }
            return AsyncThrowingStream { continuation in
                lock.withLock { _openStreams += 1 }
                let task = Task {
                    for frame in frames {
                        switch frame {
                        case .success(let value): continuation.yield(value)
                        case .failure(let error): continuation.finish(throwing: error); return
                        }
                    }
                    if hold {
                        while !Task.isCancelled { try? await Task.sleep(nanoseconds: 1_000_000) }
                    }
                    continuation.finish()
                }
                continuation.onTermination = { [weak self] _ in
                    task.cancel()
                    self?.lock.withLock { self?._openStreams -= 1 }
                }
            }
        }

        func cockpitAsset(_ digest: String) async throws -> CockpitAssetData {
            CockpitAssetData(content: Data(), contentType: "text/plain")
        }
        private var _calls: [String] = []
        var calls: [String] { lock.withLock { _calls } }
        private func record(_ call: String) { lock.withLock { _calls.append(call) } }
        var failWrites = false

        var detailDelayNanoseconds: UInt64 = 0

        func cockpitWidget(_ slug: String) async throws -> CockpitWidgetDetail {
            record("detail \(slug)")
            if detailDelayNanoseconds > 0 { try? await Task.sleep(nanoseconds: detailDelayNanoseconds) }
            return try JSONDecoder.server.decode(CockpitWidgetDetail.self, from: Data("""
            {"slug": "\(slug)", "state": "active", "live_revision_id": null, "settings_schema": null,
             "settings": {}, "revisions": [], "import_failures": []}
            """.utf8))
        }
        func putCockpitSettings(_ slug: String, values: [String: JSONValue]) async throws {
            if failWrites { throw HTTPTransportError.api(APIError(code: .malformedRequest, message: "The settings do not fit.", requestID: "r", statusCode: 400)) }
            record("settings \(slug) \(values.keys.sorted())")
        }
        func putCockpitLayout(
            _ deviceClass: CockpitDeviceClass, placements: [CockpitPlacement], expectedRevision: Int?
        ) async throws -> CockpitLayout {
            record("layout \(deviceClass.rawValue) \(placements.map(\.slug)) after \(expectedRevision.map(String.init) ?? "none")")
            return CockpitLayout(revision: (expectedRevision ?? 0) + 1, placements: placements)
        }
        func revertCockpitWidget(_ slug: String, to revision: UUID) async throws -> CockpitChange {
            record("revert \(slug)")
            return CockpitChange(slug: slug, operation: "write", state: "pending_commit", revisionID: revision, diagnostics: [])
        }
        func deleteCockpitWidget(_ slug: String) async throws -> CockpitChange {
            record("delete \(slug)")
            return CockpitChange(slug: slug, operation: "delete", state: "queued", revisionID: nil, diagnostics: [])
        }
        func restoreCockpitWidget(_ slug: String) async throws -> CockpitChange {
            record("restore \(slug)")
            return CockpitChange(slug: slug, operation: "write", state: "rejected", revisionID: nil, diagnostics: [])
        }
    }

    final class Sleeps: @unchecked Sendable {
        private let lock = NSLock()
        private var _delays: [Double] = []
        var delays: [Double] { lock.withLock { _delays } }
        func record(_ seconds: Double) { lock.withLock { _delays.append(seconds) } }
    }

    static let now = Date(timeIntervalSince1970: 1_793_000_000)

    static func snapshot(cursor: Int, revision: Int = 1) -> CockpitSnapshot {
        CockpitSnapshot(
            cursor: cursor, serverTime: now.addingTimeInterval(2), runtime: nil,
            widgets: [
                CockpitWidget(
                    slug: "weather",
                    revisionID: UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", revision))!,
                    bundleSHA256: String(repeating: "d", count: 64), manifest: [:], changedAt: now
                ),
            ]
        )
    }

    static func notFound() -> Error {
        HTTPTransportError.api(APIError(code: .notFound, message: "Not found", requestID: "r", statusCode: 404))
    }

    func model(_ client: FakeClient, sleeps: Sleeps = Sleeps()) -> CockpitViewModel {
        CockpitViewModel(
            makeClient: { client }, now: { Self.now },
            sleep: { seconds in sleeps.record(seconds); await Task.yield() }
        )
    }

    func eventually(_ condition: @MainActor () -> Bool) async {
        for _ in 0..<2000 where !condition() { try? await Task.sleep(nanoseconds: 1_000_000) }
        #expect(condition())
    }

    @Test
    func becomingVisibleLoadsTheBoardAndStreamsAfterItsCursorUntilHidden() async {
        let client = FakeClient()
        client.queueSnapshot(.success(Self.snapshot(cursor: 41)))
        client.queueStream([])
        let board = model(client)
        #expect(client.openStreams == 0)
        board.setVisible(true)
        await eventually { client.openStreams == 1 && board.connection == .live }
        #expect(board.snapshot?.cursor == 41)
        #expect(client.openedAfter == [41])
        #expect(board.availability == .available)
        #expect(board.offsetMilliseconds == 2000)
        board.setVisible(false)
        await eventually { client.openStreams == 0 }
        #expect(board.connection == .idle)
        // Visible again: the stream reopens; nothing streams while hidden.
        client.queueStream([])
        board.setVisible(true)
        await eventually { client.openStreams == 1 }
        board.setVisible(false)
        await eventually { client.openStreams == 0 }
    }

    @Test
    func aServerWithoutTheRoutesIsUnavailableAndNothingStreams() async {
        let client = FakeClient()
        client.queueSnapshot(.failure(Self.notFound()))
        let board = model(client)
        board.setVisible(true)
        await eventually { board.availability == .unavailable }
        #expect(client.openedAfter.isEmpty)
        #expect(board.connection == .idle)
        board.setVisible(false)
    }

    @Test
    func everyChangeRefetchesTheBoardAndAResetReopensFromTheNewCursor() async {
        let client = FakeClient()
        client.queueSnapshot(.success(Self.snapshot(cursor: 41)))
        client.queueSnapshot(.success(Self.snapshot(cursor: 42, revision: 2)))
        client.queueSnapshot(.success(Self.snapshot(cursor: 90, revision: 3)))
        client.queueStream([
            .success(SSEFrame(id: 42, event: "widget.revised", data: ["subject": .string("weather")])),
            .success(SSEFrame(id: nil, event: "stream.reset", data: [:])),
        ])
        client.queueStream([])
        let board = model(client)
        board.setVisible(true)
        await eventually { client.openedAfter == [41, 90] }
        #expect(client.snapshotLoads == 3)
        #expect(board.snapshot?.cursor == 90)
        board.setVisible(false)
        await eventually { client.openStreams == 0 }
    }

    @Test
    func aFailingStreamReconnectsWithCappedBackoffAndNeverGivesUp() async {
        let client = FakeClient()
        client.queueSnapshot(.success(Self.snapshot(cursor: 41)))
        for _ in 0..<12 { client.queueStream([.failure(URLError(.networkConnectionLost))]) }
        client.queueStream([])
        let sleeps = Sleeps()
        let board = model(client, sleeps: sleeps)
        board.setVisible(true)
        await eventually { client.openedAfter.count == 13 && board.connection == .live }
        #expect(sleeps.delays == [1, 2, 4, 8, 16, 32, 60, 60, 60, 60, 60, 60])
        board.setVisible(false)
        await eventually { client.openStreams == 0 }
    }

    @Test
    func aQuarantinedWidgetStaysOutUntilItsNextRevisionOrARetry() async {
        let client = FakeClient()
        client.queueSnapshot(.success(Self.snapshot(cursor: 41)))
        client.queueStream([])
        let board = model(client)
        board.setVisible(true)
        await eventually { board.snapshot != nil }
        board.hostReported(.quarantined(slug: "weather", revision: "anything"))
        #expect(board.quarantined == ["weather"])
        board.retry("weather")
        #expect(board.quarantined.isEmpty)
        board.quarantineNewest()
        #expect(board.quarantined == ["weather"])
        board.apply(Self.snapshot(cursor: 43, revision: 2))
        #expect(board.quarantined.isEmpty)
        board.hostReported(.select(slug: "weather"))
        #expect(board.selectedSlug == "weather")
        board.setVisible(false)
    }

    @Test
    func theOwnersChangesGoThroughTheRoutesAndRefreshTheBoard() async {
        let client = FakeClient()
        client.queueSnapshot(.success(CockpitSnapshot(
            cursor: 41, runtime: nil, widgets: Self.snapshot(cursor: 41).widgets,
            layouts: [.wide: CockpitLayout(revision: 3, placements: [CockpitPlacement(slug: "weather", size: "md")])]
        )))
        client.queueStream([])
        let board = model(client)
        board.setVisible(true)
        await eventually { board.snapshot != nil }
        await board.openDetail("weather")
        #expect(board.detail?.slug == "weather")
        await board.saveSettings("weather", values: ["place": .string("Harbor")])
        await board.saveLayout(.wide, placements: [])
        await board.revert("weather", to: UUID(uuidString: "00000000-0000-0000-0000-000000000009")!)
        await board.delete("weather")
        #expect(board.actionMessage == "Removing weather. It leaves the board once its commit lands.")
        await board.restore("weather")
        #expect(board.actionMessage == "The restored weather was rejected; its diagnostics are in the history.")
        #expect(client.calls == [
            "detail weather",
            "settings weather [\"place\"]", "detail weather",
            "layout wide [] after 3",
            "revert weather", "detail weather",
            "delete weather", "detail weather",
            "restore weather", "detail weather",
        ])
        client.failWrites = true
        await board.saveSettings("weather", values: ["place": .string("x")])
        #expect(board.errorMessage == "The settings do not fit.")
        board.setVisible(false)
    }

    @Test
    func anOpenIntentNavigatesOnlyToADeliveredIdentifier() async {
        let delivered = UUID(uuidString: "00000000-0000-4000-8000-0000000000b1")!
        let base = Self.snapshot(cursor: 41)
        let widget = CockpitWidget(
            slug: "approvals", revisionID: UUID(), bundleSHA256: String(repeating: "e", count: 64),
            manifest: ["feeds": .array([.object(["id": .string("veetbot.approvals.pending")])])]
        )
        let client = FakeClient()
        client.queueSnapshot(.success(CockpitSnapshot(
            cursor: base.cursor, runtime: nil, widgets: base.widgets + [widget],
            feeds: ["veetbot.approvals.pending": CockpitFeed(current: CockpitFeedEntry(
                observedAt: Self.now, expectedIntervalSeconds: 300,
                data: .object(["items": .array([.object([
                    "id": .string(delivered.uuidString.lowercased()),
                    "sessionId": .string(delivered.uuidString.lowercased()),
                ])])])
            ))]
        )))
        client.queueStream([])
        let board = model(client)
        var opened: [CockpitDestination] = []
        board.onOpen = { opened.append($0) }
        board.setVisible(true)
        await eventually { board.snapshot != nil }
        board.hostReported(.open(slug: "approvals", kind: .approval, id: delivered.uuidString.lowercased()))
        board.hostReported(.open(slug: "approvals", kind: .session, id: delivered.uuidString.lowercased()))
        board.hostReported(.open(slug: "approvals", kind: .emailThread, id: UUID().uuidString.lowercased()))
        board.hostReported(.open(slug: "weather", kind: .session, id: delivered.uuidString.lowercased()))
        board.hostReported(.open(slug: "approvals", kind: .schedule, id: "not-a-uuid"))
        #expect(opened == [.approval(delivered), .session(delivered)])
        board.setVisible(false)
    }

    @Test
    func aNewConnectionForgetsTheOldDetailAndMessagesAndIgnoresLateAnswers() async {
        let client = FakeClient()
        client.queueSnapshot(.success(Self.snapshot(cursor: 41)))
        client.queueStream([])
        let board = model(client)
        board.setVisible(true)
        await eventually { board.snapshot != nil }
        await board.openDetail("weather")
        await board.delete("weather")
        client.failWrites = true
        await board.saveSettings("weather", values: [:])
        #expect(board.detail != nil && board.actionMessage != nil && board.errorMessage != nil)
        // A detail request from the old connection is still on its way.
        client.detailDelayNanoseconds = 50_000_000
        let late = Task { await board.openDetail("weather") }
        await eventually { client.calls.filter { $0 == "detail weather" }.count == 3 }
        board.resetConnection()
        #expect(board.detail == nil)
        #expect(board.actionMessage == nil)
        #expect(board.errorMessage == nil)
        #expect(!board.isSaving)
        // The same slug selected on the new board does not admit the old answer.
        board.selectedSlug = "weather"
        await late.value
        #expect(board.detail == nil)
        board.setVisible(false)
    }
}
