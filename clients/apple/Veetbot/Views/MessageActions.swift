import SwiftUI

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

extension TimelineItem {
    /// The message's text as `MarkdownContentView` draws it: one block per text part.
    var copyableMarkdown: String {
        content.compactMap(\.text).joined(separator: "\n\n")
    }

    /// Copy and Select Text appear once a message is final and has text.
    var offersMessageActions: Bool {
        !isStreaming && !copyableMarkdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
}

/// The message a Select Text sheet shows, rendered when the owner asked for it.
struct MessageTextSelection: Identifiable {
    let id: String
    let rendition: MarkdownRendition
}

/// Copy and Select Text under a finished message (ADR-0122). A message is drawn
/// as one `Text` per paragraph, and SwiftUI selection never crosses from one to
/// the next, so these are how the owner copies a whole reply or any range of it.
struct MessageActionBar: View {
    let messageID: String
    let role: TimelineItem.Role
    let markdown: String
    let selectText: (MessageTextSelection) -> Void

    @Environment(\.appTextSize) private var textSize
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @State private var copied = false

    var body: some View {
        HStack(spacing: 14) {
            Button {
                // Rendered on demand, never in `body`: a transcript redraw stays cheap.
                SystemClipboard.copy(MarkdownRendition(markdown: markdown, textSize: textSize))
                copied = true
            } label: {
                label(
                    copied ? "Copied" : "Copy",
                    systemImage: copied ? "checkmark" : "doc.on.doc"
                )
            }
            .accessibilityLabel(copied ? "Copied to clipboard" : "Copy message")
            .accessibilityIdentifier("chat.message.copy.\(messageID)")
            .help("Copy the whole message as formatted text")

            Button {
                selectText(
                    MessageTextSelection(
                        id: messageID,
                        rendition: MarkdownRendition(markdown: markdown, textSize: textSize)
                    )
                )
            } label: {
                label("Select Text", systemImage: "text.cursor")
            }
            .accessibilityLabel("Select text")
            .accessibilityHint("Opens the message so you can select any part of it")
            .accessibilityIdentifier("chat.message.select.\(messageID)")
            .help("Select any part of the message")
        }
        .buttonStyle(.plain)
        .appFont(.caption, weight: .semibold)
        .foregroundColor(.secondary)
        .task(id: copied) {
            guard copied else { return }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
            copied = false
        }
    }

    @ViewBuilder
    private func label(_ title: String, systemImage: String) -> some View {
        Group {
            if role == .user || dynamicTypeSize.isAccessibilitySize {
                Image(systemName: systemImage)
            } else {
                Label(title, systemImage: systemImage)
            }
        }
        #if os(iOS)
        .frame(minWidth: 44, minHeight: 44)
        .contentShape(Rectangle())
        #endif
    }
}

/// A whole message in a native text view, where any range can be selected.
struct MessageTextSheet: View {
    let selection: MessageTextSelection
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("Select Text")
                    .appFont(.headline)
                    .accessibilityAddTraits(.isHeader)
                Spacer()
                Button("Done") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                    .accessibilityIdentifier("chat.message.selection.done")
            }
            .padding()
            Divider()
            SelectableMessageText(attributedText: selection.rendition.attributedText)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("chat.message.selection")
        .sheetFrame(
            minWidth: 320,
            macMinWidth: 520,
            idealWidth: 680,
            maxWidth: .infinity,
            minHeight: 280,
            idealHeight: 560,
            maxHeight: .infinity
        )
    }
}

#if os(macOS)
struct SelectableMessageText: NSViewRepresentable {
    let attributedText: NSAttributedString

    final class Coordinator {
        var shown: NSAttributedString?
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSScrollView {
        let scrollView = NSTextView.scrollableTextView()
        scrollView.drawsBackground = false
        guard let textView = scrollView.documentView as? NSTextView else { return scrollView }
        textView.isEditable = false
        textView.isSelectable = true
        // Rich text makes ⌘C on a partial selection copy its formatting too.
        textView.isRichText = true
        textView.drawsBackground = false
        textView.isAutomaticLinkDetectionEnabled = false
        textView.textContainerInset = NSSize(width: 12, height: 12)
        textView.setAccessibilityIdentifier("chat.message.selection.text")
        textView.setAccessibilityLabel("Message text")
        show(in: textView, coordinator: context.coordinator)
        DispatchQueue.main.async { textView.window?.makeFirstResponder(textView) }
        return scrollView
    }

    func updateNSView(_ scrollView: NSScrollView, context: Context) {
        guard let textView = scrollView.documentView as? NSTextView else { return }
        show(in: textView, coordinator: context.coordinator)
    }

    private func show(in textView: NSTextView, coordinator: Coordinator) {
        // A re-render with the same rendition keeps the owner's selection.
        guard coordinator.shown !== attributedText else { return }
        coordinator.shown = attributedText
        textView.textStorage?.setAttributedString(displayText(attributedText))
    }
}
#elseif os(iOS)
struct SelectableMessageText: UIViewRepresentable {
    let attributedText: NSAttributedString

    final class Coordinator {
        var shown: NSAttributedString?
    }

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeUIView(context: Context) -> UITextView {
        let textView = UITextView()
        textView.isEditable = false
        textView.isSelectable = true
        textView.isScrollEnabled = true
        textView.backgroundColor = .clear
        textView.adjustsFontForContentSizeCategory = false
        textView.dataDetectorTypes = []
        textView.textContainerInset = UIEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        textView.accessibilityIdentifier = "chat.message.selection.text"
        textView.accessibilityLabel = "Message text"
        show(in: textView, coordinator: context.coordinator)
        return textView
    }

    func updateUIView(_ textView: UITextView, context: Context) {
        show(in: textView, coordinator: context.coordinator)
    }

    private func show(in textView: UITextView, coordinator: Coordinator) {
        // A re-render with the same rendition keeps the owner's selection.
        guard coordinator.shown !== attributedText else { return }
        coordinator.shown = attributedText
        textView.attributedText = displayText(attributedText)
    }
}
#endif

/// The sheet's copy of the text gains the system label color so it reads in
/// dark mode; the rendition itself stays colorless for the clipboard.
private func displayText(_ text: NSAttributedString) -> NSAttributedString {
    let display = NSMutableAttributedString(attributedString: text)
    #if os(macOS)
    let color = NSColor.labelColor
    #else
    let color = UIColor.label
    #endif
    display.addAttribute(
        .foregroundColor, value: color, range: NSRange(location: 0, length: display.length)
    )
    return display
}
