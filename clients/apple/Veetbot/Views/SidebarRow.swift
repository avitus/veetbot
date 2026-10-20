import SwiftUI

/// One sidebar row, whatever it holds (design system revision 2, Sidebar
/// anatomy): a fixed icon column, 8, the title in the ui role, and a trailing
/// slot in the meta role. Every title therefore starts at the same x: chats,
/// folders, schedules, history and Needs you rows alike, and a row with no
/// icon keeps the empty column. A nested row (a chat inside an open folder)
/// gives up the icon column and indents by one column plus 12.
///
/// The trailing slot receives whether the pointer is over the row, so a
/// secondary or destructive action can replace the trailing text on hover and
/// is never permanently visible.
struct SidebarRow<Icon: View, Title: View, Trailing: View>: View {
    var nested = false
    @ViewBuilder let icon: () -> Icon
    @ViewBuilder let title: () -> Title
    @ViewBuilder let trailing: (_ hovering: Bool) -> Trailing
    @State private var hovering = false

    var body: some View {
        HStack(spacing: VeetSpace.s2) {
            if nested {
                Color.clear.frame(width: A2Metrics.sidebarIconColumn + VeetSpace.s1)
            } else {
                // The column keeps its width when the row has no icon.
                Color.clear
                    .frame(width: A2Metrics.sidebarIconColumn, height: A2Metrics.sidebarIconColumn)
                    .overlay {
                        icon()
                            .veetIcon(.small, weight: .semibold)
                            .foregroundColor(AppTheme.inkSoft)
                    }
            }
            title()
                .veetType(.ui)
                .foregroundColor(AppTheme.ink)
                .lineLimit(1)
                .frame(maxWidth: .infinity, alignment: .leading)
            trailing(hovering)
                .veetType(.meta)
                .monospacedDigit()
                .foregroundColor(AppTheme.inkFaint)
        }
        .frame(minHeight: A2Metrics.rowHeight)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
    }
}

extension SidebarRow where Trailing == EmptyView {
    init(nested: Bool = false, @ViewBuilder icon: @escaping () -> Icon, @ViewBuilder title: @escaping () -> Title) {
        self.init(nested: nested, icon: icon, title: title, trailing: { _ in EmptyView() })
    }
}

extension SidebarRow where Icon == EmptyView {
    init(
        nested: Bool = false,
        @ViewBuilder title: @escaping () -> Title,
        @ViewBuilder trailing: @escaping (_ hovering: Bool) -> Trailing
    ) {
        self.init(nested: nested, icon: { EmptyView() }, title: title, trailing: trailing)
    }
}

/// A folder's or schedule's disclosure mark, for the icon column.
struct SidebarChevron: View {
    let isExpanded: Bool

    var body: some View {
        Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
            .accessibilityHidden(true)
    }
}

/// A sidebar section label: the label role in faint ink, aligned with the
/// icon column, 16 below the section before it, with any section action (New
/// folder) as a small + at its right. A label that collapses its section
/// shows a chevron on hover, always on touch.
struct SidebarSectionLabel<Action: View>: View {
    let title: String
    var isExpanded: Bool? = nil
    var onToggle: (() -> Void)? = nil
    /// What VoiceOver and the UI tests call a collapsible label's toggle.
    var toggleLabel: String? = nil
    var toggleIdentifier: String? = nil
    @ViewBuilder let action: () -> Action
    @State private var hovering = false

    var body: some View {
        HStack(spacing: VeetSpace.s1) {
            if let onToggle {
                let name = toggleLabel ?? title
                Button(action: onToggle) { label }
                    .buttonStyle(.plain)
                    .accessibilityLabel(name)
                    .accessibilityValue(isExpanded == true ? "Expanded" : "Collapsed")
                    .accessibilityHint("\(isExpanded == true ? "Collapses" : "Expands") \(name.lowercased())")
                    .accessibilityIdentifier(toggleIdentifier ?? "sidebar.section.\(title.lowercased())")
            } else {
                label
            }
            Spacer(minLength: VeetSpace.s2)
            action()
        }
        .padding(.top, VeetSpace.s4)
        .frame(minHeight: A2Metrics.rowHeight - VeetSpace.s2, alignment: .bottom)
        .onHover { hovering = $0 }
    }

    private var label: some View {
        HStack(spacing: VeetSpace.s1) {
            Text(title)
                .veetType(.label)
                .foregroundColor(AppTheme.inkFaint)
                .textCase(nil)
            if let isExpanded {
                Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                    .veetIcon(.small, weight: .semibold)
                    .foregroundColor(AppTheme.inkFaint)
                    .opacity(showsChevron ? 1 : 0)
                    .accessibilityHidden(true)
            }
        }
        .contentShape(Rectangle())
    }

    private var showsChevron: Bool {
        #if os(macOS)
        hovering
        #else
        true
        #endif
    }
}

extension SidebarSectionLabel where Action == EmptyView {
    init(
        title: String,
        isExpanded: Bool? = nil,
        onToggle: (() -> Void)? = nil,
        toggleLabel: String? = nil,
        toggleIdentifier: String? = nil
    ) {
        self.init(
            title: title, isExpanded: isExpanded, onToggle: onToggle,
            toggleLabel: toggleLabel, toggleIdentifier: toggleIdentifier
        ) { EmptyView() }
    }
}

/// The small + beside a section label, such as New folder.
struct SidebarSectionAction: View {
    let systemImage: String
    let label: String
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemImage)
                .veetIcon(.small, weight: .semibold)
                .foregroundColor(AppTheme.inkSoft)
                .frame(width: A2Metrics.sidebarIconColumn, height: A2Metrics.sidebarIconColumn)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(label)
        .accessibilityLabel(label)
    }
}

/// The sidebar's search field: the field well with a magnifier, ui role.
struct SidebarSearchField: View {
    @Binding var text: String

    var body: some View {
        HStack(spacing: VeetSpace.s2) {
            Image(systemName: "magnifyingglass")
                .veetIcon(.small, weight: .semibold)
                .foregroundColor(AppTheme.inkFaint)
                .accessibilityHidden(true)
            TextField("Search", text: $text)
                .textFieldStyle(.plain)
                .veetType(.ui)
                .accessibilityLabel("Search chats")
                .accessibilityIdentifier("sidebar.search")
            if !text.isEmpty {
                Button { text = "" } label: {
                    Image(systemName: "xmark.circle.fill")
                        .veetIcon(.small)
                        .foregroundColor(AppTheme.inkFaint)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear search")
            }
        }
        .padding(.horizontal, VeetSpace.s2)
        .frame(minHeight: A2Metrics.controlHeight)
        .background(
            RoundedRectangle(cornerRadius: A2Metrics.fieldRadius, style: .continuous)
                .fill(AppTheme.field)
                .allowsHitTesting(false)
        )
    }
}

/// New chat: a teal compose icon beside search, never a full-width capsule.
struct ComposeButton<Label: View>: View {
    @ViewBuilder let label: () -> Label

    var body: some View {
        label()
            .veetIcon(.large)
            .foregroundColor(AppTheme.tealText)
            .frame(width: A2Metrics.controlHeight, height: A2Metrics.controlHeight)
            .contentShape(Rectangle())
    }
}

/// A sidebar row's list chrome: the selected row's tint inset by the
/// sidebar gutter with the row radius, the content 8 inside it, and on iOS,
/// where list rows otherwise come white with separators, neither.
private struct SidebarRowModifier: ViewModifier {
    let selected: Bool

    func body(content: Content) -> some View {
        let inset = A2Metrics.sidebarGutter + VeetSpace.s2
        let styled = content
            .listRowInsets(EdgeInsets(top: 0, leading: inset, bottom: 0, trailing: inset))
            .listRowBackground(background)
        #if os(iOS)
        styled.listRowSeparator(.hidden)
        #else
        styled
        #endif
    }

    private var background: some View {
        RoundedRectangle(cornerRadius: A2Metrics.rowRadius, style: .continuous)
            .fill(selected ? AppTheme.selected : Color.clear)
            .padding(.horizontal, A2Metrics.sidebarGutter)
    }
}

extension View {
    func sidebarRow(selected: Bool = false) -> some View {
        modifier(SidebarRowModifier(selected: selected))
    }
}

/// A conversation's sidebar row. The open control (a button, or a link on
/// a pushed list) covers the whole row; a History row's date sits in the
/// trailing slot, and on hover the row's actions replace it.
struct SidebarChatRow<Open: View, Actions: View>: View {
    let nested: Bool
    let hasUnreadReport: Bool
    let title: String
    /// Shown in the trailing slot for History rows.
    let date: Date?
    let open: (AnyView) -> Open
    @ViewBuilder let actions: () -> Actions
    @State private var hovering = false

    var body: some View {
        open(AnyView(label))
            .overlay(alignment: .trailing) {
                if hovering {
                    actions()
                }
            }
            .onHover { hovering = $0 }
    }

    private var label: some View {
        // A nested row has no icon column, so its New report dot trails.
        SidebarRow(nested: nested) {
            if hasUnreadReport, !nested { NewReportIndicator() }
        } title: {
            Text(title)
        } trailing: { _ in
            if hasUnreadReport, nested { NewReportIndicator() }
            if let date, !hovering {
                Text(date, format: .dateTime.month(.abbreviated).day())
            }
        }
    }
}
