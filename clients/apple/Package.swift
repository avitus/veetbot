// swift-tools-version: 6.0

import PackageDescription
import Foundation

let commandLineDeveloper = "/Library/Developer/CommandLineTools"
let testingFrameworks = "\(commandLineDeveloper)/Library/Developer/Frameworks"
let testingLibraries = "\(commandLineDeveloper)/Library/Developer/usr/lib"
let testingMacros = "\(commandLineDeveloper)/usr/lib/swift/host/plugins/testing/libTestingMacros.dylib"
let commandLineTestingFlags: [SwiftSetting] = FileManager.default.fileExists(atPath: testingMacros)
    ? [.unsafeFlags(["-F", testingFrameworks, "-load-plugin-library", testingMacros])]
    : []
let commandLineTestingLinkerFlags: [LinkerSetting] = FileManager.default.fileExists(atPath: testingMacros)
    ? [.unsafeFlags([
        "-F", testingFrameworks,
        "-framework", "Testing",
        "-Xlinker", "-rpath",
        "-Xlinker", testingFrameworks,
        "-Xlinker", "-rpath",
        "-Xlinker", testingLibraries,
    ])]
    : []

let package = Package(
    name: "VeetbotAppleClient",
    platforms: [
        .iOS(.v15),
        .macOS(.v12),
    ],
    products: [
        .library(name: "VeetbotCore", targets: ["VeetbotCore"]),
    ],
    targets: [
        .target(
            name: "VeetbotCore",
            path: "Veetbot",
            exclude: [
                "Resources",
                "VeetbotApp.swift",
            ]
        ),
        .testTarget(
            name: "VeetbotCoreTests",
            dependencies: ["VeetbotCore"],
            path: "Tests/VeetbotCoreTests",
            swiftSettings: commandLineTestingFlags,
            linkerSettings: commandLineTestingLinkerFlags
        ),
    ],
    swiftLanguageModes: [.v5]
)
