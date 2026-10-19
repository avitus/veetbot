import Foundation
import Testing

@testable import VeetbotCore

/// The A2 Dock Memory, People and Schedules surfaces: lists on paper with a
/// kind line over each row, reading pages with a serif title and record
/// groups on surface cards, capsule buttons for the actions, and every
/// identifier and line the journeys drive kept in place.
@Suite struct MemoryPeopleSchedulesSurfaceStructureTests {
    @Test
    func testMemoryListsAndRecordsWearTheA2System() throws {
        let browser = try source(at: "Veetbot/Views/MemoryBrowserView.swift")
        let detail = try source(at: "Veetbot/Views/MemoryDetailView.swift")
        let synthesis = try source(at: "Veetbot/Views/MemorySynthesisView.swift")

        #expect(browser.contains("struct MemoryRow"))
        #expect(browser.contains(".a2List()"))
        #expect(browser.contains("AppTheme.amberText"))
        #expect(browser.contains(".pickerStyle(.segmented)"))
        #expect(browser.contains(".accessibilityIdentifier(\"memory.row.\\(item.id.uuidString)\")"))
        #expect(browser.contains(".accessibilityIdentifier(\"memory.filter.review\")"))
        #expect(!browser.contains("Color.secondary.opacity"))

        #expect(detail.contains(".appDisplayFont("))
        #expect(detail.contains("A2RecordGroup("))
        #expect(detail.contains("Text(memory.subject)"))
        #expect(detail.contains(".accessibilityIdentifier(\"memory.detail.review.not_here\")"))
        #expect(detail.contains("Button(\"Delete memory\", role: .destructive)"))
        #expect(!detail.contains("Section(\"Original memory\")"))

        #expect(synthesis.contains("struct MemorySynthesisRow"))
        #expect(synthesis.contains("AppTheme.tealText"))
        #expect(synthesis.contains(".accessibilityIdentifier(\"memory.synthesis.undo.confirm\")"))
        #expect(synthesis.contains("derived memory"))
        #expect(!synthesis.contains(".regularMaterial"))
    }

    @Test
    func testPeopleDrawsWithTheA2TokensAndKeepsItsJourneys() throws {
        let components = try source(at: "Veetbot/Views/PeopleProfileComponents.swift")
        let browser = try source(at: "Veetbot/Views/PeopleBrowserView.swift")
        let detail = try source(at: "Veetbot/Views/PeopleDetailView.swift")

        // Links and glyphs use the teal text token: the mark colour reads 4.2:1
        // on white and fails the Large text audit.
        #expect(components.contains("static let accent = AppTheme.tealText"))
        #expect(components.contains("AppTheme.tealWash"))
        #expect(components.contains("static let card = AppTheme.surface"))
        #expect(!components.contains("windowBackgroundColor"))
        #expect(!components.contains("systemGroupedBackground"))
        #expect(!components.contains("brandGradient"))
        #expect(!components.contains("AppTheme.teal\n"))

        #expect(browser.contains("AppTheme.selected"))
        #expect(browser.contains(".accessibilityIdentifier(\"people.row.\\(person.id.uuidString)\")"))
        #expect(browser.contains("Label(\"Import history\", systemImage: \"clock.arrow.circlepath\")"))
        #expect(browser.contains("Label(\"Add person\", systemImage: \"person.badge.plus\")"))
        #expect(browser.contains("A2SheetHeader("))

        #expect(detail.contains(".appDisplayFont("))
        #expect(detail.contains(".primaryCapsule()"))
        #expect(detail.contains("SecondaryCapsuleButtonStyle"))
        #expect(detail.contains("Button(\"Repair identity…\")"))
        #expect(detail.contains("Button(\"Forget person…\", role: .destructive)"))
        #expect(detail.contains(".accessibilityIdentifier(\"people.detail\")"))
        #expect(!detail.contains(".buttonStyle(.bordered)"))
    }

    @Test
    func testSchedulesReadAsA2Pages() throws {
        let browser = try source(at: "Veetbot/Views/ScheduleBrowserView.swift")
        let detail = try source(at: "Veetbot/Views/ScheduleDetailView.swift")
        let controls = try source(at: "Veetbot/Views/A2Controls.swift")

        #expect(browser.contains("struct ScheduleRow"))
        #expect(browser.contains(".a2List()"))
        #expect(browser.contains(".accessibilityIdentifier(\"schedule.section\")"))
        #expect(browser.contains("Text(\"Recent History\")"))

        #expect(detail.contains("AppTheme.paperDeep"))
        #expect(detail.contains("A2RecordGroup("))
        #expect(detail.contains("Text(record.revision.instruction)"))
        #expect(detail.contains(".accessibilityIdentifier(\"schedule.websiteAccess\")"))
        #expect(detail.contains(".accessibilityIdentifier(\"schedule.websiteAccess.error\")"))
        #expect(!detail.contains("Section(\"Instruction\")"))

        #expect(controls.contains("struct A2SectionHeading"))
        #expect(controls.contains("struct A2SheetHeader"))
        #expect(controls.contains("struct A2RecordGroup"))
        #expect(controls.contains("struct A2EmptyState"))
        #expect(controls.contains("func a2List("))
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}
