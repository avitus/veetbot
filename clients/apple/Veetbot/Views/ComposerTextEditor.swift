import SwiftUI

enum ComposerReturnAction: Equatable {
    case send
    case insertNewline
}

enum ComposerKeyboardPolicy {
    static func returnAction(commandPressed: Bool) -> ComposerReturnAction {
        commandPressed ? .insertNewline : .send
    }
}

#if os(macOS)
import AppKit

struct ComposerTextEditor: NSViewRepresentable {
    @Binding var text: String
    let onSubmit: () -> Void
    @Environment(\.appFontStyle) private var appFontStyle
    @Environment(\.appTextSize) private var appTextSize

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeNSView(context: Context) -> NSScrollView {
        let scrollView = NSScrollView()
        scrollView.borderType = .noBorder
        scrollView.drawsBackground = false
        scrollView.hasHorizontalScroller = false
        scrollView.hasVerticalScroller = true
        scrollView.autohidesScrollers = true

        let textView = ComposerNSTextView()
        textView.delegate = context.coordinator
        textView.onSubmit = onSubmit
        textView.string = text
        applyFont(to: textView)
        textView.isRichText = false
        textView.importsGraphics = false
        textView.allowsUndo = true
        textView.drawsBackground = false
        textView.isHorizontallyResizable = false
        textView.isVerticallyResizable = true
        textView.autoresizingMask = [.width]
        textView.textContainerInset = NSSize(width: 10, height: 8)
        textView.textContainer?.lineFragmentPadding = 0
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(
            width: 0,
            height: CGFloat.greatestFiniteMagnitude
        )
        scrollView.documentView = textView
        return scrollView
    }

    func updateNSView(_ scrollView: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let textView = scrollView.documentView as? ComposerNSTextView else { return }
        textView.onSubmit = onSubmit
        applyFont(to: textView)
        if textView.string != text {
            textView.string = text
        }
    }

    private func applyFont(to textView: NSTextView) {
        let pointSize =
            appPointSize(for: .body, textSize: appTextSize)
            ?? NSFont.systemFontSize
        let base = NSFont.systemFont(ofSize: pointSize)
        let descriptor = base.fontDescriptor.withDesign(appFontStyle.nsDesign)
        textView.font = descriptor.flatMap { NSFont(descriptor: $0, size: pointSize) } ?? base
    }

    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: ComposerTextEditor

        init(parent: ComposerTextEditor) {
            self.parent = parent
        }

        func textDidChange(_ notification: Notification) {
            guard let textView = notification.object as? NSTextView else { return }
            parent.text = textView.string
        }
    }
}

final class ComposerNSTextView: NSTextView {
    var onSubmit: () -> Void = {}

    override func keyDown(with event: NSEvent) {
        let isReturn = event.keyCode == 36 || event.keyCode == 76
        guard isReturn, !hasMarkedText() else {
            super.keyDown(with: event)
            return
        }

        let commandPressed = event.modifierFlags
            .intersection(.deviceIndependentFlagsMask)
            .contains(.command)
        switch ComposerKeyboardPolicy.returnAction(commandPressed: commandPressed) {
        case .send:
            onSubmit()
        case .insertNewline:
            insertNewline(nil)
        }
    }
}

#elseif os(iOS)
import UIKit

struct ComposerTextEditor: UIViewRepresentable {
    @Binding var text: String
    let onSubmit: () -> Void
    @Environment(\.appFontStyle) private var appFontStyle
    @Environment(\.appTextSize) private var appTextSize

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> ComposerUITextView {
        let textView = ComposerUITextView()
        textView.delegate = context.coordinator
        installCommandReturnHandler(on: textView, coordinator: context.coordinator)
        textView.text = text
        applyFont(to: textView)
        textView.adjustsFontForContentSizeCategory = false
        textView.backgroundColor = .clear
        textView.isScrollEnabled = true
        textView.textContainerInset = UIEdgeInsets(top: 8, left: 10, bottom: 8, right: 10)
        textView.textContainer.lineFragmentPadding = 0
        return textView
    }

    func updateUIView(_ textView: ComposerUITextView, context: Context) {
        context.coordinator.parent = self
        installCommandReturnHandler(on: textView, coordinator: context.coordinator)
        applyFont(to: textView)
        if textView.text != text {
            textView.text = text
        }
    }

    private func applyFont(to textView: UITextView) {
        let preferred: UIFontDescriptor
        if let pointSize = appPointSize(for: .body, textSize: appTextSize) {
            preferred = UIFont.systemFont(ofSize: pointSize).fontDescriptor
        } else {
            preferred = UIFontDescriptor.preferredFontDescriptor(
                withTextStyle: .body,
                compatibleWith: nil
            )
        }
        let descriptor = preferred.withDesign(appFontStyle.uiDesign) ?? preferred
        textView.font = UIFont(descriptor: descriptor, size: 0)
    }

    private func installCommandReturnHandler(
        on textView: ComposerUITextView,
        coordinator: Coordinator
    ) {
        textView.commandReturnHandler = { [weak textView, weak coordinator] in
            guard let textView, let coordinator else { return }
            coordinator.insertNewline(in: textView)
        }
    }

    final class Coordinator: NSObject, UITextViewDelegate {
        var parent: ComposerTextEditor

        init(parent: ComposerTextEditor) {
            self.parent = parent
        }

        func textViewDidChange(_ textView: UITextView) {
            parent.text = textView.text
        }

        func textView(
            _ textView: UITextView,
            shouldChangeTextIn range: NSRange,
            replacementText replacement: String
        ) -> Bool {
            guard replacement == "\n" || replacement == "\r" else { return true }
            guard textView.markedTextRange == nil else { return true }
            if let composer = textView as? ComposerUITextView,
                composer.isInsertingCommandNewline
            {
                return true
            }
            parent.onSubmit()
            return false
        }

        func insertNewline(in textView: ComposerUITextView) {
            textView.isInsertingCommandNewline = true
            defer { textView.isInsertingCommandNewline = false }
            textView.insertText("\n")
        }
    }
}

final class ComposerUITextView: UITextView {
    var commandReturnHandler: () -> Void = {}
    var isInsertingCommandNewline = false

    override var keyCommands: [UIKeyCommand]? {
        let command = UIKeyCommand(
            input: "\r",
            modifierFlags: .command,
            action: #selector(insertCommandNewline)
        )
        command.wantsPriorityOverSystemBehavior = true
        return (super.keyCommands ?? []) + [command]
    }

    @objc private func insertCommandNewline() {
        commandReturnHandler()
    }
}
#endif
