// swift-tools-version: 6.0

import Foundation
import PackageDescription

let commandLineDeveloper = "/Library/Developer/CommandLineTools"
let testingFrameworks = "\(commandLineDeveloper)/Library/Developer/Frameworks"
let testingLibraries = "\(commandLineDeveloper)/Library/Developer/usr/lib"
let testingMacros =
    "\(commandLineDeveloper)/usr/lib/swift/host/plugins/testing/libTestingMacros.dylib"
let selectedDeveloperDirectory = ProcessInfo.processInfo.environment["DEVELOPER_DIR"] ?? ""
let fullXcodeFlags: [SwiftSetting] =
    selectedDeveloperDirectory.contains(".app/Contents/Developer")
    ? [.define("XCODE_BUILD")]
    : []
// Detect Command Line Tools versus full Xcode so cached manifests do not retain
// incompatible test-runner flags; see the test-runner note in README.md.
let commandLineTestingFlags: [SwiftSetting] =
    FileManager.default.fileExists(atPath: testingMacros)
    ? [.unsafeFlags(["-F", testingFrameworks, "-load-plugin-library", testingMacros])]
    : []
let commandLineTestingLinkerFlags: [LinkerSetting] =
    FileManager.default.fileExists(atPath: testingMacros)
    ? [
        .unsafeFlags([
            "-F", testingFrameworks,
            "-framework", "Testing",
            "-Xlinker", "-rpath",
            "-Xlinker", testingFrameworks,
            "-Xlinker", "-rpath",
            "-Xlinker", testingLibraries,
        ])
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
