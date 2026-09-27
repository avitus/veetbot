import ImageIO
import SwiftUI
import UniformTypeIdentifiers

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

public struct ArtifactViewerView: View {
    @ObservedObject var model: ChatViewModel
    let artifactID: UUID
    @Environment(\.dismiss) private var dismiss
    @State private var artifact: LoadedArtifact?
    @State private var errorMessage: String?
    @State private var exporting = false
    @State private var exportError: String?

    public init(model: ChatViewModel, artifactID: UUID) {
        self.model = model
        self.artifactID = artifactID
    }

    public var body: some View {
        presentation
        .task {
            do {
                artifact = try await model.loadArtifact(artifactID)
            } catch {
                errorMessage = error.localizedDescription
            }
        }
        .fileExporter(
            isPresented: $exporting,
            document: artifact.map { ArtifactDocument(data: $0.data) },
            contentType: artifact.flatMap { UTType(mimeType: $0.metadata.mediaType) } ?? .data,
            defaultFilename: artifact?.metadata.name ?? "artifact"
        ) { result in
            if case .failure(let error) = result {
                exportError = error.localizedDescription
            }
        }
        .alert("Couldn’t download this file", isPresented: Binding(
            get: { exportError != nil },
            set: { if !$0 { exportError = nil } }
        )) {
            Button("OK", role: .cancel) { exportError = nil }
        } message: {
            Text(exportError ?? "")
        }
    }

    @ViewBuilder
    private var presentation: some View {
        #if os(macOS)
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Image(systemName: "doc")
                    .font(.title2)
                    .foregroundColor(.secondary)
                Text(artifact?.metadata.name ?? "Artifact")
                    .appFont(.headline)
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                Spacer(minLength: 0)
            }
            .padding(20)

            Divider()
            preview
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()

            HStack {
                Spacer()
                Button("Close") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Download") { exporting = true }
                    .keyboardShortcut(.defaultAction)
                    .disabled(artifact == nil)
            }
            .padding(16)
        }
        .sheetFrame(
            minWidth: 320,
            macMinWidth: 680,
            idealWidth: 760,
            maxWidth: .infinity,
            minHeight: 420,
            idealHeight: 640,
            maxHeight: .infinity
        )
        #else
        NavigationView {
            preview
                .navigationTitle(artifact?.metadata.name ?? "Artifact")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { dismiss() }
                    }
                    ToolbarItem(placement: .primaryAction) {
                        Button("Download") { exporting = true }
                            .disabled(artifact == nil)
                    }
                }
        }
        #endif
    }

    @ViewBuilder
    private var preview: some View {
        if let artifact {
            content(artifact)
        } else if let errorMessage {
            previewPlaceholder(
                title: "Couldn’t load this file",
                message: errorMessage,
                symbol: "exclamationmark.triangle"
            )
        } else {
            ProgressView("Loading artifact…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func previewPlaceholder(
        title: String, message: String, symbol: String = "doc"
    ) -> some View {
        VStack(spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 40))
                .foregroundColor(.secondary)
            Text(title).appFont(.headline)
            Text(message)
                .appFont(.body)
                .foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: 360)
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private func content(_ artifact: LoadedArtifact) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(artifact.metadata.mediaType)
                Spacer()
                Text(
                    ByteCountFormatter.string(
                        fromByteCount: Int64(artifact.metadata.sizeBytes),
                        countStyle: .file
                    ))
            }
            .appFont(.caption)
            .foregroundColor(.secondary)
            .padding(.horizontal, 20)
            .padding(.top, 16)

            if artifact.metadata.mediaType.hasPrefix("text/") {
                let preview = textPreview(data: artifact.data)
                ScrollView([.horizontal, .vertical]) {
                    VStack(alignment: .leading, spacing: 12) {
                        Text(preview.text)
                            .appCodeFont(.body)
                            .textSelection(.enabled)
                        if preview.isTruncated {
                            Label(
                                "Preview truncated. Download the artifact for the full content.",
                                systemImage: "scissors"
                            )
                            .appFont(.caption)
                            .foregroundColor(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding()
                }
            } else if artifact.metadata.mediaType.hasPrefix("image/") {
                image(data: artifact.data)
            } else {
                unavailablePreview
            }
        }
    }

    private var unavailablePreview: some View {
        previewPlaceholder(
            title: "Preview unavailable",
            message: "Download this file to open it in an app that supports its format."
        )
    }

    private func textPreview(data: Data) -> (text: String, isTruncated: Bool) {
        let maximumPreviewBytes = 64 * 1_024
        let bytes = data.prefix(maximumPreviewBytes)
        return (
            String(decoding: bytes, as: UTF8.self),
            data.count > maximumPreviewBytes
        )
    }

    @ViewBuilder
    private func image(data: Data) -> some View {
        #if os(iOS)
        if let thumbnail = ArtifactImageDecoder.thumbnail(from: data) {
            ScrollView([.horizontal, .vertical]) {
                Image(uiImage: UIImage(cgImage: thumbnail))
                    .resizable().scaledToFit().padding()
            }
        } else {
            unavailablePreview
        }
        #elseif os(macOS)
        if let thumbnail = ArtifactImageDecoder.thumbnail(from: data) {
            ScrollView([.horizontal, .vertical]) {
                Image(nsImage: NSImage(cgImage: thumbnail, size: .zero))
                    .resizable().scaledToFit().padding()
            }
        } else {
            unavailablePreview
        }
        #endif
    }
}

enum ArtifactImageDecoder {
    static let maximumPreviewDimension = 2_048

    static func thumbnail(from data: Data) -> CGImage? {
        guard
            let source = CGImageSourceCreateWithData(
                data as CFData,
                [kCGImageSourceShouldCache: false] as CFDictionary
            )
        else { return nil }

        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: maximumPreviewDimension,
            kCGImageSourceShouldCacheImmediately: true,
        ]
        return CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
    }
}

private struct ArtifactDocument: FileDocument {
    static var readableContentTypes: [UTType] { [.data] }
    let data: Data

    init(data: Data) { self.data = data }

    init(configuration: ReadConfiguration) throws {
        data = configuration.file.regularFileContents ?? Data()
    }

    func fileWrapper(configuration: WriteConfiguration) throws -> FileWrapper {
        FileWrapper(regularFileWithContents: data)
    }
}
