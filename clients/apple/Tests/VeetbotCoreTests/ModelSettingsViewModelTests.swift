import Foundation
import Testing

@testable import VeetbotCore

/// The owner's model choices (`GET`/`PUT /v1/settings/models`): every picker
/// change is one guarded PUT of the whole state, a lost version race reloads
/// the server's values, and any other failure falls back to the last state
/// the server confirmed.
@Suite(.serialized) @MainActor struct ModelSettingsViewModelTests {
    @Test
    func testLoadAppliesTheServerStateAndItsOptions() async throws {
        let model = try makeModel { request in
            try self.route(request, get: { self.settingsJSON(version: 3) })
        }

        await model.load()

        #expect(model.settings?.version == 3)
        #expect(model.chat == ModelChoice(modelPolicy: "astra", reasoningEffort: .high))
        #expect(model.memory == ModelChoice(modelPolicy: "balanced", reasoningEffort: nil))
        #expect(model.chatOptions.map(\.modelPolicy) == ["astra", "fable", "balanced"])
        #expect(model.chatEffortChoices == [.low, .medium, .high, .xhigh, .max])
        #expect(model.memoryModelChoices.map(\.modelPolicy) == ["balanced", "astra"])
        #expect(model.memoryEffortChoices == [nil])
        #expect(model.isEditable)
        #expect(model.unavailable == false)
        #expect(model.statusMessage == nil)
    }

    @Test
    func testAnUnsavedOwnerSeesTheDeploymentDefaults() async throws {
        let model = try makeModel { request in
            try self.route(request, get: { self.settingsJSON(version: 0) })
        }

        await model.load()

        #expect(model.settings?.version == 0)
        #expect(model.chat == ModelChoice(modelPolicy: "astra", reasoningEffort: .high))
        #expect(model.memory == ModelChoice(modelPolicy: "balanced", reasoningEffort: nil))
        #expect(model.isEditable)
    }

    @Test
    func testSwitchingTheChatModelTakesItsDefaultEffortAndSavesTheWholeState() async throws {
        let recorder = ModelSettingsRequestRecorder()
        let model = try makeModel { request in
            try self.route(
                request,
                recorder: recorder,
                get: { self.settingsJSON(version: 3) },
                put: {
                    self.settingsJSON(
                        version: 4, chat: #"{"model_policy":"fable","reasoning_effort":"high"}"#
                    )
                }
            )
        }
        await model.load()

        await model.selectChatModel("fable")

        #expect(model.chat == ModelChoice(modelPolicy: "fable", reasoningEffort: .high))
        #expect(model.settings?.version == 4)
        #expect(model.statusMessage == nil)
        #expect(model.isSaving == false)
        let body = try #require(recorder.puts.first)
        let sent = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(sent["expected_version"] as? Int == 3)
        let chat = try #require(sent["chat"] as? [String: Any])
        #expect(chat["model_policy"] as? String == "fable")
        #expect(chat["reasoning_effort"] as? String == "high")
        let memory = try #require(sent["memory"] as? [String: Any])
        #expect(memory["model_policy"] as? String == "balanced")
        #expect(memory["reasoning_effort"] is NSNull, "a null effort is sent explicitly, never omitted")
        #expect(Set(sent.keys) == ["expected_version", "chat", "memory"])
    }

    @Test
    func testAChatModelWithoutAListedDefaultTakesItsFirstEffortOrNone() async throws {
        let recorder = ModelSettingsRequestRecorder()
        let options = """
            {"model_policy":"astra","display_name":"GPT-6 Astra","provider":"openai","model":"gpt-6-astra","reasoning_efforts":["low","medium","high","xhigh","max"],"default_reasoning_effort":"high"},
            {"model_policy":"sol","display_name":"Sol","provider":"openai","model":"sol","reasoning_efforts":["medium","high"],"default_reasoning_effort":"max"},
            {"model_policy":"terse","display_name":"Terse","provider":"openai","model":"terse","reasoning_efforts":["low","high"],"default_reasoning_effort":null},
            {"model_policy":"local","display_name":"Local model","provider":"local","model":"llama","reasoning_efforts":[],"default_reasoning_effort":null}
            """
        let model = try makeModel { request in
            try self.route(
                request,
                recorder: recorder,
                get: { self.settingsJSON(version: 3, chatOptions: options) },
                put: { self.settingsJSON(version: 3, chatOptions: options) }
            )
        }
        await model.load()

        await model.selectChatModel("sol")
        await model.selectChatModel("terse")
        await model.selectChatModel("local")

        let chats = try recorder.puts.map { body -> [String: Any] in
            let sent = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
            return try #require(sent["chat"] as? [String: Any])
        }
        try #require(chats.count == 3)
        #expect(chats.map { $0["model_policy"] as? String } == ["sol", "terse", "local"])
        #expect(chats[0]["reasoning_effort"] as? String == "medium")
        #expect(chats[1]["reasoning_effort"] as? String == "low")
        #expect(chats[2]["reasoning_effort"] is NSNull)
    }

    @Test
    func testTheChatEffortPickerIsEmptyForAModelWithoutEfforts() async throws {
        let options = """
            {"model_policy":"local","display_name":"Local model","provider":"local","model":"llama","reasoning_efforts":[],"default_reasoning_effort":null}
            """
        let model = try makeModel { request in
            try self.route(
                request,
                get: {
                    self.settingsJSON(
                        version: 1,
                        chat: #"{"model_policy":"local","reasoning_effort":null}"#,
                        chatOptions: options
                    )
                }
            )
        }

        await model.load()

        #expect(model.chat == ModelChoice(modelPolicy: "local", reasoningEffort: nil))
        #expect(model.chatEffortChoices.isEmpty)
    }

    @Test
    func testChangingOnlyTheChatEffortSavesIt() async throws {
        let recorder = ModelSettingsRequestRecorder()
        let model = try makeModel { request in
            try self.route(
                request,
                recorder: recorder,
                get: { self.settingsJSON(version: 3) },
                put: {
                    self.settingsJSON(
                        version: 4, chat: #"{"model_policy":"astra","reasoning_effort":"xhigh"}"#
                    )
                }
            )
        }
        await model.load()

        await model.selectChatEffort(.xhigh)

        #expect(model.chat == ModelChoice(modelPolicy: "astra", reasoningEffort: .xhigh))
        let body = try #require(recorder.puts.first)
        let sent = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        let chat = try #require(sent["chat"] as? [String: Any])
        #expect(chat["model_policy"] as? String == "astra")
        #expect(chat["reasoning_effort"] as? String == "xhigh")
    }

    @Test
    func testReselectingTheCurrentValueSendsNothing() async throws {
        let recorder = ModelSettingsRequestRecorder()
        let model = try makeModel { request in
            try self.route(
                request,
                recorder: recorder,
                get: { self.settingsJSON(version: 3) },
                put: { self.settingsJSON(version: 4) }
            )
        }
        await model.load()

        await model.selectChatModel("astra")
        await model.selectChatEffort(.high)
        await model.selectMemoryModel("balanced")
        await model.selectMemoryEffort(nil)

        #expect(recorder.puts.isEmpty)
        #expect(model.settings?.version == 3)
    }

    @Test
    func testSwitchingTheMemoryModelSelectsItsFirstListedCombination() async throws {
        let recorder = ModelSettingsRequestRecorder()
        let memoryOptions = """
            {"model_policy":"balanced","display_name":"GPT-5.6 Sol","provider":"openai","model":"gpt-5.6-sol","reasoning_effort":null},
            {"model_policy":"astra","display_name":"GPT-6 Astra","provider":"openai","model":"gpt-6-astra","reasoning_effort":"medium"},
            {"model_policy":"astra","display_name":"GPT-6 Astra","provider":"openai","model":"gpt-6-astra","reasoning_effort":"high"}
            """
        let model = try makeModel { request in
            try self.route(
                request,
                recorder: recorder,
                get: { self.settingsJSON(version: 3, memoryOptions: memoryOptions) },
                put: {
                    self.settingsJSON(
                        version: 4,
                        memory: #"{"model_policy":"astra","reasoning_effort":"medium"}"#,
                        memoryOptions: memoryOptions
                    )
                }
            )
        }
        await model.load()
        #expect(model.memoryModelChoices.map(\.modelPolicy) == ["balanced", "astra"])

        await model.selectMemoryModel("astra")

        #expect(model.memory == ModelChoice(modelPolicy: "astra", reasoningEffort: .medium))
        #expect(model.memoryEffortChoices == [.medium, .high])
        let body = try #require(recorder.puts.first)
        let sent = try #require(JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(sent["expected_version"] as? Int == 3)
        let memory = try #require(sent["memory"] as? [String: Any])
        #expect(memory["model_policy"] as? String == "astra")
        #expect(memory["reasoning_effort"] as? String == "medium")
        let chat = try #require(sent["chat"] as? [String: Any])
        #expect(chat["model_policy"] as? String == "astra")
        #expect(chat["reasoning_effort"] as? String == "high")
    }

    @Test
    func testChangingTheMemoryEffortStaysWithinTheSelectedModel() async throws {
        let recorder = ModelSettingsRequestRecorder()
        let memoryOptions = """
            {"model_policy":"astra","display_name":"GPT-6 Astra","provider":"openai","model":"gpt-6-astra","reasoning_effort":"medium"},
            {"model_policy":"astra","display_name":"GPT-6 Astra","provider":"openai","model":"gpt-6-astra","reasoning_effort":"high"}
            """
        let model = try makeModel { request in
            try self.route(
                request,
                recorder: recorder,
                get: {
                    self.settingsJSON(
                        version: 2,
                        memory: #"{"model_policy":"astra","reasoning_effort":"medium"}"#,
                        memoryOptions: memoryOptions
                    )
                },
                put: {
                    self.settingsJSON(
                        version: 3,
                        memory: #"{"model_policy":"astra","reasoning_effort":"high"}"#,
                        memoryOptions: memoryOptions
                    )
                }
            )
        }
        await model.load()

        await model.selectMemoryEffort(.low)
        #expect(recorder.puts.isEmpty, "an effort not listed for the model is never sent")

        await model.selectMemoryEffort(.high)

        #expect(model.memory == ModelChoice(modelPolicy: "astra", reasoningEffort: .high))
        #expect(recorder.puts.count == 1)
    }

    @Test
    func testAStaleVersionReloadsAndKeepsTheServerValues() async throws {
        let lock = NSLock()
        var reads = 0
        let model = try makeModel { request in
            try self.route(
                request,
                get: {
                    let read = lock.withLock {
                        reads += 1
                        return reads
                    }
                    return read == 1
                        ? self.settingsJSON(version: 3)
                        : self.settingsJSON(
                            version: 5,
                            chat: #"{"model_policy":"fable","reasoning_effort":"xhigh"}"#
                        )
                },
                putStatus: 409,
                put: {
                    #"{"error":{"code":"conflict","message":"model settings expected version 3 but head is 5","details":{},"request_id":"r-1"}}"#
                }
            )
        }
        await model.load()

        await model.selectChatModel("balanced")

        #expect(model.settings?.version == 5)
        #expect(model.chat == ModelChoice(modelPolicy: "fable", reasoningEffort: .xhigh))
        #expect(model.statusMessage == "Settings changed elsewhere; reloaded.")
        #expect(model.isSaving == false)
        #expect(lock.withLock { reads } == 2)
    }

    @Test
    func testARejectedChoiceRevertsToTheLastServerState() async throws {
        let model = try makeModel { request in
            try self.route(
                request,
                get: { self.settingsJSON(version: 3) },
                putStatus: 400,
                put: {
                    #"{"error":{"code":"malformed_request","message":"chat choice fable/high is not offered","details":{},"request_id":"r-2"}}"#
                }
            )
        }
        await model.load()

        await model.selectChatModel("fable")

        #expect(model.chat == ModelChoice(modelPolicy: "astra", reasoningEffort: .high))
        #expect(model.memory == ModelChoice(modelPolicy: "balanced", reasoningEffort: nil))
        #expect(model.settings?.version == 3)
        #expect(model.statusMessage == "chat choice fable/high is not offered")
        #expect(model.isEditable)
    }

    @Test
    func testAMissingScopeExplainsWhichScopesAreNeeded() async throws {
        let forbidden = #"{"error":{"code":"authorization_error","message":"missing scope","details":{},"request_id":"r-3"}}"#
        let readDenied = try makeModel { request in
            try self.route(request, getStatus: 403, get: { forbidden })
        }

        await readDenied.load()

        #expect(readDenied.settings == nil)
        #expect(readDenied.isEditable == false)
        #expect(
            readDenied.statusMessage
                == "Model settings need the settings.read and settings.write scopes on the server."
        )

        let writeDenied = try makeModel { request in
            try self.route(
                request,
                get: { self.settingsJSON(version: 3) },
                putStatus: 403,
                put: { forbidden }
            )
        }
        await writeDenied.load()

        await writeDenied.selectChatEffort(.max)

        #expect(writeDenied.chat == ModelChoice(modelPolicy: "astra", reasoningEffort: .high))
        #expect(
            writeDenied.statusMessage
                == "Model settings need the settings.read and settings.write scopes on the server."
        )
    }

    @Test
    func testAServerWithoutTheResourceHidesThePickers() async throws {
        for statusCode in [404, 405] {
            let model = try makeModel { request in
                try self.route(
                    request,
                    getStatus: statusCode,
                    get: {
                        #"{"error":{"code":"not_found","message":"The requested resource was not found.","details":{},"request_id":"r-4"}}"#
                    }
                )
            }

            await model.load()

            #expect(model.unavailable == true)
            #expect(model.isEditable == false)
            #expect(model.statusMessage == "This server doesn't support model settings yet.")
        }
    }

    @Test
    func testAWriteToAServerWithoutTheResourceHidesThePickers() async throws {
        let model = try makeModel { request in
            try self.route(
                request,
                get: { self.settingsJSON(version: 3) },
                putStatus: 404,
                put: {
                    #"{"error":{"code":"not_found","message":"The requested resource was not found.","details":{},"request_id":"r-5"}}"#
                }
            )
        }
        await model.load()

        await model.selectChatModel("fable")

        #expect(model.unavailable == true)
        #expect(model.isEditable == false)
        #expect(model.statusMessage == "This server doesn't support model settings yet.")
    }

    @Test
    func testAMissingConnectionIsExplainedRatherThanLoaded() async {
        let model = ModelSettingsViewModel(makeAPIClient: { nil })

        await model.load()

        #expect(model.settings == nil)
        #expect(model.isEditable == false)
        #expect(model.statusMessage == "Configure a Veetbot server connection first.")
    }

    // MARK: - Fixtures

    private func makeModel(
        handler: @escaping (URLRequest) throws -> (HTTPURLResponse, Data)
    ) throws -> ModelSettingsViewModel {
        let configuration = try ConnectionConfiguration(baseURLString: "https://veetbot.test")
        let sessionConfiguration = URLSessionConfiguration.ephemeral
        let handlerID = ModelSettingsURLProtocol.register(handler)
        sessionConfiguration.httpAdditionalHeaders = [
            ModelSettingsURLProtocol.handlerHeader: handlerID
        ]
        sessionConfiguration.protocolClasses = [ModelSettingsURLProtocol.self]
        let session = URLSession(configuration: sessionConfiguration)
        let transport = HTTPTransport(
            configuration: configuration,
            tokenStore: InMemoryTokenStore(token: "valid"),
            session: session
        )
        let client = VeetbotAPIClient(transport: transport)
        return ModelSettingsViewModel(makeAPIClient: { client })
    }

    /// Serves the one resource: GET and PUT answer with the given bodies,
    /// and every PUT body is recorded; any other route is a 404.
    private func route(
        _ request: URLRequest,
        recorder: ModelSettingsRequestRecorder? = nil,
        getStatus: Int = 200,
        get: () -> String,
        putStatus: Int = 200,
        put: () -> String = { "{}" }
    ) throws -> (HTTPURLResponse, Data) {
        switch (request.httpMethod, request.url?.path) {
        case ("GET", "/v1/settings/models"):
            return try response(for: request, statusCode: getStatus, body: get())
        case ("PUT", "/v1/settings/models"):
            recorder?.record(request)
            return try response(for: request, statusCode: putStatus, body: put())
        default:
            return try response(for: request, statusCode: 404, body: "{}")
        }
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

    nonisolated private static let defaultChatOptions = """
        {"model_policy":"astra","display_name":"GPT-6 Astra","provider":"openai","model":"gpt-6-astra","reasoning_efforts":["low","medium","high","xhigh","max"],"default_reasoning_effort":"high"},
        {"model_policy":"fable","display_name":"Claude Fable 5.1","provider":"anthropic","model":"claude-fable-5-1","reasoning_efforts":["low","medium","high","xhigh","max"],"default_reasoning_effort":"high"},
        {"model_policy":"balanced","display_name":"GPT-5.6 Sol","provider":"openai","model":"gpt-5.6-sol","reasoning_efforts":["low","medium","high","xhigh","max"],"default_reasoning_effort":"high"}
        """

    nonisolated private static let defaultMemoryOptions = """
        {"model_policy":"balanced","display_name":"GPT-5.6 Sol","provider":"openai","model":"gpt-5.6-sol","reasoning_effort":null},
        {"model_policy":"astra","display_name":"GPT-6 Astra","provider":"openai","model":"gpt-6-astra","reasoning_effort":"medium"}
        """

    private func settingsJSON(
        version: Int,
        chat: String = #"{"model_policy":"astra","reasoning_effort":"high"}"#,
        memory: String = #"{"model_policy":"balanced","reasoning_effort":null}"#,
        chatOptions: String = defaultChatOptions,
        memoryOptions: String = defaultMemoryOptions
    ) -> String {
        """
        {"version":\(version),"chat":\(chat),"memory":\(memory),"chat_options":[\(chatOptions)],"memory_options":[\(memoryOptions)]}
        """
    }
}

/// Records every PUT body; a stream-backed request exposes it only through
/// `httpBodyStream`.
private final class ModelSettingsRequestRecorder: @unchecked Sendable {
    private let lock = NSLock()
    private var bodies: [Data] = []

    var puts: [Data] { lock.withLock { bodies } }

    func record(_ request: URLRequest) {
        var body = request.httpBody
        if body == nil, let stream = request.httpBodyStream {
            stream.open()
            defer { stream.close() }
            var collected = Data()
            var buffer = [UInt8](repeating: 0, count: 1_024)
            while stream.hasBytesAvailable {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                collected.append(buffer, count: count)
            }
            body = collected
        }
        lock.withLock { bodies.append(body ?? Data()) }
    }
}

private final class ModelSettingsURLProtocol: URLProtocol {
    static let handlerHeader = "X-Veetbot-Model-Settings-Test-Handler-ID"
    private static let handlerStore = ModelSettingsURLProtocolHandlerStore()

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

private final class ModelSettingsURLProtocolHandlerStore: @unchecked Sendable {
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
