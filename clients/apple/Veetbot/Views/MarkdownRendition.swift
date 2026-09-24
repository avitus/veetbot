import Foundation
import SwiftUI
import UniformTypeIdentifiers

#if os(iOS)
import UIKit
typealias RenditionFont = UIFont
#elseif os(macOS)
import AppKit
typealias RenditionFont = NSFont
#endif

/// A reply rendered once for the clipboard and the Select Text sheet (ADR-0122):
/// formatted text, the same text without Markdown symbols, and semantic HTML.
/// The formatted text carries no colors, so a copy made in dark mode never
/// pastes white text.
struct MarkdownRendition {
    let attributedText: NSAttributedString
    let plainText: String
    let html: String

    func rtfData() -> Data? {
        try? attributedText.data(
            from: NSRange(location: 0, length: attributedText.length),
            documentAttributes: [.documentType: NSAttributedString.DocumentType.rtf]
        )
    }
}

extension MarkdownRendition {
    /// Renders at the body size the transcript uses for the app's text size.
    init(markdown: String, textSize: AppTextSize = .system) {
        self = MarkdownRenditionBuilder.render(
            MarkdownContentParser.parse(markdown),
            bodySize: appPointSize(for: .body, textSize: textSize) ?? systemBodyPointSize()
        )
    }
}

private func systemBodyPointSize() -> CGFloat {
    #if os(macOS)
    return NSFont.preferredFont(forTextStyle: .body, options: [:]).pointSize
    #else
    return UIFont.preferredFont(forTextStyle: .body).pointSize
    #endif
}

/// Walks the transcript parser's blocks once and writes all three renditions.
enum MarkdownRenditionBuilder {
    static func render(_ blocks: [MarkdownContentBlock], bodySize: CGFloat) -> MarkdownRendition {
        var writer = RenditionWriter(bodySize: bodySize)
        writer.appendBlocks(blocks, indent: 0)
        return writer.finish()
    }
}

private struct RenditionWriter {
    private static let quoteIndent: CGFloat = 16
    private static let listIndent: CGFloat = 20
    private static let markerWidth: CGFloat = 22

    let bodySize: CGFloat
    private let attributed = NSMutableAttributedString()
    private var plain = ""
    private var html = ""
    private var startedBlock = false

    init(bodySize: CGFloat) {
        self.bodySize = bodySize
    }

    mutating func finish() -> MarkdownRendition {
        let text = attributed.string as NSString
        var end = text.length
        while end > 0, text.character(at: end - 1) == 10 { end -= 1 }
        attributed.deleteCharacters(in: NSRange(location: end, length: text.length - end))
        while plain.hasSuffix("\n") { plain.removeLast() }
        return MarkdownRendition(
            attributedText: NSAttributedString(attributedString: attributed),
            plainText: plain,
            html: "<html><head><meta charset=\"utf-8\"></head><body>\(html)</body></html>"
        )
    }

    mutating func appendBlocks(_ blocks: [MarkdownContentBlock], indent: CGFloat) {
        for block in blocks {
            appendBlock(block, indent: indent)
        }
    }

    private mutating func separateBlocks() {
        if startedBlock {
            attributed.append(NSAttributedString(string: "\n", attributes: [.font: bodyFont()]))
            plain += "\n"
        }
        startedBlock = true
    }

    private mutating func appendBlock(_ block: MarkdownContentBlock, indent: CGFloat) {
        switch block {
        case .paragraph(let text):
            separateBlocks()
            let inline = InlineRendition(text, font: bodyFont())
            appendLine(inline.attributed, style: paragraphStyle(indent: indent))
            plain += inline.plain + "\n"
            html += "<p>\(inline.html)</p>"
        case .heading(let level, let text):
            separateBlocks()
            let inline = InlineRendition(text, font: headingFont(level))
            appendLine(inline.attributed, style: paragraphStyle(indent: indent))
            plain += inline.plain + "\n"
            let tag = "h\(min(max(level, 1), 6))"
            html += "<\(tag)>\(inline.html)</\(tag)>"
        case .blockquote(let blocks):
            html += "<blockquote>"
            appendBlocks(blocks, indent: indent + Self.quoteIndent)
            html += "</blockquote>"
        case .list(let items):
            separateBlocks()
            appendList(items, indent: indent)
        case .codeBlock(_, let code):
            separateBlocks()
            let font = RenditionFont.monospacedSystemFont(ofSize: bodySize * 0.92, weight: .regular)
            appendLine(
                NSAttributedString(string: code, attributes: [.font: font]),
                style: paragraphStyle(indent: indent)
            )
            plain += code + "\n"
            html += "<pre><code>\(escapeHTML(code))</code></pre>"
        case .thematicBreak:
            separateBlocks()
            let style = paragraphStyle(indent: indent)
            style.alignment = .center
            appendLine(NSAttributedString(string: "———", attributes: [.font: bodyFont()]), style: style)
            plain += "———\n"
            html += "<hr>"
        case .table(let table):
            separateBlocks()
            appendTable(table, indent: indent)
        }
    }

    private mutating func appendLine(_ line: NSAttributedString, style: NSParagraphStyle) {
        let paragraph = NSMutableAttributedString(attributedString: line)
        paragraph.append(NSAttributedString(string: "\n", attributes: [.font: bodyFont()]))
        paragraph.addAttribute(
            .paragraphStyle, value: style, range: NSRange(location: 0, length: paragraph.length)
        )
        attributed.append(paragraph)
    }

    private mutating func appendList(_ items: [MarkdownListItem], indent: CGFloat) {
        var openLists: [String] = []
        var itemOpen: [Bool] = []
        func closeLevel(_ html: inout String) {
            if itemOpen.removeLast() { html += "</li>" }
            html += "</\(openLists.removeLast())>"
        }
        for item in items {
            let marker = listMarker(item)
            let inline = InlineRendition(item.text, font: bodyFont())
            let line = NSMutableAttributedString(
                string: "\(marker)\t", attributes: [.font: bodyFont()]
            )
            line.append(inline.attributed)
            let start = indent + CGFloat(item.depth) * Self.listIndent
            let style = paragraphStyle(indent: start)
            style.headIndent = start + Self.markerWidth
            style.tabStops = [NSTextTab(textAlignment: .natural, location: start + Self.markerWidth)]
            appendLine(line, style: style)
            plain += String(repeating: "    ", count: item.depth) + "\(marker) \(inline.plain)\n"

            let level = item.depth
            let tag: String
            let opening: String
            switch item.marker {
            case .bullet:
                tag = "ul"
                opening = "<ul>"
            case .number(let number):
                tag = "ol"
                opening = number == 1 ? "<ol>" : "<ol start=\"\(number)\">"
            }
            while openLists.count > level + 1 { closeLevel(&html) }
            if openLists.count == level + 1, openLists[level] != tag { closeLevel(&html) }
            if openLists.count == level + 1, itemOpen[level] {
                html += "</li>"
                itemOpen[level] = false
            }
            while openLists.count < level + 1 {
                html += opening
                openLists.append(tag)
                itemOpen.append(false)
            }
            let task = item.taskIsComplete.map { $0 ? "☑ " : "☐ " } ?? ""
            html += "<li>\(task)\(inline.html)"
            itemOpen[level] = true
        }
        while !openLists.isEmpty { closeLevel(&html) }
    }

    private func listMarker(_ item: MarkdownListItem) -> String {
        if let isComplete = item.taskIsComplete { return isComplete ? "☑" : "☐" }
        switch item.marker {
        case .bullet: return "•"
        case .number(let number): return "\(number)."
        }
    }

    private mutating func appendTable(_ table: MarkdownTable, indent: CGFloat) {
        let header = table.headers.map { InlineRendition($0, font: boldBodyFont()) }
        let rows = table.rows.map { row in row.map { InlineRendition($0, font: bodyFont()) } }
        var widths = header.map { $0.attributed.size().width }
        for row in rows {
            for (column, cell) in row.enumerated() where column < widths.count {
                widths[column] = max(widths[column], cell.attributed.size().width)
            }
        }
        let style = paragraphStyle(indent: indent)
        var location = indent
        style.tabStops = widths.dropLast().map { width in
            location += width + 24
            return NSTextTab(textAlignment: .natural, location: location)
        }
        for cells in [header] + rows {
            let line = NSMutableAttributedString()
            for (column, cell) in cells.enumerated() {
                if column > 0 { line.append(NSAttributedString(string: "\t", attributes: [.font: bodyFont()])) }
                line.append(cell.attributed)
            }
            appendLine(line, style: style)
            plain += cells.map(\.plain).joined(separator: "\t") + "\n"
        }

        html += "<table border=\"1\" cellpadding=\"4\">"
        html += "<tr>" + header.enumerated().map { column, cell in
            "<th\(alignmentStyle(table, column))>\(cell.html)</th>"
        }.joined() + "</tr>"
        for row in rows {
            html += "<tr>" + row.enumerated().map { column, cell in
                "<td\(alignmentStyle(table, column))>\(cell.html)</td>"
            }.joined() + "</tr>"
        }
        html += "</table>"
    }

    private func alignmentStyle(_ table: MarkdownTable, _ column: Int) -> String {
        guard column < table.alignments.count else { return "" }
        switch table.alignments[column] {
        case .leading: return ""
        case .center: return " style=\"text-align:center\""
        case .trailing: return " style=\"text-align:right\""
        }
    }

    private func paragraphStyle(indent: CGFloat) -> NSMutableParagraphStyle {
        let style = NSMutableParagraphStyle()
        style.firstLineHeadIndent = indent
        style.headIndent = indent
        style.paragraphSpacing = 2
        return style
    }

    private func bodyFont() -> RenditionFont {
        RenditionFont.systemFont(ofSize: bodySize)
    }

    private func boldBodyFont() -> RenditionFont {
        RenditionFont.systemFont(ofSize: bodySize, weight: .bold)
    }

    /// The transcript's heading scale (`MarkdownBlockView.headingTextStyle`)
    /// relative to body text, with its weights.
    private func headingFont(_ level: Int) -> RenditionFont {
        let scale: CGFloat
        let weight: RenditionFont.Weight
        switch level {
        case 1: (scale, weight) = (2.0, .bold)
        case 2: (scale, weight) = (1.3, .bold)
        case 3: (scale, weight) = (1.15, .semibold)
        case 4: (scale, weight) = (1.0, .bold)
        case 5: (scale, weight) = (0.87, .semibold)
        default: (scale, weight) = (0.77, .regular)
        }
        return RenditionFont.systemFont(ofSize: bodySize * scale, weight: weight)
    }
}

/// One run of inline Markdown, parsed with the transcript's own options.
private struct InlineRendition {
    let attributed: NSAttributedString
    let plain: String
    let html: String

    init(_ source: String, font: RenditionFont) {
        guard
            let parsed = try? AttributedString(
                markdown: source,
                options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)
            )
        else {
            attributed = NSAttributedString(string: source, attributes: [.font: font])
            plain = source
            html = escapeHTML(source)
            return
        }
        let output = NSMutableAttributedString()
        var plain = ""
        var html = ""
        var linkLabel = ""
        var currentLink: URL?
        func closeLink() {
            guard let link = currentLink else { return }
            html += "</a>"
            if linkLabel != link.absoluteString {
                plain += " (\(link.absoluteString))"
            }
            currentLink = nil
            linkLabel = ""
        }
        for run in parsed.runs {
            let text = String(parsed[run.range].characters)
            let intent = run.inlinePresentationIntent ?? []
            let link = run.link.flatMap(safeLink)
            if link != currentLink {
                closeLink()
                if let link {
                    html += "<a href=\"\(escapeHTML(link.absoluteString))\">"
                    currentLink = link
                }
            }
            var attributes: [NSAttributedString.Key: Any] = [
                .font: styledFont(
                    font,
                    bold: intent.contains(.stronglyEmphasized),
                    italic: intent.contains(.emphasized),
                    code: intent.contains(.code)
                )
            ]
            if intent.contains(.strikethrough) {
                attributes[.strikethroughStyle] = NSUnderlineStyle.single.rawValue
            }
            if let link { attributes[.link] = link }
            output.append(NSAttributedString(string: text, attributes: attributes))
            plain += text
            if currentLink != nil { linkLabel += text }
            var fragment = escapeHTML(text)
            if intent.contains(.code) { fragment = "<code>\(fragment)</code>" }
            if intent.contains(.strikethrough) { fragment = "<del>\(fragment)</del>" }
            if intent.contains(.emphasized) { fragment = "<em>\(fragment)</em>" }
            if intent.contains(.stronglyEmphasized) { fragment = "<strong>\(fragment)</strong>" }
            html += fragment.replacingOccurrences(of: "\n", with: "<br>")
        }
        closeLink()
        attributed = output
        self.plain = plain
        self.html = html
    }
}

/// Only web and mail links survive a copy; anything else stays plain text.
private func safeLink(_ url: URL) -> URL? {
    guard let scheme = url.scheme?.lowercased(), ["http", "https", "mailto"].contains(scheme)
    else { return nil }
    return url
}

private func styledFont(_ base: RenditionFont, bold: Bool, italic: Bool, code: Bool) -> RenditionFont {
    var font = base
    if code {
        font = RenditionFont.monospacedSystemFont(
            ofSize: base.pointSize * 0.92, weight: bold ? .bold : .regular)
    } else if bold {
        font = RenditionFont.systemFont(ofSize: base.pointSize, weight: .bold)
    }
    guard italic else { return font }
    #if os(macOS)
    let descriptor = font.fontDescriptor.withSymbolicTraits(
        font.fontDescriptor.symbolicTraits.union(.italic))
    return NSFont(descriptor: descriptor, size: font.pointSize) ?? font
    #else
    guard
        let descriptor = font.fontDescriptor.withSymbolicTraits(
            font.fontDescriptor.symbolicTraits.union(.traitItalic))
    else { return font }
    return UIFont(descriptor: descriptor, size: font.pointSize)
    #endif
}

private func escapeHTML(_ text: String) -> String {
    var escaped = ""
    escaped.reserveCapacity(text.count)
    for character in text {
        switch character {
        case "&": escaped += "&amp;"
        case "<": escaped += "&lt;"
        case ">": escaped += "&gt;"
        case "\"": escaped += "&quot;"
        default: escaped.append(character)
        }
    }
    return escaped
}

extension SystemClipboard {
    #if os(macOS)
    /// Writes one item with RTF, HTML, and plain-text representations.
    @MainActor
    static func copy(_ rendition: MarkdownRendition, to pasteboard: NSPasteboard = .general) {
        let item = NSPasteboardItem()
        if let rtf = rendition.rtfData() { item.setData(rtf, forType: .rtf) }
        item.setString(rendition.html, forType: .html)
        item.setString(rendition.plainText, forType: .string)
        pasteboard.clearContents()
        pasteboard.writeObjects([item])
    }
    #elseif os(iOS)
    /// Writes one item with RTF, HTML, and plain-text representations.
    @MainActor
    static func copy(_ rendition: MarkdownRendition, to pasteboard: UIPasteboard = .general) {
        var item: [String: Any] = [
            UTType.html.identifier: rendition.html,
            UTType.utf8PlainText.identifier: rendition.plainText,
        ]
        if let rtf = rendition.rtfData() { item[UTType.rtf.identifier] = rtf }
        pasteboard.setItems([item])
    }
    #endif
}
