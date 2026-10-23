import SwiftUI

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

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
        VStack(alignment: .leading, spacing: VeetSpace.s3) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                MarkdownBlockView(block: block)
                    // Proposed less height than its wrap needs, a block cuts
                    // its last line off with an ellipsis; a quote's bar also
                    // stretched past its text and took that height from the
                    // paragraphs around it.
                    .fixedSize(horizontal: false, vertical: true)
                    // 24 before a heading: the 12 between blocks and 12 more.
                    .padding(.top, index > 0 && block.isHeading ? VeetSpace.s3 : 0)
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
                .frame(maxWidth: .infinity, alignment: .leading)
        case .heading(let level, let text):
            // Headings in prose use the title role in sans; the smaller
            // levels are body text at 600.
            inlineMarkdownText(text)
                .veetType(level <= 3 ? .title : .body, weight: markdownHeadingWeight(level))
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityAddTraits(.isHeader)
        case .blockquote(let blocks):
            MarkdownQuoteView(rows: MarkdownQuoteRow.rows(blocks))
        case .list(let items):
            MarkdownListView(items: items)
        case .codeBlock(let language, let code):
            MarkdownCodeBlockView(language: language, code: code)
        case .thematicBreak:
            HairlineDivider().padding(.vertical, VeetSpace.s1)
        case .table(let table):
            MarkdownTableView(table: table)
        }
    }

}

extension MarkdownContentBlock {
    var isHeading: Bool {
        if case .heading = self { return true }
        return false
    }
}

/// One block of a quote and how many levels deep it sits, so a nested reply
/// chain renders as one flat stack. One stack per quote level made SwiftUI's
/// layout cost grow about sevenfold per level, and a ten-level thread hung
/// the app.
struct MarkdownQuoteRow: Equatable, Sendable {
    let depth: Int
    let block: MarkdownContentBlock

    /// Flattens nested quotes; depth 1 is the outer quote, and no row holds a quote.
    static func rows(_ blocks: [MarkdownContentBlock], depth: Int = 1) -> [MarkdownQuoteRow] {
        var rows: [MarkdownQuoteRow] = []
        append(blocks, depth: depth, to: &rows)
        return rows
    }

    /// One shared accumulator keeps flattening linear in the number of rows.
    private static func append(
        _ blocks: [MarkdownContentBlock], depth: Int, to rows: inout [MarkdownQuoteRow]
    ) {
        for block in blocks {
            if case .blockquote(let inner) = block {
                append(inner, depth: depth + 1, to: &rows)
            } else {
                rows.append(MarkdownQuoteRow(depth: depth, block: block))
            }
        }
    }
}

private struct MarkdownQuoteView: View {
    let rows: [MarkdownQuoteRow]

    private static let barWidth: CGFloat = 2
    private static let levelIndent: CGFloat = VeetSpace.s4

    var body: some View {
        HStack(alignment: .top, spacing: VeetSpace.s3) {
            Rectangle()
                .fill(AppTheme.hairlineStrong)
                .frame(width: Self.barWidth)
            VStack(alignment: .leading, spacing: VeetSpace.s3) {
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    AnyView(MarkdownBlockView(block: row.block))
                        .padding(.leading, CGFloat(row.depth - 1) * Self.levelIndent)
                        .overlay(innerBars(count: row.depth - 1), alignment: .leading)
                }
            }
        }
        .padding(.vertical, VeetSpace.s1)
        .frame(maxWidth: .infinity, alignment: .leading)
        .foregroundColor(AppTheme.inkSoft)
    }

    /// One fainter bar per inner level, in the indent the row leaves for them.
    private func innerBars(count: Int) -> some View {
        HStack(spacing: Self.levelIndent - Self.barWidth) {
            ForEach(0..<count, id: \.self) { _ in
                Rectangle()
                    .fill(AppTheme.hairline)
                    .frame(width: Self.barWidth)
            }
        }
    }
}

/// Every heading is 600: bold is never used (type revision 2).
func markdownHeadingWeight(_ level: Int) -> VeetType.Weight {
    .semibold
}

private struct MarkdownListView: View {
    let items: [MarkdownListItem]

    var body: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s1) {
            ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
                    marker(for: item)
                        .frame(width: VeetSpace.s5, alignment: .trailing)
                    // Beside the marker the row can propose a line less than
                    // the wrap needs, which cut the item off with an ellipsis.
                    inlineMarkdownText(item.text)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .padding(.leading, CGFloat(item.depth) * VeetSpace.s5)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func marker(for item: MarkdownListItem) -> some View {
        if let isComplete = item.taskIsComplete {
            Image(systemName: isComplete ? "checkmark.square.fill" : "square")
                .foregroundColor(isComplete ? AppTheme.teal : AppTheme.inkFaint)
                .accessibilityLabel(isComplete ? "Completed" : "Not completed")
        } else {
            switch item.marker {
            case .bullet:
                Text("•")
            case .number(let number):
                Text("\(number).")
                    .foregroundColor(AppTheme.inkSoft)
            }
        }
    }
}

private struct MarkdownCodeBlockView: View {
    let language: String?
    let code: String

    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: VeetSpace.s2) {
                Text(language ?? "Code")
                    .veetType(.meta, weight: .semibold)
                    .foregroundColor(AppTheme.inkSoft)
                Spacer(minLength: VeetSpace.s2)
                Button {
                    SystemClipboard.copy(code)
                    copied = true
                } label: {
                    if copied {
                        Label("Copied", systemImage: "checkmark")
                    } else {
                        Label("Copy", systemImage: "doc.on.doc")
                    }
                }
                .buttonStyle(.plain)
                .veetType(.meta, weight: .semibold)
                .foregroundColor(AppTheme.inkSoft)
                .accessibilityLabel(
                    copied ? "Copied to clipboard" : "Copy code to clipboard"
                )
                .accessibilityIdentifier("markdown.code.copy")
            }
            .padding(.horizontal, VeetSpace.s3)
            .padding(.vertical, VeetSpace.s2)
            HairlineDivider()
            ScrollView(.horizontal, showsIndicators: true) {
                Text(code.isEmpty ? " " : code)
                    .veetMono()
                    .fixedSize(horizontal: true, vertical: true)
                    .padding(VeetSpace.s3)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppTheme.paperDeep)
        .clipShape(RoundedRectangle(cornerRadius: A2Metrics.tableRadius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: A2Metrics.tableRadius, style: .continuous)
                .stroke(AppTheme.hairline)
        )
        .onChange(of: code) { copied = false }
    }
}

enum SystemClipboard {
    @MainActor
    static func copy(_ text: String) {
        #if os(iOS)
        UIPasteboard.general.string = text
        #elseif os(macOS)
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        #endif
    }
}

/// A table in the dense role: a semibold soft-ink header on paper-deep,
/// 8 × 12 cells, hairline row rules and a 10-radius hairline frame. On the Mac
/// and iPad long cells wrap inside a capped column; on the iPhone cells never
/// wrap and the table scrolls sideways, bleeding to the right edge.
private struct MarkdownTableView: View {
    let table: MarkdownTable

    @ScaledMetric(relativeTo: .body) private var minimumColumnWidth: CGFloat = 100
    @ScaledMetric(relativeTo: .body) private var maximumColumnWidth: CGFloat = 320
    @ScaledMetric(relativeTo: .body) private var approximateCharacterWidth: CGFloat = 7

    @Environment(\.veetTranscript) private var inTranscript

    private var scrollsInsteadOfWrapping: Bool { VeetType.Platform.current == .iPhone }
    /// Only the transcript's tables run to the screen's right edge.
    private var bleeds: Bool { scrollsInsteadOfWrapping && inTranscript }

    var body: some View {
        ScrollView(.horizontal, showsIndicators: true) {
            Grid(alignment: .leading, horizontalSpacing: 0, verticalSpacing: 0) {
                row(table.headers, isHeader: true)
                ForEach(Array(table.rows.enumerated()), id: \.offset) { _, cells in
                    HairlineDivider().gridCellUnsizedAxes(.horizontal)
                    row(cells, isHeader: false)
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: A2Metrics.tableRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: A2Metrics.tableRadius, style: .continuous)
                    .strokeBorder(AppTheme.hairline)
            )
            .padding(.trailing, bleeds ? A2Metrics.transcriptSidePadding : 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.trailing, bleeds ? -A2Metrics.transcriptSidePadding : 0)
        .environment(\.veetTranscript, false)
    }

    private func row(_ cells: [String], isHeader: Bool) -> some View {
        GridRow {
            ForEach(Array(cells.enumerated()), id: \.offset) { index, cell in
                cellText(cell, isHeader: isHeader, column: index)
                    .veetType(.dense, weight: isHeader ? .semibold : .regular)
                    .monospacedDigit()
                    .foregroundColor(isHeader ? AppTheme.inkSoft : AppTheme.ink)
                    .multilineTextAlignment(table.alignments[index].textAlignment)
                    .padding(.horizontal, VeetSpace.s3)
                    .padding(.vertical, VeetSpace.s2)
                    .frame(maxWidth: .infinity, alignment: table.alignments[index].viewAlignment)
                    .background(isHeader ? AppTheme.paperDeep : Color.clear)
            }
        }
    }

    @ViewBuilder
    private func cellText(_ cell: String, isHeader: Bool, column: Int) -> some View {
        if scrollsInsteadOfWrapping {
            inlineMarkdownText(cell)
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: true)
        } else {
            inlineMarkdownText(cell)
                .fixedSize(horizontal: false, vertical: true)
                .frame(width: columnWidth(at: column), alignment: table.alignments[column].viewAlignment)
        }
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

/// Inline Markdown as one `Text`. Strong text renders at 600, never bold:
/// its runs drop the strong intent and take the semibold weight instead.
private func inlineMarkdownText(_ source: String) -> Text {
    guard
        let attributed = try? AttributedString(
            markdown: source,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        )
    else { return Text(source) }
    let runs = attributed.runs
    guard runs.contains(where: { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true }) else {
        return Text(attributed)
    }
    return runs.reduce(Text(verbatim: "")) { text, run in
        var slice = AttributedString(attributed[run.range])
        guard var intent = run.inlinePresentationIntent, intent.contains(.stronglyEmphasized) else {
            return Text("\(text)\(Text(slice))")
        }
        intent.remove(.stronglyEmphasized)
        slice.inlinePresentationIntent = intent.isEmpty ? nil : intent
        return Text("\(text)\(Text(slice).fontWeight(.semibold))")
    }
}
