import Foundation
import Testing

@testable import VeetbotCore

/// A consumer that falls behind slows the read. It never tears the connection down:
/// the 2026-11-03 outage was a long run's replay reconnecting every 0.4 seconds.
@Suite(.serialized) struct ReconnectingEventStreamTests {
    private static let runID = UUID(uuidString: "6a1c3f0e-2b4d-4c8e-9f10-000000000003")!
    private static let route = "GET veetbot.test /v1/runs/\(runID.uuidString)/events"
    private static let frameCount = 700

    /// The durable history after a resume point, ending in the terminal frame.
    private static func replay(after lastEventID: Int) -> String {
        var body = ""
        for sequence in (lastEventID + 1)...(frameCount + 1) {
            let event = sequence > frameCount ? "run.completed" : "tool.call.completed"
            body += "id: \(sequence)\nevent: \(event)\ndata: {\"sequence\":\(sequence)}\n\n"
        }
        return body
    }

    @Test
    func aSlowConsumerReadsALongReplayOverOneConnection() async throws {
        let server = DeviceFlowServer { request in
            guard "\(request.httpMethod ?? "") \(request.url?.host ?? "") \(request.url?.path ?? "")" == Self.route
            else { return nil }
            let lastEventID = Int(request.value(forHTTPHeaderField: "Last-Event-ID") ?? "") ?? 0
            return (200, Self.replay(after: lastEventID))
        }
        let transport = HTTPTransport(
            configuration: try ConnectionConfiguration(baseURLString: "https://veetbot.test"),
            tokenStore: InMemoryTokenStore(token: "valid"),
            session: URLSession(configuration: server.configuration(base: .ephemeral))
        )
        let stream = ReconnectingEventStream(reader: SSEReader(transport: transport))

        var sequences: [Int] = []
        for try await frame in stream.frames(runID: Self.runID) {
            if let id = frame.id { sequences.append(id) }
            // The app's reducer awaits approval reads here; a brief pause overruns any buffer.
            if sequences.count == 1 { try await Task.sleep(nanoseconds: 300_000_000) }
            if sequences.count % 50 == 0 { await Task.yield() }
        }

        #expect(sequences == Array(1...(Self.frameCount + 1)))
        #expect(server.routes.filter { $0 == Self.route }.count == 1)
    }

    /// One durable frame with the given id.
    private static func frame(_ sequence: Int) -> SSEFrame {
        SSEFrame(id: sequence, event: "tool.call.completed", data: [:])
    }

    /// Order and completeness survive the producer waiting for room.
    @Test
    func aFullChannelMakesTheProducerWaitAndLosesNothing() async throws {
        let channel = BoundedFrameChannel(capacity: 2)
        let producer = Task {
            for sequence in 1...5 {
                #expect(await channel.send(Self.frame(sequence)))
            }
            await channel.finish()
        }

        var received: [Int] = []
        while let frame = try await channel.next() {
            received.append(try #require(frame.id))
        }
        await producer.value

        #expect(received == [1, 2, 3, 4, 5])
    }

    /// The case this channel exists for: a producer that keeps the queue full while a
    /// slow consumer reads a long replay must not keep every frame it already delivered.
    @Test
    func aQueueThatStaysFullDoesNotKeepConsumedFrames() async throws {
        let channel = BoundedFrameChannel(capacity: 2)
        #expect(await channel.send(Self.frame(0)))
        #expect(await channel.send(Self.frame(1)))
        for sequence in 2..<50 {
            #expect(try await channel.next()?.id == sequence - 2)
            #expect(await channel.send(Self.frame(sequence)))
            #expect(await channel.retainedFrameCount <= 2)
        }
    }

    /// A producer waiting for room must not outlive its consumer.
    @Test
    func aConsumerThatLeavesReleasesAWaitingProducer() async {
        let channel = BoundedFrameChannel(capacity: 1)
        #expect(await channel.send(Self.frame(1)))
        let blocked = Task { await channel.send(Self.frame(2)) }
        await Task.yield()

        await channel.cancel()

        #expect(await blocked.value == false)
    }

    /// A failed read still delivers what it already received, then reports once.
    @Test
    func queuedFramesAreDeliveredBeforeTheProducersFailure() async throws {
        struct Failure: Error {}
        let channel = BoundedFrameChannel(capacity: 4)
        #expect(await channel.send(Self.frame(1)))
        await channel.finish(throwing: Failure())

        #expect(try await channel.next()?.id == 1)
        await #expect(throws: Failure.self) { try await channel.next() }
        #expect(try await channel.next() == nil)
    }

    /// Letting the stream go, without cancelling anything, stops the read.
    @Test
    func stoppingIterationEndsTheProducer() async throws {
        let stopped = AsyncStream<Void>.makeStream()
        var stream: AsyncThrowingStream<SSEFrame, Error>? = BoundedFrameStream.make(capacity: 1) { channel in
            var sequence = 0
            while await channel.send(Self.frame(sequence)) { sequence += 1 }
            stopped.continuation.finish()
        }
        var iterator = stream?.makeAsyncIterator()
        _ = try await iterator?.next()

        iterator = nil
        stream = nil

        for await _ in stopped.stream {}
    }
}
