// swift-tools-version: 6.0

import Foundation
import PackageDescription

let commandLineDeveloper = "/Library/Developer/CommandLineTools"

func activeDeveloperDirectory() -> String {
    if let configured = ProcessInfo.processInfo.environment["DEVELOPER_DIR"]?
        .trimmingCharacters(in: .whitespacesAndNewlines),
        !configured.isEmpty
    {
        return URL(fileURLWithPath: configured).standardizedFileURL.path
    }

    let process = Process()
    let output = Pipe()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/xcode-select")
    process.arguments = ["--print-path"]
    process.standardOutput = output
    process.standardError = FileHandle.nullDevice
    guard (try? process.run()) != nil else { return commandLineDeveloper }
    process.waitUntilExit()
    guard process.terminationStatus == 0 else { return commandLineDeveloper }

    let selected = String(
        decoding: output.fileHandleForReading.readDataToEndOfFile(),
        as: UTF8.self
    ).trimmingCharacters(in: .whitespacesAndNewlines)
    return selected.isEmpty
        ? commandLineDeveloper
        : URL(fileURLWithPath: selected).standardizedFileURL.path
}

let selectedDeveloperDirectory = activeDeveloperDirectory()
let testingFrameworks = "\(selectedDeveloperDirectory)/Library/Developer/Frameworks"
let testingLibraries = "\(selectedDeveloperDirectory)/Library/Developer/usr/lib"
let testingMacros =
    "\(selectedDeveloperDirectory)/usr/lib/swift/host/plugins/testing/libTestingMacros.dylib"
let fullXcodeFlags: [SwiftSetting] =
    selectedDeveloperDirectory.contains(".app/Contents/Developer")
    ? [.define("XCODE_BUILD")]
    : []
// Command Line Tools needs explicit macOS-host Testing paths. Full Xcode and
// iOS builds resolve their matching Testing artifacts without this workaround;
// see the test-runner note in README.md.
let commandLineTestingFlags: [SwiftSetting] =
    !selectedDeveloperDirectory.contains(".app/Contents/Developer")
        && FileManager.default.fileExists(atPath: testingMacros)
    ? [
        .unsafeFlags(
            ["-F", testingFrameworks, "-load-plugin-library", testingMacros],
            .when(platforms: [.macOS])
        )
    ]
    : []
let commandLineTestingLinkerFlags: [LinkerSetting] =
    !selectedDeveloperDirectory.contains(".app/Contents/Developer")
        && FileManager.default.fileExists(atPath: testingMacros)
    ? [
        .unsafeFlags([
            "-F", testingFrameworks,
            "-framework", "Testing",
            "-Xlinker", "-rpath",
            "-Xlinker", testingFrameworks,
            "-Xlinker", "-rpath",
            "-Xlinker", testingLibraries,
        ], .when(platforms: [.macOS]))
    ]
    : []

let package = Package(
    name: "VeetbotAppleClient",
    platforms: [
        .iOS(.v15),
        .macOS(.v12),
    ],
    products: [
        .library(name: "VeetbotCore", targets: ["VeetbotCore"])
    ],
    targets: [
        .target(
            name: "VeetbotCore",
            path: "Veetbot",
            exclude: [
                "Resources",
                "VeetbotApp.swift",
                "Veetbot.entitlements",
            ],
            swiftSettings: fullXcodeFlags
        ),
        .testTarget(
            name: "VeetbotCoreTests",
            dependencies: ["VeetbotCore"],
            path: "Tests/VeetbotCoreTests",
            swiftSettings: commandLineTestingFlags + fullXcodeFlags,
            linkerSettings: commandLineTestingLinkerFlags
        ),
    ],
    swiftLanguageModes: [.v5]
)
