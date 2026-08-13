import Testing
@testable import VeetbotCore

#if os(macOS)
import AppKit
#endif

@Suite struct ComposerTextEditorTests {
    @Test(arguments: [
        (commandPressed: false, expected: ComposerReturnAction.send),
        (commandPressed: true, expected: ComposerReturnAction.insertNewline),
    ])
    func testReturnPolicy(
        commandPressed: Bool,
        expected: ComposerReturnAction
    ) {
        #expect(
            ComposerKeyboardPolicy.returnAction(commandPressed: commandPressed) == expected
        )
    }

#if os(macOS)
    @Test @MainActor
    func testReturnSendsWithoutEditingText() throws {
        let textView = ComposerNSTextView()
        textView.string = "Send this"
        var submissionCount = 0
        textView.onSubmit = { submissionCount += 1 }

        textView.keyDown(with: try returnEvent(modifiers: []))

        #expect(submissionCount == 1)
        #expect(textView.string == "Send this")
    }

    @Test @MainActor
    func testCommandReturnInsertsNewlineAtCursor() throws {
        let textView = ComposerNSTextView()
        textView.string = "FirstSecond"
        textView.setSelectedRange(NSRange(location: 5, length: 0))
        var submissionCount = 0
        textView.onSubmit = { submissionCount += 1 }

        textView.keyDown(with: try returnEvent(modifiers: [.command]))

        #expect(submissionCount == 0)
        #expect(textView.string == "First\nSecond")
    }

    @MainActor
    private func returnEvent(modifiers: NSEvent.ModifierFlags) throws -> NSEvent {
        try #require(
            NSEvent.keyEvent(
                with: .keyDown,
                location: .zero,
                modifierFlags: modifiers,
                timestamp: 0,
                windowNumber: 0,
                context: nil,
                characters: "\r",
                charactersIgnoringModifiers: "\r",
                isARepeat: false,
                keyCode: 36
            )
        )
    }
#endif
}
