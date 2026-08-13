import Testing

@testable import VeetbotCore

@Suite struct SSEReaderTests {
    @Test
    func testByteDecoderPreservesBlankSSEFrameBoundaries() throws {
        let payload = """
            id: 2\r
            event: run.queued\r
            data: {"run_id":"r-1"}\r
            \r
            id: 3\r
            event: run.completed\r
            data: {"run_id":"r-1"}\r
            \r
            """
        var decoder = SSEByteLineDecoder()
        var parser = SSEFrameParser()
        var frames: [SSEFrame] = []

        for byte in payload.utf8 {
            if let line = try decoder.consume(byte: byte),
                let frame = try parser.consume(line: line)
            {
                frames.append(frame)
            }
        }
        if let line = try decoder.finishAtEOF(),
            let frame = try parser.consume(line: line)
        {
            frames.append(frame)
        }
        if let frame = try parser.finishAtEOF() { frames.append(frame) }

        #expect(frames.count == 2)
        #expect(frames.map(\.id) == [2, 3])
        #expect(frames.map(\.event) == ["run.queued", "run.completed"])
    }

    @Test
    func testParserIgnoresHeartbeatAndEmitsAtBlankLine() throws {
        var parser = SSEFrameParser()
        #expect(try parser.consume(line: ": heartbeat") == nil)
        #expect(try parser.consume(line: "") == nil)
        #expect(try parser.consume(line: "id: 41") == nil)
        #expect(try parser.consume(line: "event: run.queued") == nil)
        #expect(try parser.consume(line: "data: {\"run_id\":\"r-1\"}") == nil)

        let parsed = try parser.consume(line: "")
        let frame = try #require(parsed)
        #expect(frame.id == 41)
        #expect(frame.event == "run.queued")
        #expect(frame.data["run_id"] == .string("r-1"))
    }

    @Test
    func testTransientFrameHasNoIDAndSuspensionIsNotTerminal() throws {
        var parser = SSEFrameParser()
        _ = try parser.consume(line: "event: run.waiting_for_user")
        _ = try parser.consume(line: "data: {\"question_id\":\"q-1\"}")
        let parsed = try parser.consume(line: "")
        let frame = try #require(parsed)
        #expect(frame.id == nil)
        #expect(!frame.isTerminal)
        #expect(
            SSEFrame(id: 9, event: "run.completed", data: [:]).isTerminal
        )
    }

    @Test
    func testNonContiguousSessionSequencesAreAccepted() throws {
        var parser = SSEFrameParser()
        for line in ["id: 2", "event: run.queued", "data: {}"] {
            _ = try parser.consume(line: line)
        }
        #expect(try parser.consume(line: "")?.id == 2)
        for line in ["id: 19", "event: run.started", "data: {}"] {
            _ = try parser.consume(line: line)
        }
        #expect(try parser.consume(line: "")?.id == 19)
    }

    @Test
    func testInvalidPersistedIDIsRejected() throws {
        var parser = SSEFrameParser()
        _ = try parser.consume(line: "id: nope")
        _ = try parser.consume(line: "event: run.queued")
        _ = try parser.consume(line: "data: {}")
        do {
            _ = try parser.consume(line: "")
            Issue.record("expected invalid persisted id")
        } catch SSEReaderError.invalidID("nope") {
            // Expected.
        } catch {
            Issue.record("unexpected error: \(error)")
        }
    }

    @Test
    func testReconnectLimitReportsUnderlyingError() {
        let error = ReconnectingEventStreamError.reconnectLimitExceeded(
            lastError: "The event stream returned invalid JSON data."
        )
        #expect(error.localizedDescription.contains("invalid JSON data"))
    }

    @Test
    func testReconnectAccountingCountsEveryAttemptAndRepeatedOverflow() {
        var counter = ReconnectAttemptCounter()

        counter.record(durableProgress: true, overflow: false)
        #expect(counter.total == 1)
        #expect(counter.consecutive == 0)

        counter.record(durableProgress: true, overflow: true)
        counter.record(durableProgress: false, overflow: true)
        #expect(counter.total == 3)
        #expect(counter.consecutive == 2)
    }
}
