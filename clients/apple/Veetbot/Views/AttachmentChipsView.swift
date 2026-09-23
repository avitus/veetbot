import SwiftUI
import UniformTypeIdentifiers

#if os(iOS)
import PhotosUI
#endif

/// The files waiting to be sent with the next message (ADR-0118).
struct AttachmentChipsView: View {
    let attachments: [ComposerAttachment]
    let remove: (UUID) -> Void
    let retry: (UUID) -> Void

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(attachments) { attachment in
                    AttachmentChip(
                        attachment: attachment,
                        remove: { remove(attachment.id) },
                        retry: { retry(attachment.id) }
                    )
                }
            }
            .padding(.horizontal, 2)
            .padding(.vertical, 2)
        }
        .accessibilityIdentifier("composer.attachments")
    }
}

private struct AttachmentChip: View {
    let attachment: ComposerAttachment
    let remove: () -> Void
    let retry: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: attachment.isImage ? "photo" : "doc")
                .foregroundColor(AppTheme.turquoise)
            VStack(alignment: .leading, spacing: 3) {
                Text(attachment.filename)
                    .appFont(.caption)
                    .lineLimit(1)
                    .truncationMode(.middle)
                status
            }
            .frame(maxWidth: 180, alignment: .leading)
            if case .failed = attachment.state {
                Button(action: retry) {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Retry \(attachment.filename)")
            }
            Button(action: remove) {
                Image(systemName: "xmark.circle.fill")
                    .foregroundColor(.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Remove \(attachment.filename)")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 10)
                .fill(AppTheme.turquoise.opacity(0.12))
        )
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(isFailed ? Color.red.opacity(0.5) : AppTheme.turquoise.opacity(0.35))
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel(accessibilitySummary)
        .accessibilityIdentifier("composer.attachment")
    }

    @ViewBuilder
    private var status: some View {
        switch attachment.state {
        case .uploading(let progress):
            ProgressView(value: progress)
                .frame(width: 90)
                .accessibilityLabel("Uploading")
        case .uploaded:
            Text("Ready")
                .appFont(.caption)
                .foregroundColor(.secondary)
        case .failed(let message):
            Text(message)
                .appFont(.caption)
                .foregroundColor(.red)
                .lineLimit(2)
        }
    }

    private var isFailed: Bool {
        if case .failed = attachment.state { return true }
        return false
    }

    private var accessibilitySummary: String {
        switch attachment.state {
        case .uploading: return "\(attachment.filename), uploading"
        case .uploaded: return "\(attachment.filename), ready to send"
        case .failed(let message): return "\(attachment.filename), \(message)"
        }
    }
}

/// Accepts dropped files anywhere on the conversation; plain text is left alone.
struct AttachmentDropDelegate: DropDelegate {
    @Binding var isTargeted: Bool
    let isEnabled: Bool
    let attach: ([NSItemProvider]) -> Void

    private func providers(_ info: DropInfo) -> [NSItemProvider] {
        info.itemProviders(for: [.item]).filter {
            ComposerDropPolicy.takesAttachments(typeIdentifiers: $0.registeredTypeIdentifiers)
        }
    }

    func validateDrop(info: DropInfo) -> Bool {
        isEnabled && !providers(info).isEmpty
    }

    func dropEntered(info: DropInfo) {
        isTargeted = validateDrop(info: info)
    }

    func dropUpdated(info: DropInfo) -> DropProposal? {
        validateDrop(info: info) ? DropProposal(operation: .copy) : nil
    }

    func dropExited(info: DropInfo) {
        isTargeted = false
    }

    func performDrop(info: DropInfo) -> Bool {
        isTargeted = false
        let accepted = providers(info)
        guard isEnabled, !accepted.isEmpty else { return false }
        attach(accepted)
        return true
    }
}

/// The highlight shown while files are dragged over the conversation.
struct AttachmentDropHighlight: View {
    var body: some View {
        RoundedRectangle(cornerRadius: 14)
            .stroke(AppTheme.orange, style: StrokeStyle(lineWidth: 2, dash: [8, 6]))
            .background(
                RoundedRectangle(cornerRadius: 14)
                    .fill(AppTheme.orange.opacity(0.06))
            )
            .overlay(
                Label("Drop files to attach", systemImage: "paperclip")
                    .appFont(.headline)
                    .padding(12)
                    .background(.regularMaterial, in: Capsule())
            )
            .padding(8)
            .allowsHitTesting(false)
            .accessibilityIdentifier("chat.dropTarget")
    }
}

#if os(iOS)
/// The system photo picker; it needs no photo-library permission.
struct PhotoLibraryPicker: UIViewControllerRepresentable {
    let pick: ([NSItemProvider]) -> Void

    func makeCoordinator() -> Coordinator { Coordinator(pick: pick) }

    func makeUIViewController(context: Context) -> PHPickerViewController {
        var configuration = PHPickerConfiguration()
        configuration.selectionLimit = ComposerAttachment.maximumPerMessage
        configuration.filter = .images
        let picker = PHPickerViewController(configuration: configuration)
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ picker: PHPickerViewController, context: Context) {}

    final class Coordinator: NSObject, PHPickerViewControllerDelegate {
        let pick: ([NSItemProvider]) -> Void

        init(pick: @escaping ([NSItemProvider]) -> Void) {
            self.pick = pick
        }

        func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
            picker.dismiss(animated: true)
            let providers = results.map(\.itemProvider)
            if !providers.isEmpty { pick(providers) }
        }
    }
}
#endif
