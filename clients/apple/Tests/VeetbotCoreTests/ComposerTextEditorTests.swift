import Testing
@testable import VeetbotCore

#if os(macOS)
import AppKit
#elseif os(iOS)
import SwiftUI
import UIKit
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

#if os(iOS)
    @Test @MainActor
    func testIOSReturnSendsWithoutEditingText() {
        let harness = IOSComposerHarness(text: "Send this")

        let shouldEdit = harness.coordinator.textView(
            harness.textView,
            shouldChangeTextIn: NSRange(
                location: harness.textView.text.count,
                length: 0
            ),
            replacementText: "\n"
        )

        #expect(!shouldEdit)
        #expect(harness.submissionCount == 1)
        #expect(harness.textView.text == "Send this")
    }

    @Test @MainActor
    func testIOSCommandReturnInsertsNewlineAtCursor() {
        let harness = IOSComposerHarness(text: "FirstSecond")
        harness.textView.selectedRange = NSRange(location: 5, length: 0)

        harness.coordinator.insertNewline(in: harness.textView)

        #expect(harness.submissionCount == 0)
        #expect(harness.textView.text == "First\nSecond")
    }

    @Test @MainActor
    func testIOSReturnPreservesMarkedText() throws {
        let harness = IOSComposerHarness(text: "compose")
        harness.textView.setMarkedText("d", selectedRange: NSRange(location: 1, length: 0))
        _ = try #require(harness.textView.markedTextRange)

        let shouldEdit = harness.coordinator.textView(
            harness.textView,
            shouldChangeTextIn: NSRange(
                location: harness.textView.text.count,
                length: 0
            ),
            replacementText: "\n"
        )

        #expect(shouldEdit)
        #expect(harness.submissionCount == 0)
    }
#endif
}

#if os(iOS)
@MainActor
private final class IOSComposerHarness {
    var text: String
    var submissionCount = 0
    let textView: ComposerUITextView
    let coordinator: ComposerTextEditor.Coordinator

    init(text: String) {
        self.text = text
        let binding = Binding(
            get: { "" },
            set: { _ in }
        )
        let editor = ComposerTextEditor(text: binding) {}
        coordinator = editor.makeCoordinator()
        textView = ComposerUITextView()

        let connectedBinding = Binding(
            get: { [weak self] in self?.text ?? "" },
            set: { [weak self] in self?.text = $0 }
        )
        coordinator.parent = ComposerTextEditor(text: connectedBinding) { [weak self] in
            self?.submissionCount += 1
        }
        textView.text = text
        textView.delegate = coordinator
    }
}
#endif
