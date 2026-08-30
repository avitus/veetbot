import Foundation
import Testing
@testable import VeetbotCore

#if os(macOS)
import AppKit
#elseif os(iOS)
import SwiftUI
import UIKit
#endif

@Suite struct ComposerTextEditorTests {
    @Test
    func testChatDismissesIOSKeyboardOnlyAfterSuccessfulSend() throws {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let source = try String(
            contentsOf: packageRoot.appendingPathComponent("Veetbot/Views/ChatView.swift"),
            encoding: .utf8
        )
        let submitStart = try #require(source.range(of: "private func submitDraft()"))
        let submission = String(source[submitStart.lowerBound...])
        let send = try #require(submission.range(of: "let sent = await model.send(message)"))
        let success = try #require(submission.range(of: "if sent {"))
        let successBody = try bracedBody(in: submission, startingAt: success)
        let dismissal = "UIApplication.shared.sendAction("

        #expect(send.lowerBound < success.lowerBound)
        #expect(successBody.contains(dismissal))
        #expect(submission.components(separatedBy: dismissal).count == 2)
    }

    private func bracedBody(
        in source: String,
        startingAt marker: Range<String.Index>
    ) throws -> Substring {
        let openingBrace = try #require(
            source[marker].firstIndex(of: "{")
        )
        var depth = 0
        var cursor = openingBrace

        while cursor < source.endIndex {
            switch source[cursor] {
            case "{":
                depth += 1
            case "}":
                depth -= 1
                if depth == 0 {
                    return source[source.index(after: openingBrace)..<cursor]
                }
            default:
                break
            }
            cursor = source.index(after: cursor)
        }

        Issue.record("Unbalanced success branch in ChatView.submitDraft")
        return source[source.endIndex..<source.endIndex]
    }

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

    @Test
    func testSystemSizeChangeRefreshesVisibleComposerOnlyForSystemSetting() {
        var refreshCount = 0
        let controller = ComposerFontRefreshController()
        controller.refresh = { refreshCount += 1 }

        controller.textSize = .system
        controller.systemTextSizeDidChange()
        #expect(refreshCount == 1)

        controller.textSize = .large
        controller.systemTextSizeDidChange()
        #expect(refreshCount == 1)
    }

#if os(macOS)
    @Test @MainActor
    func testSystemComposerFontUsesThePreferredBodyPointSize() {
        #expect(
            composerBaseFont(textSize: .system).pointSize
                == NSFont.preferredFont(forTextStyle: .body).pointSize
        )
        #expect(
            composerBaseFont(textSize: .large).pointSize
                == appPointSize(for: .body, textSize: .large)
        )
    }

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
