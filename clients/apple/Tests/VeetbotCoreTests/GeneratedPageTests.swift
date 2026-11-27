#if os(macOS)
import Foundation
import Testing
import WebKit
@testable import VeetbotCore

@MainActor
@Suite struct GeneratedPageTests {
    @Test func followsOnlyNewSameNameReplyAttachments() {
        let original = UUID(), latestAtOpen = UUID(), revision = UUID()
        func reply(_ id: UUID, _ name: String = "plot.html", role: TimelineItem.Role = .assistant) -> TimelineItem {
            TimelineItem(id: id.uuidString, role: role, content: [.file(artifactID: id, mediaType: "text/html", filename: name)])
        }
        let history = [reply(original), reply(latestAtOpen)]
        let request = GeneratedPageRequest(sessionID: UUID(), connection: UUID(), artifactID: original,
                                           name: "plot.html", timeline: history)
        #expect(request.displayedID(in: history) == original)
        #expect(request.displayedID(in: [reply(UUID())] + history) == original)
        #expect(request.displayedID(in: history + [reply(UUID(), role: .user), reply(UUID(), "notes.html")]) == original)
        #expect(request.displayedID(in: history + [reply(revision)]) == revision)
    }

    @Test func rendersEmbeddedImagesAndKeepsScriptsAndNetworkOff() async throws {
        let listener = try CockpitHostedBoardTests.Listener()
        defer { listener.listener.cancel() }
        let port = try await listener.start()
        let page = GeneratedPageDocument.makePage()
        let html = """
            <!doctype html><img id="plot" src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">
            <img src="http://127.0.0.1:\(port)/external.png">
            <script>document.title = "script ran"</script>
            """
        for try await _ in page.load(html: html) {}
        let width = try await page.callJavaScript("return document.getElementById('plot').naturalWidth",
                                                  contentWorld: .defaultClient) as? Int
        #expect(width == 1)
        #expect(page.title != "script ran")
        #expect(listener.connections == 0)
    }

    @Test func recognizesHTMLIncludingGenericOlderExports() {
        #expect(GeneratedPageRequest.isHTML("Text/HTML; charset=UTF-8", name: "notes"))
        #expect(GeneratedPageRequest.isHTML("application/octet-stream", name: "PLOT.HTM"))
        #expect(GeneratedPageRequest.isHTML("text/plain", name: "plot.html"))
        #expect(!GeneratedPageRequest.isHTML("text/markdown", name: "notes.md"))
        #expect(!GeneratedPageRequest.isHTML("image/png", name: "plot.html"))
    }
}
#endif
