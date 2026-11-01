#if os(macOS)
import AppKit
import Foundation
import SwiftUI
import Testing

@testable import VeetbotCore

/// A Mac sheet is user-resizable only when it sets a presentation sizing and
/// its frame leaves room to grow; without both, macOS locks the sheet at its
/// opening size. The website sign-in sheet was locked that way, which made
/// signing in to a website that needs more room than the default harder than
/// it should be. Each test presents a real sheet and inspects its window.
@Suite(.serialized) @MainActor struct MacSheetResizabilityTests {
    @Test func websiteSignInSheetResizes() async throws {
        let model = try await configuredChatModel()
        #expect(model.beginDeviceSignIn(websiteURL: "https://sign-in.test") != nil)
        let sheet = try await presentedSheetWindow { Color.clear.deviceSignInPresentation(model: model) }
        Self.expectResizable(sheet)
        #expect(sheet.contentMinSize.width >= 820)
        #expect(sheet.contentMinSize.height >= 680)
    }

    /// ADR-0277: the chat presents the sign-in window its sign-in card starts.
    @Test func chatPresentsTheSignInWindowItsCardStarts() async throws {
        let model = try await configuredChatModel()
        let profile = try JSONDecoder.server.decode(BrowserProfileView.self, from: Data(Self.profileJSON.utf8))
        #expect(model.beginDeviceSignIn(profile: profile, from: .chat) != nil)
        let sheet = try await presentedSheetWindow {
            Color.clear.deviceSignInPresentation(model: model, surface: .chat)
        }
        Self.expectResizable(sheet)
    }

    @Test func websiteAccessSheetResizes() async throws {
        let model = try await configuredChatModel()
        let sheet = try await presentedSheetWindow {
            Color.clear.sheet(isPresented: .constant(true)) {
                ConnectionSettingsView(model: model, embedded: false).websiteAccessSheetFrame()
            }
        }
        Self.expectResizable(sheet)
    }

    @Test func sheetFrameResizesUnlessCapped() async throws {
        let sheet = try await presentedSheetWindow {
            Color.clear.sheet(isPresented: .constant(true)) {
                Text("Name").sheetFrame(minWidth: 300, macMinWidth: 420, idealWidth: 480, minHeight: 0)
            }
        }
        Self.expectResizable(sheet)
    }

    @Test func scheduleCreateSheetResizes() async throws {
        let model = ScheduleViewModel(makeAPIClient: { nil })
        let sheet = try await presentedSheetWindow {
            Color.clear.sheet(isPresented: .constant(true)) { ScheduleCreateSheet(model: model) }
        }
        Self.expectResizable(sheet)
    }

    @Test func memoryWordingSheetResizes() async throws {
        let memory = try JSONDecoder.server.decode(MemoryView.self, from: Data(Self.memoryJSON.utf8))
        let model = MemoryViewModel(makeAPIClient: { nil })
        let sheet = try await presentedSheetWindow {
            Color.clear.sheet(isPresented: .constant(true)) {
                MemoryWordingSheet(memory: memory, model: model) { _ in }
            }
        }
        Self.expectResizable(sheet)
    }

    @Test func peopleLookupSheetResizes() async throws {
        let sheet = try await presentedSheetWindow {
            Color.clear.sheet(isPresented: .constant(true)) { PeopleLookupSheet(lookup: PeopleLookup()) }
        }
        Self.expectResizable(sheet)
    }

    private static func expectResizable(_ sheet: NSWindow, sourceLocation: SourceLocation = #_sourceLocation) {
        #expect(sheet.styleMask.contains(.resizable), "the sheet window has no resize handle", sourceLocation: sourceLocation)
        #expect(sheet.contentMaxSize.width > sheet.contentMinSize.width,
                "the sheet cannot grow wider", sourceLocation: sourceLocation)
        #expect(sheet.contentMaxSize.height > sheet.contentMinSize.height,
                "the sheet cannot grow taller", sourceLocation: sourceLocation)
    }

    private func configuredChatModel() async throws -> ChatViewModel {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [EmptyListURLProtocol.self]
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: URLSession(configuration: configuration)
        )
        #expect(await model.configure(baseURLString: "https://sheet-resize.test", token: "fixture"))
        return model
    }

    private static let profileJSON = """
    {"id":"00000000-0000-0000-0000-000000000301","allowed_origins":["https://example.org"],"status":"needs_user","generation":2,"created_at":"2026-10-01T00:00:00Z","updated_at":"2026-10-30T00:00:00Z","last_used_at":null}
    """

    private static let memoryJSON = """
    {"id":"00000000-0000-0000-0000-000000000201","subject":"owner","statement":"Prefers morning meetings.","belief_type":"preference","claim_kind":"preference","derivation":"direct","longevity":"durable","status":"active","polarity":"assert","scope":"session","portability":"portable","authority":"user","sensitivity":"normal","confidence":0.9,"corroboration_count":1,"flagged_for_review":false,"conflicts_with":[],"superseded_by":null,"source_session_id":"00000000-0000-0000-0000-000000000203","source_event_ids":[10],"formation_run_id":"00000000-0000-0000-0000-000000000900","consolidation_policy_version":"formation@1","origin_scopes":["session"],"valid_from":"2026-08-01T00:00:00Z","valid_to":null,"expires_at":null,"last_evidence_at":"2026-08-15T00:00:00Z","last_used_at":null,"last_reinforced_at":"2026-08-15T00:00:00Z","created_at":"2026-07-01T00:00:00Z","updated_at":"2026-08-20T00:00:00Z"}
    """
}

/// Hosts `host` in a desktop-sized window and returns its settled sheet window.
/// The sheet gets its final frame before SwiftUI applies its size limits, and
/// while the display sleeps that update can trail the frame by several polls
/// of a busy full run, leaving AppKit's defaults (a zero minimum) to be read.
/// So the sheet has settled only once its frame and limits hold still and its
/// minimum is set.
@MainActor
private func presentedSheetWindow<Host: View>(@ViewBuilder _ host: () -> Host) async throws -> NSWindow {
    _ = NSApplication.shared
    let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1600, height: 1100),
                          styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
    window.isReleasedWhenClosed = false
    window.contentView = NSHostingView(rootView: host())
    window.orderFront(nil)
    defer {
        window.attachedSheet.map { window.endSheet($0) }
        window.orderOut(nil)
        window.contentView = nil
    }
    var settled: NSWindow?
    var previous: [NSSize]?
    let deadline = Date().addingTimeInterval(10)
    while settled == nil, Date() < deadline {
        try await Task.sleep(nanoseconds: 50_000_000)
        guard let sheet = window.attachedSheet else { continue }
        let sizes = [sheet.frame.size, sheet.contentMinSize, sheet.contentMaxSize]
        if sizes == previous, sheet.contentMinSize != .zero { settled = sheet }
        previous = sizes
    }
    return try #require(settled, "the sheet never presented with its size limits")
}

private final class EmptyListURLProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(#"{"items":[],"next_cursor":null}"#.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
#endif
