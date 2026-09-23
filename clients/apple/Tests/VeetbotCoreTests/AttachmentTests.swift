import Foundation
import ImageIO
import Testing
import UniformTypeIdentifiers

@testable import VeetbotCore

/// ADR-0120: files attached to a chat message — staging, upload, and sending.
@Suite(.serialized) @MainActor struct AttachmentTests {
    private let sessionID = UUID(uuidString: "00000000-0000-0000-0000-00000000a001")!
    private let runID = UUID(uuidString: "00000000-0000-0000-0000-00000000a002")!

    // MARK: Transport and client

    @Test
    func testUploadSendsTheFileWithItsTypeNameAndKey() async throws {
        let recorder = AttachmentRequestRecorder()
        let artifactID = UUID()
        let client = try makeClient { request in
            recorder.record(request)
            return try jsonResponse(request, status: 201, body: artifactJSON(artifactID, runID: nil))
        }
        let body = Data("hello".utf8)
        let artifact = try await client.uploadArtifact(
            sessionID: sessionID,
            data: body,
            filename: "Café notes.md",
            mediaType: "text/markdown",
            idempotencyKey: "upload-key"
        )
        #expect(artifact.id == artifactID)
        #expect(artifact.runID == nil)
        let sent = try #require(recorder.entries.first)
        #expect(sent.method == "POST")
        #expect(sent.path == "/v1/sessions/\(sessionID.uuidString)/artifacts")
        #expect(sent.headers["Content-Type"] == "text/markdown")
        #expect(sent.headers["X-Filename"] == "Caf%C3%A9%20notes.md")
        #expect(sent.headers["Idempotency-Key"] == "upload-key")
        #expect(sent.body == body)
    }

    @Test
    func testUploadRetriesAServerFailureWithTheSameKey() async throws {
        let recorder = AttachmentRequestRecorder()
        let client = try makeClient { request in
            recorder.record(request)
            if recorder.entries.count == 1 {
                return try jsonResponse(request, status: 503, body: "{}", headers: ["Retry-After": "0"])
            }
            return try jsonResponse(request, status: 200, body: artifactJSON(UUID(), runID: nil))
        }
        _ = try await client.uploadArtifact(
            sessionID: sessionID, data: Data("x".utf8), filename: "a.txt",
            mediaType: "text/plain", idempotencyKey: "same-key"
        )
        let keys = recorder.entries.map { $0.headers["Idempotency-Key"] }
        #expect(keys == ["same-key", "same-key"])
    }

    @Test
    func testAServerWithoutTheRouteIsReportedAsNotAcceptingAttachments() async throws {
        let missingRoute = try makeClient { request in
            try jsonResponse(
                request, status: 404,
                body: #"{"error":{"code":"not_found","message":"The requested resource was not found.","details":{},"request_id":"r"}}"#
            )
        }
        await #expect(throws: VeetbotAPIClientError.attachmentsUnavailable) {
            _ = try await missingRoute.uploadArtifact(
                sessionID: sessionID, data: Data("x".utf8), filename: "a.txt",
                mediaType: "text/plain", idempotencyKey: "k"
            )
        }
        let missingSession = try makeClient { request in
            try jsonResponse(
                request, status: 404,
                body: #"{"error":{"code":"not_found","message":"session not found","details":{},"request_id":"r"}}"#
            )
        }
        do {
            _ = try await missingSession.uploadArtifact(
                sessionID: sessionID, data: Data("x".utf8), filename: "a.txt",
                mediaType: "text/plain", idempotencyKey: "k"
            )
            Issue.record("expected the missing session to fail")
        } catch HTTPTransportError.api(let error) {
            #expect(error.message == "session not found")
        }
    }

    @Test
    func testAnUnclaimedArtifactDecodesWithoutARun() throws {
        let decoded = try JSONDecoder.server.decode(
            ArtifactView.self, from: Data(artifactJSON(UUID(), runID: nil).utf8)
        )
        #expect(decoded.runID == nil)
    }

    // MARK: Staging

    @Test
    func testNamesAreMadeSafeForTheServer() {
        #expect(AttachmentStaging.cleanedName("a\"b/c\\d\ne.txt") == "a_b_c_d_e.txt")
        #expect(AttachmentStaging.cleanedName("   ") == "attachment")
        let long = String(repeating: "é", count: 200) + ".pdf"
        let cleaned = AttachmentStaging.cleanedName(long)
        #expect(cleaned.utf8.count <= 255)
        #expect(cleaned.hasSuffix(".pdf"))
    }

    @Test
    func testTypesComeFromTheExtensionTableBeforeThePlatform() throws {
        let markdown = try AttachmentStaging.stage(
            data: Data("# hi".utf8), filename: "notes.md", type: UTType(filenameExtension: "md")
        )
        #expect(markdown.mediaType == "text/markdown")
        let unknown = try AttachmentStaging.stage(
            data: Data([0, 1, 2]), filename: "blob", type: nil
        )
        #expect(unknown.mediaType == "application/octet-stream")
    }

    @Test
    func testPhotosAreReencodedWithoutLocationAndWithinTheLongEdge() throws {
        let photo = try makeImage(width: 3000, height: 1500, type: .jpeg, gps: true)
        #expect(imageProperties(photo)[kCGImagePropertyGPSDictionary] != nil)
        let staged = try AttachmentStaging.stage(data: photo, filename: "IMG_0001.HEIC", type: .jpeg)
        #expect(staged.mediaType == "image/jpeg")
        #expect(staged.filename == "IMG_0001.jpg")
        #expect(imageProperties(staged.data)[kCGImagePropertyGPSDictionary] == nil)
        #expect(AttachmentStaging.longEdge(of: staged.data) == AttachmentStaging.imageLongEdge)
    }

    @Test
    func testASmallPNGIsSentUnchanged() throws {
        let png = try makeImage(width: 40, height: 20, type: .png, gps: false)
        let staged = try AttachmentStaging.stage(data: png, filename: "chart.png", type: .png)
        #expect(staged.data == png)
        #expect(staged.mediaType == "image/png")
    }

    @Test
    func testAFileOverTheLimitIsRefusedBeforeUpload() throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("\(UUID().uuidString).bin")
        defer { try? FileManager.default.removeItem(at: url) }
        try Data(count: AttachmentStaging.maximumBytes + 1).write(to: url)
        #expect(throws: AttachmentStagingError.tooLarge(filename: url.lastPathComponent)) {
            _ = try AttachmentStaging.stage(fileURL: url)
        }
    }

    // MARK: Drop policy

    @Test
    func testFilesAndImagesDropAsAttachmentsWhileTextStaysText() {
        #expect(ComposerDropPolicy.takesAttachments(typeIdentifiers: ["public.file-url"]))
        #expect(ComposerDropPolicy.takesAttachments(typeIdentifiers: ["public.png"]))
        #expect(ComposerDropPolicy.takesAttachments(typeIdentifiers: ["com.adobe.pdf"]))
        #expect(
            ComposerDropPolicy.takesAttachments(
                typeIdentifiers: ["com.apple.pasteboard.promised-file-url"]
            )
        )
        #expect(!ComposerDropPolicy.takesAttachments(typeIdentifiers: ["public.utf8-plain-text"]))
        #expect(
            !ComposerDropPolicy.takesAttachments(
                typeIdentifiers: ["public.utf8-plain-text", "public.html", "public.rtf"]
            )
        )
        #expect(!ComposerDropPolicy.takesAttachments(typeIdentifiers: ["public.url"]))
        #expect(!ComposerDropPolicy.takesAttachments(typeIdentifiers: []))
    }

    // MARK: View model

    @Test
    func testDroppedFilesShareOneNewConversationAndSendWithoutText() async throws {
        let recorder = AttachmentRequestRecorder()
        let imageID = UUID()
        let noteID = UUID()
        let model = try makeModel { request in
            recorder.record(request)
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "", path) {
            case ("GET", "/v1/sessions"):
                return try jsonResponse(request, status: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("POST", "/v1/sessions"):
                return try jsonResponse(request, status: 201, body: sessionJSON())
            case ("GET", "/v1/sessions/\(sessionID.uuidString)"):
                return try jsonResponse(request, status: 200, body: sessionJSON())
            case ("POST", "/v1/sessions/\(sessionID.uuidString)/artifacts"):
                let isImage = request.value(forHTTPHeaderField: "Content-Type") == "image/png"
                return try jsonResponse(
                    request, status: 201,
                    body: artifactJSON(
                        isImage ? imageID : noteID,
                        runID: nil,
                        name: isImage ? "chart.png" : "notes.md",
                        mediaType: isImage ? "image/png" : "text/markdown"
                    )
                )
            case ("POST", "/v1/sessions/\(sessionID.uuidString)/messages"):
                return try jsonResponse(
                    request, status: 202,
                    body: #"{"run_id":"\#(runID.uuidString)","status":"QUEUED"}"#
                )
            case ("GET", "/v1/runs/\(runID.uuidString)"):
                return try jsonResponse(request, status: 200, body: runJSON())
            default:
                return try jsonResponse(request, status: 404, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "t"))
        let directory = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: directory) }
        let png = directory.appendingPathComponent("chart.png")
        try makeImage(width: 20, height: 10, type: .png, gps: false).write(to: png)
        let notes = directory.appendingPathComponent("notes.md")
        try Data("# Notes".utf8).write(to: notes)

        await model.attach(fileURLs: [png, notes])
        try await waitUntil { model.attachmentsReady && model.attachments.count == 2 }
        #expect(recorder.matching("POST", "/v1/sessions").count == 1)
        #expect(model.selectedSessionID == sessionID)
        let uploads = recorder.matching("POST", "/v1/sessions/\(sessionID.uuidString)/artifacts")
        #expect(uploads.count == 2)
        #expect(Set(uploads.compactMap { $0.headers["Idempotency-Key"] }).count == 2)

        #expect(await model.send("   "))
        let submitted = try #require(
            recorder.matching("POST", "/v1/sessions/\(sessionID.uuidString)/messages").first
        )
        let body = try #require(submitted.body)
        let content = try #require(
            (try JSONSerialization.jsonObject(with: body) as? [String: Any])?["content"]
                as? [[String: Any]]
        )
        #expect(content.count == 2)
        #expect(content[0]["type"] as? String == "image")
        #expect(content[0]["artifact_id"] as? String == imageID.uuidString)
        #expect(content[1]["type"] as? String == "file")
        #expect(content[1]["filename"] as? String == "notes.md")
        #expect(model.attachments.isEmpty)
        #expect(model.history.first?.title == "chart.png")
    }

    @Test
    func testAFailedUploadCanBeRetriedAndBlocksSendingUntilThen() async throws {
        let recorder = AttachmentRequestRecorder()
        let failures = FailureSwitch(remaining: 1)
        let model = try makeModel { request in
            recorder.record(request)
            switch (request.httpMethod ?? "", request.url?.path ?? "") {
            case ("GET", "/v1/sessions"):
                return try jsonResponse(request, status: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("POST", "/v1/sessions"):
                return try jsonResponse(request, status: 201, body: sessionJSON())
            case ("POST", "/v1/sessions/\(sessionID.uuidString)/artifacts"):
                if failures.consume() {
                    return try jsonResponse(
                        request, status: 400,
                        body: #"{"error":{"code":"malformed_request","message":"The file name is empty or longer than 255 bytes.","details":{},"request_id":"r"}}"#
                    )
                }
                return try jsonResponse(request, status: 201, body: artifactJSON(UUID(), runID: nil))
            default:
                return try jsonResponse(request, status: 404, body: "{}")
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "t"))
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID()).txt")
        defer { try? FileManager.default.removeItem(at: url) }
        try Data("text".utf8).write(to: url)

        await model.attach(fileURLs: [url])
        try await waitUntil {
            if case .failed = model.attachments.first?.state { return true }
            return false
        }
        #expect(!model.attachmentsReady)
        #expect(await model.send("hello") == false)

        let id = try #require(model.attachments.first?.id)
        model.retryAttachment(id)
        try await waitUntil { model.attachmentsReady }
        let keys = recorder.matching("POST", "/v1/sessions/\(sessionID.uuidString)/artifacts")
            .compactMap { $0.headers["Idempotency-Key"] }
        #expect(keys.count == 2)
        #expect(Set(keys).count == 1)
    }

    @Test
    func testStartingANewConversationDropsStagedAttachments() async throws {
        let model = try makeModel { request in
            switch (request.httpMethod ?? "", request.url?.path ?? "") {
            case ("GET", "/v1/sessions"):
                return try jsonResponse(request, status: 200, body: #"{"items":[],"next_cursor":null}"#)
            case ("POST", "/v1/sessions"):
                return try jsonResponse(request, status: 201, body: sessionJSON())
            default:
                return try jsonResponse(
                    request, status: 404,
                    body: #"{"error":{"code":"not_found","message":"The requested resource was not found.","details":{},"request_id":"r"}}"#
                )
            }
        }
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "t"))
        let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID()).txt")
        defer { try? FileManager.default.removeItem(at: url) }
        try Data("text".utf8).write(to: url)
        await model.attach(fileURLs: [url])
        try await waitUntil {
            if case .failed(let message) = model.attachments.first?.state {
                return message == "This server does not accept attachments yet."
            }
            return false
        }
        model.newSession()
        #expect(model.attachments.isEmpty)
        #expect(model.attachmentsReady)
    }

    // MARK: Fixtures

    private func makeClient(
        handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> VeetbotAPIClient {
        let configuration = try ConnectionConfiguration(baseURLString: "https://veetbot.test")
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: "t"),
            session: stubSession(handler)
        )
        return VeetbotAPIClient(transport: transport)
    }

    private func makeModel(
        handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> ChatViewModel {
        let suiteName = "com.veetbot.tests.attachments.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        return ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: stubSession(handler)
        )
    }

    private func stubSession(
        _ handler: @escaping @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)
    ) -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpAdditionalHeaders = [
            AttachmentURLProtocol.handlerHeader: AttachmentURLProtocol.register(handler)
        ]
        configuration.protocolClasses = [AttachmentURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    private func waitUntil(
        _ condition: @MainActor () -> Bool,
        timeout: TimeInterval = 5
    ) async throws {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() {
            guard Date() < deadline else {
                Issue.record("condition not met in time")
                return
            }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
    }

    nonisolated private func sessionJSON() -> String {
        #"{"id":"\#(sessionID.uuidString)","status":"ACTIVE","agent_id":"general","agent_version":"1","title":null,"metadata":{},"created_at":"2026-09-23T00:00:00Z","updated_at":"2026-09-23T00:00:00Z","active_run_id":null,"last_run_id":null}"#
    }

    nonisolated private func runJSON() -> String {
        #"{"id":"\#(runID.uuidString)","session_id":"\#(sessionID.uuidString)","parent_run_id":null,"status":"QUEUED","step_count":0,"model_call_count":0,"tool_call_count":0,"usage":{"input_tokens":0,"output_tokens":0,"cost_usd":"0"},"limits":{"max_steps":40,"deadline_at":null,"max_cost_usd":"1.00"},"failure":null,"cancel_requested_at":null,"created_at":"2026-09-23T00:00:00Z","updated_at":"2026-09-23T00:00:00Z"}"#
    }
}

private func artifactJSON(
    _ id: UUID,
    runID: UUID?,
    name: String = "notes.md",
    mediaType: String = "text/markdown"
) -> String {
    let run = runID.map { "\"\($0.uuidString)\"" } ?? "null"
    return #"{"id":"\#(id.uuidString)","session_id":"00000000-0000-0000-0000-00000000a001","run_id":\#(run),"name":"\#(name)","media_type":"\#(mediaType)","sha256":"abc","size_bytes":5,"metadata":{"attachment":{"kind":"text"}},"created_at":"2026-09-23T00:00:00Z"}"#
}

private func jsonResponse(
    _ request: URLRequest,
    status: Int,
    body: String,
    headers: [String: String] = [:]
) throws -> (HTTPURLResponse, Data) {
    var fields = ["Content-Type": "application/json"]
    fields.merge(headers) { _, new in new }
    let response = try #require(
        HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: fields)
    )
    return (response, Data(body.utf8))
}

private func makeImage(width: Int, height: Int, type: UTType, gps: Bool) throws -> Data {
    let context = try #require(
        CGContext(
            data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        )
    )
    context.setFillColor(red: 0.2, green: 0.6, blue: 0.8, alpha: 1)
    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    let image = try #require(context.makeImage())
    let output = NSMutableData()
    let destination = try #require(
        CGImageDestinationCreateWithData(output, type.identifier as CFString, 1, nil)
    )
    var properties: [CFString: Any] = [:]
    if gps {
        properties[kCGImagePropertyGPSDictionary] = [
            kCGImagePropertyGPSLatitude: 37.33,
            kCGImagePropertyGPSLatitudeRef: "N",
            kCGImagePropertyGPSLongitude: 122.03,
            kCGImagePropertyGPSLongitudeRef: "W",
        ]
    }
    CGImageDestinationAddImage(destination, image, properties as CFDictionary)
    #expect(CGImageDestinationFinalize(destination))
    return output as Data
}

private func imageProperties(_ data: Data) -> [CFString: Any] {
    guard let source = CGImageSourceCreateWithData(data as CFData, nil),
        let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any]
    else { return [:] }
    return properties
}

/// Allows exactly `remaining` failures, then succeeds.
private final class FailureSwitch: @unchecked Sendable {
    private let lock = NSLock()
    private var remaining: Int

    init(remaining: Int) { self.remaining = remaining }

    func consume() -> Bool {
        lock.withLock {
            guard remaining > 0 else { return false }
            remaining -= 1
            return true
        }
    }
}

/// Records method, path, headers, and body; an upload's body arrives as a stream.
private final class AttachmentRequestRecorder: @unchecked Sendable {
    struct Entry: Sendable {
        let method: String
        let path: String
        let headers: [String: String]
        let body: Data?
    }

    private let lock = NSLock()
    private var recorded: [Entry] = []

    var entries: [Entry] { lock.withLock { recorded } }

    func record(_ request: URLRequest) {
        var body = request.httpBody
        if body == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var collected = Data()
            var buffer = [UInt8](repeating: 0, count: 4_096)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                collected.append(buffer, count: count)
            }
            body = collected
        }
        let entry = Entry(
            method: request.httpMethod ?? "",
            path: request.url?.path ?? "",
            headers: request.allHTTPHeaderFields ?? [:],
            body: body
        )
        lock.withLock { recorded.append(entry) }
    }

    func matching(_ method: String, _ path: String) -> [Entry] {
        entries.filter { $0.method == method && $0.path == path }
    }
}

private final class AttachmentURLProtocol: URLProtocol {
    typealias Handler = @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)
    static let handlerHeader = "X-Veetbot-Attachment-Test-Handler"
    private static let lock = NSLock()
    nonisolated(unsafe) private static var handlers: [String: Handler] = [:]

    static func register(_ handler: @escaping Handler) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }

    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let handler = request.value(forHTTPHeaderField: Self.handlerHeader).flatMap { id in
            Self.lock.withLock { Self.handlers[id] }
        }
        guard let handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}
