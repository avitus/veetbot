import Testing
@testable import VeetbotCore

struct IntegrationSecretsTests {
    @Test func settingsOffersIntegrationKeys() {
        #expect(ConnectionSettingsSection.allCases.contains { $0.rawValue == "integrationKeys" })
    }
}

import Foundation

extension IntegrationSecretsTests {
    @Test func writesRetryWithStableIdentityAndReadsContainOnlyMetadata() async throws {
        let id = UUID(uuidString: "00000000-0000-0000-0000-000000000270")!
        let metadata = #"{"id":"00000000-0000-0000-0000-000000000270","reference":"stored:00000000-0000-0000-0000-000000000270","label":"Example","origin":"https://api.example.com","auth_scheme":"bearer","revision":1,"created_at":"2026-10-30T12:00:00Z","updated_at":"2026-10-30T12:00:00Z"}"#
        let server = DeviceFlowServer { request in
            switch request.httpMethod {
            case "GET": return (200, "{\"secrets\":[\(metadata)]}")
            case "PUT": return (200, metadata)
            case "DELETE":
                #expect(request.value(forHTTPHeaderField: "If-Match") == "1")
                return (204, "")
            default: return nil
            }
        }
        let api = VeetbotAPIClient(transport: HTTPTransport(
            configuration: try ConnectionConfiguration(baseURLString: "https://veetbot.test"),
            tokenStore: InMemoryTokenStore(token: "test-token"),
            session: URLSession(configuration: server.configuration(base: .ephemeral))
        ))
        for _ in 0..<2 {
            let result = try await api.saveIntegrationKey(
                id: id, label: "Example", origin: "https://api.example.com", authScheme: "bearer",
                value: "synthetic-client-canary", expectedRevision: 0
            )
            #expect(result.revision == 1)
            #expect(result.reference == "stored:\(id.uuidString.lowercased())")
        }
        let route = "PUT veetbot.test /v1/integration-secrets/\(id.uuidString.lowercased())"
        #expect(server.routes.filter { $0 == route }.count == 2)
        #expect(server.json(of: route)?["value"] as? String == "synthetic-client-canary")
        #expect(server.json(of: route)?["expected_revision"] as? Int == 0)
        #expect(server.entry(of: route)?.authorization == "Bearer test-token")
        let keys = try await api.listIntegrationKeys().secrets
        #expect(keys.count == 1)
        #expect(!String(describing: keys).contains("synthetic-client-canary"))
        #expect(!Mirror(reflecting: keys[0]).children.contains { $0.label == "value" })
        let draft = IntegrationKeyDraft(key: keys[0])
        #expect(draft.id == id)
        #expect(!Mirror(reflecting: draft).children.contains { $0.label == "value" })
        try await api.deleteIntegrationKey(keys[0])
    }
}
