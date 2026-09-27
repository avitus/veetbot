#if os(macOS)
import AppKit
import Foundation
import SwiftUI
import Testing

@testable import VeetbotCore

/// macOS opens a sheet at its content's minimum width and ignores the ideal
/// width unless fitted presentation sizing is set, so these sheets opened at
/// the system floor instead of their intended reading widths. Each test
/// presents the sheet from a desktop-sized window and measures the sheet window.
@Suite(.serialized) @MainActor struct SheetLayoutTests {
    @Test func personaEditorOpensAtItsIdealWidth() async throws {
        let width = try await presentedSheetWidth { PersonaEditorView(model: PersonaViewModel(makeAPIClient: { nil })) }
        #expect(width >= Self.openingWidth(ideal: 640, minimum: 520))
    }

    @Test func callResultOpensWideEnoughToReadTheTranscript() async throws {
        let result = CallResultViewData(
            callID: UUID(), direction: "outbound", status: "completed", counterparty: "+15555550100",
            summary: "Confirmed the Thursday appointment.", transcript: Self.transcript,
            summaryComplete: true, transcriptComplete: true, erased: false
        )
        let width = try await presentedSheetWidth { CallResultSheet(result: result, close: {}, delete: {}) }
        #expect(width >= Self.openingWidth(ideal: 540, minimum: 480))
    }

    @Test func emailLearningOpensAtItsIdealWidth() async throws {
        let model = EmailViewModel(makeAPIClient: { nil })
        let width = try await presentedSheetWidth { EmailLearningScreen(model: model) }
        #expect(width >= Self.openingWidth(ideal: 540, minimum: 480))
    }

    @Test func draftHistoryOpensAtItsIdealWidth() async throws {
        let model = EmailViewModel(makeAPIClient: { nil })
        let width = try await presentedSheetWidth { EmailRevisionsScreen(model: model) }
        #expect(width >= Self.openingWidth(ideal: 600, minimum: 520))
    }

    @Test func sendReviewOpensAtItsIdealWidth() async throws {
        let model = EmailViewModel(makeAPIClient: { nil })
        let width = try await presentedSheetWidth { EmailSendReview(model: model) }
        #expect(width >= Self.openingWidth(ideal: 600, minimum: 520))
    }

    @Test func artifactViewerOpensAtItsIdealWidth() async throws {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore()
        )
        // Loading fails without a connection; the sheet still opens at its width.
        let width = try await presentedSheetWidth(
            inspect: { sheet in
                let content = try #require(sheet.contentView)
                #expect(!Self.containsSplitView(content),
                        "A single artifact must occupy the sheet, not a navigation sidebar.")
            }
        ) { ArtifactViewerView(model: model, artifactID: UUID()) }
        #expect(width >= Self.openingWidth(ideal: 760, minimum: 680))
    }

    @Test func svgDownloadUsesTheFullDocumentSheet() async throws {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defer { defaults.removePersistentDomain(forName: suiteName) }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [ArtifactLayoutURLProtocol.self]
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: URLSession(configuration: configuration)
        )
        #expect(await model.configure(baseURLString: "https://artifact-layout.test", token: "fixture"))
        _ = try await presentedSheetWidth(inspect: { sheet in
            let content = try #require(sheet.contentView)
            // Let the mocked metadata/content request and SwiftUI update finish.
            try await Task.sleep(nanoseconds: 300_000_000)
            #expect(!Self.containsSplitView(content))
            #expect(content.bounds.width >= Self.openingWidth(ideal: 760, minimum: 680))

        }) {
            ArtifactViewerView(model: model, artifactID: ArtifactLayoutURLProtocol.artifactID)
        }
    }

    @Test func messageTextSheetOpensAtItsIdealWidth() async throws {
        let selection = MessageTextSelection(
            id: "event-2", rendition: MarkdownRendition(markdown: Self.transcript)
        )
        let width = try await presentedSheetWidth { MessageTextSheet(selection: selection) }
        #expect(width >= Self.openingWidth(ideal: 680, minimum: 520))
    }

    @Test func aRedrawWithTheSameMessageKeepsTheSelection() throws {
        let rendition = MarkdownRendition(markdown: "First paragraph\n\nSecond paragraph")
        let host = NSHostingView(rootView: SelectableMessageText(attributedText: rendition.attributedText))
        host.frame = NSRect(x: 0, y: 0, width: 480, height: 320)
        host.layoutSubtreeIfNeeded()
        let textView = try #require(Self.textView(in: host))
        #expect(textView.isEditable == false)
        #expect(textView.isSelectable)
        textView.setSelectedRange(NSRange(location: 6, length: 20))

        host.rootView = SelectableMessageText(attributedText: rendition.attributedText)
        host.layoutSubtreeIfNeeded()

        #expect(textView.selectedRange() == NSRange(location: 6, length: 20))
        #expect(textView.string == rendition.attributedText.string)
    }

    private static func containsSplitView(_ view: NSView) -> Bool {
        view is NSSplitView || view.subviews.contains(where: containsSplitView)
    }

    private static func textView(in view: NSView) -> NSTextView? {
        if let textView = view as? NSTextView { return textView }
        for subview in view.subviews {
            if let found = textView(in: subview) { return found }
        }
        return nil
    }

    /// macOS 15 and later open a fitted sheet at its ideal width; earlier
    /// releases open it at the content's minimum width.
    private static func openingWidth(ideal: CGFloat, minimum: CGFloat) -> CGFloat {
        if #available(macOS 15, *) { return ideal }
        return minimum
    }

    private static let transcript = Array(
        repeating: "Agent: Hello, I am calling to confirm the appointment on Thursday afternoon.",
        count: 12
    ).joined(separator: "\n")
}

/// Presents `sheet` from a desktop-sized window and returns the settled sheet width.
@MainActor
func presentedSheetWidth<Sheet: View>(
    inspect: (NSWindow) async throws -> Void = { _ in },
    @ViewBuilder _ sheet: @escaping () -> Sheet
) async throws -> CGFloat {
    _ = NSApplication.shared
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1400, height: 1000),
                          styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = NSHostingView(rootView: Color.clear.sheet(isPresented: .constant(true), content: sheet))
    window.orderFront(nil)
    defer {
        window.attachedSheet.map { window.endSheet($0) }
        window.orderOut(nil)
        window.contentView = nil
    }
    var settled: CGFloat?
    var previous: CGFloat?
    for _ in 0..<60 {
        try await Task.sleep(nanoseconds: 50_000_000)
        let width = window.attachedSheet?.frame.width
        if let width, width == previous { settled = width; break }
        previous = width
    }
    let width = try #require(settled, "the sheet never presented")
    try await inspect(#require(window.attachedSheet))
    return width
}

private final class ArtifactLayoutURLProtocol: URLProtocol {
    static let artifactID = UUID(uuidString: "00000000-0000-0000-0000-000000000123")!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let url = request.url!
        let svg = "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"400\" height=\"200\"><rect width=\"400\" height=\"200\" fill=\"teal\"/></svg>"
        let body: String
        if url.path.hasSuffix("/content") {
            body = svg
        } else if url.path.hasSuffix(Self.artifactID.uuidString) {
            body = """
            {"id":"\(Self.artifactID)","session_id":"\(Self.artifactID)","run_id":null,"name":"Quarterly-market-overview-and-portfolio-summary.svg","media_type":"image/svg+xml","sha256":"abc","size_bytes":\(svg.utf8.count),"metadata":{},"created_at":"2026-09-27T00:00:00Z"}
            """
        } else {
            body = #"{"items":[],"next_cursor":null}"#
        }
        let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
#endif
