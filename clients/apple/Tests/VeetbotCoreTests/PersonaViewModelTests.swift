import Foundation
import Testing

@testable import VeetbotCore

@Suite(.serialized) @MainActor struct PersonaViewModelTests {
    @Test
    func testLoadRendersTheDocumentAndOpenNominations() async throws {
        let beliefID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000501"))
        let nominationID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000601"))
        let model = try makeModel { request in
            switch request.url?.path {
            case "/v1/persona":
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: self.personaJSON(version: 2, affirmedBeliefID: beliefID)
                )
            case "/v1/persona/nominations":
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: """
                        {"items":[\(self.nominationJSON(id: nominationID, beliefID: beliefID))],"next_cursor":null}
                        """
                )
            default:
                return try self.response(for: request, statusCode: 404, body: "{}")
            }
        }

        await model.load()

        #expect(model.version == 2)
        #expect(model.drafts.count == 2)
        #expect(model.drafts[0].text == "User values direct answers.")
        #expect(model.drafts[1].sourceBeliefID == beliefID)
        #expect(model.nominations.map(\.id) == [nominationID])
        #expect(model.unavailable == false)
        #expect(model.errorMessage == nil)
    }

    @Test
    func testSaveNamesTheReadVersionAndAppliesTheResponse() async throws {
        let lock = NSLock()
        var bodies: [Data] = []
        let model = try makeModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/persona"):
                return try self.response(
                    for: request, statusCode: 200, body: self.personaJSON(version: 3)
                )
            case ("GET", "/v1/persona/nominations"):
                return try self.response(
                    for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#
                )
            case ("PUT", "/v1/persona"):
                lock.withLock { bodies.append(request.bodyData ?? Data()) }
                return try self.response(
                    for: request, statusCode: 200, body: self.personaJSON(version: 4)
                )
            default:
                return try self.response(for: request, statusCode: 404, body: "{}")
            }
        }

        await model.load()
        await model.save()

        #expect(model.version == 4)
        #expect(model.conflictDetected == false)
        let body = try #require(lock.withLock { bodies.first })
        let decoded = try JSONSerialization.jsonObject(with: body) as? [String: Any]
        #expect(decoded?["expected_version"] as? Int == 3)
    }

    @Test
    func testAConflictRetainsBothHeadsAndBlocksSaveUntilOwnerResolves() async throws {
        let lock = NSLock()
        var personaReads = 0
        var personaWrites = 0
        var personaWriteBodies: [Data] = []
        let model = try makeModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/persona"):
                let read = lock.withLock {
                    personaReads += 1
                    return personaReads
                }
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: read == 1
                        ? self.personaJSON(version: 1)
                        : self.personaJSON(
                            version: 2,
                            entries: """
                                {"text":"User values collaborative answers.","source":"user_edit","source_belief_id":null,"sensitivity":"internal"}
                                """
                        )
                )
            case ("GET", "/v1/persona/nominations"):
                return try self.response(
                    for: request, statusCode: 200, body: #"{"items":[],"next_cursor":null}"#
                )
            case ("PUT", "/v1/persona"):
                let write = lock.withLock {
                    personaWrites += 1
                    personaWriteBodies.append(request.bodyData ?? Data())
                    return personaWrites
                }
                if write == 1 {
                    return try self.response(
                        for: request,
                        statusCode: 409,
                        body: """
                            {"error":{"code":"conflict","message":"persona expected version 1 but head is 2","details":{},"request_id":"r-1"}}
                            """
                    )
                }
                return try self.response(
                    for: request, statusCode: 200, body: self.personaJSON(version: 3)
                )
            default:
                return try self.response(for: request, statusCode: 404, body: "{}")
            }
        }

        await model.load()
        model.drafts[0].text = "User values direct answers, edited."
        await model.save()

        #expect(model.conflictDetected == true)
        #expect(model.hasPendingMerge == true)
        #expect(model.drafts[0].text == "User values direct answers, edited.")
        #expect(model.version == 1)

        await model.reloadAfterConflict()
        await model.save()

        #expect(model.drafts[0].text == "User values direct answers, edited.")
        #expect(model.version == 1)
        #expect(model.conflictHead?.version == 2)
        #expect(model.conflictHead?.entries.first?.text == "User values collaborative answers.")
        #expect(model.hasPendingMerge == true)
        #expect(lock.withLock { personaWrites } == 1)

        model.resolveConflictKeepingDrafts()
        #expect(model.version == 2)
        #expect(model.conflictHead == nil)

        await model.save()

        #expect(model.version == 3)
        #expect(lock.withLock { personaWrites } == 2)
        let retryBody = try #require(lock.withLock { personaWriteBodies.last })
        let retry = try JSONSerialization.jsonObject(with: retryBody) as? [String: Any]
        #expect(retry?["expected_version"] as? Int == 2)
    }

    @Test
    func testAffirmRefreshesTheDocumentAndTheOpenSet() async throws {
        let nominationID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000601"))
        let beliefID = try #require(UUID(uuidString: "00000000-0000-0000-0000-000000000501"))
        let lock = NSLock()
        var affirmCalls = 0
        let model = try makeModel { request in
            switch (request.httpMethod, request.url?.path) {
            case ("GET", "/v1/persona"):
                return try self.response(
                    for: request, statusCode: 200, body: self.personaJSON(version: 0, entries: "")
                )
            case ("GET", "/v1/persona/nominations"):
                let affirmed = lock.withLock { affirmCalls } > 0
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: affirmed
                        ? #"{"items":[],"next_cursor":null}"#
                        : """
                        {"items":[\(self.nominationJSON(id: nominationID, beliefID: beliefID))],"next_cursor":null}
                        """
                )
            case ("POST", "/v1/persona/nominations/\(nominationID.uuidString)/affirm"):
                lock.withLock { affirmCalls += 1 }
                return try self.response(
                    for: request,
                    statusCode: 200,
                    body: self.personaJSON(version: 1, affirmedBeliefID: beliefID, soloAffirmed: true)
                )
            default:
                return try self.response(for: request, statusCode: 404, body: "{}")
            }
        }

        await model.load()
        #expect(model.nominations.count == 1)

        await model.affirm(nominationID)

        #expect(model.version == 1)
        #expect(model.drafts.count == 1)
        #expect(model.drafts[0].sourceBeliefID == beliefID)
        #expect(model.nominations.isEmpty)
    }

    @Test
    func testAnAbsentSurfaceDegradesToUnavailable() async throws {
        let model = try makeModel { request in
            try self.response(
                for: request,
                statusCode: 404,
                body: """
                    {"error":{"code":"not_found","message":"no route","details":{},"request_id":"r-2"}}
                    """
            )
        }

        await model.load()

        #expect(model.unavailable == true)
        #expect(model.errorMessage == nil)
    }

    private func makeModel(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> PersonaViewModel {
        let configuration = try ConnectionConfiguration(baseURLString: "https://veetbot.test")
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        let handlerID = PersonaViewModelURLProtocol.register(handler)
        sessionConfiguration.httpAdditionalHeaders = [
            PersonaViewModelURLProtocol.handlerHeader: handlerID
        ]
        sessionConfiguration.protocolClasses = [PersonaViewModelURLProtocol.self]
        let session = URLSession(configuration: sessionConfiguration)
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: "valid"),
            session: session
        )
        let client = VeetbotAPIClient(transport: transport)
        return PersonaViewModel(makeAPIClient: { client })
    }

    private func response(
        for request: URLRequest,
        statusCode: Int,
        body: String
    ) throws -> (HTTPURLResponse, Data) {
        let response = try #require(
            HTTPURLResponse(
                url: request.url!,
                statusCode: statusCode,
                httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )
        )
        return (response, Data(body.utf8))
    }

    private func personaJSON(
        version: Int,
        affirmedBeliefID: UUID? = nil,
        soloAffirmed: Bool = false,
        entries: String? = nil
    ) -> String {
        let renderedEntries: String
        if let entries {
            renderedEntries = entries
        } else {
            var rows = [
                """
                {"text":"User values direct answers.","source":"user_edit","source_belief_id":null,"sensitivity":"internal"}
                """
            ]
            if soloAffirmed { rows = [] }
            if let affirmedBeliefID {
                rows.append(
                    """
                    {"text":"User prefers concise answers.","source":"affirmation","source_belief_id":"\(affirmedBeliefID.uuidString)","sensitivity":"internal"}
                    """
                )
            }
            renderedEntries = rows.joined(separator: ",")
        }
        return """
            {"version":\(version),"entries":[\(renderedEntries)],"source":"user_edit","created_at":"2026-09-01T12:00:00Z"}
            """
    }

    private func nominationJSON(id: UUID, beliefID: UUID) -> String {
        """
        {"id":"\(id.uuidString)","belief_id":"\(beliefID.uuidString)","statement":"User prefers concise answers.","belief_type":"preference","authority":"affirmed","confidence":0.9,"corroboration_count":3,"sensitivity":"internal","state":"nominated","nominated_at":"2026-09-01T11:00:00Z","resolved_at":null,"affirmed_version":null}
        """
    }
}

extension URLRequest {
    fileprivate var bodyData: Data? {
        if let httpBody { return httpBody }
        guard let stream = httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let bufferSize = 4096
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: bufferSize)
        defer { buffer.deallocate() }
        while stream.hasBytesAvailable {
            let read = stream.read(buffer, maxLength: bufferSize)
            if read <= 0 { break }
            data.append(buffer, count: read)
        }
        return data
    }
}

private final class PersonaViewModelURLProtocol: URLProtocol {
    static let handlerHeader = "X-Veetbot-Persona-Test-Handler-ID"
    private static let handlerStore = PersonaViewModelURLProtocolHandlerStore()

    static func register(
        _ handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) -> String {
        handlerStore.register(handler)
    }

    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard
            let handlerID = request.value(forHTTPHeaderField: Self.handlerHeader),
            let handler = Self.handlerStore.handler(for: handlerID)
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }
        let capturedRequest = request
        DispatchQueue.global().async { [weak self] in
            guard let self else { return }
            do {
                let (response, data) = try handler(capturedRequest)
                self.client?.urlProtocol(
                    self,
                    didReceive: response,
                    cacheStoragePolicy: .notAllowed
                )
                self.client?.urlProtocol(self, didLoad: data)
                self.client?.urlProtocolDidFinishLoading(self)
            } catch {
                self.client?.urlProtocol(self, didFailWithError: error)
            }
        }
    }

    override func stopLoading() {}
}

private final class PersonaViewModelURLProtocolHandlerStore: @unchecked Sendable {
    typealias Handler = (URLRequest) throws -> (HTTPURLResponse, Data)

    private let lock = NSLock()
    private var handlers: [String: Handler] = [:]

    func register(_ handler: @escaping Handler) -> String {
        let id = UUID().uuidString
        lock.withLock { handlers[id] = handler }
        return id
    }

    func handler(for id: String) -> Handler? {
        lock.withLock { handlers[id] }
    }
}
