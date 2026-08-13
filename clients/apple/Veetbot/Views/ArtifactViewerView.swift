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

    public init(model: ChatViewModel, artifactID: UUID) {
        self.model = model
        self.artifactID = artifactID
    }

    public var body: some View {
        NavigationView {
            Group {
                if let artifact {
                    content(artifact)
                } else if let errorMessage {
                    VStack(spacing: 12) {
                        Image(systemName: "exclamationmark.triangle")
                        Text(errorMessage)
                    }
                    .padding()
                } else {
                    ProgressView("Loading artifact…")
                }
            }
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
        ) { _ in }
    }

    @ViewBuilder
    private func content(_ artifact: LoadedArtifact) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text(artifact.metadata.mediaType)
                Spacer()
                Text(ByteCountFormatter.string(
                    fromByteCount: Int64(artifact.metadata.sizeBytes),
                    countStyle: .file
                ))
            }
            .font(.caption)
            .foregroundColor(.secondary)
            .padding(.horizontal)

            if artifact.metadata.mediaType.hasPrefix("text/"),
               let text = String(data: artifact.data, encoding: .utf8)
            {
                ScrollView([.horizontal, .vertical]) {
                    Text(text)
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding()
                }
            } else if artifact.metadata.mediaType.hasPrefix("image/") {
                image(data: artifact.data)
            } else {
                VStack(spacing: 10) {
                    Image(systemName: "doc.fill").font(.largeTitle)
                    Text("This artifact can be downloaded for viewing in another app.")
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    @ViewBuilder
    private func image(data: Data) -> some View {
#if os(iOS)
        if let image = UIImage(data: data) {
            ScrollView([.horizontal, .vertical]) {
                Image(uiImage: image).resizable().scaledToFit().padding()
            }
        } else {
            Text("The image data could not be decoded.")
        }
#elseif os(macOS)
        if let image = NSImage(data: data) {
            ScrollView([.horizontal, .vertical]) {
                Image(nsImage: image).resizable().scaledToFit().padding()
            }
        } else {
            Text("The image data could not be decoded.")
        }
#endif
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
