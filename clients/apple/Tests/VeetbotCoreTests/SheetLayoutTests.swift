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
func presentedSheetWidth<Sheet: View>(@ViewBuilder _ sheet: @escaping () -> Sheet) async throws -> CGFloat {
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
    return try #require(settled, "the sheet never presented")
}
#endif
