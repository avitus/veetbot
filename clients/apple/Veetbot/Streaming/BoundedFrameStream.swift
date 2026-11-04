import Foundation

/// Hands frames from one producer to one consumer, with the producer waiting while
/// the consumer is behind. A paused read slows the socket; it never drops a frame or
/// reconnects, which is what makes a long run's replay safe on a busy main actor.
actor BoundedFrameChannel {
    private let capacity: Int
    private var frames: [SSEFrame] = []
    private var waitingProducer: CheckedContinuation<Void, Never>?
    private var waitingConsumer: CheckedContinuation<SSEFrame?, Error>?
    private var finished = false
    private var cancelled = false
    private var failure: Error?

    init(capacity: Int) {
        self.capacity = max(1, capacity)
    }

    /// Frames held in memory, which capacity bounds even while the queue stays full.
    var retainedFrameCount: Int { frames.count }

    /// Waits for room, then queues the frame. False means the consumer has gone away.
    func send(_ frame: SSEFrame) async -> Bool {
        while !cancelled, frames.count >= capacity {
            await withCheckedContinuation { waitingProducer = $0 }
        }
        guard !cancelled else { return false }
        if let consumer = waitingConsumer {
            waitingConsumer = nil
            consumer.resume(returning: frame)
        } else {
            frames.append(frame)
        }
        return true
    }

    /// The next frame, nil once the producer has finished and the queue is empty.
    func next() async throws -> SSEFrame? {
        if cancelled { throw CancellationError() }
        if !frames.isEmpty {
            let frame = frames.removeFirst()
            resumeProducer()
            return frame
        }
        if finished {
            if let failure {
                self.failure = nil
                throw failure
            }
            return nil
        }
        return try await withCheckedThrowingContinuation { waitingConsumer = $0 }
    }

    /// Ends production; frames already queued are still delivered first.
    func finish(throwing error: Error? = nil) {
        guard !finished else { return }
        finished = true
        failure = error
        guard let consumer = waitingConsumer else { return }
        waitingConsumer = nil
        if let error {
            failure = nil
            consumer.resume(throwing: error)
        } else {
            consumer.resume(returning: nil)
        }
    }

    /// The consumer has gone away: release whichever side is waiting.
    func cancel() {
        cancelled = true
        frames.removeAll()
        resumeProducer()
        waitingConsumer?.resume(throwing: CancellationError())
        waitingConsumer = nil
    }

    /// Wakes the producer if it is waiting for room.
    private func resumeProducer() {
        waitingProducer?.resume()
        waitingProducer = nil
    }
}

/// Ties a producing task to the stream that reads it, so the read stops when the
/// consumer is cancelled or simply stops iterating and lets the stream go.
private final class BoundedFrameProducer: Sendable {
    private let channel: BoundedFrameChannel
    private let task: Task<Void, Never>

    init(channel: BoundedFrameChannel, task: Task<Void, Never>) {
        self.channel = channel
        self.task = task
    }

    deinit {
        cancel()
    }

    /// The channel's next frame.
    func next() async throws -> SSEFrame? {
        try await channel.next()
    }

    /// Stops the read and releases whichever side of the channel is waiting.
    func cancel() {
        task.cancel()
        let channel = channel
        Task { await channel.cancel() }
    }
}

enum BoundedFrameStream {
    static let defaultCapacity = 256

    /// Starts `produce` at once and streams what it sends; its thrown error ends the
    /// stream after the queued frames, and leaving the stream cancels it.
    static func make(
        capacity: Int = defaultCapacity,
        produce: @escaping @Sendable (BoundedFrameChannel) async throws -> Void
    ) -> AsyncThrowingStream<SSEFrame, Error> {
        let channel = BoundedFrameChannel(capacity: capacity)
        let task = Task {
            do {
                try await produce(channel)
                await channel.finish()
            } catch {
                await channel.finish(throwing: error)
            }
        }
        let producer = BoundedFrameProducer(channel: channel, task: task)
        return AsyncThrowingStream(unfolding: {
            try await withTaskCancellationHandler {
                try await producer.next()
            } onCancel: {
                producer.cancel()
            }
        })
    }
}
