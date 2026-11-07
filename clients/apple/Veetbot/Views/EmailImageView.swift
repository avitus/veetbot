import SwiftUI

#if os(macOS)
import AppKit
#endif

private struct EmailImageLoaderKey: EnvironmentKey {
    static let defaultValue: (@MainActor (String, Int) async throws -> Data)? = nil
}

extension EnvironmentValues {
    var emailImageLoader: (@MainActor (String, Int) async throws -> Data)? {
        get { self[EmailImageLoaderKey.self] }
        set { self[EmailImageLoaderKey.self] = newValue }
    }
}

/// Only server-authored, whole-paragraph image references are actionable.
struct EmailImageReference: Equatable {
    let index: Int
    let label: String

    /// Parse one bounded server image marker without accepting arbitrary Markdown images.
    init?(_ block: MarkdownContentBlock) {
        guard case .paragraph(let text) = block,
              let marker = text.range(of: "](veetbot-email-image:", options: .backwards),
              text.hasPrefix("!["), text.hasSuffix(")"),
              let index = Int(text[marker.upperBound..<text.index(before: text.endIndex)]),
              (0..<20).contains(index) else { return nil }
        self.index = index
        self.label = String(text[text.index(text.startIndex, offsetBy: 2)..<marker.lowerBound])
            .replacingOccurrences(of: "\\[", with: "[").replacingOccurrences(of: "\\]", with: "]")
    }
}

/// Email alone admits image references, with permission local to this visible message.
struct EmailMarkdownBlocksView: View {
    let blocks: [MarkdownContentBlock]
    let messageID: String
    @State private var loadsImages = false

    private var rows: [MarkdownQuoteRow] { MarkdownQuoteRow.rows(blocks, depth: 0) }

    /// Keep images inert until this message receives an explicit load gesture.
    var body: some View {
        #if os(macOS)
        if !rows.contains(where: { EmailImageReference($0.block) != nil }) {
            MarkdownBlocksView(blocks: blocks)
        } else {
            VStack(alignment: .leading, spacing: VeetSpace.s3) {
                if !loadsImages {
                    VStack(alignment: .leading, spacing: 4) {
                        Button("Load images") { loadsImages = true }
                            .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                            .accessibilityIdentifier("email.images.load")
                        Text("Loading images may tell the sender you opened this email.")
                            .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                    }
                }
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    Group {
                        if let reference = EmailImageReference(row.block) {
                            if loadsImages {
                                EmailRemoteImageView(reference: reference, messageID: messageID)
                            } else {
                                Label(reference.label, systemImage: "photo")
                                    .foregroundColor(AppTheme.inkFaint)
                            }
                        } else if row.depth == 0 {
                            MarkdownBlocksView(blocks: [row.block])
                        } else {
                            MarkdownBlocksView(blocks: [.blockquote([row.block])])
                        }
                    }
                    .padding(.leading, CGFloat(max(0, row.depth - 1)) * 12)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .onChange(of: blocks) { loadsImages = false }
            .onChange(of: messageID) { loadsImages = false }
        }
        #else
        MarkdownBlocksView(blocks: blocks)
        #endif
    }
}

#if os(macOS)
private struct EmailRemoteImageView: View {
    let reference: EmailImageReference
    let messageID: String
    @Environment(\.emailImageLoader) private var loadImage
    @State private var image: NSImage?
    @State private var failed = false
    @State private var attempt = 0

    /// Load one rendition per explicit attempt and leave failures available for manual retry.
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let image {
                Image(nsImage: image).resizable().scaledToFit()
                    .frame(maxWidth: image.size.width, maxHeight: 800, alignment: .leading)
                    .accessibilityLabel(reference.label)
                    .accessibilityIdentifier("email.images.image")
            } else if failed {
                Label("Image could not be loaded", systemImage: "photo")
                    .foregroundColor(AppTheme.inkFaint)
                Button("Try again") { attempt += 1 }
                    .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
            } else {
                ProgressView("Loading image…")
            }
        }
        .task(id: attempt) {
            image = nil
            failed = false
            do {
                guard let loadImage else { throw CancellationError() }
                let data = try await loadImage(messageID, reference.index)
                try Task.checkCancellation()
                guard let decoded = NSImage(data: data) else {
                    failed = true
                    return
                }
                image = decoded
            } catch is CancellationError {
                // A departed message must not display a late response.
            } catch {
                failed = true
            }
        }
    }
}
#endif
