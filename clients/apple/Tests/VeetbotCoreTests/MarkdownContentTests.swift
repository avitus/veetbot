import Testing

@testable import VeetbotCore

@Suite struct MarkdownContentTests {
    @Test
    func testParsesTableBetweenProseBlocksAndPreservesAlignment() {
        let blocks = MarkdownContentParser.parse(
            """
            Summary

            | Name | Value | Notes |
            | :--- | ---: | :---: |
            | Alpha | 12 | Ready |

            Done
            """
        )

        #expect(
            blocks == [
                .paragraph("Summary"),
                .table(
                    MarkdownTable(
                        headers: ["Name", "Value", "Notes"],
                        alignments: [.leading, .trailing, .center],
                        rows: [["Alpha", "12", "Ready"]]
                    )
                ),
                .paragraph("Done"),
            ]
        )
    }

    @Test
    func testNormalizesCarriageReturnLineEndingsBeforeParsingTables() {
        let expected: [MarkdownContentBlock] = [
            .table(
                MarkdownTable(
                    headers: ["Name", "Value"],
                    alignments: [.leading, .trailing],
                    rows: [["Alpha", "12"]]
                )
            )
        ]

        let crlf = "| Name | Value |\r\n| --- | ---: |\r\n| Alpha | 12 |"
        let carriageReturn = "| Name | Value |\r| --- | ---: |\r| Alpha | 12 |"

        #expect(MarkdownContentParser.parse(crlf) == expected)
        #expect(MarkdownContentParser.parse(carriageReturn) == expected)
    }

    @Test
    func testPipesInsideCodeAndEscapedPipesStayInsideCells() throws {
        let blocks = MarkdownContentParser.parse(
            """
            | Expression | Description |
            | --- | --- |
            | `left|right` | first \\| second |
            """
        )
        let block = try #require(blocks.first)
        guard case .table(let table) = block else {
            Issue.record("Expected a Markdown table")
            return
        }

        #expect(table.rows == [["`left|right`", "first \\| second"]])
    }

    @Test
    func testShortRowsArePaddedAndExtraCellsAreIgnored() throws {
        let blocks = MarkdownContentParser.parse(
            """
            A | B | C
            --- | --- | ---
            1 | 2
            3 | 4 | 5 | 6
            """
        )
        let block = try #require(blocks.first)
        guard case .table(let table) = block else {
            Issue.record("Expected a Markdown table")
            return
        }

        #expect(table.rows == [["1", "2", ""], ["3", "4", "5"]])
    }

    @Test
    func testIncompleteTablesRemainParagraphsAndFencedTablesRemainCode() {
        let incomplete = "| A | B |\n| -- | --- |"
        #expect(
            MarkdownContentParser.parse(incomplete) == [
                .paragraph("| A | B | | -- | --- |")
            ]
        )

        let fenced = """
            ```markdown
            | A | B |
            | --- | --- |
            ```
            """
        #expect(
            MarkdownContentParser.parse(fenced) == [
                .codeBlock(language: "markdown", code: "| A | B |\n| --- | --- |")
            ]
        )
    }

    @Test
    func testParsesATXAndSetextHeadings() {
        let blocks = MarkdownContentParser.parse(
            """
            # Main **heading**

            ## Second heading ##

            Setext heading
            ===

            Underlined heading
            ---
            """
        )

        #expect(
            blocks == [
                .heading(level: 1, text: "Main **heading**"),
                .heading(level: 2, text: "Second heading"),
                .heading(level: 1, text: "Setext heading"),
                .heading(level: 2, text: "Underlined heading"),
            ]
        )
    }

    @Test
    func testLevelSixHeadingKeepsCaptionDefaultWeight() {
        #expect(markdownHeadingWeight(6) == nil)
    }

    @Test
    func testParsesBlockquotesListsAndTasks() {
        let blocks = MarkdownContentParser.parse(
            """
            > ### Quoted heading
            > Body with **bold** text.

            - Alpha
              - Nested *item*
            - [x] Complete
            1. First
            2. Second
            """
        )

        #expect(
            blocks == [
                .blockquote([
                    .heading(level: 3, text: "Quoted heading"),
                    .paragraph("Body with **bold** text."),
                ]),
                .list([
                    MarkdownListItem(
                        depth: 0,
                        marker: .bullet,
                        taskIsComplete: nil,
                        text: "Alpha"
                    ),
                    MarkdownListItem(
                        depth: 1,
                        marker: .bullet,
                        taskIsComplete: nil,
                        text: "Nested *item*"
                    ),
                    MarkdownListItem(
                        depth: 0,
                        marker: .bullet,
                        taskIsComplete: true,
                        text: "Complete"
                    ),
                    MarkdownListItem(
                        depth: 0,
                        marker: .number(1),
                        taskIsComplete: nil,
                        text: "First"
                    ),
                    MarkdownListItem(
                        depth: 0,
                        marker: .number(2),
                        taskIsComplete: nil,
                        text: "Second"
                    ),
                ]),
            ]
        )
    }

    @Test
    func testParsesFencedAndIndentedCodeAndThematicBreaks() {
        let blocks = MarkdownContentParser.parse(
            """
            ```swift
            let value = 1
            ```

            ---

                indented()
            """
        )

        #expect(
            blocks == [
                .codeBlock(language: "swift", code: "let value = 1"),
                .thematicBreak,
                .codeBlock(language: nil, code: "indented()"),
            ]
        )
    }

    @Test
    func testParagraphsFoldSoftWrapsAndPreserveExplicitLineBreaks() {
        let blocks = MarkdownContentParser.parse(
            "soft\nwrap\n\nfirst  \nsecond\nthird\\\nfourth"
        )

        #expect(
            blocks == [
                .paragraph("soft wrap"),
                .paragraph("first\nsecond third\nfourth"),
            ]
        )
    }
}
