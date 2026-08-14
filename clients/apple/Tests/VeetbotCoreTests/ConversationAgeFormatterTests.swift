import Foundation
import Testing
@testable import VeetbotCore

@Suite struct ConversationAgeFormatterTests {
    private let now = Date(timeIntervalSince1970: 10_000)
    private let locale = Locale(identifier: "en_US_POSIX")

    @Test
    func testSecondsAreShownDuringFirstMinute() {
        #expect(formattedAge(seconds: 59) == "59s ago")
    }

    @Test(arguments: [
        (seconds: 60, expected: "1m ago"),
        (seconds: 61, expected: "1m ago"),
        (seconds: 119, expected: "1m ago"),
        (seconds: 120, expected: "2m ago"),
        (seconds: 3_600, expected: "1h ago"),
    ])
    func testSecondsAreOmittedAfterFirstMinute(
        seconds: Int,
        expected: String
    ) {
        #expect(formattedAge(seconds: seconds) == expected)
    }

    private func formattedAge(seconds: Int) -> String {
        ConversationAgeFormatter.string(
            since: now.addingTimeInterval(-Double(seconds)),
            relativeTo: now,
            locale: locale
        )
    }
}
