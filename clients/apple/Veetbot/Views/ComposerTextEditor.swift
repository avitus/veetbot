import SwiftUI
import UniformTypeIdentifiers

/// Decides whether a drop becomes attachments or text (ADR-0120).
///
/// Text, rich text, and web links keep their ordinary meaning and are inserted
/// into the message; files, images, and anything else become attachments.
enum ComposerDropPolicy {
    private static let promisedFilePrefixes = [
        "com.apple.pasteboard.promised-file",
        "com.apple.NSFilePromiseItemMetaData",
    ]

    static func takesAttachments(typeIdentifiers: [String]) -> Bool {
        typeIdentifiers.contains { identifier in
            if promisedFilePrefixes.contains(where: identifier.hasPrefix) { return true }
            guard let type = UTType(identifier) else { return false }
            if type.conforms(to: .fileURL) { return true }
            if type.conforms(to: .text) || type.conforms(to: .url) { return false }
            return type.conforms(to: .data) || type.conforms(to: .content)
        }
    }
}

enum ComposerReturnAction: Equatable {
    case send
    case insertNewline
}

enum ComposerKeyboardPolicy {
    static func returnAction(commandPressed: Bool) -> ComposerReturnAction {
        commandPressed ? .insertNewline : .send
    }
}

final class ComposerFontRefreshController {
    var textSize: AppTextSize = .system
    var refresh: () -> Void = {}

    func systemTextSizeDidChange() {
        guard textSize == .system else { return }
        refresh()
    }
}

#if os(macOS)
import AppKit

@MainActor
func composerBaseFont(textSize: AppTextSize) -> NSFont {
    guard let pointSize = appPointSize(for: .body, textSize: textSize) else {
        return NSFont.preferredFont(forTextStyle: .body)
    }
    return NSFont.systemFont(ofSize: pointSize)
}

struct ComposerTextEditor: NSViewRepresentable {
    @Binding var text: String
    let onSubmit: () -> Void
    var onDropFiles: ([URL]) -> Void = { _ in }
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
        textView.onDropFiles = onDropFiles
        textView.string = text
        installFontRefresh(on: textView)
        applyFont(to: textView)
        textView.isRichText = false
        #if DEBUG && !SWIFT_PACKAGE
        // macOS 27 floats a Writing Tools affordance window beside the focused
        // composer, over controls that Mac UI tests click and hit-test.
        if ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.launchArgument),
           #available(macOS 15.0, *) {
            textView.writingToolsBehavior = .none
        }
        #endif
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
        textView.onDropFiles = onDropFiles
        installFontRefresh(on: textView)
        applyFont(to: textView)
        if textView.string != text {
            textView.string = text
        }
    }

    private func applyFont(to textView: NSTextView) {
        let base = composerBaseFont(textSize: appTextSize)
        let pointSize = base.pointSize
        let descriptor = base.fontDescriptor.withDesign(appFontStyle.nsDesign)
        textView.font = descriptor.flatMap { NSFont(descriptor: $0, size: pointSize) } ?? base
    }

    private func installFontRefresh(on textView: ComposerNSTextView) {
        textView.fontRefreshController.textSize = appTextSize
        textView.fontRefreshController.refresh = { [weak textView] in
            guard let textView else { return }
            self.applyFont(to: textView)
        }
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
    var onDropFiles: ([URL]) -> Void = { _ in }
    let fontRefreshController = ComposerFontRefreshController()
    private var observesSystemTextSize = false

    override var readablePasteboardTypes: [NSPasteboard.PasteboardType] {
        super.readablePasteboardTypes + [.fileURL, .tiff, .png]
            + NSFilePromiseReceiver.readableDraggedTypes.map { NSPasteboard.PasteboardType(rawValue: $0) }
    }

    private func takesAttachments(_ sender: NSDraggingInfo) -> Bool {
        let identifiers = (sender.draggingPasteboard.types ?? []).map(\.rawValue)
        return ComposerDropPolicy.takesAttachments(typeIdentifiers: identifiers)
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        takesAttachments(sender) ? .copy : super.draggingEntered(sender)
    }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        takesAttachments(sender) ? .copy : super.draggingUpdated(sender)
    }

    /// Files become attachments instead of pasted paths; text still inserts.
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        guard takesAttachments(sender) else { return super.performDragOperation(sender) }
        let pasteboard = sender.draggingPasteboard
        let urls =
            pasteboard.readObjects(
                forClasses: [NSURL.self],
                options: [.urlReadingFileURLsOnly: true]
            ) as? [URL] ?? []
        if !urls.isEmpty {
            onDropFiles(urls)
            return true
        }
        let promises =
            pasteboard.readObjects(forClasses: [NSFilePromiseReceiver.self]) as? [NSFilePromiseReceiver]
            ?? []
        if !promises.isEmpty {
            let destination = FileManager.default.temporaryDirectory
                .appendingPathComponent("veetbot-drops", isDirectory: true)
                .appendingPathComponent(UUID().uuidString, isDirectory: true)
            try? FileManager.default.createDirectory(
                at: destination, withIntermediateDirectories: true
            )
            let deliver = onDropFiles
            for promise in promises {
                promise.receivePromisedFiles(
                    atDestination: destination, options: [:], operationQueue: .main
                ) { url, error in
                    if error == nil { deliver([url]) }
                }
            }
            return true
        }
        if let image = NSImage(pasteboard: pasteboard),
            let tiff = image.tiffRepresentation,
            let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:])
        {
            let file = FileManager.default.temporaryDirectory
                .appendingPathComponent("veetbot-drops", isDirectory: true)
                .appendingPathComponent("\(UUID().uuidString).png")
            try? FileManager.default.createDirectory(
                at: file.deletingLastPathComponent(), withIntermediateDirectories: true
            )
            if (try? png.write(to: file)) != nil {
                onDropFiles([file])
                return true
            }
        }
        return super.performDragOperation(sender)
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window != nil, !observesSystemTextSize {
            observeSystemTextSize()
        } else if window == nil, observesSystemTextSize {
            stopObservingSystemTextSize()
        }
    }

    deinit {
        stopObservingSystemTextSize()
    }

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

    private func observeSystemTextSize() {
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(systemTextSizeDidChange),
            name: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
            object: nil
        )
        observesSystemTextSize = true
    }

    private func stopObservingSystemTextSize() {
        guard observesSystemTextSize else { return }
        NSWorkspace.shared.notificationCenter.removeObserver(
            self,
            name: NSWorkspace.accessibilityDisplayOptionsDidChangeNotification,
            object: nil
        )
        observesSystemTextSize = false
    }

    @objc private func systemTextSizeDidChange() {
        fontRefreshController.systemTextSizeDidChange()
    }
}

#elseif os(iOS)
import UIKit

struct ComposerTextEditor: UIViewRepresentable {
    @Binding var text: String
    let onSubmit: () -> Void
    var onDropItems: ([NSItemProvider]) -> Void = { _ in }
    @Environment(\.appFontStyle) private var appFontStyle
    @Environment(\.appTextSize) private var appTextSize

    func makeCoordinator() -> Coordinator {
        Coordinator(parent: self)
    }

    func makeUIView(context: Context) -> ComposerUITextView {
        let textView = ComposerUITextView()
        textView.delegate = context.coordinator
        textView.textDropDelegate = context.coordinator
        installCommandReturnHandler(on: textView, coordinator: context.coordinator)
        installFontRefresh(on: textView)
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
        installFontRefresh(on: textView)
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

    private func installFontRefresh(on textView: ComposerUITextView) {
        textView.fontRefreshController.textSize = appTextSize
        textView.fontRefreshController.refresh = { [weak textView] in
            guard let textView else { return }
            self.applyFont(to: textView)
        }
    }

    final class Coordinator: NSObject, UITextViewDelegate, UITextDropDelegate {
        var parent: ComposerTextEditor

        init(parent: ComposerTextEditor) {
            self.parent = parent
        }

        private func attachmentProviders(_ drop: UITextDropRequest) -> [NSItemProvider] {
            drop.dropSession.items.map(\.itemProvider).filter {
                ComposerDropPolicy.takesAttachments(typeIdentifiers: $0.registeredTypeIdentifiers)
            }
        }

        /// A file dropped on the message field becomes an attachment, not text.
        func textDroppableView(
            _ textDroppableView: UIView & UITextDroppable,
            proposalForDrop drop: UITextDropRequest
        ) -> UITextDropProposal {
            guard !attachmentProviders(drop).isEmpty else { return drop.suggestedProposal }
            let proposal = UITextDropProposal(operation: .copy)
            proposal.dropPerformer = .delegate
            return proposal
        }

        func textDroppableView(
            _ textDroppableView: UIView & UITextDroppable,
            willPerformDrop drop: UITextDropRequest
        ) {
            let providers = attachmentProviders(drop)
            guard !providers.isEmpty else { return }
            parent.onDropItems(providers)
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
    let fontRefreshController = ComposerFontRefreshController()

    override init(frame: CGRect, textContainer: NSTextContainer?) {
        super.init(frame: frame, textContainer: textContainer)
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(systemTextSizeDidChange),
            name: UIContentSizeCategory.didChangeNotification,
            object: nil
        )
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(systemTextSizeDidChange),
            name: UIContentSizeCategory.didChangeNotification,
            object: nil
        )
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

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

    @objc private func systemTextSizeDidChange() {
        fontRefreshController.systemTextSizeDidChange()
    }
}
#endif
