import Foundation
import Testing

@testable import VeetbotCore

/// The client's deployment floors (ADR-0211): iOS 27.0, and macOS 26.0 until a
/// hosted CircleCI image runs macOS 27. Every Xcode build configuration and the
/// package manifest declare the same floors, no availability check below them
/// survives in the source, and local history has one backend, SwiftData, with
/// no file-backed store left for systems the floors exclude.
@Suite struct DeploymentFloorTests {
    private static let iOSFloor = (major: 27, minor: 0)
    private static let macOSFloor = (major: 26, minor: 0)

    private static var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private static func contents(_ relativePath: String) throws -> String {
        try String(
            contentsOf: packageRoot.appendingPathComponent(relativePath),
            encoding: .utf8
        )
    }

    private static func settingValues(_ setting: String, in project: String) throws -> [String] {
        let pattern = try NSRegularExpression(pattern: "\(setting) = ([0-9.]+);")
        let range = NSRange(project.startIndex..., in: project)
        return pattern.matches(in: project, range: range).compactMap { match in
            Range(match.range(at: 1), in: project).map { String(project[$0]) }
        }
    }

    @Test
    func testEveryXcodeConfigurationTargetsTheFloors() throws {
        let project = try Self.contents("Veetbot.xcodeproj/project.pbxproj")
        let iOSTargets = try Self.settingValues("IPHONEOS_DEPLOYMENT_TARGET", in: project)
        let macOSTargets = try Self.settingValues("MACOSX_DEPLOYMENT_TARGET", in: project)

        // The app and UI-test targets, each with Debug and Release.
        #expect(iOSTargets.count == 4)
        #expect(macOSTargets.count == 4)
        #expect(Set(iOSTargets) == ["\(Self.iOSFloor.major).\(Self.iOSFloor.minor)"])
        #expect(Set(macOSTargets) == ["\(Self.macOSFloor.major).\(Self.macOSFloor.minor)"])
    }

    @Test
    func testThePackageManifestTargetsTheFloors() throws {
        let manifest = try Self.contents("Package.swift")

        #expect(manifest.contains(".iOS(\"\(Self.iOSFloor.major).\(Self.iOSFloor.minor)\")"))
        #expect(manifest.contains(".macOS(\"\(Self.macOSFloor.major).\(Self.macOSFloor.minor)\")"))
    }

    /// Every `#available` and `@available` clause in the app, the package tests
    /// and the UI tests names a version at or above the floors; anything below
    /// is a branch the floors made dead.
    @Test
    func testNoAvailabilityCheckBelowTheFloorsRemains() throws {
        let clause = try NSRegularExpression(pattern: "available\\(([^)]*)\\)")
        let platformVersion = try NSRegularExpression(
            pattern: "^(iOS|macOS)\\s+([0-9]+)(?:\\.([0-9]+))?$"
        )
        var offenders: [String] = []
        for file in try Self.swiftSources(under: ["Veetbot", "Tests", "VeetbotUITests"]) {
            let source = try String(contentsOf: file, encoding: .utf8)
            let range = NSRange(source.startIndex..., in: source)
            for match in clause.matches(in: source, range: range) {
                guard let argumentRange = Range(match.range(at: 1), in: source) else { continue }
                for argument in source[argumentRange].split(separator: ",") {
                    let token = argument.trimmingCharacters(in: .whitespacesAndNewlines)
                    let tokenRange = NSRange(token.startIndex..., in: token)
                    guard let version = platformVersion.firstMatch(in: token, range: tokenRange),
                        let platformRange = Range(version.range(at: 1), in: token),
                        let majorRange = Range(version.range(at: 2), in: token),
                        let major = Int(token[majorRange])
                    else { continue }
                    let minor = Range(version.range(at: 3), in: token).flatMap { Int(token[$0]) } ?? 0
                    let floor = token[platformRange] == "iOS" ? Self.iOSFloor : Self.macOSFloor
                    if (major, minor) < floor {
                        let line = source[..<argumentRange.lowerBound].count(where: { $0 == "\n" }) + 1
                        offenders.append("\(file.lastPathComponent):\(line) \(token)")
                    }
                }
            }
        }

        #expect(offenders.isEmpty, "availability checks below the deployment floors: \(offenders)")
    }

    @Test
    func testLocalHistoryHasNoFileBackedStore() throws {
        let factory = try Self.contents("Veetbot/Store/SessionHistoryStore.swift")
        let legacyStore = Self.packageRoot.appendingPathComponent("Veetbot/Store/FileSessionHistoryStore.swift")

        #expect(!factory.contains("FileSessionHistoryStore"))
        #expect(!FileManager.default.fileExists(atPath: legacyStore.path))
    }

    private static func swiftSources(under directories: [String]) throws -> [URL] {
        var files: [URL] = []
        let ownFile = URL(fileURLWithPath: #filePath).standardizedFileURL
        for directory in directories {
            let root = packageRoot.appendingPathComponent(directory, isDirectory: true)
            guard let enumerator = FileManager.default.enumerator(
                at: root, includingPropertiesForKeys: [.isRegularFileKey]
            ) else { continue }
            for case let url as URL in enumerator
            where url.pathExtension == "swift" && url.standardizedFileURL != ownFile {
                files.append(url)
            }
        }
        return files.sorted { $0.path < $1.path }
    }
}
