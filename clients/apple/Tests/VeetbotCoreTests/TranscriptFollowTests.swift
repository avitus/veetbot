import CoreGraphics
import Testing
@testable import VeetbotCore

/// The composer floats over the transcript's end, so growth the view does not
/// follow ends up beneath it.
@Suite struct TranscriptFollowTests {
    @Test
    func aGrowingAnswerThatFitsKeepsTheEndInView() {
        let step = TranscriptFollow.step(
            following: true, contentGrew: true, latestMessageHeight: 300,
            visibleHeight: 700, endClearance: 90
        )
        #expect(step == .end)
    }

    @Test
    func anAnswerTallerThanTheViewStopsWithItsBeginningAtTheTop() {
        let step = TranscriptFollow.step(
            following: true, contentGrew: true, latestMessageHeight: 650,
            visibleHeight: 700, endClearance: 90
        )
        #expect(step == .latestTop)
    }

    @Test
    func growthAfterTheOwnerScrolledAwayLeavesTheViewAlone() {
        let step = TranscriptFollow.step(
            following: false, contentGrew: true, latestMessageHeight: 300,
            visibleHeight: 700, endClearance: 90
        )
        #expect(step == .stay)
    }

    @Test
    func growingWorkRowsFollowTheEnd() {
        let step = TranscriptFollow.step(
            following: true, contentGrew: true, latestMessageHeight: nil,
            visibleHeight: 700, endClearance: 90
        )
        #expect(step == .end)
    }

    @Test
    func noGrowthNoScroll() {
        let step = TranscriptFollow.step(
            following: true, contentGrew: false, latestMessageHeight: 300,
            visibleHeight: 700, endClearance: 90
        )
        #expect(step == .stay)
    }

    @Test
    func theEndOfATranscriptEndingInATallAnswerIsItsBeginning() {
        #expect(TranscriptFollow.end(latestMessageHeight: 650, visibleHeight: 700, endClearance: 90) == .latestTop)
        #expect(TranscriptFollow.end(latestMessageHeight: 300, visibleHeight: 700, endClearance: 90) == .end)
        #expect(TranscriptFollow.end(latestMessageHeight: nil, visibleHeight: 700, endClearance: 90) == .end)
    }

    @Test
    func aViewNotYetLaidOutGoesToTheLastRow() {
        #expect(TranscriptFollow.end(latestMessageHeight: 650, visibleHeight: 0, endClearance: 90) == .end)
    }

    @Test
    func theEndIsInViewWithinAFewPoints() {
        #expect(TranscriptFollow.isAtEnd(contentHeight: 1500, offset: 800, containerHeight: 700, bottomInset: 0))
        #expect(TranscriptFollow.isAtEnd(contentHeight: 1500, offset: 797, containerHeight: 700, bottomInset: 0))
        #expect(!TranscriptFollow.isAtEnd(contentHeight: 1500, offset: 700, containerHeight: 700, bottomInset: 0))
        #expect(!TranscriptFollow.isAtEnd(contentHeight: 1500, offset: 800, containerHeight: 700, bottomInset: 34))
    }

    @Test
    func contentShorterThanTheViewIsAtItsEnd() {
        #expect(TranscriptFollow.isAtEnd(contentHeight: 400, offset: -100, containerHeight: 700, bottomInset: 0))
    }
}
