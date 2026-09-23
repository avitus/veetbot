import Foundation
import ImageIO
import UniformTypeIdentifiers

/// One file prepared for upload: its bytes, a server-safe name, and a declared type (ADR-0120).
public struct StagedAttachment: Equatable, Sendable {
    public let filename: String
    public let mediaType: String
    public let data: Data

    public var isImage: Bool { mediaType.hasPrefix("image/") }
}

public enum AttachmentStagingError: Error, LocalizedError, Equatable, Sendable {
    case tooLarge(filename: String)
    case unreadable(filename: String)

    public var errorDescription: String? {
        switch self {
        case .tooLarge(let filename):
            return "“\(filename)” is larger than the 32 MB attachment limit."
        case .unreadable(let filename):
            return "“\(filename)” could not be read."
        }
    }
}

/// Reads a dropped or picked file and prepares it for the upload route.
///
/// Images other than small PNG, GIF, and WebP files are re-encoded as JPEG with
/// a 2000-pixel long edge, which keeps them within what models read and drops
/// their metadata, including location.
public enum AttachmentStaging {
    public static let maximumBytes = 32 * 1024 * 1024
    public static let imageLongEdge = 2000
    static let passthroughImageBytes = 7 * 1024 * 1024
    private static let readableImageSourceBytes = 4 * maximumBytes
    private static let passthroughImageTypes: Set<String> = ["image/png", "image/gif", "image/webp"]
    private static let forbiddenNameScalars: Set<Unicode.Scalar> = ["\"", "/", "\\"]
    private static let extensionTypes: [String: String] = [
        "md": "text/markdown",
        "markdown": "text/markdown",
        "txt": "text/plain",
        "text": "text/plain",
        "log": "text/plain",
        "csv": "text/csv",
        "json": "application/json",
        "yaml": "application/yaml",
        "yml": "application/yaml",
        "pdf": "application/pdf",
    ]

    public static func stage(fileURL: URL) throws -> StagedAttachment {
        let accessing = fileURL.startAccessingSecurityScopedResource()
        defer {
            if accessing { fileURL.stopAccessingSecurityScopedResource() }
        }
        let name = cleanedName(fileURL.lastPathComponent)
        let values = try? fileURL.resourceValues(
            forKeys: [.fileSizeKey, .contentTypeKey, .isDirectoryKey]
        )
        if values?.isDirectory == true {
            throw AttachmentStagingError.unreadable(filename: name)
        }
        let type = values?.contentType ?? UTType(filenameExtension: fileURL.pathExtension)
        let isImage = type?.conforms(to: .image) ?? false
        if let size = values?.fileSize,
            size > (isImage ? readableImageSourceBytes : maximumBytes)
        {
            throw AttachmentStagingError.tooLarge(filename: name)
        }
        let data: Data
        do {
            data = try Data(contentsOf: fileURL, options: .mappedIfSafe)
        } catch {
            throw AttachmentStagingError.unreadable(filename: name)
        }
        return try stage(data: data, filename: name, type: type)
    }

    public static func stage(data: Data, filename: String, type: UTType?) throws -> StagedAttachment {
        var name = cleanedName(filename)
        var mediaType = mediaType(for: type, filename: name)
        var bytes = data
        guard !bytes.isEmpty else { throw AttachmentStagingError.unreadable(filename: name) }
        if type?.conforms(to: .image) == true || mediaType.hasPrefix("image/") {
            if needsNormalization(data: bytes, mediaType: mediaType) {
                guard let jpeg = normalizedJPEG(bytes) else {
                    throw AttachmentStagingError.unreadable(filename: name)
                }
                bytes = jpeg
                mediaType = "image/jpeg"
                name = replacingExtension(of: name, with: "jpg")
            }
        }
        guard bytes.count <= maximumBytes else {
            throw AttachmentStagingError.tooLarge(filename: name)
        }
        return StagedAttachment(filename: name, mediaType: mediaType, data: bytes)
    }

    /// The declared type: the platform's MIME type, a fixed extension table, or bytes.
    static func mediaType(for type: UTType?, filename: String) -> String {
        let pathExtension = (filename as NSString).pathExtension.lowercased()
        if let known = extensionTypes[pathExtension] { return known }
        if let mime = type?.preferredMIMEType { return mime.lowercased() }
        if type?.conforms(to: .plainText) == true { return "text/plain" }
        return "application/octet-stream"
    }

    /// A name the server accepts: no quotes, separators, or control characters, at most 255 bytes.
    public static func cleanedName(_ raw: String) -> String {
        var scalars = String.UnicodeScalarView()
        for scalar in raw.unicodeScalars {
            switch scalar.properties.generalCategory {
            case .control, .format, .lineSeparator, .paragraphSeparator:
                scalars.append("_")
            default:
                scalars.append(forbiddenNameScalars.contains(scalar) ? "_" : scalar)
            }
        }
        let replaced = String(scalars).trimmingCharacters(in: .whitespacesAndNewlines)
        let name = replaced.isEmpty ? "attachment" : replaced
        guard name.utf8.count > 255 else { return name }
        let pathExtension = (name as NSString).pathExtension
        let suffix = pathExtension.isEmpty ? "" : ".\(pathExtension)"
        var stem = (name as NSString).deletingPathExtension
        while !stem.isEmpty, stem.utf8.count + suffix.utf8.count > 255 {
            stem.removeLast()
        }
        return stem + suffix
    }

    static func needsNormalization(data: Data, mediaType: String) -> Bool {
        guard passthroughImageTypes.contains(mediaType),
            data.count <= passthroughImageBytes,
            let edge = longEdge(of: data)
        else { return true }
        return edge > imageLongEdge
    }

    static func longEdge(of data: Data) -> Int? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil),
            let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil)
                as? [CFString: Any],
            let width = properties[kCGImagePropertyPixelWidth] as? Int,
            let height = properties[kCGImagePropertyPixelHeight] as? Int
        else { return nil }
        return max(width, height)
    }

    /// Re-encode as JPEG within the long-edge limit, applying orientation and carrying no metadata.
    static func normalizedJPEG(_ data: Data) -> Data? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: imageLongEdge,
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary)
        else { return nil }
        let output = NSMutableData()
        guard
            let destination = CGImageDestinationCreateWithData(
                output, UTType.jpeg.identifier as CFString, 1, nil
            )
        else { return nil }
        CGImageDestinationAddImage(
            destination,
            image,
            [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary
        )
        guard CGImageDestinationFinalize(destination) else { return nil }
        return output as Data
    }

    private static func replacingExtension(of name: String, with newExtension: String) -> String {
        let stem = (name as NSString).deletingPathExtension
        return cleanedName("\(stem.isEmpty ? "image" : stem).\(newExtension)")
    }
}

extension AttachmentStaging {
    /// Stage whatever a drag or the photo picker carries: a file URL, image data, or any file.
    public static func stage(itemProvider provider: NSItemProvider) async throws -> StagedAttachment {
        let suggested = cleanedName(provider.suggestedName ?? "attachment")
        if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier),
            let url = await loadFileURL(from: provider)
        {
            return try stage(fileURL: url)
        }
        let identifiers = provider.registeredTypeIdentifiers
        let preferred =
            identifiers.first { UTType($0)?.conforms(to: .image) == true }
            ?? identifiers.first { UTType($0)?.conforms(to: .data) == true }
            ?? identifiers.first
        guard let identifier = preferred else {
            throw AttachmentStagingError.unreadable(filename: suggested)
        }
        let type = UTType(identifier)
        let (data, fileName) = try await loadFileData(from: provider, identifier: identifier)
        var name = fileName.map(cleanedName) ?? suggested
        if (name as NSString).pathExtension.isEmpty,
            let pathExtension = type?.preferredFilenameExtension
        {
            name = "\(name).\(pathExtension)"
        }
        return try stage(data: data, filename: name, type: type)
    }

    private static func loadFileURL(from provider: NSItemProvider) async -> URL? {
        await withCheckedContinuation { continuation in
            _ = provider.loadObject(ofClass: URL.self) { url, _ in
                continuation.resume(returning: url?.isFileURL == true ? url : nil)
            }
        }
    }

    /// The temporary file only lives inside the callback, so its bytes are read there.
    private static func loadFileData(
        from provider: NSItemProvider,
        identifier: String
    ) async throws -> (Data, String?) {
        let fallbackName = cleanedName(provider.suggestedName ?? "attachment")
        return try await withCheckedThrowingContinuation { continuation in
            _ = provider.loadFileRepresentation(forTypeIdentifier: identifier) { url, _ in
                guard let url,
                    let size = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize,
                    size <= readableImageSourceBytes,
                    let data = try? Data(contentsOf: url)
                else {
                    continuation.resume(
                        throwing: AttachmentStagingError.unreadable(filename: fallbackName)
                    )
                    return
                }
                continuation.resume(returning: (data, url.lastPathComponent))
            }
        }
    }
}
