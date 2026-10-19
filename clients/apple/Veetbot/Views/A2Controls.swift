import SwiftUI

/// The shared controls of the A2 Dock design: capsule buttons in three
/// weights, the teal mark beside an answer, the amber line that says what the
/// transcript is waiting for, and the sidebar's section headers. No capsule
/// limits its label to one line: the Dynamic Type audit reads a one-line
/// limit as text that larger sizes would clip.
enum A2Metrics {
    #if os(macOS)
    static let controlHeight: CGFloat = 28
    static let rowHeight: CGFloat = 28
    static let composerRadius: CGFloat = 22
    #else
    static let controlHeight: CGFloat = 36
    static let rowHeight: CGFloat = 44
    static let composerRadius: CGFloat = 24
    #endif
    static let cardRadius: CGFloat = 14
    static let transcriptMaxWidth: CGFloat = 760
}

/// A filled teal capsule: the one action the owner most likely takes.
struct PrimaryCapsuleButtonStyle: ButtonStyle {
    var expands = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .appFont(.callout, weight: .medium)
            .padding(.horizontal, 14)
            .frame(maxWidth: expands ? .infinity : nil, minHeight: A2Metrics.controlHeight)
            .foregroundColor(AppTheme.onTeal)
            .background(Capsule().fill(AppTheme.tealFill))
            .opacity(!isEnabled ? 0.45 : configuration.isPressed ? 0.85 : 1)
            .contentShape(Capsule())
    }
}

/// A hairline capsule: an alternative that still deserves a border.
struct SecondaryCapsuleButtonStyle: ButtonStyle {
    var expands = false
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .appFont(.callout, weight: .medium)
            .padding(.horizontal, 14)
            .frame(maxWidth: expands ? .infinity : nil, minHeight: A2Metrics.controlHeight)
            .foregroundColor(AppTheme.ink)
            .background(
                Capsule().fill(configuration.isPressed ? AppTheme.field : Color.clear)
            )
            .overlay(Capsule().stroke(AppTheme.hairlineStrong))
            .opacity(isEnabled ? 1 : 0.45)
            .contentShape(Capsule())
    }
}

/// Text only, in soft ink: the quiet way out.
struct QuietCapsuleButtonStyle: ButtonStyle {
    var expands = false
    /// A colour other than soft ink, for a destructive or teal action.
    var tint: Color? = nil
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .appFont(.callout, weight: .medium)
            .padding(.horizontal, 12)
            .frame(maxWidth: expands ? .infinity : nil, minHeight: A2Metrics.controlHeight)
            .foregroundColor(tint ?? (configuration.isPressed ? AppTheme.ink : AppTheme.inkSoft))
            .background(
                Capsule().fill(configuration.isPressed ? AppTheme.field : Color.clear)
            )
            .opacity(isEnabled ? 1 : 0.45)
            .contentShape(Capsule())
    }
}

/// A filter pill on a touch surface: field-coloured at rest, ink when chosen.
struct FilterPillButtonStyle: ButtonStyle {
    let selected: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .appFont(.subheadline, weight: .medium)
            .padding(.horizontal, 14)
            .frame(minHeight: 32)
            .foregroundColor(selected ? AppTheme.paper : AppTheme.inkSoft)
            .background(Capsule().fill(selected ? AppTheme.ink : AppTheme.field))
            .opacity(configuration.isPressed ? 0.85 : 1)
            .contentShape(Capsule())
    }
}

/// A one-point rule in the hairline colour.
struct HairlineDivider: View {
    var body: some View {
        Rectangle()
            .fill(AppTheme.hairline)
            .frame(height: 1)
            .accessibilityHidden(true)
    }
}

/// A text field or box on the surface colour behind a strong hairline.
struct A2FieldModifier: ViewModifier {
    var radius: CGFloat = 8

    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 10)
            .padding(.vertical, 6)
            .background(RoundedRectangle(cornerRadius: radius, style: .continuous).fill(AppTheme.surface))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous).stroke(AppTheme.hairlineStrong))
    }
}

extension View {
    /// Draws the view as an A2 field: surface colour, strong hairline, small radius.
    func a2Field(radius: CGFloat = 8) -> some View {
        modifier(A2FieldModifier(radius: radius))
    }

    /// The filled teal capsule for a button that sits in scrolling content.
    /// On the Mac it is the system's prominent button in a capsule: on macOS
    /// 26 a custom-styled button that a scroll brought on screen reports a
    /// stale frame to accessibility, so a click lands beside it. Touch keeps
    /// the custom capsule.
    @ViewBuilder
    func primaryCapsule(expands: Bool = false) -> some View {
        #if os(macOS)
        buttonStyle(.borderedProminent)
            .buttonBorderShape(.capsule)
            .tint(AppTheme.tealFill)
            .controlSize(.large)
        #else
        buttonStyle(PrimaryCapsuleButtonStyle(expands: expands))
        #endif
    }
}

/// A square icon button in soft ink, for the composer and the sidebar.
struct QuietIconButtonStyle: ButtonStyle {
    var side: CGFloat = A2Metrics.controlHeight

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .frame(width: side, height: side)
            .foregroundColor(configuration.isPressed ? AppTheme.ink : AppTheme.inkSoft)
            .background(
                RoundedRectangle(cornerRadius: 8)
                    .fill(configuration.isPressed ? AppTheme.field : Color.clear)
            )
            .contentShape(RoundedRectangle(cornerRadius: 8))
    }
}

/// The small teal mark that opens an assistant answer.
struct AssistantMark: View {
    var body: some View {
        Circle()
            .fill(AppTheme.teal)
            .frame(width: 8, height: 8)
            .background(Circle().fill(AppTheme.tealWash).padding(-3))
            .accessibilityHidden(true)
    }
}

/// One amber line in the transcript: the run is waiting on the owner, and the
/// card to answer it sits above the composer.
struct WaitingLine: View {
    let text: String

    var body: some View {
        Label {
            Text(text)
        } icon: {
            Image(systemName: "flag")
        }
        .appFont(.callout, weight: .semibold)
        .foregroundColor(AppTheme.amberText)
        .accessibilityIdentifier("chat.waiting")
    }
}

/// A sidebar section heading in the A2 voice: small, semibold, soft ink.
struct SidebarSectionLabel: View {
    let title: String

    var body: some View {
        Text(title)
            .appFont(.caption, weight: .semibold)
            .foregroundColor(AppTheme.inkSoft)
            .textCase(nil)
    }
}

/// The amber or teal dot beside a Needs you row.
struct AttentionDot: View {
    let kind: SidebarAttention.Kind

    var body: some View {
        Circle()
            .fill(kind == .approval ? AppTheme.amber : AppTheme.teal)
            .frame(width: 7, height: 7)
            .accessibilityHidden(true)
    }
}

// MARK: - Reading pages and lists

/// A section heading on a reading page: small, semibold, soft ink.
struct A2SectionHeading: View {
    let title: String

    var body: some View {
        Text(title)
            .appFont(.subheadline, weight: .semibold)
            .foregroundColor(AppTheme.inkSoft)
            .accessibilityAddTraits(.isHeader)
    }
}

/// A short uppercase label naming a row's kind.
struct A2KindLabel: View {
    let text: String

    var body: some View {
        Text(text)
            .textCase(.uppercase)
            .appFont(.caption2, weight: .bold)
            .tracking(0.6)
            .foregroundColor(AppTheme.inkSoft)
    }
}

/// The top of a sheet: its title in the serif face and a quiet way out.
struct A2SheetHeader: View {
    let title: String
    var dismissTitle = "Done"
    let dismiss: () -> Void

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(title).appDisplayFont(.title2)
            Spacer()
            Button(dismissTitle, action: dismiss).buttonStyle(QuietCapsuleButtonStyle())
        }
    }
}

/// One line of a record: a key in soft ink and its value.
struct A2Record: Identifiable {
    let key: String
    let value: String
    var id: String { key }

    init(_ key: String, _ value: String) {
        self.key = key
        self.value = value
    }
}

/// A titled card of rows on the surface colour, parted by hairlines.
struct A2Group<Content: View>: View {
    var title: String? = nil
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let title { A2SectionHeading(title: title) }
            VStack(alignment: .leading, spacing: 0) { content() }
                .frame(maxWidth: .infinity, alignment: .leading)
                .a2Card()
        }
        .accessibilityElement(children: .contain)
    }
}

/// A titled card of key-value lines.
struct A2RecordGroup: View {
    let title: String
    let rows: [A2Record]

    var body: some View {
        A2Group(title: title) {
            ForEach(Array(rows.enumerated()), id: \.element.id) { index, row in
                if index > 0 { HairlineDivider().padding(.leading, 14) }
                HStack(alignment: .firstTextBaseline, spacing: 12) {
                    Text(row.key)
                        .appFont(.caption)
                        .foregroundColor(AppTheme.inkSoft)
                        .frame(minWidth: 96, alignment: .leading)
                    Spacer(minLength: 0)
                    Text(row.value)
                        .appFont(.caption)
                        .foregroundColor(AppTheme.ink)
                        .multilineTextAlignment(.trailing)
                        .textSelection(.enabled)
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 9)
                .accessibilityElement(children: .combine)
            }
        }
    }
}

/// A quoted original behind a teal rule, with where it came from beneath.
struct A2Quote<Content: View>: View {
    var cite: String? = nil
    @ViewBuilder let content: () -> Content

    var body: some View {
        HStack(alignment: .top, spacing: 0) {
            Rectangle().fill(AppTheme.teal).frame(width: 2).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                content()
                if let cite {
                    Text(cite).appFont(.caption).foregroundColor(AppTheme.inkSoft)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(AppTheme.surface)
        .clipShape(
            UnevenRoundedRectangle(
                cornerRadii: .init(topLeading: 0, bottomLeading: 0, bottomTrailing: 10, topTrailing: 10),
                style: .continuous
            )
        )
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// A centred message for a list with nothing to show, with any way forward
/// beneath it.
struct A2EmptyState<Actions: View>: View {
    let systemImage: String
    let title: String
    var message: String? = nil
    @ViewBuilder let actions: () -> Actions

    init(
        systemImage: String,
        title: String,
        message: String? = nil,
        @ViewBuilder actions: @escaping () -> Actions
    ) {
        self.systemImage = systemImage
        self.title = title
        self.message = message
        self.actions = actions
    }

    var body: some View {
        VStack(spacing: 12) {
            Image(systemName: systemImage)
                .font(.system(size: 28, weight: .light))
                .foregroundColor(AppTheme.tealText)
                .frame(width: 60, height: 60)
                .background(Circle().fill(AppTheme.tealWash))
                .accessibilityHidden(true)
            Text(title)
                .appFont(.body, weight: .medium)
                .foregroundColor(AppTheme.ink)
                .multilineTextAlignment(.center)
            if let message {
                Text(message)
                    .appFont(.callout)
                    .foregroundColor(AppTheme.inkSoft)
                    .multilineTextAlignment(.center)
            }
            actions()
        }
        .frame(maxWidth: 360)
        .fixedSize(horizontal: false, vertical: true)
        .padding(24)
        // The ideal height bounds the intrinsic size a Mac sheet column reports
        // for this state: without it the column lays out at the screen's
        // height and the title above it is cut off.
        .frame(maxWidth: .infinity, minHeight: 0, idealHeight: 320, maxHeight: .infinity)
    }
}

extension A2EmptyState where Actions == EmptyView {
    init(systemImage: String, title: String, message: String? = nil) {
        self.init(systemImage: systemImage, title: title, message: message) { EmptyView() }
    }
}

extension View {
    /// A card on the page: the surface colour behind a hairline.
    func a2Card(border: Color = AppTheme.hairline) -> some View {
        background(
            RoundedRectangle(cornerRadius: A2Metrics.cardRadius, style: .continuous)
                .fill(AppTheme.surface)
        )
        .overlay(
            RoundedRectangle(cornerRadius: A2Metrics.cardRadius, style: .continuous)
                .stroke(border)
        )
    }

    /// A list on paper with hairline separators and no system backdrop.
    func a2List() -> some View {
        listStyle(.plain)
            .scrollContentBackground(.hidden)
            .background(AppTheme.paper)
    }

    /// A row of an A2 list: paper behind it, a hairline beneath.
    func a2ListRow() -> some View {
        listRowBackground(AppTheme.paper)
            .listRowSeparatorTint(AppTheme.hairline)
    }
}
