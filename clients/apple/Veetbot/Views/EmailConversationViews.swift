import SwiftUI

/// Builds a thread's conversation once per revision: the thread screen
/// re-renders on every draft keystroke and the parse is not free.
@MainActor
enum EmailConversationCache {
    private static var built: [String: (messages: [EmailMessageView], conversation: EmailConversation)] = [:]

    /// The cached parse, reused only while the messages are unchanged: retention
    /// can empty bodies without moving the thread's revision.
    static func conversation(for thread: EmailThreadView, ownerAddress: String?) -> EmailConversation {
        let messages = thread.messages ?? []
        let key = "\(thread.id)|\(thread.revision)|\(messages.count)|\(ownerAddress ?? "")"
        if let cached = built[key], cached.messages == messages { return cached.conversation }
        if built.count > 8 { built.removeAll() }
        let conversation = EmailConversation(messages: messages, ownerAddresses: Set([ownerAddress].compactMap { $0 }))
        built[key] = (messages, conversation)
        return conversation
    }

    /// Drops every parse, so no mail text outlives the connection that read it.
    static func removeAll() { built.removeAll() }

    /// Whether any parse of this thread is held.
    static func holds(threadID: UUID) -> Bool { built.keys.contains { $0.hasPrefix("\(threadID)|") } }
}

/// One muted ink per writer, in the order they first wrote, with a dark-mode partner.
enum EmailPersonTint {
    private static let tokens: [AppColorToken] = [
        AppColorToken(light: AppRGBA(hex: 0x0B6E5F), dark: AppRGBA(hex: 0x5EE3C9)),
        AppColorToken(light: AppRGBA(hex: 0x8A4B12), dark: AppRGBA(hex: 0xF2B266)),
        AppColorToken(light: AppRGBA(hex: 0x3B55A0), dark: AppRGBA(hex: 0x9DB4F2)),
        AppColorToken(light: AppRGBA(hex: 0x86386A), dark: AppRGBA(hex: 0xE59BCB)),
        AppColorToken(light: AppRGBA(hex: 0x3D6B2F), dark: AppRGBA(hex: 0xA6D98F)),
        AppColorToken(light: AppRGBA(hex: 0x9C3F2B), dark: AppRGBA(hex: 0xF09A84)),
    ]

    /// The writer's ink by the order they first wrote; others are faint.
    static func color(for person: EmailPerson, in conversation: EmailConversation) -> Color {
        guard let index = conversation.writers.firstIndex(where: { $0.person.address == person.address }) else {
            return AppTheme.inkFaint
        }
        return tokens[index % tokens.count].color
    }
}

/// A tinted circle with the person's initials.
struct EmailPersonAvatar: View {
    let person: EmailPerson
    let tint: Color
    var size: CGFloat = 28

    /// Initials over the writer's tint, at the given size.
    var body: some View {
        Text(person.initials)
            .veetType(size >= 32 ? .ui : .label, weight: .semibold)
            .lineLimit(1).minimumScaleFactor(0.5)
            .foregroundColor(.white)
            .frame(width: size, height: size)
            .background(Circle().fill(tint))
            .accessibilityHidden(true)
    }
}

/// "Dana", "you and Sean", "Dana, Sean and Yoav", "Dana, Sean and 4 others".
/// Lists always follow other words, so the owner is a lowercase "you".
func emailPeopleList(_ people: [EmailPerson], limit: Int = 3) -> String {
    let names = people.map { $0.isOwner ? "you" : $0.short }
    switch names.count {
    case 0: return ""
    case 1: return names[0]
    case ...limit: return names.dropLast().joined(separator: ", ") + " and " + names[names.count - 1]
    default:
        let shown = names.prefix(limit - 1).joined(separator: ", ")
        let rest = names.count - (limit - 1)
        return "\(shown) and \(rest) others"
    }
}

/// A thread of two or more messages as a conversation, or, when it is complex,
/// as a brief first (ADR-0301). The caller gives it `.id(thread.id)` so the
/// view, the focus and the filter start fresh for each thread.
struct EmailThreadConversationView<Attachments: View>: View {
    /// Which view of the thread is showing.
    enum Mode: Hashable { case conversation, brief }

    let thread: EmailThreadView
    let conversation: EmailConversation
    /// The Mac panel's Show original for the whole thread; nil lets each message choose.
    let showsOriginal: Bool?
    /// The brief repeats Veetbot's summary when nothing else on screen shows it.
    let showsSummary: Bool
    let proxy: ScrollViewProxy?
    let onName: (EmailMessageView) -> Void
    /// Opens the thread's attachment list, where the platform has one.
    let onFiles: (() -> Void)?
    /// The view to open on instead of the one the thread's complexity picks.
    var startsOn: Mode? = nil
    @ViewBuilder let attachmentLine: (EmailMessageView) -> Attachments

    @State private var mode: Mode?
    @State private var focus: String?
    @State private var sideID: String?
    @State private var expanded: Set<String> = []
    @State private var openWriters: Set<String> = []

    /// Brief for a complex thread unless the owner switched; otherwise the conversation.
    private var effectiveMode: Mode {
        Self.mode(chosen: mode, startsOn: startsOn, isComplex: conversation.isComplex, showsOriginal: showsOriginal)
    }

    /// Whether a message shares the previous one's name line: same writer,
    /// same day, no marker between them. A message claiming the owner's
    /// address always gets its own name line, which carries its warning.
    static func continuesGroup(
        _ entry: EmailConversation.Entry, after previous: EmailConversation.Entry?, newDay: Bool,
        marker: EmailConversation.Marker?
    ) -> Bool {
        !entry.unverifiedOwner && !newDay && marker == nil && previous?.author.address == entry.author.address
    }

    /// The view to show. Show original needs whole messages, which only the
    /// conversation holds; otherwise the owner's choice, else the opening view,
    /// else the brief for a complex thread.
    static func mode(chosen: Mode?, startsOn: Mode?, isComplex: Bool, showsOriginal: Bool?) -> Mode {
        if showsOriginal == true { return .conversation }
        return chosen ?? startsOn ?? (isComplex ? .brief : .conversation)
    }

    /// The Conversation | Brief switch over whichever view is showing.
    var body: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s4) {
            if conversation.isComplex {
                Picker("Show", selection: Binding(get: { effectiveMode }, set: { mode = $0 })) {
                    Text("Conversation").tag(Mode.conversation)
                    Text("Brief").tag(Mode.brief)
                }
                .pickerStyle(.segmented).labelsHidden().fixedSize()
                .disabled(showsOriginal == true)
                .help(showsOriginal == true ? "Turn off Show original to read the brief" : "")
                .accessibilityIdentifier("email.thread.view")
            }
            switch effectiveMode {
            case .conversation: conversationBody
            case .brief: brief
            }
        }
        .environment(\.openURL, OpenURLAction { url in
            guard ["https", "http"].contains(url.scheme?.lowercased() ?? ""), url.host != nil else { return .discarded }
            return .systemAction
        })
    }

    /// The person's ink in this thread.
    private func tint(_ person: EmailPerson) -> Color { EmailPersonTint.color(for: person, in: conversation) }

    // MARK: - Conversation

    /// Every entry, or only one side conversation's when it is chosen.
    private var visibleEntries: [EmailConversation.Entry] {
        guard let sideID, let side = conversation.sideConversation(id: sideID) else { return conversation.entries }
        return conversation.entries.filter { side.entryIDs.contains($0.id) }
    }

    /// People, an optional side-conversation filter, then the bubbles by day.
    private var conversationBody: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s3) {
            people
            if let sideID, let side = conversation.sideConversation(id: sideID) {
                HStack(spacing: VeetSpace.s2) {
                    Label("Addressed to \(emailPeopleList(side.members, limit: 4))", systemImage: "person.2")
                        .veetType(.meta, weight: .medium).foregroundColor(AppTheme.amberText)
                        .help(EmailConversation.audienceCaveat)
                        .accessibilityHint(EmailConversation.audienceCaveat)
                    Spacer(minLength: VeetSpace.s2)
                    Button("Show everyone's messages") { self.sideID = nil }
                        .buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.tealText)
                        .accessibilityIdentifier("email.conversation.side.clear")
                }
            }
            VStack(alignment: .leading, spacing: 0) {
                let entries = visibleEntries
                ForEach(Array(entries.enumerated()), id: \.element.id) { index, entry in
                    let previous = index > 0 ? entries[index - 1] : nil
                    let newDay = previous.map { !Calendar.current.isDate($0.message.sentAt, inSameDayAs: entry.message.sentAt) } ?? true
                    let marker = sideID == nil ? entry.marker : nil
                    let grouped = Self.continuesGroup(entry, after: previous, newDay: newDay, marker: marker)
                    if newDay { dayDivider(entry.message.sentAt) }
                    if let marker { markerView(marker, author: entry.author) }
                    row(entry, grouped: grouped)
                        .padding(.top, grouped ? VeetSpace.s1 : VeetSpace.s3)
                        .id("email.message.\(entry.id)")
                }
            }
        }
    }

    /// Who wrote, as chips that fade everyone else, then who was only copied.
    private var people: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            Text(summaryLine).veetType(.meta).foregroundColor(AppTheme.inkFaint)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: VeetSpace.s2) {
                    ForEach(conversation.writers) { writer in
                        let selected = focus == writer.person.address
                        Button {
                            focus = selected ? nil : writer.person.address
                        } label: {
                            HStack(spacing: 6) {
                                EmailPersonAvatar(person: writer.person, tint: tint(writer.person), size: 20)
                                Text(writer.person.short).veetType(.ui, weight: .medium).foregroundColor(AppTheme.ink)
                                Text("\(writer.count)").veetType(.meta).foregroundColor(AppTheme.inkFaint)
                            }
                            .padding(.leading, 3).padding(.trailing, 10).frame(height: 28)
                            .background(Capsule().fill(selected ? AppTheme.selected : Color.clear))
                            .overlay(Capsule().stroke(selected ? AppTheme.hairlineStrong : AppTheme.hairline))
                            .contentShape(Capsule())
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("\(writer.person.short), \(writer.count) messages")
                        .accessibilityHint(selected ? "Shows everyone's messages" : "Fades everyone else's messages")
                        .accessibilityAddTraits(selected ? .isSelected : [])
                        .accessibilityIdentifier("email.conversation.writer")
                    }
                }
                .padding(1)
            }
            if !conversation.silent.isEmpty {
                Text("Copied, haven't written: \(emailPeopleList(conversation.silent, limit: 5))")
                    .veetType(.meta).foregroundColor(AppTheme.inkFaint).fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    /// "N emails · N people wrote", with the folded share when it is worth saying.
    private var summaryLine: String {
        var parts = ["\(conversation.entries.count) emails", "\(conversation.writers.count) people wrote"]
        let share = Int((conversation.hiddenShare * 100).rounded())
        if share >= 5 { parts.append("\(share)% repeated text folded") }
        return parts.joined(separator: " · ")
    }

    /// A hairline with the day between messages sent on different days.
    private func dayDivider(_ date: Date) -> some View {
        HStack(spacing: VeetSpace.s3) {
            Rectangle().fill(AppTheme.hairline).frame(height: 1)
            Text(date, format: .dateTime.weekday(.wide).month(.abbreviated).day())
                .veetType(.label).foregroundColor(AppTheme.inkFaint).fixedSize()
            Rectangle().fill(AppTheme.hairline).frame(height: 1)
        }
        .padding(.top, VeetSpace.s4).padding(.bottom, VeetSpace.s1)
        .accessibilityElement(children: .combine)
    }

    /// The audience change above the message that made it.
    @ViewBuilder private func markerView(_ marker: EmailConversation.Marker, author: EmailPerson) -> some View {
        switch marker {
        case .narrowed:
            markerPill(marker.text(author: author), symbol: "person.2", attention: true)
                .help(EmailConversation.audienceCaveat)
                .accessibilityHint(EmailConversation.audienceCaveat)
        case .added:
            markerPill(marker.text(author: author), symbol: "person.badge.plus", attention: false)
        case .widened:
            markerPill(marker.text(author: author), symbol: "person.3", attention: false)
        }
    }

    /// A centered capsule; amber when it starts a side conversation.
    private func markerPill(_ text: String, symbol: String, attention: Bool) -> some View {
        HStack {
            Spacer(minLength: 0)
            Label(text, systemImage: symbol)
                .veetType(.meta, weight: .medium)
                .foregroundColor(attention ? AppTheme.amberText : AppTheme.inkSoft)
                .multilineTextAlignment(.center)
                .padding(.horizontal, VeetSpace.s3).padding(.vertical, 6)
                .background(Capsule().fill(attention ? AppTheme.amberWash : AppTheme.paperDeep))
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.top, VeetSpace.s4)
        .accessibilityIdentifier("email.conversation.marker")
    }

    /// A bubble on its writer's side, under their avatar and name unless grouped.
    @ViewBuilder private func row(_ entry: EmailConversation.Entry, grouped: Bool) -> some View {
        let dim = focus != nil && focus != entry.author.address
        if entry.isOwner {
            HStack(alignment: .top, spacing: 0) {
                Spacer(minLength: VeetSpace.s7)
                VStack(alignment: .trailing, spacing: VeetSpace.s1) {
                    if !grouped {
                        Text(entry.message.sentAt, format: .dateTime.hour().minute())
                            .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                    }
                    bubble(entry)
                    attachmentLine(entry.message)
                }
            }
            .opacity(dim ? 0.3 : 1)
        } else {
            HStack(alignment: .top, spacing: VeetSpace.s2) {
                Group {
                    if grouped { Color.clear } else { EmailPersonAvatar(person: entry.author, tint: tint(entry.author)) }
                }
                .frame(width: 28, height: 28)
                VStack(alignment: .leading, spacing: VeetSpace.s1) {
                    if !grouped {
                        HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
                            Button {
                                onName(entry.message)
                            } label: {
                                Text(entry.author.name).veetType(EmailType.sender, weight: .semibold)
                                    .foregroundColor(tint(entry.author))
                            }
                            .buttonStyle(.plain)
                            .help("Show who wrote this")
                            .accessibilityIdentifier("email.sender.people")
                            Text(entry.message.sentAt, format: .dateTime.hour().minute())
                                .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                        }
                        if entry.unverifiedOwner {
                            Label("Uses your address, but isn't in your sent mail", systemImage: "exclamationmark.triangle")
                                .veetType(.meta, weight: .medium).foregroundColor(AppTheme.amberText)
                                .accessibilityIdentifier("email.conversation.unverified")
                        }
                    }
                    bubble(entry)
                    attachmentLine(entry.message)
                }
                Spacer(minLength: VeetSpace.s6)
            }
            .opacity(dim ? 0.3 : 1)
        }
    }

    /// The message's own words, its audience line, and Show full email.
    private func bubble(_ entry: EmailConversation.Entry) -> some View {
        let open = expanded.contains(entry.id)
        return VStack(alignment: .leading, spacing: VeetSpace.s2) {
            if entry.isPrivate {
                Label("Addressed to \(emailPeopleList(entry.members, limit: 4))", systemImage: "person.2")
                    .veetType(.meta).foregroundColor(AppTheme.amberText)
            }
            if showsOriginal == true || open {
                EmailReadingView(message: entry.message, showsOriginal: showsOriginal)
                if open {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("To: \(entry.message.to.joined(separator: ", "))")
                        if !entry.message.cc.isEmpty { Text("Cc: \(entry.message.cc.joined(separator: ", "))") }
                    }
                    .veetType(.meta).foregroundColor(AppTheme.inkFaint).textSelection(.enabled)
                }
            } else {
                Group {
                    if entry.isPlainText {
                        Text(verbatim: entry.message.body).fixedSize(horizontal: false, vertical: true)
                    } else {
                        MarkdownBlocksView(blocks: entry.own)
                    }
                }
                .textSelection(.enabled).veetType(.ui).lineSpacing(5)
                .accessibilityIdentifier("email.conversation.text")
            }
            if !entry.message.complete {
                Label("This message is incomplete. Refresh before relying on its full contents.",
                      systemImage: "exclamationmark.circle")
                    .veetType(.meta).foregroundColor(AppTheme.amberText)
            }
            if showsOriginal != true, entry.hasMore || open {
                Button(open ? "Show less" : "Show full email") {
                    if open { expanded.remove(entry.id) } else { expanded.insert(entry.id) }
                }
                .buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.tealText)
                .accessibilityIdentifier("email.conversation.more")
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous)
            .fill(entry.isOwner ? AppTheme.tealWash : AppTheme.surface))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
            .stroke(entry.isPrivate ? AppTheme.amberLine : AppTheme.hairline, lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("email.conversation.message")
    }

    // MARK: - Brief

    /// Where it stands, who is saying what, side conversations, files and the copied.
    private var brief: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s5) {
            if showsSummary, !thread.summary.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Where it stands").veetType(.meta, weight: .semibold).foregroundColor(AppTheme.tealText)
                    Text(thread.summary).veetType(.ui).fixedSize(horizontal: false, vertical: true)
                }
                .padding(.leading, VeetSpace.s3)
                .overlay(alignment: .leading) { RoundedRectangle(cornerRadius: 1).fill(AppTheme.teal).frame(width: 2) }
            }
            briefSection("Who's saying what") {
                ForEach(conversation.writers) { writer in writerCard(writer) }
            }
            if !conversation.sideConversations.isEmpty {
                briefSection("Side conversations") {
                    ForEach(conversation.sideConversations) { side in sideRow(side) }
                    Text(EmailConversation.audienceCaveat)
                        .veetType(.meta).foregroundColor(AppTheme.inkFaint).fixedSize(horizontal: false, vertical: true)
                }
            }
            if !conversation.files.isEmpty {
                briefSection("Files") {
                    ForEach(conversation.files) { file in fileRow(file) }
                }
            }
            if !conversation.silent.isEmpty {
                briefSection("Only copied") {
                    Text(conversation.silent.map(\.name).joined(separator: ", "))
                        .veetType(.ui).foregroundColor(AppTheme.inkSoft).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .accessibilityIdentifier("email.brief")
    }

    /// A titled brief section.
    private func briefSection<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: VeetSpace.s3) {
            Text(title).veetType(.title)
                .accessibilityAddTraits(.isHeader)
            content()
        }
    }

    /// A brief line keeps its inline emphasis and links, except a body that had
    /// no reader text, which shows exactly as typed.
    private func briefText(_ entry: EmailConversation.Entry) -> AttributedString {
        guard !entry.isPlainText else { return AttributedString(entry.briefLine) }
        return (try? AttributedString(
            markdown: entry.briefLine, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(entry.briefLine)
    }

    /// A writer's latest point, opening to a line per message.
    private func writerCard(_ writer: EmailConversation.Writer) -> some View {
        let open = openWriters.contains(writer.id)
        let latest = conversation.entry(id: writer.latestID)
        let theirs = conversation.entries.filter { $0.author.address == writer.person.address }
        return HStack(alignment: .top, spacing: VeetSpace.s3) {
            EmailPersonAvatar(person: writer.person, tint: tint(writer.person), size: 32)
            VStack(alignment: .leading, spacing: VeetSpace.s1) {
                HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
                    Text(writer.person.isOwner ? "You" : writer.person.name)
                        .veetType(.ui, weight: .semibold).foregroundColor(tint(writer.person))
                    if let latest {
                        Text(briefMeta(count: writer.count, latest: latest.message.sentAt))
                            .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                    }
                }
                if let latest {
                    Text(briefText(latest)).veetType(.ui).foregroundColor(AppTheme.ink)
                        .lineLimit(4).fixedSize(horizontal: false, vertical: true)
                }
                Button(open ? "Hide messages" : (writer.count == 1 ? "Show the message" : "Show \(writer.count) messages")) {
                    if open { openWriters.remove(writer.id) } else { openWriters.insert(writer.id) }
                }
                .buttonStyle(.plain).veetType(.meta, weight: .medium).foregroundColor(AppTheme.tealText)
                .accessibilityIdentifier("email.brief.writer.messages")
                if open {
                    VStack(alignment: .leading, spacing: VeetSpace.s2) {
                        ForEach(theirs) { entry in
                            Button {
                                reveal(entry.id)
                            } label: {
                                HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
                                    Text(entry.message.sentAt, format: .dateTime.month(.abbreviated).day())
                                        .veetType(.meta).foregroundColor(AppTheme.inkFaint).frame(width: 48, alignment: .leading)
                                    if entry.isPrivate {
                                        Image(systemName: "person.2").veetType(.meta).foregroundColor(AppTheme.amberText)
                                            .accessibilityLabel("Side conversation")
                                    }
                                    Text(briefText(entry)).veetType(.ui).foregroundColor(AppTheme.inkSoft)
                                        .lineLimit(2).multilineTextAlignment(.leading)
                                }
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            .help("Show this message in the conversation")
                            .accessibilityIdentifier("email.brief.message")
                        }
                    }
                    .padding(.top, VeetSpace.s1)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.vertical, VeetSpace.s1)
    }

    /// "N messages · last <date>".
    private func briefMeta(count: Int, latest: Date) -> String {
        let when = latest.formatted(.dateTime.month(.abbreviated).day().hour().minute())
        return "\(count == 1 ? "1 message" : "\(count) messages") · last \(when)"
    }

    /// A side conversation; pressing it filters the conversation to it.
    private func sideRow(_ side: EmailConversation.SideConversation) -> some View {
        Button {
            mode = .conversation
            focus = nil
            sideID = side.id
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Label("Addressed to \(emailPeopleList(side.members, limit: 4))", systemImage: "person.2")
                    .veetType(.ui, weight: .semibold).foregroundColor(AppTheme.amberText)
                Text("\(side.entryIDs.count == 1 ? "1 message" : "\(side.entryIDs.count) messages") · started by \(side.startedBy.short), \(side.startedAt.formatted(.dateTime.month(.abbreviated).day()))")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                if !side.notIncluded.isEmpty {
                    Text("Not addressed: \(emailPeopleList(side.notIncluded, limit: 4))")
                        .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(VeetSpace.s3)
            .background(RoundedRectangle(cornerRadius: 10, style: .continuous).fill(AppTheme.amberWash))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Show this side conversation")
        .accessibilityHint("Shows only these messages in the conversation")
        .accessibilityIdentifier("email.brief.side")
    }

    /// A file with who sent it, opening the attachments or its message.
    @ViewBuilder private func fileRow(_ file: EmailConversation.File) -> some View {
        let label = HStack(spacing: VeetSpace.s2) {
            Image(systemName: "paperclip").foregroundColor(AppTheme.tealText)
            Text(file.attachment.filename).veetType(.ui, weight: .medium).foregroundColor(AppTheme.ink).lineLimit(1)
            Text("from \(file.author.short)").veetType(.meta).foregroundColor(AppTheme.inkFaint)
            Spacer(minLength: 0)
        }
        if let onFiles {
            Button(action: onFiles) { label.contentShape(Rectangle()) }
                .buttonStyle(.plain).help("Show the attachments in the panel")
                .accessibilityIdentifier("email.brief.file")
        } else {
            Button { reveal(file.entryID) } label: { label.contentShape(Rectangle()) }
                .buttonStyle(.plain).help("Show the message it came with")
                .accessibilityIdentifier("email.brief.file")
        }
    }

    /// Opens the conversation at one message.
    private func reveal(_ id: String) {
        mode = .conversation
        focus = nil
        if let sideID, conversation.sideConversation(id: sideID)?.entryIDs.contains(id) != true { self.sideID = nil }
        DispatchQueue.main.async {
            proxy?.scrollTo("email.message.\(id)", anchor: .top)
        }
    }
}
