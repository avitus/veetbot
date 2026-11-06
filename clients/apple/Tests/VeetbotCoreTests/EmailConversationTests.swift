import Foundation
import SwiftUI
import Testing
@testable import VeetbotCore

/// A long reply-all thread reads as a conversation: each message's own words,
/// who wrote them, and who could see them (ADR-0301).
struct EmailConversationTests {
    private static let owner = "owner@work.example"
    private static let dana = "Dana Moss <dana@lumen.example>"
    private static let sean = "Sean Doyle <sean@ironwood.example>"
    private static let yoav = "Yoav Shalev <yoav@meridian.example>"
    private static let nate = "Nate Price <nate@bank.example>"

    /// A server message payload.
    private static func message(
        _ id: String, from sender: String, to: [String], cc: [String] = [], reader: String,
        sent: Bool = false, day: Int = 2, attachments: [[String: Any]] = []
    ) -> [String: Any] {
        [
            "id": id, "sender": sender, "to": to, "cc": cc, "subject": "Term sheet",
            "body": reader, "reader_body": reader, "direction": sent ? "sent" : "received",
            "sent_at": String(format: "2026-11-%02dT10:00:00Z", day), "complete": true,
            "attachments": attachments,
        ]
    }

    /// Decodes through the production decoder.
    private static func decode(_ payloads: [[String: Any]]) throws -> [EmailMessageView] {
        try JSONDecoder.server.decode(
            [EmailMessageView].self, from: JSONSerialization.data(withJSONObject: payloads))
    }

    /// Five messages: a board email, a private side note, a second private
    /// note, everyone again with a banker added, and the owner's reply.
    private static func boardThread() throws -> EmailConversation {
        let everyone = [owner, sean, yoav]
        let outlookChain = "From: Dana Moss \\<dana@lumen.example>\\\nSent: Monday, November 2, 2026 5:17 PM\\\n"
            + "To: Sean Doyle \\<sean@ironwood.example>\n\nThe whole earlier email."
        return EmailConversation(
            messages: try decode([
                message("m1", from: dana, to: everyone, reader: "Hi all,\n\nThe offer is in. Can we meet Monday?", day: 1),
                message("m2", from: sean, to: [owner],
                        reader: "Andy,\n\nI would hold out for 30%.\n\nBest,\\\nSean\n\n" + outlookChain),
                message("m3", from: sean, to: [owner],
                        reader: "One more thought on the cap.\n\nBest,\\\nSean\n\n" + outlookChain),
                message("m4", from: dana, to: [owner, sean, yoav, nate],
                        reader: "Adding Nate from the bank.\n\nOn Mon, Nov 2, 2026 at 5:17 PM, Sean Doyle \\<sean@ironwood.example> wrote: Andy,\n\nOld text.",
                        day: 3,
                        attachments: [["filename": "Term sheet.pdf", "mime_type": "application/pdf", "size": 2048]]),
                message("m5", from: "Andy Vitus <\(owner)>", to: [dana], cc: [sean, yoav, nate],
                        reader: "Monday works.", sent: true, day: 3),
            ]),
            ownerAddresses: [owner])
    }

    /// The owner's mail is marked for the right-hand side.
    @Test func testOwnersMessagesSitOnTheirOwnSide() throws {
        let conversation = try Self.boardThread()
        #expect(conversation.entries.map(\.isOwner) == [false, false, false, false, true])
        #expect(conversation.entries[0].author.name == "Dana Moss")
        #expect(conversation.entries[0].author.short == "Dana")
        #expect(conversation.entries[4].author.address == Self.owner)
    }

    /// The bubble keeps what the writer added; the quoted chain, an Outlook
    /// header block and a sign-off repeated across their messages fold away.
    @Test func testEachMessageShowsOnlyItsOwnWords() throws {
        let entries = try Self.boardThread().entries
        #expect(entries[1].own == MarkdownContentParser.parse("Andy,\n\nI would hold out for 30%."))
        #expect(entries[1].hasMore)
        #expect(entries[2].own == MarkdownContentParser.parse("One more thought on the cap."))
        #expect(entries[3].own == MarkdownContentParser.parse("Adding Nate from the bank."))
        #expect(entries[0].hasMore == false)
        #expect(entries[4].own == MarkdownContentParser.parse("Monday works."))
    }

    /// A narrower audience, a person added, and a return to everyone each
    /// get one marker where they happen.
    @Test func testAudienceChangesAreMarkedWhereTheyHappen() throws {
        let entries = try Self.boardThread().entries
        #expect(entries[0].marker == nil)
        guard case .narrowed(let members, let hidden) = entries[1].marker else {
            Issue.record("Sean's private note must be marked"); return
        }
        #expect(members.map(\.address) == ["sean@ironwood.example", Self.owner])
        #expect(Set(hidden.map(\.address)) == ["dana@lumen.example", "yoav@meridian.example"])
        #expect(entries[1].isPrivate)
        #expect(entries[2].marker == nil, "The same side conversation continues without a second marker")
        #expect(entries[2].isPrivate)
        guard case .added(let by, let people) = entries[3].marker else {
            Issue.record("Dana adding Nate must be marked"); return
        }
        #expect(by.address == "dana@lumen.example")
        #expect(people.map(\.name) == ["Nate Price"])
        #expect(entries[3].isPrivate == false)
        #expect(entries[4].marker == nil)
    }

    /// Someone reached only through a side conversation never joins "everyone":
    /// forwarding the thread to an assistant must not make every later board
    /// email look private.
    @Test func testASideConversationsPeopleDoNotJoinEveryone() throws {
        let board = [Self.owner, Self.sean, Self.yoav]
        let entries = EmailConversation(
            messages: try Self.decode([
                Self.message("b1", from: Self.dana, to: board, reader: "The offer is in."),
                Self.message("b2", from: "Andy <\(Self.owner)>", to: ["Priya <priya@scale.example>"],
                             reader: "Please add Monday.\n\nAndy\n\n> Forwarded from Dana Moss \\<dana@lumen.example>\n\nThe offer is in.",
                             sent: true),
                Self.message("b3", from: Self.sean, to: [Self.dana], cc: [Self.owner, Self.yoav], reader: "Thanks Dana."),
            ]),
            ownerAddresses: [Self.owner]
        ).entries
        #expect(entries[1].isPrivate)
        #expect(entries[1].own == MarkdownContentParser.parse("Please add Monday.\n\nAndy"), "The forwarded copy folds")
        #expect(entries[1].hasMore)
        #expect(entries[2].isPrivate == false)
        #expect(entries[2].marker == .widened)
    }

    /// Writers are counted in order; the copied who never wrote are listed apart.
    @Test func testWritersAndThoseOnlyCopiedAreCounted() throws {
        let conversation = try Self.boardThread()
        #expect(conversation.writers.map(\.person.short) == ["Dana", "Sean", "You"])
        #expect(conversation.writers.map(\.count) == [2, 2, 1])
        #expect(conversation.silent.map(\.name) == ["Yoav Shalev", "Nate Price"])
        #expect(conversation.files.map(\.attachment.filename) == ["Term sheet.pdf"])
        #expect(conversation.files.first?.author.short == "Dana")
    }

    /// A side conversation names its members and who was not addressed.
    @Test func testSideConversationsNameWhoIsLeftOut() throws {
        let sides = try Self.boardThread().sideConversations
        #expect(sides.count == 1)
        #expect(sides[0].entryIDs == ["m2", "m3"])
        #expect(sides[0].startedBy.short == "Sean")
        #expect(Set(sides[0].notIncluded.map(\.short)) == ["Dana", "Yoav", "Nate"])
    }

    /// A brief line skips the greeting and keeps the writer's first point.
    @Test func testBriefLinesSkipGreetings() throws {
        let conversation = try Self.boardThread()
        #expect(conversation.entries[0].briefLine == "The offer is in. Can we meet Monday?")
        #expect(conversation.entries[1].briefLine == "I would hold out for 30%.")
        let latest = conversation.writers.map { conversation.entry(id: $0.latestID)?.briefLine }
        #expect(latest == ["Adding Nate from the bank.", "One more thought on the cap.", "Monday works."])
    }

    /// Three or more writers plus a side conversation, or many messages, is complex;
    /// a long exchange between two people is not.
    @Test func testComplexityNeedsSeveralWritersAndASideConversationOrLength() throws {
        #expect(try Self.boardThread().isComplex)
        let pair = try Self.decode((1...10).map { index in
            Self.message("p\(index)", from: index.isMultiple(of: 2) ? "Andy <\(Self.owner)>" : Self.dana,
                         to: [index.isMultiple(of: 2) ? Self.dana : Self.owner], reader: "Note \(index).",
                         sent: index.isMultiple(of: 2))
        })
        #expect(EmailConversation(messages: pair, ownerAddresses: [Self.owner]).isComplex == false)
    }

    /// The folded share reflects the quoted history removed.
    @Test func testRepeatedTextShareCountsWhatFolded() throws {
        let conversation = try Self.boardThread()
        #expect(conversation.hiddenShare > 0.4)
        #expect(conversation.hiddenShare < 1)
    }

    /// Without reader text the raw body is not Markdown: a sender's link label
    /// or emphasis shows as typed, while reader text still renders.
    @Test func testABodyWithoutReaderTextStaysPlain() throws {
        var raw = Self.message("r1", from: Self.dana, to: [Self.owner], reader: "")
        raw["reader_body"] = NSNull()
        raw["body"] = "See [your bank](https://bank.attacker.example) *now*"
        let entries = EmailConversation(
            messages: try Self.decode([raw, Self.message("r2", from: Self.sean, to: [Self.owner], reader: "**Noted.**")]),
            ownerAddresses: [Self.owner]
        ).entries
        #expect(entries[0].isPlainText)
        #expect(entries[0].briefLine == "See [your bank](https://bank.attacker.example) *now*")
        #expect(entries[1].isPlainText == false)
    }

    /// A list or table reads as its text in the brief and in folding, never as
    /// a description of the parsed structure.
    @Test func testListsAndTablesReadAsTheirText() throws {
        let entries = EmailConversation(
            messages: try Self.decode([
                Self.message("l1", from: Self.dana, to: [Self.owner], reader: "- Bring the deck\n- Book the room"),
            ]),
            ownerAddresses: [Self.owner]
        ).entries
        #expect(entries[0].briefLine == "Bring the deck Book the room")
        let table = MarkdownContentParser.parse("| Term | Ask |\n| --- | --- |\n| Cap | 30% |")
        #expect(table.map(EmailConversation.plainText) == ["Term, Ask\nCap, 30%"])
    }

    /// A side conversation is read from To and Cc, so its marker says who the
    /// message was addressed to, never that anyone cannot see it (Bcc and lists
    /// are invisible).
    @Test func testAudienceMarkersDescribeOnlyTheAddresses() throws {
        let entries = try Self.boardThread().entries
        let sean = try #require(entries[1].marker).text(author: entries[1].author)
        #expect(sean == "Sean wrote to you · not addressed to Dana and Yoav")
        let dana = try #require(entries[3].marker).text(author: entries[3].author)
        #expect(dana == "Dana added Nate")
        for word in ["privately", "only", "can't"] {
            #expect(!sean.lowercased().contains(word))
        }
        #expect(EmailConversation.audienceCaveat.contains("Bcc"))
    }

    /// Only mail the server saw the owner send sits on the owner's side: a
    /// received message forging the owner's From is someone else's, flagged.
    @Test func testAReceivedMessageFromTheOwnersAddressIsNotTheOwners() throws {
        let conversation = EmailConversation(
            messages: try Self.decode([
                Self.message("s1", from: "Andy Vitus <\(Self.owner)>", to: [Self.dana], reader: "Real note.", sent: true),
                Self.message("s2", from: "Andy Vitus <\(Self.owner)>", to: [Self.dana], reader: "Wire the funds."),
                Self.message("s4", from: "Another Name <\(Self.owner.uppercased())>", to: [Self.dana], reader: "Follow up."),
                Self.message("s3", from: Self.dana, to: [Self.owner], reader: "Done."),
            ]),
            ownerAddresses: [Self.owner]
        )
        let entries = conversation.entries
        #expect(entries[0].isOwner && entries[0].unverifiedOwner == false)
        #expect(entries[1].isOwner == false)
        #expect(entries[1].unverifiedOwner)
        #expect(entries[1].author.short != "You")
        #expect(entries[1].author.name == Self.owner)
        #expect(entries[1].marker == nil, "The forged message's audience is still the owner's")
        #expect(entries[2].unverifiedOwner && entries[2].isOwner == false)
        #expect(entries[1].author.address == entries[2].author.address, "One unverified writer, whatever the name or case")
        #expect(entries[1].author.address != entries[0].author.address)
        #expect(conversation.writers.map(\.count) == [1, 2, 1])
        #expect(conversation.writers.map(\.unverifiedOwner) == [false, true, false], "The brief warns on its card too")
        // Each unverified message keeps its own name line, so its warning is never grouped away.
        typealias View = EmailThreadConversationView<EmptyView>
        #expect(View.continuesGroup(entries[2], after: entries[1], newDay: false, marker: nil) == false)
        #expect(View.continuesGroup(entries[1], after: entries[0], newDay: false, marker: nil) == false)
        #expect(View.continuesGroup(entries[3], after: entries[3], newDay: false, marker: nil), "A writer's run still groups")
    }

    /// The owner's side needs the server to say the owner sent it: a missing
    /// or unrecognized direction on the owner's address is unverified too.
    @Test(arguments: [nil, "draft"] as [String?])
    func testOnlyExplicitlySentMailIsTheOwners(direction: String?) throws {
        var unknown = Self.message("u1", from: "Andy Vitus <\(Self.owner)>", to: [Self.dana], reader: "Unclear origin.")
        unknown["direction"] = direction.map { $0 as Any } ?? NSNull()
        let entries = EmailConversation(
            messages: try Self.decode([
                Self.message("u0", from: "Andy Vitus <\(Self.owner)>", to: [Self.dana], reader: "Sent.", sent: true),
                unknown,
            ]),
            ownerAddresses: [Self.owner]
        ).entries
        #expect(entries[0].isOwner)
        #expect(entries[1].isOwner == false)
        #expect(entries[1].unverifiedOwner)
    }

    /// Names and addresses parse from the usual envelope forms.
    @Test func testPeopleParseFromEnvelopeForms() {
        #expect(EmailPerson(envelope: "\"Shalev, Yoav\" <Yoav@Meridian.example>").address == "yoav@meridian.example")
        #expect(EmailPerson(envelope: "\"Shalev, Yoav\" <Yoav@Meridian.example>").name == "Shalev, Yoav")
        #expect(EmailPerson(envelope: "sam@example.test").name == "sam@example.test")
        #expect(EmailPerson(envelope: "sam@example.test").short == "sam")
        #expect(EmailPerson(envelope: "Riley <riley@example.test>").initials == "R")
        #expect(EmailPerson(envelope: "Dana Moss <dana@lumen.example>").initials == "DM")
    }
}

/// Show original shows whole messages, which only the conversation holds, so
/// it overrides the brief a complex thread opens on (ADR-0301).
struct EmailThreadModeTests {
    private typealias View = EmailThreadConversationView<EmptyView>

    @Test func testShowOriginalShowsTheConversationOverTheBrief() {
        #expect(View.mode(chosen: nil, startsOn: nil, isComplex: true, showsOriginal: nil) == .brief)
        #expect(View.mode(chosen: nil, startsOn: nil, isComplex: true, showsOriginal: false) == .brief)
        #expect(View.mode(chosen: nil, startsOn: nil, isComplex: true, showsOriginal: true) == .conversation)
        #expect(View.mode(chosen: .brief, startsOn: nil, isComplex: true, showsOriginal: true) == .conversation)
        #expect(View.mode(chosen: .brief, startsOn: nil, isComplex: true, showsOriginal: false) == .brief)
        #expect(View.mode(chosen: nil, startsOn: nil, isComplex: false, showsOriginal: nil) == .conversation)
    }
}

/// The cache keeps one parse per thread revision, but never past a change to
/// the messages themselves, such as bodies expired by retention.
@MainActor
struct EmailConversationCacheTests {
    /// A two-message thread at a fixed revision.
    private static func thread(body: String, complete: Bool, revision: Int = 4) throws -> EmailThreadView {
        let message: (String) -> [String: Any] = { id in
            [
                "id": id, "sender": "Dana <dana@example.test>", "to": ["owner@example.test"], "cc": [],
                "subject": "Plan", "body": body, "reader_body": body, "direction": "received",
                "sent_at": "2026-11-02T10:00:00Z", "complete": complete, "attachments": [],
            ]
        }
        let payload: [String: Any] = [
            "id": "00000000-0000-0000-0000-000000000901", "account_id": "work", "subject": "Plan",
            "senders": ["dana@example.test"], "updated_at": "2026-11-02T10:00:00Z", "revision": revision,
            "summary": "", "reason": "", "needs_reply": false, "priority": 0.5, "complete": complete,
            "messages": [message("c1"), message("c2")],
        ]
        return try JSONDecoder.server.decode(EmailThreadView.self, from: JSONSerialization.data(withJSONObject: payload))
    }

    /// Emptied bodies at the same revision rebuild the conversation.
    @Test func testChangedMessagesRebuildAtTheSameRevision() throws {
        let before = EmailConversationCache.conversation(
            for: try Self.thread(body: "The full text.", complete: true), ownerAddress: "owner@example.test")
        #expect(before.entries[0].own == MarkdownContentParser.parse("The full text."))
        let after = EmailConversationCache.conversation(
            for: try Self.thread(body: "", complete: false), ownerAddress: "owner@example.test")
        #expect(after.entries[0].message.complete == false)
        #expect(after.entries[0].own != before.entries[0].own)
    }

    /// A new revision replaces the thread's older parse instead of keeping both.
    @Test func testANewRevisionReplacesTheOlderParse() throws {
        let older = try Self.thread(body: "First.", complete: true, revision: 7)
        _ = EmailConversationCache.conversation(for: older, ownerAddress: "owner@example.test")
        _ = EmailConversationCache.conversation(
            for: try Self.thread(body: "Second.", complete: true, revision: 8), ownerAddress: "owner@example.test")
        #expect(EmailConversationCache.parses(of: older.id) == 1)
    }

    /// Resetting the Email connection drops every parsed conversation with the
    /// rest of the old connection's mail.
    @Test func testResettingTheConnectionDropsCachedConversations() throws {
        let thread = try Self.thread(body: "Kept only while connected.", complete: true)
        _ = EmailConversationCache.conversation(for: thread, ownerAddress: "owner@example.test")
        #expect(EmailConversationCache.parses(of: thread.id) == 1)
        EmailViewModel(makeAPIClient: { nil }).resetConnection()
        #expect(EmailConversationCache.parses(of: thread.id) == 0)
    }
}

/// Quoted history introduced the Outlook way folds like Gmail's (ADR-0301).
struct EmailReaderOutlookFoldTests {
    /// An Outlook header block folds what follows it.
    @Test func testOutlookHeaderBlockFoldsTheRestOfTheMessage() {
        let own = "Thanks for this.\n\nBest,\\\nSean"
        let chain = "From: Dana Moss \\<dana@lumen.example>\\\nSent: Monday, November 2, 2026 5:17 PM\\\n"
            + "To: Andy \\<andy@example.com>\n\nHi Andy,\n\nThe earlier email."
        let sections = EmailReaderSections(markdown: own + "\n\n" + chain)
        #expect(sections.current == MarkdownContentParser.parse(own))
        #expect(sections.earlier == MarkdownContentParser.parse(chain))
    }

    /// An unquoted attribution naming an address, or a time on a date, folds.
    @Test func testUnquotedAttributionWithAnAddressOrTimeFolds() {
        let own = "Adding Nate."
        let chain = "On Mon, Nov 2, 2026 at 5:17 PM, Sean \\<sean@example.com> wrote: Andy,\n\nOld text."
        let sections = EmailReaderSections(markdown: own + "\n\n" + chain)
        #expect(sections.current == MarkdownContentParser.parse(own))
        #expect(sections.earlier == MarkdownContentParser.parse(chain))
        let dated = "On Nov 5, 2026, at 10:30 AM, Priya Shah wrote: Andy,\n\nOld text."
        #expect(EmailReaderSections(markdown: "Agreed.\n\n" + dated).earlier == MarkdownContentParser.parse(dated))
        let original = "Intro.\n\n-----Original Message-----\n\nOld text."
        #expect(EmailReaderSections(markdown: original).current == MarkdownContentParser.parse("Intro."))
    }

    /// The rule still never hides the message itself, nor prose that merely
    /// mentions someone writing.
    @Test func testOutlookFoldNeverHidesTheMessage() {
        for markdown in [
            "From: Dana \\<dana@example.com>\\\nSent: Monday\n\nA forward with no note.",
            "Intro.\n\nOn Friday we met. Sam wrote:\n\nNo quote follows.",
            "Intro.\n\nFrom: the desk of Sam\n\nA letterhead, not a header block.",
            "Intro.\n\nFrom: Boston\nDate: Nov 5\n\nThe trip went well.",
            "Intro.\n\nOn the 10:30 call, Priya wrote: we should move the close.\n\nMore of my own.",
        ] {
            let sections = EmailReaderSections(markdown: markdown)
            #expect(sections.current == MarkdownContentParser.parse(markdown))
            #expect(sections.earlier.isEmpty)
        }
    }
}
