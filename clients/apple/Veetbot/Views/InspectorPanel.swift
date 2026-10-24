import SwiftUI

#if os(macOS)
import AppKit

/// The Mac right panel (ADR-0237; MagicPath merry-moon-8243 and
/// clever-village-5022): a column on `paperDeep` at the trailing edge of a
/// mode, behind a hairline the owner can drag. Its sizes and choices live in
/// `InspectorModel`; `InspectorLayoutStore` keeps them on this Mac.
extension View {
    func macInspector<Panel: View>(
        _ inspector: InspectorModel, mode: ClientMode, @ViewBuilder panel: @escaping () -> Panel
    ) -> some View {
        InspectorColumn(inspector: inspector, mode: mode, content: self, panel: panel)
    }
}

private struct InspectorColumn<Content: View, Panel: View>: View {
    @ObservedObject var inspector: InspectorModel
    let mode: ClientMode
    let content: Content
    let panel: () -> Panel

    var body: some View {
        HStack(spacing: 0) {
            content
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            if inspector.isOpen(mode) {
                InspectorResizeHandle(inspector: inspector, mode: mode)
                panel()
                    .frame(width: inspector.width(mode))
                    .frame(maxHeight: .infinity)
                    .background(AppTheme.paperDeep)
                    .accessibilityElement(children: .contain)
                    .accessibilityLabel(panelLabel)
                    .accessibilityIdentifier("inspector.\(mode.rawValue)")
            }
        }
    }

    private var panelLabel: String {
        switch mode {
        case .cockpit: "Board panel"
        case .chat: "Conversation panel"
        case .email: "Thread panel"
        }
    }
}

/// The hairline between a mode and its panel. Dragging it moves the panel's
/// edge live; the width is remembered when the drag ends.
private struct InspectorResizeHandle: View {
    @ObservedObject var inspector: InspectorModel
    let mode: ClientMode

    var body: some View {
        MacColumnDivider(
            edge: .leading, width: inspector.width(mode), label: "Panel width",
            identifier: "inspector.resize",
            resize: { inspector.setWidth($0, for: mode, remember: $1) },
            reset: { inspector.setWidth(InspectorLayout.defaultWidth(mode), for: mode, remember: true) }
        )
    }
}

/// A column's draggable hairline (MagicPath cozy-evening-6871): teal while
/// hovered or dragged, the arrow keys of VoiceOver's adjust action step it,
/// and a double-click returns the column to its starting width. `edge` is the
/// side of the column the hairline sits on, so a trailing hairline widens its
/// column as it moves right and a leading one as it moves left. `resize`
/// receives each live width, then the final one with `remember` set.
struct MacColumnDivider: View {
    enum Edge { case leading, trailing }

    let edge: Edge
    let width: CGFloat
    let label: String
    let identifier: String
    let resize: (_ width: CGFloat, _ remember: Bool) -> Void
    let reset: () -> Void
    @State private var dragStart: CGFloat?
    @State private var hovering = false

    private var sign: CGFloat { edge == .trailing ? 1 : -1 }

    var body: some View {
        Rectangle()
            .fill(hovering || dragStart != nil ? AppTheme.teal : AppTheme.hairline)
            .frame(width: 1)
            .frame(maxHeight: .infinity)
            .overlay {
                Color.clear
                    .frame(width: VeetSpace.s2)
                    .contentShape(Rectangle())
                    .onHover { inside in
                        guard inside != hovering else { return }
                        hovering = inside
                        if inside { NSCursor.resizeLeftRight.push() } else { NSCursor.pop() }
                    }
                    .gesture(
                        DragGesture(minimumDistance: 1, coordinateSpace: .global)
                            .onChanged { value in
                                let start = dragStart ?? width
                                dragStart = start
                                resize(start + sign * value.translation.width, false)
                            }
                            .onEnded { value in
                                let start = dragStart ?? width
                                resize(start + sign * value.translation.width, true)
                                dragStart = nil
                            }
                    )
                    .simultaneousGesture(TapGesture(count: 2).onEnded { reset() })
            }
            .accessibilityElement()
            .accessibilityLabel(label)
            .accessibilityValue("\(Int(width)) points")
            .accessibilityAdjustableAction { direction in
                let step: CGFloat = direction == .increment ? VeetSpace.s5 : -VeetSpace.s5
                resize(width + step, true)
            }
            .accessibilityIdentifier(identifier)
    }
}

/// Hands the transcript and the thread their way into the panel. Only this
/// modifier observes the model, so a reveal redraws what reads it, not the root.
struct InspectorRevealEnvironment: ViewModifier {
    @ObservedObject var inspector: InspectorModel

    func body(content: Content) -> some View {
        content.environment(\.inspectorReveal, InspectorReveal(inspector))
    }
}

/// The window toolbar's panel button. Only the active mode contributes one,
/// and it acts on that mode's panel.
struct InspectorToggleButton: View {
    @ObservedObject var inspector: InspectorModel
    let mode: ClientMode

    var body: some View {
        let open = inspector.isOpen(mode)
        Button { inspector.toggle(mode) } label: {
            Image(systemName: "sidebar.trailing")
                .foregroundColor(open ? AppTheme.ink : AppTheme.inkSoft)
        }
        .keyboardShortcut("i", modifiers: [.command, .option])
        .help(open ? "Hide panel (⌥⌘I)" : "Show panel (⌥⌘I)")
        .accessibilityLabel(open ? "Hide panel" : "Show panel")
        .accessibilityValue(open ? "Shown" : "Hidden")
        .accessibilityIdentifier("inspector.toggle")
    }
}

/// The panel's top: its sections as a segmented control, and a way to hide it.
struct InspectorHeader<Tab: Hashable>: View {
    @Binding var selection: Tab
    let tabs: [(tab: Tab, title: String)]
    let hide: () -> Void

    var body: some View {
        HStack(spacing: VeetSpace.s2) {
            Picker("Panel section", selection: $selection) {
                ForEach(tabs, id: \.tab) { item in
                    Text(item.title).tag(item.tab)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .accessibilityIdentifier("inspector.tabs")
            Button(action: hide) {
                Image(systemName: "sidebar.trailing").veetIcon(.regular)
            }
            .buttonStyle(QuietIconButtonStyle())
            .help("Hide panel (⌥⌘I)")
            .accessibilityLabel("Hide panel")
            .accessibilityIdentifier("inspector.hide")
        }
        .padding(.horizontal, VeetSpace.s3)
        .padding(.top, VeetSpace.s3)
        .padding(.bottom, VeetSpace.s2)
    }
}

/// A labelled part of the panel that folds to its label.
struct InspectorSection<Content: View>: View {
    let title: String
    var trailing: String? = nil
    @ViewBuilder let content: () -> Content
    @State private var expanded = true

    var body: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            Button {
                expanded.toggle()
            } label: {
                HStack(spacing: VeetSpace.s1) {
                    Text(title).veetType(.label)
                    Image(systemName: "chevron.down")
                        .veetIcon(.small, weight: .semibold)
                        .rotationEffect(.degrees(expanded ? 0 : -90))
                    Spacer(minLength: VeetSpace.s2)
                    if let trailing {
                        Text(trailing).veetType(.meta).monospacedDigit()
                    }
                }
                .foregroundColor(AppTheme.inkFaint)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            .accessibilityAddTraits(.isHeader)
            if expanded {
                content()
            }
        }
    }
}

extension View {
    /// A card on the panel: the surface colour behind a hairline, or an
    /// amber line when it needs the owner.
    func inspectorCard(border: Color = AppTheme.hairline) -> some View {
        padding(VeetSpace.s3)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: A2Metrics.tableRadius, style: .continuous).fill(AppTheme.surface))
            .overlay(RoundedRectangle(cornerRadius: A2Metrics.tableRadius, style: .continuous).stroke(border))
    }
}

/// The Now card: what the run is doing, with a few facts beneath a hairline.
struct InspectorRunCard: View {
    let summary: InspectorRunSummary
    var facts: [(label: String, value: String)] = []

    var body: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s1) {
            HStack(spacing: VeetSpace.s2) {
                Circle().fill(color).frame(width: 7, height: 7).accessibilityHidden(true)
                Text(summary.title).veetType(.ui, weight: .semibold).foregroundColor(textColor)
            }
            if let detail = summary.detail, !detail.isEmpty {
                Text(detail)
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !facts.isEmpty {
                HairlineDivider().padding(.vertical, VeetSpace.s1)
                HStack(alignment: .top, spacing: VeetSpace.s4) {
                    ForEach(facts, id: \.label) { fact in
                        VStack(alignment: .leading, spacing: 0) {
                            Text(fact.label).foregroundColor(AppTheme.inkFaint)
                            Text(fact.value).foregroundColor(AppTheme.ink).monospacedDigit()
                        }
                        .veetType(.meta)
                    }
                }
            }
        }
        .inspectorCard(border: summary.tone == .attention ? AppTheme.amberLine : AppTheme.hairline)
        .accessibilityElement(children: .combine)
    }

    private var color: Color {
        switch summary.tone {
        case .attention: AppTheme.amber
        case .active, .done: AppTheme.teal
        case .failed: AppTheme.red
        case .idle: AppTheme.inkFaint
        }
    }

    private var textColor: Color {
        switch summary.tone {
        case .attention: AppTheme.amberText
        case .active: AppTheme.tealText
        case .failed: AppTheme.red
        case .done, .idle: AppTheme.ink
        }
    }
}

/// What a panel tab says when it has nothing to show yet.
struct InspectorEmptyState: View {
    let symbol: String
    let title: String
    let message: String

    var body: some View {
        VStack(spacing: VeetSpace.s2) {
            Image(systemName: symbol)
                .veetIcon(.large)
                .foregroundColor(AppTheme.inkFaint)
                .accessibilityHidden(true)
            Text(title).veetType(.ui, weight: .medium).foregroundColor(AppTheme.inkSoft)
            Text(message)
                .veetType(.meta)
                .foregroundColor(AppTheme.inkFaint)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, VeetSpace.s4)
        .padding(.vertical, VeetSpace.s7)
        .frame(maxWidth: .infinity)
    }
}
#endif
