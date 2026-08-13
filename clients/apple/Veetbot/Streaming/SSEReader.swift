import Foundation

public enum SSEReaderError: Error, LocalizedError, Sendable {
    case invalidHTTPResponse
    case invalidID(String)
    case missingEvent
    case invalidData
    case frameTooLarge
    case clientBufferOverflow(lastPersistedID: Int?)

    public var errorDescription: String? {
        switch self {
        case .invalidHTTPResponse: return "The event stream did not return HTTP."
        case let .invalidID(value): return "The event stream returned an invalid id: \(value)."
        case .missingEvent: return "The event stream returned a frame without an event name."
        case .invalidData: return "The event stream returned invalid JSON data."
        case .frameTooLarge: return "The event stream returned a frame larger than 1 MiB."
        case .clientBufferOverflow: return "The local event buffer overflowed."
        }
    }
}

public struct SSEReader: Sendable {
    private let transport: HTTPTransport
    private let maximumFrameBytes: Int

    public init(transport: HTTPTransport, maximumFrameBytes: Int = 1_048_576) {
        self.transport = transport
        self.maximumFrameBytes = maximumFrameBytes
    }

    public func frames(path: String, lastEventID: Int?) -> AsyncThrowingStream<SSEFrame, Error> {
        AsyncThrowingStream(bufferingPolicy: .bufferingOldest(256)) { continuation in
            let producer = Task {
                do {
                    let request = try await transport.makeSSERequest(
                        path: path,
                        lastEventID: lastEventID
                    )
                    let session = await transport.streamingSession()
                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        throw SSEReaderError.invalidHTTPResponse
                    }
                    guard (200 ... 299).contains(http.statusCode) else {
                        var errorBody = Data()
                        for try await byte in bytes {
                            if errorBody.count >= maximumFrameBytes { break }
                            errorBody.append(byte)
                        }
                        try await transport.decodeStreamingError(data: errorBody, response: http)
                    }

                    var parser = SSEFrameParser(maximumFrameBytes: maximumFrameBytes)
                    var lineDecoder = SSEByteLineDecoder(maximumLineBytes: maximumFrameBytes)
                    var lastPersisted = lastEventID
                    for try await byte in bytes {
                        try Task.checkCancellation()
                        guard let line = try lineDecoder.consume(byte: byte) else { continue }
                        if let frame = try parser.consume(line: line),
                           try !enqueue(frame, on: continuation, lastPersisted: &lastPersisted)
                        {
                            return
                        }
                    }
                    if let line = try lineDecoder.finishAtEOF(),
                       let frame = try parser.consume(line: line),
                       try !enqueue(frame, on: continuation, lastPersisted: &lastPersisted)
                    {
                        return
                    }
                    if let frame = try parser.finishAtEOF() {
                        if try !enqueue(
                            frame,
                            on: continuation,
                            lastPersisted: &lastPersisted
                        ) {
                            return
                        }
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish(throwing: CancellationError())
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { @Sendable _ in
                // Cancelling this task cancels URLSession.bytes(for:)'s underlying data task.
                producer.cancel()
            }
        }
    }

    private func enqueue(
        _ frame: SSEFrame,
        on continuation: AsyncThrowingStream<SSEFrame, Error>.Continuation,
        lastPersisted: inout Int?
    ) throws -> Bool {
        switch continuation.yield(frame) {
        case .enqueued:
            if let id = frame.id { lastPersisted = id }
            return true
        case .dropped:
            throw SSEReaderError.clientBufferOverflow(lastPersistedID: lastPersisted)
        case .terminated:
            return false
        @unknown default:
            return false
        }
    }
}

/// `URLSession.AsyncBytes.lines` omits empty records on supported Apple OS versions.
/// SSE uses an empty line as its frame boundary, so decode lines from bytes directly.
struct SSEByteLineDecoder {
    private let maximumLineBytes: Int
    private var buffer: [UInt8] = []

    init(maximumLineBytes: Int = 1_048_576) {
        self.maximumLineBytes = maximumLineBytes
    }

    mutating func consume(byte: UInt8) throws -> String? {
        guard byte == 0x0A else {
            buffer.append(byte)
            guard buffer.count <= maximumLineBytes else {
                throw SSEReaderError.frameTooLarge
            }
            return nil
        }
        return try finishLine()
    }

    mutating func finishAtEOF() throws -> String? {
        guard !buffer.isEmpty else { return nil }
        return try finishLine()
    }

    private mutating func finishLine() throws -> String {
        if buffer.last == 0x0D { buffer.removeLast() }
        guard let line = String(bytes: buffer, encoding: .utf8) else {
            throw SSEReaderError.invalidData
        }
        buffer.removeAll(keepingCapacity: true)
        return line
    }
}

struct SSEFrameParser {
    private let maximumFrameBytes: Int
    private var id: String?
    private var event: String?
    private var dataLines: [String] = []
    private var byteCount = 0

    init(maximumFrameBytes: Int = 1_048_576) {
        self.maximumFrameBytes = maximumFrameBytes
    }

    mutating func consume(line rawLine: String) throws -> SSEFrame? {
        let line = rawLine.hasSuffix("\r") ? String(rawLine.dropLast()) : rawLine
        byteCount += line.utf8.count + 1
        guard byteCount <= maximumFrameBytes else { throw SSEReaderError.frameTooLarge }
        if line.isEmpty {
            return try emit()
        }
        if line.hasPrefix(":") { return nil }

        let pieces = line.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        let field = String(pieces[0])
        var value = pieces.count == 2 ? String(pieces[1]) : ""
        if value.hasPrefix(" ") { value.removeFirst() }
        switch field {
        case "id": id = value
        case "event": event = value
        case "data": dataLines.append(value)
        default: break
        }
        return nil
    }

    mutating func finishAtEOF() throws -> SSEFrame? {
        guard id != nil || event != nil || !dataLines.isEmpty else { return nil }
        return try emit()
    }

    private mutating func emit() throws -> SSEFrame? {
        defer { reset() }
        guard id != nil || event != nil || !dataLines.isEmpty else { return nil }
        guard let event, !event.isEmpty else { throw SSEReaderError.missingEvent }
        let eventID: Int?
        if let id, !id.isEmpty {
            guard let parsed = Int(id), parsed >= 0 else { throw SSEReaderError.invalidID(id) }
            eventID = parsed
        } else {
            eventID = nil
        }
        let data = Data(dataLines.joined(separator: "\n").utf8)
        guard let object = try? JSONDecoder.server.decode([String: JSONValue].self, from: data)
        else {
            throw SSEReaderError.invalidData
        }
        return SSEFrame(id: eventID, event: event, data: object)
    }

    private mutating func reset() {
        id = nil
        event = nil
        dataLines.removeAll(keepingCapacity: true)
        byteCount = 0
    }
}
