import Foundation
import Testing

/// At the ADR-0211 floors the single-parameter `onChange(of:perform:)`,
/// `NavigationLink(isActive:)` and `Text + Text` are deprecated, so the client
/// uses none of them.
///
/// The replacement `onChange` runs the current render's closure, where the
/// single-parameter form ran the previous render's. Handlers that read the mode,
/// scene phase, window state or covering sheet still take the changed value from
/// the action's `newValue` parameter, so their result never depends on which
/// render's environment the closure captured.
@Suite struct ChangeHandlerFormTests {
    private static var appRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("Veetbot")
    }

    private static func sources() throws -> [(name: String, text: String)] {
        let enumerator = try #require(FileManager.default.enumerator(at: appRoot, includingPropertiesForKeys: nil))
        return try enumerator.compactMap { $0 as? URL }
            .filter { $0.pathExtension == "swift" }
            .map { ($0.lastPathComponent, try String(contentsOf: $0, encoding: .utf8)) }
    }

    private static func contents(_ path: String) throws -> String {
        try String(contentsOf: appRoot.appendingPathComponent(path), encoding: .utf8)
    }

    @Test func noChangeHandlerUsesTheDeprecatedSingleValueForm() throws {
        // `.onChange(of: x) { value in` or `{ ... $0 ... }` is the deprecated form;
        // the current forms take no parameters or `{ oldValue, newValue in`.
        let singleParameter = try NSRegularExpression(
            pattern: #"\.onChange\(of:[^\n]*\)\s*\{\s*(?:[A-Za-z_][A-Za-z0-9_]*|_)\s+in\b"#
        )
        let anonymousArgument = try NSRegularExpression(pattern: #"\.onChange\(of:[^\n]*\)\s*\{[^\n}]*\$0"#)
        var offenders: [String] = []
        for (name, text) in try Self.sources() {
            let range = NSRange(text.startIndex..., in: text)
            for match in singleParameter.matches(in: text, range: range) + anonymousArgument.matches(in: text, range: range) {
                let line = text[..<text.index(text.startIndex, offsetBy: match.range.location)]
                    .filter { $0 == "\n" }.count + 1
                offenders.append("\(name):\(line)")
            }
        }
        #expect(offenders.isEmpty, "single-parameter onChange at \(offenders.sorted())")
    }

    @Test func noNavigationLinkUsesTheDeprecatedActiveBinding() throws {
        let offenders = try Self.sources().filter { $0.text.contains("NavigationLink(isActive:") }.map(\.name)
        #expect(offenders.isEmpty, "NavigationLink(isActive:) in \(offenders)")
    }

    @Test func noTextIsConcatenatedWithPlus() throws {
        // `Text + Text` is deprecated at the floors; interpolate a `Text` instead.
        let concatenation = try NSRegularExpression(pattern: #"(?m)^\s*\+\s*Text\("#)
        let offenders = try Self.sources().filter {
            concatenation.firstMatch(in: $0.text, range: NSRange($0.text.startIndex..., in: $0.text)) != nil
        }.map(\.name)
        #expect(offenders.isEmpty, "Text concatenation in \(offenders)")
    }

    @Test func handlersThatReadTheEnvironmentTakeTheChangedValue() throws {
        let root = try Self.contents("Views/RootView.swift")
        #expect(root.contains(".onChange(of: scenePhase) { _, phase in updateEmailActivity(scenePhase: phase) }"))
        #expect(root.contains(".onChange(of: controlActiveState) { _, state in updateEmailActivity(keyWindow: state == .key) }"))
        let chat = try Self.contents("Views/ChatView.swift")
        #expect(chat.contains(".onChange(of: notificationCovered) { _, covered in updateNotificationVisibility(covered: covered) }"))
        #expect(chat.contains(".onChange(of: activeMode) { _, mode in updateNotificationVisibility(mode: mode) }"))
    }

    @Test func synthesisDetailIsPresentedFromTheSelection() throws {
        let synthesis = try Self.contents("Views/MemorySynthesisView.swift")
        #expect(synthesis.contains(".navigationDestination(isPresented: Binding(get: { selectedID != nil }, set: { if !$0 { selectedID = nil } }))"))
    }
}
