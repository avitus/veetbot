import SwiftUI

enum MarkdownTableAlignment: Equatable, Sendable {
    case leading
    case center
    case trailing

    var viewAlignment: Alignment {
        switch self {
        case .leading: return .leading
        case .center: return .center
        case .trailing: return .trailing
        }
    }

    var textAlignment: TextAlignment {
        switch self {
        case .leading: return .leading
        case .center: return .center
        case .trailing: return .trailing
        }
    }
}

struct MarkdownTable: Equatable, Sendable {
    let headers: [String]
    let alignments: [MarkdownTableAlignment]
    let rows: [[String]]
}

enum MarkdownListMarker: Equatable, Sendable {
    case bullet
    case number(Int)
}

struct MarkdownListItem: Equatable, Sendable {
    let depth: Int
    let marker: MarkdownListMarker
    let taskIsComplete: Bool?
    var text: String
}

indirect enum MarkdownContentBlock: Equatable, Sendable {
    case paragraph(String)
    case heading(level: Int, text: String)
    case blockquote([MarkdownContentBlock])
    case list([MarkdownListItem])
    case codeBlock(language: String?, code: String)
    case thematicBreak
    case table(MarkdownTable)
}

enum MarkdownContentParser {
    static func parse(_ text: String) -> [MarkdownContentBlock] {
        let normalized =
            text
            .replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n")
        return parseLines(normalized.components(separatedBy: "\n"))
    }

    private static func parseLines(_ lines: [String]) -> [MarkdownContentBlock] {
        var blocks: [MarkdownContentBlock] = []
        var index = 0

        while index < lines.count {
            if lines[index].trimmingCharacters(in: .whitespaces).isEmpty {
                index += 1
                continue
            }
            if let fence = openingFence(lines[index]) {
                let parsed = fencedCodeBlock(lines, startingAt: index, fence: fence)
                blocks.append(parsed.block)
                index = parsed.nextIndex
                continue
            }
            if let heading = atxHeading(lines[index]) {
                blocks.append(.heading(level: heading.level, text: heading.text))
                index += 1
                continue
            }
            if quoteContent(lines[index]) != nil {
                let parsed = blockquote(lines, startingAt: index)
                blocks.append(parsed.block)
                index = parsed.nextIndex
                continue
            }
            if let table = table(lines, startingAt: index) {
                blocks.append(.table(table.table))
                index = table.nextIndex
                continue
            }
            if index + 1 < lines.count, let level = setextHeadingLevel(lines[index + 1]) {
                blocks.append(
                    .heading(
                        level: level,
                        text: lines[index].trimmingCharacters(in: .whitespaces)
                    )
                )
                index += 2
                continue
            }
            if isThematicBreak(lines[index]) {
                blocks.append(.thematicBreak)
                index += 1
                continue
            }
            if listItem(in: lines[index]) != nil {
                let parsed = list(lines, startingAt: index)
                blocks.append(.list(parsed.items))
                index = parsed.nextIndex
                continue
            }
            if strippedCodeIndent(lines[index]) != nil {
                let parsed = indentedCodeBlock(lines, startingAt: index)
                blocks.append(parsed.block)
                index = parsed.nextIndex
                continue
            }

            let parsed = paragraph(lines, startingAt: index)
            blocks.append(.paragraph(parsed.text))
            index = parsed.nextIndex
        }
        return blocks
    }

    private struct Fence {
        let marker: Character
        let length: Int
        let language: String?
    }

    private struct ParsedBlock {
        let block: MarkdownContentBlock
        let nextIndex: Int
    }

    private struct ParsedTable {
        let table: MarkdownTable
        let nextIndex: Int
    }

    private struct ParsedList {
        let items: [MarkdownListItem]
        let nextIndex: Int
    }

    private struct ParsedParagraph {
        let text: String
        let nextIndex: Int
    }

    private static func openingFence(_ line: String) -> Fence? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard let marker = trimmed.first, marker == "`" || marker == "~" else { return nil }
        let length = trimmed.prefix { $0 == marker }.count
        guard length >= 3 else { return nil }
        let info = trimmed.dropFirst(length).trimmingCharacters(in: .whitespaces)
        let language = info.split(whereSeparator: { $0.isWhitespace }).first.map(String.init)
        return Fence(marker: marker, length: length, language: language)
    }

    private static func closesFence(_ line: String, fence: Fence) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        let markerCount = trimmed.prefix { $0 == fence.marker }.count
        guard markerCount >= fence.length else { return false }
        return trimmed.dropFirst(markerCount).trimmingCharacters(in: .whitespaces).isEmpty
    }

    private static func fencedCodeBlock(
        _ lines: [String],
        startingAt index: Int,
        fence: Fence
    ) -> ParsedBlock {
        var codeLines: [String] = []
        var cursor = index + 1
        while cursor < lines.count, !closesFence(lines[cursor], fence: fence) {
            codeLines.append(lines[cursor])
            cursor += 1
        }
        if cursor < lines.count { cursor += 1 }
        return ParsedBlock(
            block: .codeBlock(language: fence.language, code: codeLines.joined(separator: "\n")),
            nextIndex: cursor
        )
    }

    private static func atxHeading(_ line: String) -> (level: Int, text: String)? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        let hashes = trimmed.prefix { $0 == "#" }.count
        guard hashes > 0, hashes <= 6 else { return nil }
        let remainder = trimmed.dropFirst(hashes)
        guard remainder.isEmpty || remainder.first?.isWhitespace == true else { return nil }

        var text = String(remainder).trimmingCharacters(in: .whitespaces)
        let characters = Array(text)
        var closingStart = characters.count
        while closingStart > 0, characters[closingStart - 1] == "#" { closingStart -= 1 }
        if closingStart < characters.count,
            closingStart == 0 || characters[closingStart - 1].isWhitespace
        {
            text = String(characters[..<closingStart]).trimmingCharacters(in: .whitespaces)
        }
        return (hashes, text)
    }

    private static func setextHeadingLevel(_ line: String) -> Int? {
        let marker = line.trimmingCharacters(in: .whitespaces)
        guard !marker.isEmpty else { return nil }
        if marker.allSatisfy({ $0 == "=" }) { return 1 }
        if marker.allSatisfy({ $0 == "-" }) { return 2 }
        return nil
    }

    private static func quoteContent(_ line: String) -> String? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.first == ">" else { return nil }
        var content = trimmed.dropFirst()
        if content.first == " " { content = content.dropFirst() }
        return String(content)
    }

    private static func blockquote(_ lines: [String], startingAt index: Int) -> ParsedBlock {
        var quoteLines: [String] = []
        var cursor = index
        while cursor < lines.count, let content = quoteContent(lines[cursor]) {
            quoteLines.append(content)
            cursor += 1
        }
        return ParsedBlock(
            block: .blockquote(parseLines(quoteLines)),
            nextIndex: cursor
        )
    }

    private static func isThematicBreak(_ line: String) -> Bool {
        let markers = line.filter { !$0.isWhitespace }
        guard markers.count >= 3, let marker = markers.first,
            marker == "*" || marker == "-" || marker == "_"
        else { return false }
        return markers.allSatisfy { $0 == marker }
    }

    private static func list(_ lines: [String], startingAt index: Int) -> ParsedList {
        var items: [MarkdownListItem] = []
        var cursor = index

        while cursor < lines.count {
            if let item = listItem(in: lines[cursor]) {
                items.append(item)
                cursor += 1
                continue
            }
            if lines[cursor].trimmingCharacters(in: .whitespaces).isEmpty {
                if cursor + 1 < lines.count, listItem(in: lines[cursor + 1]) != nil {
                    cursor += 1
                    continue
                }
                break
            }
            guard !items.isEmpty, leadingIndent(in: lines[cursor]) > 0 else { break }
            items[items.count - 1].text +=
                " " + lines[cursor].trimmingCharacters(in: .whitespaces)
            cursor += 1
        }
        return ParsedList(items: items, nextIndex: cursor)
    }

    private static func listItem(in line: String) -> MarkdownListItem? {
        let characters = Array(line)
        var position = 0
        var indent = 0
        while position < characters.count,
            characters[position] == " " || characters[position] == "\t"
        {
            indent += characters[position] == "\t" ? 4 : 1
            position += 1
        }
        guard position < characters.count else { return nil }

        let marker: MarkdownListMarker
        if ["-", "+", "*"].contains(characters[position]) {
            guard position + 1 < characters.count, characters[position + 1].isWhitespace else {
                return nil
            }
            marker = .bullet
            position += 2
        } else {
            let numberStart = position
            while position < characters.count, characters[position].isNumber { position += 1 }
            guard position > numberStart, position < characters.count,
                characters[position] == "." || characters[position] == ")",
                position + 1 < characters.count,
                characters[position + 1].isWhitespace,
                let number = Int(String(characters[numberStart..<position]))
            else { return nil }
            marker = .number(number)
            position += 2
        }
        while position < characters.count, characters[position].isWhitespace { position += 1 }
        let rawText = position < characters.count ? String(characters[position...]) : ""
        let task = parsedTask(in: rawText)
        return MarkdownListItem(
            depth: indent / 2,
            marker: marker,
            taskIsComplete: task.isComplete,
            text: task.text
        )
    }

    private static func parsedTask(in text: String) -> (isComplete: Bool?, text: String) {
        let characters = Array(text)
        guard characters.count >= 3, characters[0] == "[", characters[2] == "]",
            characters[1] == " " || characters[1] == "x" || characters[1] == "X",
            characters.count == 3 || characters[3].isWhitespace
        else { return (nil, text) }
        let remainder = String(characters.dropFirst(3)).trimmingCharacters(in: .whitespaces)
        return (characters[1] != " ", remainder)
    }

    private static func leadingIndent(in line: String) -> Int {
        var count = 0
        for character in line {
            if character == " " {
                count += 1
            } else if character == "\t" {
                count += 4
            } else {
                break
            }
        }
        return count
    }

    private static func strippedCodeIndent(_ line: String) -> String? {
        if line.first == "\t" { return String(line.dropFirst()) }
        guard line.hasPrefix("    ") else { return nil }
        return String(line.dropFirst(4))
    }

    private static func indentedCodeBlock(_ lines: [String], startingAt index: Int) -> ParsedBlock {
        var codeLines: [String] = []
        var cursor = index
        while cursor < lines.count {
            if let code = strippedCodeIndent(lines[cursor]) {
                codeLines.append(code)
                cursor += 1
            } else if lines[cursor].trimmingCharacters(in: .whitespaces).isEmpty {
                codeLines.append("")
                cursor += 1
            } else {
                break
            }
        }
        while codeLines.last?.isEmpty == true { codeLines.removeLast() }
        return ParsedBlock(
            block: .codeBlock(language: nil, code: codeLines.joined(separator: "\n")),
            nextIndex: cursor
        )
    }

    private static func paragraph(_ lines: [String], startingAt index: Int) -> ParsedParagraph {
        var paragraphLines: [String] = []
        var cursor = index
        while cursor < lines.count,
            !lines[cursor].trimmingCharacters(in: .whitespaces).isEmpty
        {
            if cursor > index, startsBlock(lines, at: cursor) { break }
            paragraphLines.append(trimmingLeadingWhitespace(from: lines[cursor]))
            cursor += 1
        }
        return ParsedParagraph(text: joinParagraphLines(paragraphLines), nextIndex: cursor)
    }

    private static func startsBlock(_ lines: [String], at index: Int) -> Bool {
        openingFence(lines[index]) != nil
            || atxHeading(lines[index]) != nil
            || quoteContent(lines[index]) != nil
            || table(lines, startingAt: index) != nil
            || isThematicBreak(lines[index])
            || listItem(in: lines[index]) != nil
            || strippedCodeIndent(lines[index]) != nil
            || (index + 1 < lines.count && setextHeadingLevel(lines[index + 1]) != nil)
    }

    private static func joinParagraphLines(_ lines: [String]) -> String {
        guard var result = lines.first else { return "" }
        for index in lines.indices.dropFirst() {
            if result.hasSuffix("  ") {
                result.removeLast(2)
                result.append("\n")
            } else if result.hasSuffix("\\") {
                result.removeLast()
                result.append("\n")
            } else {
                result.append(" ")
            }
            result.append(contentsOf: lines[index])
        }
        return result
    }

    private static func trimmingLeadingWhitespace(from line: String) -> String {
        String(line.drop(while: { $0 == " " || $0 == "\t" }))
    }

    private static func table(_ lines: [String], startingAt index: Int) -> ParsedTable? {
        guard index + 1 < lines.count,
            let headers = tableCells(in: lines[index]),
            let alignments = separatorAlignments(lines[index + 1]),
            headers.count == alignments.count
        else { return nil }

        var rows: [[String]] = []
        var cursor = index + 2
        while cursor < lines.count,
            !lines[cursor].trimmingCharacters(in: .whitespaces).isEmpty,
            let cells = tableCells(in: lines[cursor])
        {
            rows.append(normalized(cells, columnCount: headers.count))
            cursor += 1
        }
        return ParsedTable(
            table: MarkdownTable(headers: headers, alignments: alignments, rows: rows),
            nextIndex: cursor
        )
    }

    private static func separatorAlignments(_ line: String) -> [MarkdownTableAlignment]? {
        guard let cells = tableCells(in: line) else { return nil }
        var alignments: [MarkdownTableAlignment] = []
        for cell in cells {
            var marker = cell.trimmingCharacters(in: .whitespaces)
            let hasLeadingColon = marker.first == ":"
            let hasTrailingColon = marker.last == ":"
            if hasLeadingColon { marker.removeFirst() }
            if hasTrailingColon, !marker.isEmpty { marker.removeLast() }
            guard marker.count >= 3, marker.allSatisfy({ $0 == "-" }) else { return nil }
            if hasLeadingColon && hasTrailingColon {
                alignments.append(.center)
            } else if hasTrailingColon {
                alignments.append(.trailing)
            } else {
                alignments.append(.leading)
            }
        }
        return alignments
    }

    private static func tableCells(in line: String) -> [String]? {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return nil }

        let characters = Array(trimmed)
        var cells: [String] = []
        var current = ""
        var delimiterCount = 0
        var codeFenceLength: Int?
        var index = 0

        while index < characters.count {
            let character = characters[index]
            if character == "\\", index + 1 < characters.count {
                current.append(character)
                current.append(characters[index + 1])
                index += 2
                continue
            }
            if character == "`" {
                var runLength = 1
                while index + runLength < characters.count,
                    characters[index + runLength] == "`"
                {
                    runLength += 1
                }
                current.append(contentsOf: repeatElement("`", count: runLength))
                if codeFenceLength == nil {
                    codeFenceLength = runLength
                } else if codeFenceLength == runLength {
                    codeFenceLength = nil
                }
                index += runLength
                continue
            }
            if character == "|", codeFenceLength == nil {
                cells.append(current.trimmingCharacters(in: .whitespaces))
                current = ""
                delimiterCount += 1
            } else {
                current.append(character)
            }
            index += 1
        }
        cells.append(current.trimmingCharacters(in: .whitespaces))

        guard delimiterCount > 0 else { return nil }
        if cells.first?.isEmpty == true { cells.removeFirst() }
        if cells.last?.isEmpty == true { cells.removeLast() }
        return cells.isEmpty ? nil : cells
    }

    private static func normalized(_ cells: [String], columnCount: Int) -> [String] {
        var result = Array(cells.prefix(columnCount))
        result.append(contentsOf: repeatElement("", count: max(0, columnCount - result.count)))
        return result
    }
}

struct MarkdownContentView: View {
    let text: String

    var body: some View {
        MarkdownBlocksView(blocks: MarkdownContentParser.parse(text))
            .frame(maxWidth: .infinity, alignment: .leading)
            .textSelection(.enabled)
    }
}

private struct MarkdownBlocksView: View {
    let blocks: [MarkdownContentBlock]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                MarkdownBlockView(block: block)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct MarkdownBlockView: View {
    let block: MarkdownContentBlock

    @ViewBuilder var body: some View {
        switch block {
        case .paragraph(let text):
            inlineMarkdownText(text)
                .lineSpacing(3)
                .frame(maxWidth: .infinity, alignment: .leading)
        case .heading(let level, let text):
            inlineMarkdownText(text)
                .appFont(headingTextStyle(level), weight: markdownHeadingWeight(level))
                .padding(.top, level <= 2 ? 6 : 2)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityAddTraits(.isHeader)
        case .blockquote(let blocks):
            HStack(alignment: .top, spacing: 12) {
                RoundedRectangle(cornerRadius: 2)
                    .fill(Color.accentColor.opacity(0.65))
                    .frame(width: 3)
                VStack(alignment: .leading, spacing: 10) {
                    ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                        AnyView(MarkdownBlockView(block: block))
                    }
                }
            }
            .padding(.vertical, 6)
            .padding(.horizontal, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.045))
            .clipShape(RoundedRectangle(cornerRadius: 8))
        case .list(let items):
            MarkdownListView(items: items)
        case .codeBlock(let language, let code):
            MarkdownCodeBlockView(language: language, code: code)
        case .thematicBreak:
            Divider().padding(.vertical, 5)
        case .table(let table):
            MarkdownTableView(table: table)
        }
    }

    private func headingTextStyle(_ level: Int) -> Font.TextStyle {
        switch level {
        case 1: return .largeTitle
        case 2: return .title2
        case 3: return .title3
        case 4: return .headline
        case 5: return .subheadline
        default: return .caption
        }
    }

}

func markdownHeadingWeight(_ level: Int) -> Font.Weight? {
    switch level {
    case 1, 2: return .bold
    case 3, 5: return .semibold
    default: return nil
    }
}

private struct MarkdownListView: View {
    let items: [MarkdownListItem]

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    marker(for: item)
                        .frame(width: 24, alignment: .trailing)
                    inlineMarkdownText(item.text)
                        .lineSpacing(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.leading, CGFloat(item.depth) * 20)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func marker(for item: MarkdownListItem) -> some View {
        if let isComplete = item.taskIsComplete {
            Image(systemName: isComplete ? "checkmark.square.fill" : "square")
                .foregroundColor(isComplete ? .accentColor : .secondary)
                .accessibilityLabel(isComplete ? "Completed" : "Not completed")
        } else {
            switch item.marker {
            case .bullet:
                Text("•")
            case .number(let number):
                Text("\(number).")
                    .foregroundColor(.secondary)
            }
        }
    }
}

private struct MarkdownCodeBlockView: View {
    let language: String?
    let code: String

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let language {
                Text(language)
                    .appFont(.caption, weight: .semibold)
                    .foregroundColor(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 7)
                Divider()
            }
            ScrollView(.horizontal, showsIndicators: true) {
                Text(code.isEmpty ? " " : code)
                    .appCodeFont(.body)
                    .fixedSize(horizontal: true, vertical: true)
                    .padding(12)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.secondary.opacity(0.07))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.secondary.opacity(0.2))
        )
    }
}

private struct MarkdownTableView: View {
    let table: MarkdownTable

    @ScaledMetric(relativeTo: .body) private var minimumColumnWidth: CGFloat = 100
    @ScaledMetric(relativeTo: .body) private var maximumColumnWidth: CGFloat = 320
    @ScaledMetric(relativeTo: .body) private var approximateCharacterWidth: CGFloat = 7

    var body: some View {
        ScrollView(.horizontal, showsIndicators: true) {
            VStack(alignment: .leading, spacing: 0) {
                row(table.headers, isHeader: true, isAlternating: false)
                if !table.rows.isEmpty { Divider() }
                ForEach(Array(table.rows.enumerated()), id: \.offset) { index, cells in
                    row(cells, isHeader: false, isAlternating: index.isMultiple(of: 2))
                    if index < table.rows.count - 1 { Divider() }
                }
            }
            .background(Color.secondary.opacity(0.025))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay(
                RoundedRectangle(cornerRadius: 8)
                    .stroke(Color.secondary.opacity(0.25))
            )
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func row(_ cells: [String], isHeader: Bool, isAlternating: Bool) -> some View {
        HStack(alignment: .top, spacing: 0) {
            ForEach(Array(cells.enumerated()), id: \.offset) { index, cell in
                inlineMarkdownText(cell)
                    .appFont(isHeader ? .subheadline : .body, weight: isHeader ? .semibold : nil)
                    .multilineTextAlignment(table.alignments[index].textAlignment)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(
                        width: columnWidth(at: index),
                        alignment: table.alignments[index].viewAlignment
                    )
                    .padding(.horizontal, 10)
                    .padding(.vertical, 8)
                if index < cells.count - 1 { Divider() }
            }
        }
        .background(
            Color.secondary.opacity(isHeader ? 0.12 : (isAlternating ? 0.045 : 0))
        )
    }

    private func columnWidth(at index: Int) -> CGFloat {
        let values = [table.headers[index]] + table.rows.map { $0[index] }
        let characterCount = values.map(\.count).max() ?? 0
        return min(
            maximumColumnWidth,
            max(minimumColumnWidth, CGFloat(characterCount) * approximateCharacterWidth)
        )
    }
}

private func inlineMarkdownText(_ source: String) -> Text {
    guard
        let attributed = try? AttributedString(
            markdown: source,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )
    else { return Text(source) }
    return Text(attributed)
}
