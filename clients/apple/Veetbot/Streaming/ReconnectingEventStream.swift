import Foundation

public enum ReconnectingEventStreamError: Error, LocalizedError, Sendable {
    case reconnectLimitExceeded(lastError: String?)
    case localBufferOverflow

    public var errorDescription: String? {
        switch self {
        case .reconnectLimitExceeded(let lastError):
            let summary = "The run stream could not be reconnected after several attempts."
            guard let lastError, !lastError.isEmpty else { return summary }
            return "\(summary) Last error: \(lastError)"
        case .localBufferOverflow:
            return "The app could not reduce run events quickly enough."
        }
    }
}

public struct ReconnectingEventStream: Sendable {
    private let reader: SSEReader
    private let maximumConsecutiveReconnects: Int
    private let maximumTotalReconnects: Int

    public init(
        reader: SSEReader,
        maximumConsecutiveReconnects: Int = 8,
        maximumTotalReconnects: Int = 32
    ) {
        self.reader = reader
        self.maximumConsecutiveReconnects = maximumConsecutiveReconnects
        self.maximumTotalReconnects = maximumTotalReconnects
    }

    public func frames(
        runID: UUID,
        startingAfter initialID: Int? = nil
    ) -> AsyncThrowingStream<SSEFrame, Error> {
        AsyncThrowingStream(bufferingPolicy: .bufferingOldest(256)) { continuation in
            let task = Task {
                var lastPersistedID = initialID
                var reconnects = ReconnectAttemptCounter()
                var lastReconnectError: String?
                do {
                    streamLoop: while !Task.isCancelled {
                        let reconnectStartID = lastPersistedID
                        var overflowReconnect = false
                        var shouldReconnect = false
                        do {
                            let stream = reader.frames(
                                path: "/v1/runs/\(runID.uuidString)/events",
                                lastEventID: lastPersistedID
                            )
                            for try await frame in stream {
                                if frame.event == "stream.overflow" {
                                    overflowReconnect = true
                                    if let watermark = frame.data["last_sequence"]?.intValue {
                                        lastPersistedID = max(lastPersistedID ?? 0, watermark)
                                    }
                                    shouldReconnect = true
                                    break
                                }
                                switch continuation.yield(frame) {
                                case .enqueued:
                                    if let id = frame.id {
                                        // IDs are per session. Non-contiguous values are not gaps.
                                        lastPersistedID = id
                                    }
                                    break
                                case .dropped:
                                    throw ReconnectingEventStreamError.localBufferOverflow
                                case .terminated:
                                    return
                                @unknown default:
                                    return
                                }
                                if frame.isTerminal {
                                    continuation.finish()
                                    return
                                }
                            }
                            // Suspension is deliberately not terminal. EOF before a terminal frame
                            // is a transport interruption and reconnects from durable state.
                            lastReconnectError =
                                "The server closed the stream before a terminal event."
                            shouldReconnect = true
                        } catch is CancellationError {
                            throw CancellationError()
                        } catch let error as HTTPTransportError {
                            switch error {
                            case .reauthenticationRequired, .authorizationDenied, .api,
                                .missingToken:
                                throw error
                            default:
                                lastReconnectError = error.localizedDescription
                                shouldReconnect = true
                            }
                        } catch let error as SSEReaderError {
                            if case .clientBufferOverflow(let lastID) = error, let lastID {
                                lastPersistedID = max(lastPersistedID ?? 0, lastID)
                            }
                            lastReconnectError = error.localizedDescription
                            shouldReconnect = true
                        } catch {
                            lastReconnectError = error.localizedDescription
                            shouldReconnect = true
                        }

                        guard shouldReconnect else { continue streamLoop }
                        let durableProgress =
                            switch (reconnectStartID, lastPersistedID) {
                            case (.some(let before), .some(let after)): after > before
                            case (.none, .some): true
                            default: false
                            }
                        reconnects.record(
                            durableProgress: durableProgress,
                            overflow: overflowReconnect
                        )
                        guard
                            reconnects.consecutive <= maximumConsecutiveReconnects,
                            reconnects.total <= maximumTotalReconnects
                        else {
                            throw ReconnectingEventStreamError.reconnectLimitExceeded(
                                lastError: lastReconnectError
                            )
                        }
                        let exponent = max(0, reconnects.consecutive - 1)
                        let delaySeconds = min(0.25 * pow(2, Double(exponent)), 4)
                        try await Task.sleep(
                            nanoseconds: UInt64(delaySeconds * 1_000_000_000)
                        )
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { @Sendable _ in task.cancel() }
        }
    }
}

struct ReconnectAttemptCounter {
    private(set) var consecutive = 0
    private(set) var total = 0

    mutating func record(durableProgress: Bool, overflow: Bool) {
        total += 1
        if durableProgress, !overflow {
            consecutive = 0
        } else {
            consecutive += 1
        }
    }
}
