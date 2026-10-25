import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0251 decisions 4 and 6: the card's words, and Website Access opened
/// from a chat connecting only that chat.
@Suite struct WebsiteOfferCardTests {
    private static func offer(
        connected: String? = nil,
        websites: [(origin: String, status: String)] = [("https://mychart.example-health.org", "ready")]
    ) throws -> WebsiteOffer {
        let items = websites.map { website in
            #"{"profile_id":"\#(UUID().uuidString)","origins":["\#(website.origin)"],"status":"\#(website.status)"}"#
        }
        let connectedJSON = connected.map { "\"\($0)\"" } ?? "null"
        let json = #"{"offer_id":"\#(UUID().uuidString)","source_sequence":50,"connected_origin":\#(connectedJSON),"websites":[\#(items.joined(separator: ","))],"expires_at":"2026-10-26T17:14:49Z"}"#
        return try JSONDecoder.server.decode(WebsiteOffer.self, from: Data(json.utf8))
    }

    @Test
    func testOneWebsiteNamesItsHost() throws {
        #expect(WebsiteOfferCard.title(for: try Self.offer()) == "Connect mychart.example-health.org to this chat")
    }

    @Test
    func testAChatOnAnotherWebsiteIsOfferedASwitch() throws {
        let offer = try Self.offer(connected: "https://x.com")
        #expect(WebsiteOfferCard.title(for: offer) == "Switch this chat to mychart.example-health.org")
    }

    @Test
    func testSeveralWebsitesShareOneTitle() throws {
        let offer = try Self.offer(websites: [
            ("https://mychart.example-health.org", "ready"), ("https://www.amazon.com", "ready"),
        ])
        #expect(WebsiteOfferCard.title(for: offer) == "Connect a saved website to this chat")
    }

    @Test
    func testAWebsiteThatNeedsSignInSaysSo() throws {
        let offer = try Self.offer(websites: [("https://mychart.example-health.org", "authentication_required")])
        let website = try #require(offer.websites.first)
        #expect(WebsiteOfferCard.actionTitle(for: website) == "Sign in and continue")
        let ready = try #require(try Self.offer().websites.first)
        #expect(WebsiteOfferCard.actionTitle(for: ready) == "Connect and continue")
    }

    @Test
    func testWebsiteAccessFromAChatConnectsOnlyThatChat() throws {
        let settings = try Self.source(at: "Veetbot/Views/ConnectionSettingsView.swift")
        let chat = try Self.source(at: "Veetbot/Views/ChatView.swift")

        #expect(settings.contains("Button(\"Use in this chat\")"))
        #expect(settings.contains("\"Connected to this chat\""))
        #expect(settings.contains("await model.connectWebsiteToSelectedChat(profile.id)"))
        #expect(settings.contains("Button(\"Use for new chats\")"))
        #expect(chat.contains("ConnectionSettingsView(model: model, embedded: false, forChat: true)"))
        #expect(chat.contains("WebsiteOfferCard("))
    }

    private static func source(at relativePath: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(contentsOf: root.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
