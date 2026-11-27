#if os(macOS)
import Foundation
import Testing
@testable import VeetbotCore

@Suite struct WritableFolderTests {
    @Test func settingsOfferFolderAccessOnThisMac() {
        #expect(ConnectionSettingsSection.visibleCases.map(\.title).contains("Folder access"))
    }
    @Test func writesAndReplacesWithoutFollowingLinksOrEscaping() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        let folder = root.appending(path: "chosen")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let file = folder.appending(path: "report.txt")
        try WritableFolder.install(Data("first".utf8), named: file.lastPathComponent, in: folder)
        #expect(try String(contentsOf: file, encoding: .utf8) == "first")
        let outside = root.appending(path: "outside")
        try FileManager.default.linkItem(at: file, to: outside)
        try WritableFolder.install(Data("new".utf8), named: file.lastPathComponent, in: folder)
        #expect(try String(contentsOf: file, encoding: .utf8) == "new")
        try FileManager.default.createSymbolicLink(at: folder.appending(path: "link"), withDestinationURL: outside)
        for name in ["link", "../outside", "/outside", ".hidden", "sub/file", ""] {
            #expect(throws: (any Error).self) {
                try WritableFolder.install(Data(), named: name, in: folder)
            }
        }
        #expect(try String(contentsOf: outside, encoding: .utf8) == "first")
    }

    @Test func aReplayedInvocationPreservesLaterOwnerEdits() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        let directory = root.appending(path: "chosen")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let suite = UUID().uuidString
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let grant = try WritableFolder.select(directory, account: "owner", defaults: defaults)
        let receipts = root.appending(path: "state/receipts.json")
        let call = DeviceInvocationView(id: UUID(), toolName: grant.capability, arguments: [
            "filename": .string("report.bin"), "encoding": .string("base64"), "content": .string("AP8=")
        ], createdAt: Date(), expiresAt: Date().addingTimeInterval(60))
        #expect(try grant.apply(call, receipts: receipts) == .sent)
        let file = directory.appending(path: "report.bin")
        #expect(try Data(contentsOf: file) == Data([0, 255]))
        try Data("edited".utf8).write(to: file)
        #expect(try grant.apply(call, receipts: receipts) == .sent)
        #expect(try Data(contentsOf: file) == Data("edited".utf8))
        #expect(try grant.apply(call, receipts: receipts, now: call.expiresAt) == .expired)
        let replacement = try WritableFolder.select(directory, account: "owner", defaults: defaults)
        #expect(try replacement.apply(call, receipts: receipts) == .failed)
    }

    @Test func replacementAndRemovalInvalidateThePreviousGrant() throws {
        let name = UUID().uuidString
        let defaults = try #require(UserDefaults(suiteName: name))
        defer { defaults.removePersistentDomain(forName: name) }
        let directory = FileManager.default.temporaryDirectory
        let first = try WritableFolder.select(directory, account: "owner", defaults: defaults)
        #expect(WritableFolder.load(for: "other", defaults: defaults) == nil)
        let second = try WritableFolder.select(directory, account: "owner", defaults: defaults)
        #expect(first.capability != second.capability)
        #expect(WritableFolder.load(for: "owner", defaults: defaults)?.capability == second.capability)
        WritableFolder.remove(defaults: defaults)
        #expect(WritableFolder.load(for: "owner", defaults: defaults) == nil)
    }
}
#endif
