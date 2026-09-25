import SwiftUI

#if os(macOS)
import AppKit
#else
import UIKit
#endif

// MARK: - Tokens

/// The People palette. Accent and surfaces follow Email mode; text colors keep
/// at least 4.5:1 contrast on cards and canvas in both appearances, which the
/// Mac Large text accessibility audit checks.
enum PeopleSurface {
    static var accent: Color { EmailSurface.accent }
    /// Unconfirmed identities and questions waiting on the owner.
    static let attention = peopleAdaptiveColor(light: (167, 62, 0), dark: (255, 158, 98))
    /// Forgetting and other removals.
    static let destructive = peopleAdaptiveColor(light: (180, 35, 24), dark: (255, 138, 125))
    /// Initials on the monogram's tinted wash.
    static let monogramInk = peopleAdaptiveColor(light: (0, 84, 81), dark: (97, 209, 196))
    static var canvas: Color { EmailSurface.canvas }
    static var card: Color { EmailSurface.card }
    /// Dates, sources and other supporting text.
    static let muted = Color.primary.opacity(0.75)
    static let hairline = Color.primary.opacity(0.09)
}

enum PeopleMetrics {
    #if os(macOS)
    static let pagePadding: CGFloat = 24
    static let sectionSpacing: CGFloat = 22
    static let avatar: CGFloat = 60
    static let meta: Font.TextStyle = .subheadline
    #else
    static let pagePadding: CGFloat = 16
    static let sectionSpacing: CGFloat = 20
    static let avatar: CGFloat = 68
    static let meta: Font.TextStyle = .footnote
    #endif
    /// A readable measure for statements and summaries at any window width.
    static let readableWidth: CGFloat = 700
    static let cardRadius: CGFloat = 14
    static let rowPadding: CGFloat = 14
}

private func peopleAdaptiveColor(light: (Double, Double, Double), dark: (Double, Double, Double)) -> Color {
    #if os(macOS)
    Color(nsColor: NSColor(name: nil) { appearance in
        let rgb = appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
        return NSColor(srgbRed: rgb.0 / 255, green: rgb.1 / 255, blue: rgb.2 / 255, alpha: 1)
    })
    #else
    Color(uiColor: UIColor { traits in
        let rgb = traits.userInterfaceStyle == .dark ? dark : light
        return UIColor(red: rgb.0 / 255, green: rgb.1 / 255, blue: rgb.2 / 255, alpha: 1)
    })
    #endif
}

// MARK: - Building blocks

/// A person's initials on the brand's teal-to-orange wash. Decorative: the
/// name beside it carries the meaning, so accessibility sizes leave it out.
struct PeopleMonogram: View {
    let name: String
    var size: CGFloat = PeopleMetrics.avatar
    @Environment(\.appFontStyle) private var fontStyle
    @Environment(\.dynamicTypeSize) private var typeSize
    /// Grows the circle with the text style its initials use.
    @ScaledMetric(relativeTo: .body) private var scale: CGFloat = 1

    var body: some View {
        if !typeSize.isAccessibilitySize {
            let initials = Self.initials(of: name)
            ZStack {
                Circle().fill(AppTheme.brandGradient)
                Circle().strokeBorder(PeopleSurface.accent.opacity(0.22), lineWidth: 1)
                if initials.isEmpty {
                    Image(systemName: "person.fill").font(.system(style, design: fontStyle.design))
                } else {
                    Text(initials).font(.system(style, design: fontStyle.design).weight(.semibold))
                }
            }
            .foregroundColor(PeopleSurface.monogramInk)
            .frame(width: size * scale, height: size * scale)
            .accessibilityHidden(true)
        }
    }

    /// A text style near 0.37 of the diameter, so the initials follow Dynamic Type.
    private var style: Font.TextStyle {
        #if os(macOS)
        size >= 50 ? .title : .callout
        #else
        size >= 50 ? .title2 : .caption
        #endif
    }

    /// The first letters of the first and last words that begin with a letter.
    static func initials(of name: String) -> String {
        let words = name.split(whereSeparator: { $0.isWhitespace })
            .compactMap { word in word.first(where: \.isLetter).map { String($0).uppercased() } }
        guard let first = words.first else { return "" }
        return words.count > 1 ? first + words[words.count - 1] : first
    }
}

/// A titled profile section: the title sits on the canvas, the rows on a card.
struct PeopleSection<Content: View>: View {
    let title: String
    let content: Content

    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .appFont(.headline)
                .accessibilityAddTraits(.isHeader)
                .padding(.horizontal, 4)
            VStack(alignment: .leading, spacing: 0) { content }
                .frame(maxWidth: .infinity, alignment: .leading)
                .peopleCard()
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(title)
    }
}

/// One row of a section card; `divided` draws the rule above every row but the first.
struct PeopleRow<Content: View>: View {
    var divided = false
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if divided { Divider().padding(.leading, PeopleMetrics.rowPadding) }
            content()
                .padding(PeopleMetrics.rowPadding)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// A short capsule for a state such as Pinned or Not confirmed yet.
struct PeopleBadge: View {
    let text: String
    var systemImage: String?
    var tint: Color = PeopleSurface.muted

    var body: some View {
        Group {
            if let systemImage { Label(text, systemImage: systemImage) } else { Text(text) }
        }
        .appFont(PeopleMetrics.meta, weight: .medium)
        .foregroundColor(tint)
        .padding(.horizontal, 9)
        .padding(.vertical, 4)
        .background(tint.opacity(0.1))
        .clipShape(Capsule())
    }
}

/// A question or status that sits above the profile's sections.
struct PeopleCallout<Actions: View>: View {
    let systemImage: String
    let title: String
    var message: String?
    var tint: Color = PeopleSurface.attention
    @ViewBuilder let actions: () -> Actions

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: systemImage)
                .appFont(.title3)
                .foregroundColor(tint)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 6) {
                Text(title).appFont(.headline).fixedSize(horizontal: false, vertical: true)
                if let message {
                    Text(message).appFont(.callout).foregroundColor(PeopleSurface.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                PeopleActionRow { actions() }.padding(.top, 2)
            }
            Spacer(minLength: 0)
        }
        .padding(PeopleMetrics.rowPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tint.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: PeopleMetrics.cardRadius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: PeopleMetrics.cardRadius, style: .continuous)
                .stroke(tint.opacity(0.28), lineWidth: 1)
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel(title)
    }
}

/// Lays actions out in a row, or stacks them when the row would not fit.
struct PeopleActionRow<Content: View>: View {
    @Environment(\.dynamicTypeSize) private var typeSize
    @ViewBuilder let content: () -> Content

    var body: some View {
        if typeSize.isAccessibilitySize {
            VStack(alignment: .leading, spacing: 8) { content() }
        } else if #available(macOS 13, iOS 16, *) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { content() }
                VStack(alignment: .leading, spacing: 8) { content() }
            }
        } else {
            HStack(spacing: 8) { content() }
        }
    }
}

/// A timeline entry: a channel glyph on a rail that joins it to the next entry.
struct PeopleTimelineRow<Content: View>: View {
    let systemImage: String
    /// Spoken for the glyph, which shows how the exchange happened.
    let symbolLabel: String
    let isLast: Bool
    @ViewBuilder let content: () -> Content

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(spacing: 4) {
                Image(systemName: systemImage)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundColor(PeopleSurface.accent)
                    .frame(width: 28, height: 28)
                    .background(Circle().fill(PeopleSurface.accent.opacity(0.12)))
                    .accessibilityLabel(symbolLabel)
                if !isLast {
                    Rectangle().fill(PeopleSurface.hairline).frame(width: 2).frame(maxHeight: .infinity)
                        .accessibilityHidden(true)
                }
            }
            VStack(alignment: .leading, spacing: 4) { content() }
                .padding(.top, 4)
                .padding(.bottom, isLast ? 0 : 18)
            Spacer(minLength: 0)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// The profile column before anyone is chosen.
struct PeoplePlaceholder: View {
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "person.2")
                .font(.system(size: 30, weight: .light))
                .foregroundColor(PeopleSurface.accent)
                .frame(width: 72, height: 72)
                .background(PeopleSurface.accent.opacity(0.08))
                .clipShape(Circle())
                .accessibilityHidden(true)
            Text("Choose a person").appFont(.title3, weight: .semibold)
            Text("Their relationship to you, your history together, and what Veetbot knows about them appear here.")
                .appFont(.callout)
                .foregroundColor(PeopleSurface.muted)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 360)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(24)
        // A zero minimum keeps the Mac split view at the sheet's height; a
        // column that cannot shrink makes it grow to the directory's length.
        .frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
        .background(PeopleSurface.canvas)
        .accessibilityElement(children: .combine)
    }
}

/// Supporting text under a row's main line.
struct PeopleMeta: View {
    let text: String
    init(_ text: String) { self.text = text }

    var body: some View {
        Text(text)
            .appFont(PeopleMetrics.meta)
            .foregroundColor(PeopleSurface.muted)
            .fixedSize(horizontal: false, vertical: true)
    }
}

extension View {
    /// The profile's card surface: a hairline edge instead of a shadow.
    func peopleCard() -> some View {
        background(PeopleSurface.card)
            .clipShape(RoundedRectangle(cornerRadius: PeopleMetrics.cardRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: PeopleMetrics.cardRadius, style: .continuous)
                    .stroke(PeopleSurface.hairline, lineWidth: 1)
            )
    }

    /// Gives a compact control the 44-point target touch needs; the Mac keeps its size.
    @ViewBuilder func peopleTapTarget() -> some View {
        #if os(iOS)
        frame(minWidth: 44, minHeight: 44)
        #else
        self
        #endif
    }
}

extension View {
    /// A menu drawn like `PeopleInlineButtonStyle`, without a disclosure arrow.
    @ViewBuilder func peopleInlineMenu() -> some View {
        if #available(macOS 13, iOS 16, *) {
            menuStyle(.button).buttonStyle(PeopleInlineButtonStyle()).menuIndicator(.hidden).fixedSize()
        } else {
            menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize().peopleTapTarget()
        }
    }
}

/// A compact text action inside a row, in the accent color while it can act.
struct PeopleInlineButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        PeopleInlineButtonLabel(configuration: configuration)
    }
}

private struct PeopleInlineButtonLabel: View {
    let configuration: ButtonStyle.Configuration
    @Environment(\.isEnabled) private var isEnabled

    var body: some View {
        configuration.label
            .appFont(PeopleMetrics.meta, weight: .medium)
            .foregroundColor(isEnabled ? PeopleSurface.accent : PeopleSurface.muted)
            .opacity(configuration.isPressed ? 0.55 : 1)
            .peopleTapTarget()
            .contentShape(Rectangle())
    }
}

// MARK: - Navigation within a directory

/// Set by a directory that shows profiles beside it. Opening a related person
/// selects them there, and evidence opens in a sheet instead of replacing the
/// profile column, which has no way back on the Mac.
struct PeopleSelectionAction {
    let open: (UUID) -> Void
    func callAsFunction(_ id: UUID) { open(id) }
}

private struct PeopleSelectionKey: EnvironmentKey {
    static let defaultValue: PeopleSelectionAction? = nil
}

extension EnvironmentValues {
    var peopleSelection: PeopleSelectionAction? {
        get { self[PeopleSelectionKey.self] }
        set { self[PeopleSelectionKey.self] = newValue }
    }
}

// MARK: - Wording

extension PersonProfileView {
    /// How an endpoint reads in this profile: "you", this person's name, or a
    /// related label. `capitalized` starts a sentence without recasing a name.
    func name(of endpoint: PersonEndpointView, capitalized: Bool = false) -> String {
        let label = endpoint.id.flatMap { relatedLabels?[$0.uuidString.lowercased()] }
        switch endpoint.kind {
        case "owner": return capitalized ? "You" : "you"
        case "organization": return label ?? (capitalized ? "An organization" : "an organization")
        default:
            if endpoint.id == person.id { return person.displayName }
            return label ?? (capitalized ? "Another person" : "another person")
        }
    }

    /// Relationships between this person and the owner that still hold.
    var relationshipsToOwner: [PersonRelationshipView] {
        relationships.filter { edge in
            edge.validTo == nil && (edge.subject.kind == "owner" || edge.object.kind == "owner")
                && [edge.subject, edge.object].contains { $0.kind == "person" && $0.id == person.id }
        }
    }

    /// One line for the header, such as "Your colleague and friend".
    var relationshipSummary: String? {
        let phrases = relationshipsToOwner.map { $0.phrase(in: self) }
        guard !phrases.isEmpty else { return nil }
        let roles = phrases.compactMap { $0.hasPrefix("Your ") ? String($0.dropFirst(5)) : nil }
        var parts = phrases.filter { !$0.hasPrefix("Your ") }
        if !roles.isEmpty { parts.insert("Your " + peopleList(roles), at: 0) }
        return parts.joined(separator: "; ")
    }

    /// The first current address, number, or handle, shown under the name.
    var primaryContact: PersonAliasView? {
        let current = aliases.filter { $0.validTo == nil }
        return ["email", "phone", "handle"].lazy
            .compactMap { kind in current.first { $0.identifierKind == kind } }
            .first
    }
}

extension PersonRelationshipView {
    /// Reads one directed assertion from the profile person's side. Inverse
    /// labels such as parent and child render deterministically from one
    /// assertion (people-and-relationships.md §6).
    func phrase(in profile: PersonProfileView) -> String {
        let selfIsSubject = subject.kind == "person" && subject.id == profile.person.id
        let other = selfIsSubject ? object : subject
        let who = profile.name(of: subject, capitalized: true)
        let whom = profile.name(of: object)
        func verb(_ you: String, _ others: String) -> String { subject.kind == "owner" ? you : others }

        let inverses = ["parent": "child", "child": "parent", "sibling": "sibling", "relative": "relative",
                        "partner": "partner", "spouse": "spouse", "friend": "friend", "colleague": "colleague",
                        "collaborator": "collaborator"]
        if let inverse = inverses[predicate] {
            let role = selfIsSubject ? predicate : inverse
            if other.kind == "owner" { return "Your \(role)" }
            return "\(role.prefix(1).uppercased() + role.dropFirst()) of \(profile.name(of: other))"
        }
        switch predicate {
        case "reports_to": return "\(who) \(verb("report", "reports")) to \(whom)"
        case "introduced_by": return "\(profile.name(of: object, capitalized: true)) introduced \(profile.name(of: subject))"
        case "employment": return "\(who) \(verb("work", "works")) \(object.kind == "organization" ? "at" : "for") \(whom)"
        case "founder": return "\(who) founded \(whom)"
        case "board_member": return "\(who) \(verb("are", "is")) on the board of \(whom)"
        case "investor": return "\(who) \(verb("invest", "invests")) in \(whom)"
        default: return "Related to \(profile.name(of: other))"
        }
    }

    /// The related person this row can open, when it is someone other than the owner.
    func counterpart(in profile: PersonProfileView) -> PersonEndpointView? {
        let other = subject.kind == "person" && subject.id == profile.person.id ? object : subject
        guard other.kind == "person", let id = other.id, id != profile.person.id else { return nil }
        return other
    }

    var symbolName: String {
        switch predicate {
        case "parent", "child", "sibling", "relative": return "house"
        case "partner", "spouse": return "heart"
        case "friend": return "face.smiling"
        case "colleague", "collaborator", "reports_to": return "briefcase"
        case "employment", "founder", "board_member", "investor": return "building.2"
        case "introduced_by": return "person.2"
        default: return "link"
        }
    }
}

extension PersonCommitmentView {
    /// Who owes whom, such as "From you to Maya Chen".
    func parties(in profile: PersonProfileView) -> String {
        "From \(profile.name(of: debtor)) to \(profile.name(of: beneficiary))"
    }
}

extension PersonInteractionView {
    /// How the exchange happened, such as "Received by email".
    var channelDescription: String {
        let medium = ["email": "by email", "chat": "in Chat", "sms": "by text message"][channel]
            ?? "by \(memoryDisplayText(channel).lowercased())"
        let action: String
        switch (direction, attribution) {
        case ("reported", "owner_reported"): action = "You told Veetbot"
        case ("reported", "correspondent_reported"): action = "They reported it"
        case ("reported", _): action = "Mentioned"
        case ("incoming", _): action = "Received"
        case ("outgoing", _): action = "Sent"
        default: action = memoryDisplayText(direction)
        }
        return "\(action) \(medium)"
    }

    var symbolName: String {
        switch channel {
        case "email": return "envelope"
        case "chat": return "bubble.left.and.bubble.right"
        case "sms": return "message"
        default: return "clock"
        }
    }
}

extension PersonAliasView {
    /// How Veetbot came to hold this name or address.
    var provenance: String {
        let source: String
        switch verification {
        case "owner_confirmed": source = "Confirmed by you"
        case "channel_observed": source = "Seen in your messages"
        case "contextual": source = "From context"
        default: source = memoryDisplayText(verification)
        }
        let trimmed = (context ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty || trimmed == "owner" ? source : "\(source), \(trimmed)"
    }

    var symbolName: String {
        switch identifierKind {
        case "email": return "envelope"
        case "phone": return "phone"
        case "handle": return "at"
        case "role": return "briefcase"
        default: return "person.text.rectangle"
        }
    }
}

/// Joins words as a sentence does: "a", "a and b", "a, b, and c".
func peopleList(_ items: [String], conjunction: String = "and") -> String {
    switch items.count {
    case 0: return ""
    case 1: return items[0]
    case 2: return "\(items[0]) \(conjunction) \(items[1])"
    default: return items.dropLast().joined(separator: ", ") + ", \(conjunction) " + items[items.count - 1]
    }
}
