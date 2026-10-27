import Foundation
import Testing

/// Every field's example or hint is a placeholder in faint ink (the design
/// system's `.s-field::placeholder`), never the platform's grey, which a
/// plain field draws nearly as strong as typed text, and never a localized
/// key, whose Markdown turns an example address into a blue link. A field
/// in a `Form` passes `.a2FormPrompt`, which leaves the Mac's label alone.
@Suite struct FieldPromptTests {
    @Test
    func testEveryFieldDrawsItsPlaceholderAsAnA2Prompt() throws {
        var offences: [String] = []
        for file in try viewFiles() {
            let source = try String(contentsOf: file, encoding: .utf8)
            for call in fieldCalls(in: source)
            where !call.arguments.contains("prompt: .a2Prompt(") && !call.arguments.contains("prompt: .a2FormPrompt(") {
                offences.append("\(file.lastPathComponent):\(call.line): \(call.name)(\(call.arguments.prefix(60)))")
            }
        }
        #expect(offences.isEmpty, "Pass prompt: .a2Prompt(…) or, in a Form, .a2FormPrompt(…):\n\(offences.joined(separator: "\n"))")
    }

    @Test
    func testTheGuardSeesMultilineCallsAndIgnoresComments() {
        let source = """
            // TextField("in a comment", text: $x)
            TextField(
                "Name",
                text: $name,
                prompt: .a2Prompt("magicpath")
            )
            SecureField("Token", text: $token)
            """
        let calls = fieldCalls(in: source)
        #expect(calls.map(\.name) == ["TextField", "SecureField"])
        #expect(calls.map(\.line) == [2, 7])
        #expect(calls[0].arguments.contains("prompt: .a2Prompt(\"magicpath\")"))
        #expect(!calls[1].arguments.contains("prompt:"))
    }

    // MARK: - Source scanning

    private struct FieldCall {
        let name: String
        let line: Int
        let arguments: String
    }

    /// Each `TextField(` or `SecureField(` call outside a line comment, with
    /// its arguments up to the balancing parenthesis.
    private func fieldCalls(in source: String) -> [FieldCall] {
        let characters = Array(source)
        let pattern = try! NSRegularExpression(pattern: #"\b(TextField|SecureField)\("#)
        let nsSource = source as NSString
        var calls: [FieldCall] = []
        for match in pattern.matches(in: source, range: NSRange(location: 0, length: nsSource.length)) {
            let lineStart = nsSource.lineRange(for: NSRange(location: match.range.location, length: 0)).location
            let prefix = nsSource.substring(with: NSRange(location: lineStart, length: match.range.location - lineStart))
            if prefix.contains("//") { continue }
            let open = (source.utf16.index(source.utf16.startIndex, offsetBy: match.range.upperBound))
            var index = source.distance(from: source.startIndex, to: open)
            var depth = 1
            var inString = false
            var arguments = ""
            while index < characters.count, depth > 0 {
                let character = characters[index]
                if inString {
                    if character == "\\", index + 1 < characters.count {
                        arguments.append(character)
                        arguments.append(characters[index + 1])
                        index += 2
                        continue
                    } else if character == "\"" {
                        inString = false
                    }
                } else if character == "\"" {
                    inString = true
                } else if character == "(" {
                    depth += 1
                } else if character == ")" {
                    depth -= 1
                }
                if depth > 0 { arguments.append(character) }
                index += 1
            }
            let line = nsSource.substring(to: match.range.location).components(separatedBy: "\n").count
            calls.append(FieldCall(name: nsSource.substring(with: match.range(at: 1)), line: line, arguments: arguments))
        }
        return calls
    }

    private var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }

    private func viewFiles() throws -> [URL] {
        let directory = packageRoot.appendingPathComponent("Veetbot/Views")
        return try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
    }
}
