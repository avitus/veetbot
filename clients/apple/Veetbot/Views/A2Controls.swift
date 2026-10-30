import SwiftUI

/// The shared controls of the A2 Dock design: capsule buttons in three
/// weights, the teal mark beside an answer, the amber line that says what the
/// transcript is waiting for, and the sidebar's section headers. No capsule
/// limits its label to one line: the Dynamic Type audit reads a one-line
/// limit as text that larger sizes would clip.
enum A2Metrics {
    // Design system revision 2: Mac, iPad and iPhone values.
    static var controlHeight: CGFloat { value(mac: 28, iPad: 36, iPhone: 44) }
    static var rowHeight: CGFloat { value(mac: 28, iPad: 40, iPhone: 44) }
    /// Every sidebar row's leading icon column; titles start after it.
    static var sidebarIconColumn: CGFloat { value(mac: 16, iPad: 18, iPhone: 20) }
    /// The sidebar's side gutter.
    static var sidebarGutter: CGFloat { value(mac: 12, iPad: 16, iPhone: 20) }
    static var rowRadius: CGFloat { value(mac: 6, iPad: 10, iPhone: 12) }
    /// Room for all three mode names at the standard text sizes.
    static var sidebarWidth: CGFloat { value(mac: 320, iPad: 320, iPhone: 300) }
    static var fieldRadius: CGFloat { value(mac: 8, iPad: 10, iPhone: 12) }
    /// The transcript's side padding: 32 on the Mac and iPad, 16 on the iPhone.
    static var transcriptSidePadding: CGFloat {
        value(mac: VeetSpace.s6, iPad: VeetSpace.s6, iPhone: VeetSpace.s4)
    }
    static var transcriptMaxWidth: CGFloat { value(mac: 680, iPad: 640, iPhone: 640) }
    static let composerRadius: CGFloat = 14
    static let tableRadius: CGFloat = 10
    static let cardRadius: CGFloat = 14
    /// How far a toast sits above the bottom of its window or sheet.
    #if os(macOS)
    static let toastBottomInset: CGFloat = 24
    #else
    static let toastBottomInset: CGFloat = 16
    #endif

    private static func value(mac: CGFloat, iPad: CGFloat, iPhone: CGFloat) -> CGFloat {
        switch VeetType.Platform.current {
        case .mac: return mac
        case .iPad: return iPad
        case .iPhone: return iPhone
        }
    }
}

/// The outline of an A2 button: a capsule, or a rounded rectangle at the
/// accessibility sizes, where a label wraps and a capsule's ends would cut
/// into its first and last lines.
struct A2ButtonShape {
    let typeSize: DynamicTypeSize

    var shape: AnyShape {
        typeSize.isAccessibilitySize
            ? AnyShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            : AnyShape(Capsule())
    }

    var verticalPadding: CGFloat { typeSize.isAccessibilitySize ? VeetSpace.s2 : 0 }
}

/// A filled teal capsule: the one action the owner most likely takes.
struct PrimaryCapsuleButtonStyle: ButtonStyle {
    var expands = false
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.dynamicTypeSize) private var typeSize

    func makeBody(configuration: Configuration) -> some View {
        let outline = A2ButtonShape(typeSize: typeSize)
        return configuration.label
            .veetType(.ui, weight: .medium)
            .padding(.horizontal, VeetSpace.s4)
            .padding(.vertical, outline.verticalPadding)
            .frame(maxWidth: expands ? .infinity : nil, minHeight: A2Metrics.controlHeight)
            .foregroundColor(AppTheme.onTeal)
            .background(outline.shape.fill(AppTheme.tealFill))
            .opacity(!isEnabled ? 0.45 : configuration.isPressed ? 0.85 : 1)
            .contentShape(outline.shape)
    }
}

/// A hairline capsule: an alternative that still deserves a border.
struct SecondaryCapsuleButtonStyle: ButtonStyle {
    var expands = false
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.dynamicTypeSize) private var typeSize

    func makeBody(configuration: Configuration) -> some View {
        let outline = A2ButtonShape(typeSize: typeSize)
        return configuration.label
            .veetType(.ui, weight: .medium)
            .padding(.horizontal, VeetSpace.s4)
            .padding(.vertical, outline.verticalPadding)
            .frame(maxWidth: expands ? .infinity : nil, minHeight: A2Metrics.controlHeight)
            .foregroundColor(AppTheme.ink)
            .background(
                outline.shape.fill(configuration.isPressed ? AppTheme.field : Color.clear)
            )
            .overlay(outline.shape.stroke(AppTheme.hairlineStrong))
            .opacity(isEnabled ? 1 : 0.45)
            .contentShape(outline.shape)
    }
}

/// Text only, in soft ink: the quiet way out.
struct QuietCapsuleButtonStyle: ButtonStyle {
    var expands = false
    /// A colour other than soft ink, for a destructive or teal action.
    var tint: Color? = nil
    @Environment(\.isEnabled) private var isEnabled
    @Environment(\.dynamicTypeSize) private var typeSize

    func makeBody(configuration: Configuration) -> some View {
        let outline = A2ButtonShape(typeSize: typeSize)
        return configuration.label
            .veetType(.ui, weight: .medium)
            .padding(.horizontal, VeetSpace.s3)
            .padding(.vertical, outline.verticalPadding)
            .frame(maxWidth: expands ? .infinity : nil, minHeight: A2Metrics.controlHeight)
            .foregroundColor(tint ?? (configuration.isPressed ? AppTheme.ink : AppTheme.inkSoft))
            .background(
                outline.shape.fill(configuration.isPressed ? AppTheme.field : Color.clear)
            )
            .opacity(isEnabled ? 1 : 0.45)
            .contentShape(outline.shape)
    }
}

/// A filter pill on a touch surface: field-coloured at rest, ink when chosen.
struct FilterPillButtonStyle: ButtonStyle {
    let selected: Bool

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .veetType(.ui, weight: .medium)
            .padding(.horizontal, VeetSpace.s4)
            .frame(minHeight: A2Metrics.controlHeight)
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
    var radius: CGFloat = A2Metrics.fieldRadius

    func body(content: Content) -> some View {
        // The well's shapes take no clicks: on a 1x Mac display (macOS 26)
        // they swallowed the click meant for a plain text field under
        // them, so nothing in Settings could be focused by clicking there;
        // a 2x display never showed it.
        content
            .padding(.horizontal, VeetSpace.s2)
            .padding(.vertical, VeetSpace.s1)
            .background(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .fill(AppTheme.surface)
                    .allowsHitTesting(false)
            )
            .overlay(
                RoundedRectangle(cornerRadius: radius, style: .continuous)
                    .stroke(AppTheme.hairlineStrong)
                    .allowsHitTesting(false)
            )
    }
}

extension Text {
    /// A field's example or hint, in faint ink as the design system draws
    /// every placeholder: a plain field's own placeholder grey is nearly as
    /// strong as typed text. Verbatim, so an example address is not read as
    /// Markdown and drawn as a blue link.
    static func a2Prompt(_ text: String) -> Text {
        Text(verbatim: text).foregroundColor(AppTheme.inkFaint)
    }

    /// The prompt of a field in a `Form`, which on touch shows its title as
    /// the placeholder but on the Mac sets it as a label beside the field,
    /// where repeating it inside the field would say it twice.
    static func a2FormPrompt(_ text: String) -> Text? {
        #if os(macOS)
        nil
        #else
        a2Prompt(text)
        #endif
    }
}

extension View {
    /// Draws the view as an A2 field: surface colour, strong hairline, small radius.
    func a2Field(radius: CGFloat = A2Metrics.fieldRadius) -> some View {
        modifier(A2FieldModifier(radius: radius))
    }

    /// The filled teal capsule for a button that sits in scrolling content.
    /// On the Mac it is the system's prominent button in a capsule: on macOS
    /// 26 a custom-styled button that a scroll brought on screen reports a
    /// stale frame to accessibility, so a click lands beside it. Touch keeps
    /// the custom capsule.
    func primaryCapsule(expands: Bool = false) -> some View {
        modifier(PrimaryCapsuleModifier(expands: expands))
    }
}

/// `primaryCapsule()`: the platform's prominent button on the Mac, the
/// custom capsule elsewhere, both rounded rectangles at accessibility sizes.
private struct PrimaryCapsuleModifier: ViewModifier {
    let expands: Bool
    @Environment(\.dynamicTypeSize) private var typeSize

    func body(content: Content) -> some View {
        #if os(macOS)
        content
            .buttonStyle(.borderedProminent)
            .buttonBorderShape(typeSize.isAccessibilitySize ? .roundedRectangle : .capsule)
            .tint(AppTheme.tealFill)
            .controlSize(.large)
        #else
        content.buttonStyle(PrimaryCapsuleButtonStyle(expands: expands))
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
            .background(Circle().fill(AppTheme.tealWash).padding(-VeetSpace.s1))
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
        .veetType(.meta)
        .foregroundColor(AppTheme.amberText)
        .accessibilityIdentifier("chat.waiting")
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
            .veetType(.label)
            .foregroundColor(AppTheme.inkSoft)
            .accessibilityAddTraits(.isHeader)
    }
}

/// A short uppercase label naming a row's kind.
struct A2KindLabel: View {
    let text: String

    var body: some View {
        Text(text)
            .veetType(.label)
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
            Text(title).veetType(.display)
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
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
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
                if index > 0 { HairlineDivider().padding(.leading, VeetSpace.s4) }
                HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s3) {
                    Text(row.key)
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkSoft)
                        .frame(minWidth: 96, alignment: .leading)
                    Spacer(minLength: 0)
                    Text(row.value)
                        .veetType(.meta)
                        .foregroundColor(AppTheme.ink)
                        .multilineTextAlignment(.trailing)
                        .textSelection(.enabled)
                }
                .padding(.horizontal, VeetSpace.s4)
                .padding(.vertical, VeetSpace.s2)
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
            VStack(alignment: .leading, spacing: VeetSpace.s1) {
                content()
                if let cite {
                    Text(cite).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                }
            }
            .padding(.horizontal, VeetSpace.s3)
            .padding(.vertical, VeetSpace.s2)
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
        VStack(spacing: VeetSpace.s3) {
            Image(systemName: systemImage)
                .veetIcon(.emptyState)
                .foregroundColor(AppTheme.tealText)
                .frame(width: 60, height: 60)
                .background(Circle().fill(AppTheme.tealWash))
                .accessibilityHidden(true)
            Text(title)
                .veetType(.ui, weight: .medium)
                .foregroundColor(AppTheme.ink)
                .multilineTextAlignment(.center)
            if let message {
                Text(message)
                    .veetType(.ui)
                    .foregroundColor(AppTheme.inkSoft)
                    .multilineTextAlignment(.center)
            }
            actions()
        }
        .frame(maxWidth: 360)
        .fixedSize(horizontal: false, vertical: true)
        .padding(VeetSpace.s5)
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

/// Shared navigation dimensions. Window constraints affect the displayed width,
/// never the owner's saved preference.
enum ModeSidebarLayout {
    static let bounds: ClosedRange<CGFloat> = 300...420
    static let defaultWidth: CGFloat = 320
    static let dividerWidth: CGFloat = 12
    static let minimumSplitWidth: CGFloat = bounds.lowerBound + 360 + dividerWidth

    static func clamped(_ width: CGFloat) -> CGFloat {
        guard width.isFinite else { return defaultWidth }
        return min(max(width, bounds.lowerBound), bounds.upperBound)
    }

    static func preference(saved: CGFloat, displayed: CGFloat, proposed: CGFloat, available: CGFloat) -> CGFloat {
        let adjusted = shown(proposed, available: available)
        return adjusted == displayed ? saved : adjusted
    }

    static func shown(_ width: CGFloat, available: CGFloat) -> CGFloat {
        min(clamped(width), max(bounds.lowerBound, available - 360 - dividerWidth))
    }
}

#if os(iOS)
/// Only preferences live here; conversation and mail state remain in their models.
private enum TabletSidebarPreferences {
    static let defaults: UserDefaults = {
        #if DEBUG && !SWIFT_PACKAGE
        if ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.launchArgument) {
            let name = "com.veetbot.apple.ui-tests.tablet-sidebar"
            let defaults = UserDefaults(suiteName: name)!
            if ProcessInfo.processInfo.environment["VEETBOT_UI_TEST_KEEP_TABLET_WIDTH"] != "1" {
                defaults.removePersistentDomain(forName: name)
            }
            return defaults
        }
        #endif
        return .standard
    }()
}

/// Regular-width iPad chrome. Its detail keeps a useful reading width, while
/// compact windows continue to use the existing navigation stacks and drawer.
struct TabletSidebarSplit<Sidebar: View, Detail: View>: View {
    let mode: ClientMode
    @ViewBuilder let sidebar: () -> Sidebar
    @ViewBuilder let detail: () -> Detail
    @AppStorage("veetbot.tablet.sidebarWidth", store: TabletSidebarPreferences.defaults)
    private var savedWidth: Double = Double(ModeSidebarLayout.defaultWidth)
    @State private var dragWidth: CGFloat?
    @State private var dragStart: CGFloat?

    var body: some View {
        GeometryReader { geometry in
            let width = ModeSidebarLayout.shown(dragWidth ?? CGFloat(savedWidth), available: geometry.size.width)
            HStack(spacing: 0) {
                sidebar().frame(width: width).frame(maxHeight: .infinity)
                divider(width: width, available: geometry.size.width)
                detail().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func divider(width: CGFloat, available: CGFloat) -> some View {
        Color.clear
            .overlay { Rectangle().fill(AppTheme.paperDeep).frame(width: ModeSidebarLayout.dividerWidth) }
            .overlay { Rectangle().fill(AppTheme.hairline).frame(width: 1) }
            .overlay { Capsule().fill(AppTheme.inkFaint).frame(width: 3, height: 32) }
            .frame(width: 44)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 3, coordinateSpace: .global)
                .onChanged { value in
                    if dragStart == nil { dragStart = width }
                    dragWidth = ModeSidebarLayout.shown((dragStart ?? width) + value.translation.width, available: available)
                }
                .onEnded { _ in
                    if let dragWidth {
                        savedWidth = Double(ModeSidebarLayout.preference(
                            saved: CGFloat(savedWidth), displayed: dragStart ?? width,
                            proposed: dragWidth, available: available))
                    }
                    dragStart = nil
                    dragWidth = nil
                })
            .simultaneousGesture(TapGesture(count: 2).onEnded {
                savedWidth = Double(ModeSidebarLayout.defaultWidth)
            })
            .padding(.horizontal, (ModeSidebarLayout.dividerWidth - 44) / 2)
            .zIndex(1)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Sidebar width")
            .accessibilityValue("\(Int(width)) points")
            .accessibilityHint("Swipe up or down to resize. Double tap to reset.")
            .accessibilityAdjustableAction { direction in
                let delta: CGFloat = direction == .increment ? 20 : -20
                savedWidth = Double(ModeSidebarLayout.preference(
                    saved: CGFloat(savedWidth), displayed: width,
                    proposed: width + delta, available: available))
            }
            .accessibilityAction { savedWidth = Double(ModeSidebarLayout.defaultWidth) }
            .accessibilityIdentifier("\(mode.rawValue).sidebar.resize")
    }
}

struct ClientUtilities {
    var memory: () -> Void = {}
    var people: () -> Void = {}
    var schedules: () -> Void = {}
    var persona: () -> Void = {}
    var settings: () -> Void = {}
}

private struct ClientUtilitiesKey: EnvironmentKey {
    static let defaultValue = ClientUtilities()
}

extension EnvironmentValues {
    var clientUtilities: ClientUtilities {
        get { self[ClientUtilitiesKey.self] }
        set { self[ClientUtilitiesKey.self] = newValue }
    }
}

/// The same top-right utilities in every regular-width iPad mode.
struct ClientUtilitiesMenu: View {
    let identifier: String
    let mode: ClientMode
    @Environment(\.activeClientMode) private var activeMode
    @Environment(\.clientUtilities) private var actions

    var body: some View {
        if activeMode == mode {
            Menu {
                Button("Memory", systemImage: "brain.head.profile", action: actions.memory)
                Button("People", systemImage: "person.2", action: actions.people)
                Button("Schedules", systemImage: "calendar", action: actions.schedules)
                Button("Persona", systemImage: "sparkles", action: actions.persona)
                Button("Settings", systemImage: "gearshape", action: actions.settings)
            } label: {
                Image(systemName: "ellipsis.circle")
                    .foregroundColor(AppTheme.inkSoft)
                    .frame(minWidth: 44, minHeight: 44)
            }
            .accessibilityLabel("More")
            .accessibilityIdentifier(identifier)
        }
    }
}
#endif
