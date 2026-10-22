import Foundation
import Testing

@testable import VeetbotCore

/// A row names the person, not the address, and dates by recency.
@Suite struct EmailCorrespondentTests {
    @Test func testANamedSenderShowsItsNameWithoutTheAddress() {
        #expect(EmailCorrespondent.displayName("Max Abram <max@example.test>") == "Max Abram")
        #expect(EmailCorrespondent.displayName("  Andy Vitus via TestFlight <no_reply@example.test> ") == "Andy Vitus via TestFlight")
    }

    @Test func testAQuotedNameLosesItsQuotesAndEscapes() {
        #expect(EmailCorrespondent.displayName("\"coderabbitai[bot]\" <bot@example.test>") == "coderabbitai[bot]")
        #expect(EmailCorrespondent.displayName("\"\\\"Strike, OTA\\\"\" <news@example.test>") == "Strike, OTA")
    }

    @Test func testABareAddressStaysAnAddress() {
        #expect(EmailCorrespondent.displayName("alex@example.test") == "alex@example.test")
        #expect(EmailCorrespondent.displayName("<alex@example.test>") == "alex@example.test")
        #expect(EmailCorrespondent.displayName("   ") == "")
    }

    @Test func testTheLineNamesEachPersonOnceInOrder() {
        let senders = [
            "Max Abram <max@example.test>", "MAX@example.test", "Claire Yoon <claire@example.test>",
            "Max Abram <max.abram@other.test>", "claire@example.test",
        ]
        #expect(EmailCorrespondent.line(for: senders) == "Max Abram, Claire Yoon")
        #expect(EmailCorrespondent.line(for: []) == "")
    }

    @Test func testAThreadExposesItsCorrespondents() throws {
        let json = """
        {"id":"00000000-0000-0000-0000-000000000801","account_id":"personal","subject":"Board discussion","senders":["Max Abram <max@example.test>","sam@example.test"],"updated_at":"2026-09-11T00:00:00Z","revision":1,"summary":"Review the agenda.","reason":"Your board colleague.","needs_reply":true,"draft_id":null,"session_id":null,"priority":0.95,"complete":true,"messages":[],"draft":null}
        """
        let thread = try JSONDecoder.server.decode(EmailThreadView.self, from: Data(json.utf8))
        #expect(thread.correspondents == "Max Abram, sam@example.test")
    }
}

@Suite struct EmailRowDateTests {
    private var calendar: Calendar {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/Los_Angeles")!
        calendar.locale = Locale(identifier: "en_US")
        return calendar
    }
    private let locale = Locale(identifier: "en_US")

    private func at(_ year: Int, _ month: Int, _ day: Int, _ hour: Int = 12, _ minute: Int = 0) -> Date {
        calendar.date(from: DateComponents(year: year, month: month, day: day, hour: hour, minute: minute))!
    }

    private func label(_ date: Date, now: Date) -> String {
        EmailRowDate.label(for: date, relativeTo: now, calendar: calendar, locale: locale)
            .replacingOccurrences(of: "\u{202F}", with: " ")
    }

    /// 2026-10-21 is a Wednesday.
    @Test func testTodayReadsAsATime() {
        #expect(label(at(2026, 10, 21, 9, 41), now: at(2026, 10, 21, 15, 30)) == "9:41 AM")
    }

    @Test func testYesterdayAndTheWeekReadAsDays() {
        let now = at(2026, 10, 21, 15, 30)
        #expect(label(at(2026, 10, 20, 23, 59), now: now) == "Yesterday")
        #expect(label(at(2026, 10, 18), now: now) == "Sun")
        #expect(label(at(2026, 10, 15), now: now) == "Thu")
    }

    @Test func testOlderMailReadsAsADateWithTheYearOnceItDiffers() {
        let now = at(2026, 10, 21, 15, 30)
        #expect(label(at(2026, 10, 14), now: now) == "Oct 14")
        #expect(label(at(2026, 1, 2), now: now) == "Jan 2")
        #expect(label(at(2025, 12, 30), now: now) == "Dec 30, 2025")
        #expect(label(at(2026, 10, 22, 8), now: now) == "Oct 22", "A clock ahead of ours still reads as a date")
    }
}
