import Foundation
import Testing

@testable import VeetbotCore

/// Every finished message offers Copy and Select Text (ADR-0122). These pin the
/// wiring a unit test cannot reach without a host application.
@Suite struct MessageActionsStructureTests {
    private func source(_ path: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        return try String(
            contentsOf: packageRoot.appendingPathComponent(path), encoding: .utf8)
    }

    @Test func everyFinishedMessageShowsTheActionBarAndOneSheetServesThem() throws {
        let chat = try source("Veetbot/Views/ChatView.swift")

        #expect(chat.contains("if item.offersMessageActions {"))
        #expect(chat.contains("MessageActionBar("))
        #expect(chat.contains("@State private var textSelection: MessageTextSelection?"))
        #expect(chat.contains(".sheet(item: $textSelection)"))
        #expect(chat.contains("MessageTextSheet(selection: selection)"))
    }

    @Test func noContextMenuCompetesWithTextSelection() throws {
        // On iOS a context menu on a message would take the long-press that
        // `.textSelection(.enabled)` uses for its own Copy menu.
        #expect(!(try source("Veetbot/Views/ChatView.swift")).contains(".contextMenu"))
        #expect(!(try source("Veetbot/Views/MessageActions.swift")).contains(".contextMenu"))
    }

    @Test func theBarCopiesFormattedTextWithStableIdentifiers() throws {
        let actions = try source("Veetbot/Views/MessageActions.swift")

        #expect(actions.contains("SystemClipboard.copy(MarkdownRendition("))
        #expect(actions.contains("\"chat.message.copy.\\(messageID)\""))
        #expect(actions.contains("\"chat.message.select.\\(messageID)\""))
        #expect(actions.contains("\"chat.message.selection.text\""))
        #expect(actions.contains("\"chat.message.selection.done\""))
        #expect(actions.contains("\"Copy message\""))
        #expect(actions.contains("\"Copied to clipboard\""))
    }

    @Test func theSheetIsANativeReadOnlySelectableTextView() throws {
        let actions = try source("Veetbot/Views/MessageActions.swift")

        #expect(actions.components(separatedBy: "isEditable = false").count == 3)
        #expect(actions.components(separatedBy: "isSelectable = true").count == 3)
        // A re-render with the same rendition must not reset the selection.
        #expect(actions.contains("coordinator.shown !== attributedText"))
        #expect(actions.contains(".sheetFrame("))
    }

    @Test func theArtifactViewerOpensAtAReadingWidthOnTheMac() throws {
        #expect(try source("Veetbot/Views/ArtifactViewerView.swift").contains(".sheetFrame("))
    }
}
