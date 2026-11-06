import SwiftUI

/// Builds a thread's conversation once per revision: the thread screen
/// re-renders on every draft keystroke and the parse is not free.
@MainActor
enum EmailConversationCache {
    private static var built: [String: EmailConversation] = [:]

    static func conversation(for thread: EmailThreadView, ownerAddress: String?) -> EmailConversation {
        let messages = thread.messages ?? []
        let key = "\(thread.id)|\(thread.revision)|\(messages.count)|\(ownerAddress ?? "")"
        if let conversation = built[key] { return conversation }
        if built.count > 8 { built.removeAll() }
        let conversation = EmailConversation(messages: messages, ownerAddresses: Set([ownerAddress].compactMap { $0 }))
        built[key] = conversation
        return conversation
    }
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

    static func color(for person: EmailPerson, in conversation: EmailConversation) -> Color {
        guard let index = conversation.writers.firstIndex(where: { $0.person.address == person.address }) else {
            return AppTheme.inkFaint
        }
        return tokens[index % tokens.count].color
    }
}

struct EmailPersonAvatar: View {
    let person: EmailPerson
    let tint: Color
    var size: CGFloat = 28

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

    private var effectiveMode: Mode { mode ?? startsOn ?? (conversation.isComplex ? .brief : .conversation) }

    var body: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s4) {
            if conversation.isComplex {
                Picker("Show", selection: Binding(get: { effectiveMode }, set: { mode = $0 })) {
                    Text("Conversation").tag(Mode.conversation)
                    Text("Brief").tag(Mode.brief)
                }
                .pickerStyle(.segmented).labelsHidden().fixedSize()
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

    private func tint(_ person: EmailPerson) -> Color { EmailPersonTint.color(for: person, in: conversation) }

    // MARK: - Conversation

    private var visibleEntries: [EmailConversation.Entry] {
        guard let sideID, let side = conversation.sideConversation(id: sideID) else { return conversation.entries }
        return conversation.entries.filter { side.entryIDs.contains($0.id) }
    }

    private var conversationBody: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s3) {
            people
            if let sideID, let side = conversation.sideConversation(id: sideID) {
                HStack(spacing: VeetSpace.s2) {
                    Label("Only \(emailPeopleList(side.members, limit: 4))", systemImage: "lock")
                        .veetType(.meta, weight: .medium).foregroundColor(AppTheme.amberText)
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
                    let grouped = !newDay && marker == nil && previous?.author.address == entry.author.address
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

    private var summaryLine: String {
        var parts = ["\(conversation.entries.count) emails", "\(conversation.writers.count) people wrote"]
        let share = Int((conversation.hiddenShare * 100).rounded())
        if share >= 5 { parts.append("\(share)% repeated text folded") }
        return parts.joined(separator: " · ")
    }

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

    private func markerView(_ marker: EmailConversation.Marker, author: EmailPerson) -> some View {
        switch marker {
        case .narrowed(let members, let hidden):
            let recipients = members.filter { $0.address != author.address }
            let who = members.count == 2 && members.contains(where: \.isOwner)
                ? (author.isOwner ? "You wrote to \(emailPeopleList(recipients)) privately"
                    : "\(author.short) wrote to you privately")
                : "\(author.short) wrote to \(emailPeopleList(recipients, limit: 4)) only"
            return markerPill("\(who) · \(emailPeopleList(hidden)) can't see this", symbol: "lock", attention: true)
        case .added(let by, let people):
            return markerPill("\(by.short) added \(emailPeopleList(people, limit: 4))",
                              symbol: "person.badge.plus", attention: false)
        case .widened:
            return markerPill("Back to everyone", symbol: "person.3", attention: false)
        }
    }

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
                    }
                    bubble(entry)
                    attachmentLine(entry.message)
                }
                Spacer(minLength: VeetSpace.s6)
            }
            .opacity(dim ? 0.3 : 1)
        }
    }

    private func bubble(_ entry: EmailConversation.Entry) -> some View {
        let open = expanded.contains(entry.id)
        return VStack(alignment: .leading, spacing: VeetSpace.s2) {
            if entry.isPrivate {
                Label("Only \(emailPeopleList(entry.members, limit: 4))", systemImage: "lock")
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
                MarkdownBlocksView(blocks: entry.own)
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

    private func briefSection<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: VeetSpace.s3) {
            Text(title).veetType(.title)
                .accessibilityAddTraits(.isHeader)
            content()
        }
    }

    private func inline(_ markdown: String) -> AttributedString {
        (try? AttributedString(markdown: markdown, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(markdown)
    }

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
                    Text(inline(latest.briefLine)).veetType(.ui).foregroundColor(AppTheme.ink)
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
                                        Image(systemName: "lock").veetType(.meta).foregroundColor(AppTheme.amberText)
                                    }
                                    Text(inline(entry.briefLine)).veetType(.ui).foregroundColor(AppTheme.inkSoft)
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

    private func briefMeta(count: Int, latest: Date) -> String {
        let when = latest.formatted(.dateTime.month(.abbreviated).day().hour().minute())
        return "\(count == 1 ? "1 message" : "\(count) messages") · last \(when)"
    }

    private func sideRow(_ side: EmailConversation.SideConversation) -> some View {
        Button {
            mode = .conversation
            focus = nil
            sideID = side.id
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Label("Only \(emailPeopleList(side.members, limit: 4))", systemImage: "lock")
                    .veetType(.ui, weight: .semibold).foregroundColor(AppTheme.amberText)
                Text("\(side.entryIDs.count == 1 ? "1 message" : "\(side.entryIDs.count) messages") · started by \(side.startedBy.short), \(side.startedAt.formatted(.dateTime.month(.abbreviated).day()))")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                if !side.notIncluded.isEmpty {
                    Text("Not included: \(emailPeopleList(side.notIncluded, limit: 4))")
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
