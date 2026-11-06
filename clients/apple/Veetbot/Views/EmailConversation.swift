import Foundation

/// One address on an email envelope, by its address and the name it gave.
struct EmailPerson: Hashable, Sendable {
    /// Lowercased, so one person's mail matches however a client capitalized it.
    let address: String
    let name: String
    let isOwner: Bool

    /// A person from an already-normalized address.
    init(address: String, name: String, isOwner: Bool = false) {
        self.address = address
        self.name = name
        self.isOwner = isOwner
    }

    /// `Name <address>`, `"Last, First" <address>` or a bare address.
    init(envelope: String, isOwner: Bool = false) {
        let trimmed = envelope.trimmingCharacters(in: .whitespacesAndNewlines)
        if let open = trimmed.lastIndex(of: "<"), let close = trimmed.lastIndex(of: ">"), open < close {
            let address = trimmed[trimmed.index(after: open)..<close]
                .trimmingCharacters(in: .whitespaces).lowercased()
            let name = trimmed[..<open]
                .trimmingCharacters(in: CharacterSet.whitespaces.union(CharacterSet(charactersIn: "\"'")))
            self.init(address: address, name: name.isEmpty ? address : name, isOwner: isOwner)
        } else {
            let address = trimmed.lowercased()
            self.init(address: address, name: address, isOwner: isOwner)
        }
    }

    private var namedOnlyByAddress: Bool { name == address }

    /// What a chip or a marker calls them.
    var short: String {
        if isOwner { return "You" }
        if namedOnlyByAddress { return String(address.split(separator: "@").first ?? Substring(address)) }
        if let comma = name.firstIndex(of: ",") {
            let given = name[name.index(after: comma)...].split(separator: " ").first
            if let given { return String(given) }
        }
        return String(name.split(separator: " ").first ?? Substring(name))
    }

    /// One or two letters for the avatar.
    var initials: String {
        let words = (namedOnlyByAddress ? short : name)
            .split(whereSeparator: { $0 == " " || $0 == "," || $0 == "." })
            .filter { $0.first?.isLetter == true }
        guard let first = words.first?.first else { return "?" }
        guard words.count > 1, let last = words.last?.first else { return String(first).uppercased() }
        return (String(first) + String(last)).uppercased()
    }
}

/// A thread read as a conversation: each message's own words, who wrote them
/// and who could see them (ADR-0301). Presentation only, from the envelope and
/// the reading text the server already sends.
struct EmailConversation {
    /// Where the audience changed, shown above the message that changed it.
    enum Marker: Equatable {
        /// The message left out people already on the thread.
        case narrowed(members: [EmailPerson], hidden: [EmailPerson])
        case added(by: EmailPerson, people: [EmailPerson])
        /// Everyone on the thread can see it again.
        case widened

        /// The marker's words, said from the owner's side.
        func text(author: EmailPerson) -> String {
            switch self {
            case .narrowed(let members, let hidden):
                let recipients = members.filter { $0.address != author.address }
                let writer = author.isOwner ? "You" : author.short
                return "\(writer) wrote to \(emailPeopleList(recipients, limit: 4))"
                    + " · not addressed to \(emailPeopleList(hidden))"
            case .added(let by, let people):
                return "\(by.short) added \(emailPeopleList(people, limit: 4))"
            case .widened:
                return "Back to everyone"
            }
        }
    }

    /// One message as the conversation shows it.
    struct Entry: Identifiable {
        let message: EmailMessageView
        let author: EmailPerson
        let isOwner: Bool
        /// The writer's own words, without history, repeated sign-offs or disclaimers.
        let own: [MarkdownContentBlock]
        /// Something folded away that Show full email brings back.
        let hasMore: Bool
        let marker: Marker?
        /// Part of a side conversation that leaves someone on the thread out.
        let isPrivate: Bool
        let audience: Set<String>
        /// Who it went to: the writer first, then To and Cc, then the owner.
        let members: [EmailPerson]
        /// The first paragraph that is not a greeting, as written.
        let briefLine: String
        /// The message had no reader text, so its raw body shows as typed, never as Markdown.
        let isPlainText: Bool
        /// Received mail whose From is the owner's address: the server never saw
        /// the owner send it, so it is shown as someone else's, with a warning.
        let unverifiedOwner: Bool
        var id: String { message.id }
    }

    /// Someone who wrote in the thread, with how often and their latest message.
    struct Writer: Identifiable {
        let person: EmailPerson
        let count: Int
        let latestID: String
        var id: String { person.address }
    }

    /// Consecutive or recurring messages to the same narrower audience.
    struct SideConversation: Identifiable {
        let members: [EmailPerson]
        let notIncluded: [EmailPerson]
        let entryIDs: [String]
        let startedBy: EmailPerson
        let startedAt: Date
        let audience: Set<String>
        var id: String { entryIDs.first ?? "" }
    }

    /// An attachment, with who sent it and which message carried it.
    struct File: Identifiable {
        let attachment: EmailAttachmentView
        let author: EmailPerson
        let entryID: String
        let index: Int
        var id: String { "\(entryID)#\(index)" }
    }

    /// Said wherever an audience is shown: it comes from the headers alone.
    static let audienceCaveat = "Read from the To and Cc lines. Bcc recipients and mailing-list members aren't shown."

    let entries: [Entry]
    let writers: [Writer]
    /// On the thread but never wrote, in the order they appeared.
    let silent: [EmailPerson]
    let sideConversations: [SideConversation]
    let files: [File]
    /// The share of the thread's text that folded away.
    let hiddenShare: Double
    /// Three or more writers, and a side conversation or eight messages: open on the brief.
    let isComplex: Bool

    /// Reads the thread in order; the owner is the account address plus every sender of sent mail.
    init(messages: [EmailMessageView], ownerAddresses: Set<String>) {
        var owners = Set(ownerAddresses.map { $0.lowercased() })
        for message in messages where message.direction == "sent" {
            owners.insert(EmailPerson(envelope: message.sender).address)
        }
        let ownerKey = ownerAddresses.map { $0.lowercased() }.sorted().first
            ?? messages.first(where: { $0.direction == "sent" }).map { EmailPerson(envelope: $0.sender).address }

        // Everyone by address, with the most descriptive name any envelope gave.
        var directory: [String: EmailPerson] = [:]
        var order: [String] = []
        /// Every owner address counts as one person.
        func key(_ address: String) -> String { owners.contains(address) ? (ownerKey ?? address) : address }
        /// Records a person, keeping the most descriptive name seen.
        func note(_ envelope: String) {
            let parsed = EmailPerson(envelope: envelope)
            guard !parsed.address.isEmpty else { return }
            let address = key(parsed.address)
            let isOwner = owners.contains(parsed.address)
            if let known = directory[address] {
                if known.name == known.address, parsed.name != parsed.address {
                    directory[address] = EmailPerson(address: address, name: parsed.name, isOwner: isOwner)
                }
            } else {
                directory[address] = EmailPerson(address: address, name: parsed.name, isOwner: isOwner)
                order.append(address)
            }
        }
        for message in messages {
            note(message.sender)
            if let ownerKey, directory[ownerKey] == nil { note(ownerKey) }
            (message.to + message.cc).forEach(note)
        }
        /// The directory's person for an envelope.
        func person(_ envelope: String) -> EmailPerson {
            let address = key(EmailPerson(envelope: envelope).address)
            return directory[address] ?? EmailPerson(envelope: envelope)
        }
        /// People in the order they first appeared on the thread.
        func ordered(_ addresses: Set<String>) -> [EmailPerson] {
            order.filter(addresses.contains).compactMap { directory[$0] }
        }

        // Each message's words and audience, and where the audience changed.
        /// A message before repeated sign-offs are trimmed across the writer's mail.
        struct Draft {
            var message: EmailMessageView
            var author: EmailPerson
            var current: [MarkdownContentBlock]
            var folded: Bool
            var plain: Bool
            var unverifiedOwner: Bool
            var marker: Marker?
            var isPrivate: Bool
            var audience: Set<String>
            var members: [EmailPerson]
        }
        var drafts: [Draft] = []
        // "Everyone" grows only from messages that leave nobody out, so a
        // person reached through a side conversation never joins it.
        var seen: Set<String> = []
        var previous: Set<String>?
        var previousPrivate = false
        for message in messages {
            // The audience counts the sender as written; the side and the name
            // trust the owner only for mail the server saw the owner send.
            let sender = person(message.sender)
            let unverifiedOwner = sender.isOwner && message.direction == "received"
            let author: EmailPerson
            if unverifiedOwner {
                let claimed = EmailPerson(envelope: message.sender).address
                author = EmailPerson(address: "unverified:" + claimed, name: claimed)
                directory[author.address] = author
            } else {
                author = sender
            }
            var memberOrder: [String] = []
            for address in [sender.address] + (message.to + message.cc).map({ person($0).address }) + [ownerKey].compactMap({ $0 })
            where !address.isEmpty && !memberOrder.contains(address) {
                memberOrder.append(address)
            }
            let audience = Set(memberOrder)
            var marker: Marker?
            var isPrivate = false
            if let previous {
                let hidden = seen.subtracting(audience)
                let added = audience.subtracting(seen)
                if !hidden.isEmpty {
                    isPrivate = true
                    if audience != previous {
                        marker = .narrowed(members: memberOrder.compactMap { directory[$0] }, hidden: ordered(hidden))
                    }
                } else if !added.isEmpty {
                    marker = .added(by: author, people: memberOrder.filter(added.contains).compactMap { directory[$0] })
                } else if previousPrivate {
                    marker = .widened
                }
            }
            previousPrivate = isPrivate
            previous = audience
            if !isPrivate { seen.formUnion(audience) }

            let current: [MarkdownContentBlock]
            let folded: Bool
            let plain = (message.readerBody ?? "").isEmpty
            if let reader = message.readerBody, !reader.isEmpty {
                let sections = EmailReaderSections(markdown: reader)
                // A forwarded copy below a note repeats mail the thread already holds.
                let forward = sections.current.indices.dropFirst().first { Self.isForwardedHeader(sections.current[$0]) }
                current = forward.map { Array(sections.current[..<$0]) } ?? sections.current
                folded = !sections.earlier.isEmpty || forward != nil
            } else {
                current = [.paragraph(message.body)]
                folded = false
            }
            drafts.append(Draft(
                message: message, author: author, current: current, folded: folded, plain: plain, unverifiedOwner: unverifiedOwner, marker: marker,
                isPrivate: isPrivate, audience: audience, members: memberOrder.compactMap { directory[$0] }))
        }

        // Sign-offs and signatures repeat at the end of one writer's messages.
        var byAuthor: [String: [Int]] = [:]
        for (index, draft) in drafts.enumerated() { byAuthor[draft.author.address, default: []].append(index) }
        var built: [Entry] = []
        var shownCharacters = 0
        for (index, draft) in drafts.enumerated() {
            let others = Set((byAuthor[draft.author.address] ?? []).filter { $0 != index }
                .flatMap { drafts[$0].current.map(Self.signature) })
            var own = draft.current
            var trimmed = false
            while own.count > 1, let last = own.last,
                others.contains(Self.signature(last)) || Self.isDisclaimer(last)
            {
                own.removeLast()
                trimmed = true
            }
            shownCharacters += own.map(Self.plainText).joined().count
            built.append(Entry(
                message: draft.message, author: draft.author, isOwner: draft.author.isOwner, own: own,
                hasMore: draft.folded || trimmed, marker: draft.marker, isPrivate: draft.isPrivate,
                audience: draft.audience, members: draft.members, briefLine: Self.briefLine(own),
                isPlainText: draft.plain, unverifiedOwner: draft.unverifiedOwner))
        }
        entries = built

        var counts: [String: (Int, String)] = [:]
        var writerOrder: [String] = []
        for entry in built {
            if counts[entry.author.address] == nil { writerOrder.append(entry.author.address) }
            counts[entry.author.address] = ((counts[entry.author.address]?.0 ?? 0) + 1, entry.id)
        }
        writers = writerOrder.compactMap { address in
            guard let person = directory[address], let tally = counts[address] else { return nil }
            return Writer(person: person, count: tally.0, latestID: tally.1)
        }
        silent = order.filter { counts[$0] == nil && $0 != ownerKey }.compactMap { directory[$0] }

        var sides: [SideConversation] = []
        for draft in drafts where draft.isPrivate {
            if let at = sides.firstIndex(where: { $0.audience == draft.audience }) {
                let side = sides[at]
                sides[at] = SideConversation(
                    members: side.members, notIncluded: side.notIncluded, entryIDs: side.entryIDs + [draft.message.id],
                    startedBy: side.startedBy, startedAt: side.startedAt, audience: side.audience)
            } else {
                sides.append(SideConversation(
                    members: draft.members, notIncluded: ordered(seen.subtracting(draft.audience)),
                    entryIDs: [draft.message.id], startedBy: draft.author, startedAt: draft.message.sentAt,
                    audience: draft.audience))
            }
        }
        sideConversations = sides

        files = built.flatMap { entry in
            (entry.message.attachments ?? []).enumerated().map {
                File(attachment: $0.element, author: entry.author, entryID: entry.id, index: $0.offset)
            }
        }
        let total = messages.map(\.body.count).reduce(0, +)
        hiddenShare = total > 0 ? max(0, 1 - Double(shownCharacters) / Double(total)) : 0
        isComplex = writers.count >= 3 && (!sides.isEmpty || built.count >= 8)
    }

    /// The entry for a message id.
    func entry(id: String) -> Entry? { entries.first { $0.id == id } }

    /// The side conversation with this id.
    func sideConversation(id: String) -> SideConversation? { sideConversations.first { $0.id == id } }

    // MARK: - Text

    /// Blocks compare by their words, ignoring case and spacing.
    private static func signature(_ block: MarkdownContentBlock) -> String {
        plainText(block).lowercased().split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    /// The reader's collapsed envelope (`> Forwarded from …`) or a mailer's own dashed line.
    private static func isForwardedHeader(_ block: MarkdownContentBlock) -> Bool {
        if case .blockquote(let inner) = block, case .paragraph(let text)? = inner.first {
            return text.hasPrefix("Forwarded from ")
        }
        guard case .paragraph(let text) = block, let first = text.split(separator: "\n").first else { return false }
        return first.trimmingCharacters(in: .whitespaces).lowercased()
            .range(of: #"^-{2,}\s*forwarded message\s*-{2,}$"#, options: .regularExpression) != nil
    }

    private static let disclaimerOpenings = [
        "disclaimer", "confidentiality notice", "this email and any", "this e-mail and any",
        "the information contained in this", "this message and any attachments",
    ]

    /// A trailing legal or confidentiality notice.
    private static func isDisclaimer(_ block: MarkdownContentBlock) -> Bool {
        guard case .paragraph(let text) = block else { return false }
        let opening = text.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "*_ "))
        return disclaimerOpenings.contains { opening.hasPrefix($0) }
    }

    /// A short opening line ending in a comma, such as "Hi all," or "Andy,".
    private static func isGreeting(_ text: String) -> Bool {
        let line = text.trimmingCharacters(in: .whitespacesAndNewlines)
        return !line.contains("\n") && line.hasSuffix(",") && line.split(separator: " ").count <= 4
    }

    /// The first paragraph that is not a greeting, on one line.
    static func briefLine(_ blocks: [MarkdownContentBlock]) -> String {
        let paragraphs = blocks.compactMap { block -> String? in
            if case .paragraph(let text) = block { return text } else { return nil }
        }
        let point = paragraphs.first { !isGreeting($0) } ?? paragraphs.first ?? blocks.first.map(plainText) ?? ""
        return point.replacingOccurrences(of: "\n", with: " ")
    }

    /// A block's readable text, for the brief, sign-off matching and the folded share.
    static func plainText(_ block: MarkdownContentBlock) -> String {
        switch block {
        case .paragraph(let text), .heading(_, let text): return text
        case .blockquote(let blocks): return blocks.map(plainText).joined(separator: "\n")
        case .codeBlock(_, let code): return code
        case .list(let items): return items.map(\.text).joined(separator: "\n")
        case .table(let table):
            return ([table.headers] + table.rows).map { $0.joined(separator: ", ") }.joined(separator: "\n")
        case .thematicBreak: return ""
        }
    }
}
