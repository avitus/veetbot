#if os(macOS)
import CryptoKit
import Darwin
import Foundation

/// A grant belongs to one credential on one server. Only its random tool name leaves this Mac.
struct WritableFolder: Codable {
    let capability: String
    let bookmark: Data
    let path: String
    let account: String
    private static let key = "veetbot.mac.writableFolder"

    static func registrationRevision(defaults: UserDefaults) -> String? {
        defaults.string(forKey: key + ".revision")
    }

    static func accountKey(server: URL, token: String) -> String {
        SHA256.hash(data: Data((server.absoluteString + "\u{0}" + token).utf8))
            .map { String(format: "%02x", $0) }.joined()
    }

    static func load(for account: String, defaults: UserDefaults = .standard) -> Self? {
        guard let bytes = defaults.data(forKey: key),
              let grant = try? JSONDecoder().decode(Self.self, from: bytes),
              grant.account == account else { return nil }
        return grant
    }

    static func select(_ directory: URL, account: String, defaults: UserDefaults = .standard) throws -> Self {
        guard try directory.resourceValues(forKeys: [.isDirectoryKey]).isDirectory == true else {
            throw CocoaError(.fileWriteInvalidFileName)
        }
        let grant = Self(capability: "device.folder.write_" + UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: ""),
                         bookmark: try directory.bookmarkData(options: .withSecurityScope,
                                                               includingResourceValuesForKeys: nil, relativeTo: nil),
                         path: directory.path, account: account)
        defaults.set(UUID().uuidString, forKey: key + ".revision")
        defaults.set(try JSONEncoder().encode(grant), forKey: key)
        return grant
    }

    static func remove(defaults: UserDefaults = .standard) {
        if defaults.object(forKey: key) != nil {
            defaults.set(UUID().uuidString, forKey: key + ".revision")
        }
        defaults.removeObject(forKey: key)
    }

    /// Called serially on the model's main actor. A durable claim precedes the rename so a
    /// lost acknowledgement or app restart cannot overwrite a later edit by the owner.
    func apply(_ call: DeviceInvocationView, receipts: URL, now: Date = Date()) throws -> DeviceInvocationResult {
        guard call.expiresAt > now else { return .expired }
        guard call.toolName == capability else { return .failed }
        struct Receipt: Codable {
            var result: DeviceInvocationResult
            let expiresAt: Date
        }
        var journal: [String: Receipt] = [:]
        if FileManager.default.fileExists(atPath: receipts.path) {
            journal = try JSONDecoder().decode([String: Receipt].self, from: Data(contentsOf: receipts))
                .filter { $0.value.expiresAt > now }
        }
        let key = call.id.uuidString
        if let previous = journal[key] { return previous.result }
        guard let filename = call.arguments["filename"]?.stringValue,
              let content = call.arguments["content"]?.stringValue,
              content.utf8.count <= 262144 else { return .failed }
        let bytes: Data
        switch call.arguments["encoding"] {
        case nil, .string("utf8"): bytes = Data(content.utf8)
        case .string("base64"):
            guard let decoded = Data(base64Encoded: content) else { return .failed }
            bytes = decoded
        default: return .failed
        }
        var stale = false
        let directory = try URL(resolvingBookmarkData: bookmark, options: [.withSecurityScope, .withoutUI],
                                relativeTo: nil, bookmarkDataIsStale: &stale)
        guard !stale, directory.startAccessingSecurityScopedResource() else { return .failed }
        defer { directory.stopAccessingSecurityScopedResource() }
        try FileManager.default.createDirectory(at: receipts.deletingLastPathComponent(), withIntermediateDirectories: true)
        journal[key] = Receipt(result: .failed, expiresAt: call.expiresAt)
        try JSONEncoder().encode(journal).write(to: receipts, options: .atomic)
        try Self.install(bytes, named: filename, in: directory)
        journal[key]?.result = .sent
        try JSONEncoder().encode(journal).write(to: receipts, options: .atomic)
        return .sent
    }

    /// Anchor every operation to the same open directory. Rename replaces a directory entry
    /// atomically and never follows a destination link or modifies a hard-linked inode.
    static func install(_ bytes: Data, named filename: String, in directory: URL) throws {
        guard !filename.isEmpty, filename.utf8.count <= 255, !filename.hasPrefix("."),
              filename.rangeOfCharacter(from: CharacterSet(charactersIn: "/\\:").union(.controlCharacters)) == nil
        else { throw CocoaError(.fileWriteInvalidFileName) }
        let dir = open(directory.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard dir >= 0 else { throw POSIXError(.EACCES) }
        defer { close(dir) }
        var info = stat()
        if fstatat(dir, filename, &info, AT_SYMLINK_NOFOLLOW) == 0 {
            guard info.st_mode & S_IFMT == S_IFREG else { throw CocoaError(.fileWriteInvalidFileName) }
        } else if errno != ENOENT {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        let temporary = ".veetbot-" + UUID().uuidString
        let fd = openat(dir, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        defer { unlinkat(dir, temporary, 0) }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        try handle.write(contentsOf: bytes)
        try handle.synchronize()
        try handle.close()
        guard renameat(dir, temporary, dir, filename) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
    }
}
#endif
