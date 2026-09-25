import Foundation
import Testing

@testable import VeetbotCore

/// The profile's wording: one directed assertion reads correctly from the
/// profile person's side (people-and-relationships.md §6), and supporting
/// lines never claim more than the record holds.
@Suite struct PeopleProfilePresentationTests {
    private let grace = UUID(uuidString: "00000000-0000-0000-0000-00000000A001")!
    private let maya = UUID(uuidString: "00000000-0000-0000-0000-00000000A002")!
    private let acme = UUID(uuidString: "00000000-0000-0000-0000-00000000A003")!
    private let stranger = UUID(uuidString: "00000000-0000-0000-0000-00000000A004")!

    @Test func relationshipsReadFromTheProfilePersonsSide() throws {
        let cases: [(subject: String, predicate: String, object: String, reads: String)] = [
            (person(grace), "colleague", owner, "Your colleague"),
            (owner, "friend", person(grace), "Your friend"),
            (person(grace), "parent", owner, "Your parent"),
            (owner, "parent", person(grace), "Your child"),
            (person(grace), "child", owner, "Your child"),
            (person(grace), "friend", person(maya), "Friend of Maya"),
            (person(maya), "parent", person(grace), "Child of Maya"),
            (person(grace), "reports_to", owner, "Grace reports to you"),
            (owner, "reports_to", person(grace), "You report to Grace"),
            (person(grace), "introduced_by", owner, "You introduced Grace"),
            (owner, "introduced_by", person(grace), "Grace introduced you"),
            (person(stranger), "introduced_by", person(grace), "Grace introduced another person"),
            (person(grace), "employment", organization(acme), "Grace works at Acme"),
            (owner, "employment", person(grace), "You work for Grace"),
            (person(grace), "founder", organization(acme), "Grace founded Acme"),
            (person(grace), "board_member", organization(stranger), "Grace is on the board of an organization"),
            (person(grace), "investor", organization(acme), "Grace invests in Acme"),
            (person(grace), "other", owner, "Related to you"),
        ]
        for item in cases {
            let profile = try profile(relationships: [relationship(item.subject, item.predicate, item.object)])
            #expect(profile.relationships[0].phrase(in: profile) == item.reads, "\(item.predicate)")
        }
    }

    @Test func aRelatedPersonCanBeOpenedButTheOwnerCannot() throws {
        let profile = try profile(relationships: [
            relationship(person(grace), "friend", person(maya)),
            relationship(person(grace), "colleague", owner),
        ])
        #expect(profile.relationships[0].counterpart(in: profile)?.id == maya)
        #expect(profile.relationships[1].counterpart(in: profile) == nil)
    }

    @Test func theHeaderSummarizesCurrentRelationshipsWithTheOwner() throws {
        let profile = try profile(relationships: [
            relationship(person(grace), "colleague", owner),
            relationship(owner, "friend", person(grace)),
            relationship(person(grace), "reports_to", owner),
            relationship(person(grace), "sibling", owner, validTo: "2025-01-01T00:00:00Z"),
            relationship(person(grace), "friend", person(maya)),
        ])
        #expect(profile.relationshipSummary == "Your colleague and friend; Grace reports to you")
        #expect(try self.profile(relationships: [relationship(person(grace), "friend", person(maya))]).relationshipSummary == nil)
    }

    @Test func thePrimaryContactIsACurrentAddressNumberOrHandle() throws {
        let profile = try profile(aliases: [
            alias("Gracie", kind: "name"),
            alias("old@example.com", kind: "email", validTo: "2025-01-01T00:00:00Z"),
            alias("+1 415 555 0100", kind: "phone"),
            alias("grace@example.com", kind: "email"),
        ])
        #expect(profile.primaryContact?.value == "grace@example.com")
        #expect(try self.profile(aliases: [alias("Gracie", kind: "name")]).primaryContact == nil)
    }

    @Test func initialsUseTheFirstAndLastWordsThatStartWithALetter() {
        #expect(PeopleMonogram.initials(of: "Maya Chen") == "MC")
        #expect(PeopleMonogram.initials(of: "maya") == "M")
        #expect(PeopleMonogram.initials(of: "Contact 05") == "C")
        #expect(PeopleMonogram.initials(of: "Dr. Maya van Chen") == "DC")
        #expect(PeopleMonogram.initials(of: "José Ñúñez") == "JÑ")
        #expect(PeopleMonogram.initials(of: "李小龍") == "李")
        #expect(PeopleMonogram.initials(of: "🙂 Sam") == "S")
        #expect(PeopleMonogram.initials(of: "  ") == "")
    }

    @Test func openThreadsNameWhoOwesWhom() throws {
        let profile = try profile()
        #expect(try commitment(debtor: owner, beneficiary: person(grace)).parties(in: profile) == "From you to Grace")
        #expect(try commitment(debtor: person(grace), beneficiary: owner).parties(in: profile) == "From Grace to you")
    }

    @Test func interactionsSayHowTheyHappened() throws {
        #expect(try interaction(channel: "email", direction: "incoming", attribution: "observed").channelDescription == "Received by email")
        #expect(try interaction(channel: "sms", direction: "outgoing", attribution: "observed").channelDescription == "Sent by text message")
        #expect(try interaction(channel: "chat", direction: "reported", attribution: "owner_reported").channelDescription == "You told Veetbot in Chat")
        #expect(try interaction(channel: "email", direction: "reported", attribution: "correspondent_reported").channelDescription == "They reported it by email")
    }

    @Test func aliasesSayWhereTheyCameFrom() throws {
        #expect(try aliasView("+1 415 555 0100", kind: "phone", verification: "owner_confirmed", context: "mobile").provenance == "Confirmed by you, mobile")
        #expect(try aliasView("grace@example.com", kind: "email", verification: "channel_observed", context: "").provenance == "Seen in your messages")
        #expect(try aliasView("Gracie", kind: "name", verification: "contextual", context: "owner").provenance == "From context")
    }

    @Test func listsJoinLikeASentence() {
        #expect(peopleList(["facts"], conjunction: "or") == "facts")
        #expect(peopleList(["relationships", "facts"], conjunction: "or") == "relationships or facts")
        #expect(peopleList(["relationships", "open threads", "facts"], conjunction: "or") == "relationships, open threads, or facts")
    }

    // MARK: Builders

    private let owner = #"{"kind":"owner"}"#
    private func person(_ id: UUID) -> String { #"{"kind":"person","id":"\#(id)"}"# }
    private func organization(_ id: UUID) -> String { #"{"kind":"organization","id":"\#(id)"}"# }

    private func relationship(_ subject: String, _ predicate: String, _ object: String, validTo: String? = nil) -> String {
        let end = validTo.map { #""\#($0)""# } ?? "null"
        return """
        {"id":"\(UUID())","revision":1,"subject":\(subject),"object":\(object),"predicate":"\(predicate)","qualifier":"","valid_from":null,"valid_to":\(end),"precision":"unknown","support_ids":[]}
        """
    }

    private func alias(_ value: String, kind: String, verification: String = "channel_observed", context: String = "", validTo: String? = nil) -> String {
        let end = validTo.map { #""\#($0)""# } ?? "null"
        return """
        {"id":"\(UUID())","revision":1,"value":"\(value)","identifier_kind":"\(kind)","verification":"\(verification)","context":"\(context)","valid_to":\(end),"support_ids":[]}
        """
    }

    private func aliasView(_ value: String, kind: String, verification: String, context: String) throws -> PersonAliasView {
        try JSONDecoder.server.decode(PersonAliasView.self, from: Data(alias(value, kind: kind, verification: verification, context: context, validTo: nil).utf8))
    }

    private func commitment(debtor: String, beneficiary: String) throws -> PersonCommitmentView {
        try JSONDecoder.server.decode(PersonCommitmentView.self, from: Data("""
        {"id":"\(UUID())","revision":1,"debtor":\(debtor),"beneficiary":\(beneficiary),"description":"Send the draft","state":"open","support_ids":[]}
        """.utf8))
    }

    private func interaction(channel: String, direction: String, attribution: String) throws -> PersonInteractionView {
        try JSONDecoder.server.decode(PersonInteractionView.self, from: Data("""
        {"id":"\(UUID())","revision":1,"channel":"\(channel)","interaction_kind":"exchange","attribution":"\(attribution)","direction":"\(direction)","summary":"Talked","precision":"unknown","support_ids":[],"participants":[]}
        """.utf8))
    }

    private func profile(relationships: [String] = [], aliases: [String] = []) throws -> PersonProfileView {
        try JSONDecoder.server.decode(PersonProfileView.self, from: Data("""
        {"person":{"id":"\(grace)","revision":1,"display_name":"Grace","state":"active","pinned":false,"sensitivity":"sensitive","support_ids":[]},\
        "aliases":[\(aliases.joined(separator: ","))],"relationships":[\(relationships.joined(separator: ","))],\
        "history":[],"commitments":[],"facts":[],"fact_revisions":{},\
        "related_labels":{"\(maya.uuidString.lowercased())":"Maya","\(acme.uuidString.lowercased())":"Acme"},\
        "truncated":false,"coverage":"Owner history"}
        """.utf8))
    }
}
