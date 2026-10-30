import SwiftUI
#if canImport(UIKit)
import UIKit
#endif

/// Cockpit mode (docs/plan/cockpit.md, "The native experience"; MagicPath
/// merrily-city-4157). Native chrome around one web view: connection and
/// freshness, the last update, night watch, the arrange sheet, and an
/// unavailable state for a server without the routes. On the Mac the left pane
/// leads with the mode switch and lists the board's widgets, and a selected
/// widget's details open in the right panel; on the iPad and iPhone they open
/// as a sheet. The stream runs only while Cockpit is visible in an active app.
struct CockpitModeView: View {
    @ObservedObject var model: CockpitViewModel
    var isVisible: Bool = false
    var chooseMode: (ClientMode) -> Void = { _ in }
    /// What needs the owner, as the other modes' sidebars list it.
    var attention: [SidebarAttention.Row] = []
    var openAttention: (UUID) -> Void = { _ in }
    /// A widget's Discuss in Chat, by its title.
    var discussWidget: (String) -> Void = { _ in }
    @AppStorage("cockpit.nightWatch") private var nightWatch = false
    @AppStorage("cockpit.keepAwake") private var keepAwake = false
    @Environment(\.colorScheme) private var colorScheme
    #if os(iOS)
    @Environment(\.horizontalSizeClass) private var sizeClass
    #endif
    #if os(macOS)
    var sidebarStore = MacColumnWidthStore.cockpitSidebar()
    var panelStore = MacColumnWidthStore.cockpitPanel()
    /// Live widths while a hairline is dragged; nil reads the store.
    @State private var draggedSidebar: CGFloat?
    @State private var draggedPanel: CGFloat?
    #endif

    var body: some View {
        content
            .onAppear { model.setVisible(isVisible) }
            .onChange(of: isVisible) { _, visible in
                model.setVisible(visible)
                applyKeepAwake()
            }
            .onChange(of: keepAwake) { _, _ in applyKeepAwake() }
            .onDisappear {
                model.setVisible(false)
                #if os(iOS)
                UIApplication.shared.isIdleTimerDisabled = false
                #endif
            }
            .sheet(isPresented: $model.isArranging) {
                CockpitArrangeView(model: model, deviceClass: CockpitDevice.deviceClass)
            }
    }

    @ViewBuilder
    private var content: some View {
        #if os(macOS)
        // The design's two side columns each take the width the owner drags
        // them to, kept on this Mac; the board takes the rest.
        let sidebarWidth = draggedSidebar ?? sidebarStore.width
        let panelWidth = draggedPanel ?? panelStore.width
        HStack(spacing: 0) {
            sidebar
                .frame(width: sidebarWidth)
                .frame(maxHeight: .infinity)
            MacColumnDivider(
                edge: .trailing, width: sidebarWidth, label: "Sidebar width", identifier: "cockpit.sidebar.resize",
                resize: { width, remember in
                    if remember {
                        sidebarStore.remember(width)
                        draggedSidebar = nil
                    } else {
                        draggedSidebar = sidebarStore.clamped(width)
                    }
                },
                reset: {
                    sidebarStore.forget()
                    draggedSidebar = nil
                }
            )
            // The grab area reaches into both neighbours, and a later sibling is
            // hit-tested first: without this the board (a web view) takes every
            // press on its side of the hairline, as Email's inbox did.
            .zIndex(1)
            board
            if let slug = model.selectedSlug, model.availability == .available {
                MacColumnDivider(
                    edge: .leading, width: panelWidth, label: "Panel width", identifier: "cockpit.panel.resize",
                    resize: { width, remember in
                        if remember {
                            panelStore.remember(width)
                            draggedPanel = nil
                        } else {
                            draggedPanel = panelStore.clamped(width)
                        }
                    },
                    reset: {
                        panelStore.forget()
                        draggedPanel = nil
                    }
                )
                .zIndex(1)
                CockpitWidgetDetailView(model: model, slug: slug, close: model.closeDetail, discuss: discussWidget)
                    .frame(width: panelWidth)
                    .background(AppTheme.paperDeep)
            }
        }
        #else
        Group {
            if sizeClass == .regular {
                TabletSidebarSplit(mode: .cockpit) {
                    sidebar
                } detail: {
                    tabletBoard
                }
            } else {
                tabletBoard
            }
        }
        .sheet(item: selectionBinding) { selection in
            NavigationStack {
                CockpitWidgetDetailView(model: model, slug: selection.slug, close: model.closeDetail, discuss: discussWidget)
            }
            .presentationDetents([.medium, .large])
        }
        #endif
    }

    /// The left pane of "Mac — Cockpit · A2": the mode switch, then Needs you,
    /// the board's widgets with each one's freshness, and This screen.
    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            ModeSegment(selected: .cockpit, choose: chooseMode)
                .frame(maxWidth: .infinity)
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    if !attention.isEmpty {
                        SidebarSectionLabel(title: "Needs you")
                        ForEach(attention) { row in
                            Button { openAttention(row.entry.sessionID) } label: {
                                SidebarRow { AttentionDot(kind: row.kind) } title: {
                                    Text("\(Text(row.kind == .approval ? "Approval" : "Report").fontWeight(.semibold))\(Text(" · \(row.entry.title)").foregroundColor(AppTheme.inkSoft))")
                                }
                                .padding(.horizontal, VeetSpace.s2)
                            }
                            .buttonStyle(.plain)
                            .accessibilityLabel("\(row.label), \(row.entry.title)")
                            .accessibilityIdentifier("cockpit.attention.\(row.id)")
                        }
                    }
                    SidebarSectionLabel(title: "Widgets")
                    if model.availability == .available, let snapshot = model.snapshot {
                        TimelineView(.periodic(from: .now, by: 5)) { context in
                            VStack(alignment: .leading, spacing: 0) {
                                ForEach(CockpitBoard.placements(snapshot, deviceClass: CockpitDevice.deviceClass), id: \.widget.slug) { placed in
                                    widgetRow(placed.widget, snapshot: snapshot, now: context.date)
                                }
                            }
                        }
                    } else {
                        Text("Your board's widgets appear here once this server offers Cockpit.")
                            .veetType(.meta)
                            .foregroundColor(AppTheme.inkFaint)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.horizontal, VeetSpace.s2)
                    }
                    SidebarSectionLabel(title: "This screen")
                    Text("\(CockpitDevice.platform == "mac" ? "Mac" : "iPad") layout · Night watch \(nightWatch ? "on" : "off")")
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkFaint)
                        .padding(.horizontal, VeetSpace.s2)
                        .accessibilityIdentifier("cockpit.this-screen")
                }
                .padding(.bottom, VeetSpace.s4)
            }
        }
        .padding(.horizontal, A2Metrics.sidebarGutter)
        .padding(.top, VeetSpace.s3)
        .background(AppTheme.paperDeep)
    }

    private func widgetRow(_ widget: CockpitWidget, snapshot: CockpitSnapshot, now: Date) -> some View {
        let selected = model.selectedSlug == widget.slug
        let level = CockpitDevice.freshness(of: widget, in: snapshot, now: now)
        return Button {
            Task { await model.openDetail(widget.slug) }
        } label: {
            SidebarRow {
                CockpitFreshnessDot(level: level)
            } title: {
                Text(widget.title).fontWeight(selected ? .medium : .regular)
            } trailing: { _ in
                if model.quarantined.contains(widget.slug) {
                    Text("Paused").foregroundColor(AppTheme.amberText)
                } else {
                    Text(CockpitDevice.freshnessText(level, age: CockpitDevice.age(of: widget, in: snapshot, now: now)))
                        .foregroundColor(level == .error ? AppTheme.red : level == .stale ? AppTheme.inkSoft : AppTheme.inkFaint)
                }
            }
            .padding(.horizontal, VeetSpace.s2)
            .background(
                RoundedRectangle(cornerRadius: A2Metrics.rowRadius, style: .continuous)
                    .fill(selected ? AppTheme.selected : Color.clear)
            )
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("cockpit.widget.\(widget.slug)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
    #if os(iOS)
    private var tabletBoard: some View {
        VStack(spacing: 0) {
            statusBar
            Rectangle().fill(AppTheme.hairline).frame(height: 1).accessibilityHidden(true)
            board
        }
    }

    private var statusBar: some View {
        HStack(spacing: VeetSpace.s3) {
            connectionLine
            Spacer(minLength: 0)
            if sizeClass == .regular { ClientUtilitiesMenu(identifier: "cockpit.more", mode: .cockpit) }
            if model.availability == .available {
                Menu {
                    Toggle("Night watch", isOn: $nightWatch)
                    if CockpitDevice.platform == "ipad" {
                        Toggle("Keep screen on", isOn: $keepAwake)
                    }
                    Button("Arrange board") { model.isArranging = true }
                } label: {
                    Image(systemName: "slider.horizontal.3").veetIcon(.regular)
                }
                .accessibilityLabel("Board options")
                .accessibilityIdentifier("cockpit.options")
            }
        }
        .padding(.horizontal, VeetSpace.s4)
        .padding(.vertical, VeetSpace.s2)
        .background(AppTheme.paperDeep)
    }

    private var selectionBinding: Binding<CockpitSelection?> {
        Binding(
            get: { model.selectedSlug.map(CockpitSelection.init(slug:)) },
            set: { if $0 == nil { model.closeDetail() } }
        )
    }
    #endif

    private var connectionLine: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            HStack(spacing: VeetSpace.s1) {
                Circle().fill(connectionColor).frame(width: 7, height: 7).accessibilityHidden(true)
                Text(connectionText(now: context.date)).veetType(.meta).foregroundColor(AppTheme.inkFaint)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("cockpit.connection")
        }
    }

    private var connectionColor: Color {
        switch model.connection {
        case .live: return AppTheme.teal
        case .reconnecting: return AppTheme.amber
        case .idle, .connecting: return AppTheme.inkFaint
        }
    }

    private func connectionText(now: Date) -> String {
        let updated = model.lastUpdated.map { "Updated \(CockpitDevice.age(from: $0, to: now))" }
        switch model.connection {
        case .live: return updated.map { "Live · \($0)" } ?? "Live"
        case .reconnecting: return updated.map { "Reconnecting · \($0)" } ?? "Reconnecting"
        case .connecting: return "Connecting"
        case .idle: return updated ?? "Offline"
        }
    }

    @ViewBuilder
    private var board: some View {
        switch model.availability {
        case .unavailable:
            unavailable
        case .unknown:
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(AppTheme.paper)
                .accessibilityIdentifier("cockpit.loading")
        case .available:
            if let snapshot = model.snapshot, let runtime = snapshot.runtime {
                if snapshot.widgets.isEmpty {
                    emptyBoard
                } else {
                    CockpitWebView(
                        runtime: runtime,
                        allowed: CockpitBoard.allowedAssets(snapshot),
                        message: CockpitBoard.hostSnapshot(
                            snapshot, deviceClass: CockpitDevice.deviceClass, theme: theme,
                            platform: CockpitDevice.platform, offsetMilliseconds: model.offsetMilliseconds,
                            excluded: model.quarantined, selected: model.selectedSlug,
                            columns: boardColumns
                        ),
                        fetch: { [weak model] digest in
                            guard let model else { throw CancellationError() }
                            return try await model.asset(digest)
                        },
                        onMessage: { message in
                            model.hostReported(message)
                            if case .select(let slug) = message { Task { await model.openDetail(slug) } }
                        },
                        onUnresponsive: { model.quarantineNewest() }
                    )
                    .background(AppTheme.paper)
                }
            } else {
                notExported
            }
        }
    }

    /// Three columns beside an open details panel on the Mac (the design's
    /// board); otherwise the platform's own.
    private var boardColumns: Int? {
        #if os(macOS)
        model.selectedSlug == nil ? nil : 3
        #else
        nil
        #endif
    }

    private var theme: String {
        nightWatch ? "night" : (colorScheme == .dark ? "dark" : "light")
    }

    private func applyKeepAwake() {
        #if os(iOS)
        // Only while Cockpit is visible, and only where it is offered.
        UIApplication.shared.isIdleTimerDisabled =
            isVisible && keepAwake && CockpitDevice.platform == "ipad"
        #endif
    }

    /// The board for a server without Cockpit's routes (gate 15: off means absent).
    private var unavailable: some View {
        CockpitMessageView(
            title: "Cockpit isn't available yet",
            detail: "This server doesn't offer Cockpit. When it does, the widgets on your board appear here, live."
        )
        .accessibilityIdentifier("cockpit.unavailable")
    }

    private var emptyBoard: some View {
        CockpitMessageView(
            title: "Your board is empty",
            detail: "Ask in Chat for a widget, such as one that shows the approvals waiting on you."
        )
        .accessibilityIdentifier("cockpit.empty")
    }

    private var notExported: some View {
        CockpitMessageView(
            title: "The board isn't ready on this server",
            detail: "This server offers Cockpit but has not exported its board runtime yet."
        )
        .accessibilityIdentifier("cockpit.runtime-missing")
    }
}

#if !os(macOS)
private struct CockpitSelection: Identifiable {
    let slug: String
    var id: String { slug }
}
#endif

private struct CockpitMessageView: View {
    let title: String
    let detail: String

    var body: some View {
        VStack(spacing: VeetSpace.s2) {
            Image(systemName: ClientMode.cockpit.symbol)
                .veetIcon(.large)
                .foregroundColor(AppTheme.inkFaint)
                .accessibilityHidden(true)
            Text(title).veetType(.title)
            Text(detail)
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: 400)
        .padding(VeetSpace.s5)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(AppTheme.paper)
        .accessibilityElement(children: .combine)
    }
}

/// A feed's freshness as a dot: teal live, faint aging, amber stale, red failed.
struct CockpitFreshnessDot: View {
    let level: CockpitFreshness

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: 8, height: 8)
            .accessibilityLabel(level.rawValue.capitalized)
    }

    private var color: Color {
        switch level {
        case .live: return AppTheme.teal
        case .aging: return AppTheme.inkFaint
        case .stale: return AppTheme.amber
        case .error: return AppTheme.red
        case .pending: return AppTheme.hairlineStrong
        }
    }
}

/// The device's board: its class, the kit's platform name, and display helpers.
enum CockpitDevice {
    static var deviceClass: CockpitDeviceClass {
        switch VeetType.Platform.current {
        case .mac: return .wide
        case .iPad: return .regular
        case .iPhone: return .compact
        }
    }

    static var platform: String {
        switch VeetType.Platform.current {
        case .mac: return "mac"
        case .iPad: return "ipad"
        case .iPhone: return "iphone"
        }
    }

    /// A widget's freshness is its stalest declared feed's.
    static func freshness(of widget: CockpitWidget, in snapshot: CockpitSnapshot, now: Date = Date()) -> CockpitFreshness {
        let order: [CockpitFreshness] = [.live, .aging, .pending, .stale, .error]
        let levels = widget.declaredFeeds.map { feed in
            snapshot.feeds[feed.id]?.current?.freshness(at: now) ?? .pending
        }
        return levels.max { (order.firstIndex(of: $0) ?? 0) < (order.firstIndex(of: $1) ?? 0) } ?? .live
    }

    /// An age as the design writes it: now, 6 s, 2 min, 3 h, 2 d.
    static func shortAge(_ seconds: TimeInterval) -> String {
        let seconds = Int(max(0, seconds))
        if seconds < 5 { return "now" }
        if seconds < 60 { return "\(seconds) s" }
        let minutes = seconds / 60
        if minutes < 60 { return "\(minutes) min" }
        let hours = minutes / 60
        if hours < 48 { return "\(hours) h" }
        return "\(hours / 24) d"
    }

    /// A widget row's trailing text (the design's short freshness).
    static func freshnessText(_ level: CockpitFreshness, age: TimeInterval?) -> String {
        let text = age.map(shortAge) ?? ""
        switch level {
        case .pending: return "Waiting"
        case .error: return "Failed · \(text)"
        case .stale: return "Stale · \(text)"
        case .aging: return "\(text) ago"
        case .live: return text
        }
    }

    /// The toolbar pill: Live, or how long since the board last heard.
    static func pillText(_ connection: CockpitViewModel.Connection, lastUpdated: Date?, now: Date) -> String {
        switch connection {
        case .live: return "Live"
        case .reconnecting:
            return lastUpdated.map { "Reconnecting · last update \(shortAge(now.timeIntervalSince($0))) ago" } ?? "Reconnecting"
        case .connecting: return "Connecting"
        case .idle: return "Offline"
        }
    }

    /// The toolbar summary: what needs the owner (amber), then what is stale (soft).
    static func summary(needYou: Int, stale: Int) -> (String?, String?) {
        let attention = needYou > 0 ? "\(needYou) \(needYou == 1 ? "needs" : "need") you" : nil
        let staleText = stale > 0 ? "\(stale) stale" : nil
        return (attention, staleText.map { attention == nil ? $0 : " · \($0)" })
    }

    /// The age of a widget's stalest declared feed.
    static func age(of widget: CockpitWidget, in snapshot: CockpitSnapshot, now: Date = Date()) -> TimeInterval? {
        widget.declaredFeeds.compactMap { snapshot.feeds[$0.id]?.current?.observedAt }
            .map { now.timeIntervalSince($0) }.max()
    }

    /// How many placed widgets are stale.
    static func staleCount(_ snapshot: CockpitSnapshot, now: Date = Date()) -> Int {
        CockpitBoard.placements(snapshot, deviceClass: deviceClass)
            .filter { freshness(of: $0.widget, in: snapshot, now: now) == .stale }.count
    }

    static func age(from date: Date, to now: Date) -> String {
        let seconds = max(0, now.timeIntervalSince(date))
        if seconds < 60 { return "just now" }
        if seconds < 3600 { return "\(Int(seconds / 60)) min ago" }
        if seconds < 86_400 { return "\(Int(seconds / 3600)) h ago" }
        return "\(Int(seconds / 86_400)) d ago"
    }
}

#if os(macOS)
/// The Mac window's title while Cockpit is showing (MagicPath merrily-city-4157):
/// "Cockpit", the stream's pill, and what needs the owner, then what is stale.
struct CockpitToolbarTitle: View {
    @ObservedObject var model: CockpitViewModel
    var needYou = 0

    var body: some View {
        TimelineView(.periodic(from: .now, by: 5)) { context in
            HStack(spacing: VeetSpace.s3) {
                Text("Cockpit").fontWeight(.semibold).veetType(.ui).lineLimit(1)
                    .accessibilityIdentifier("cockpit.toolbar-title")
                if model.availability == .available {
                    pill(now: context.date)
                    summary(now: context.date)
                }
            }
            // A toolbar item is offered only its ideal width; the pill must not clip.
            .fixedSize()
        }
    }

    private func pill(now: Date) -> some View {
        let live = model.connection == .live
        return HStack(spacing: 6) {
            Circle().fill(live ? AppTheme.tealText : AppTheme.inkSoft).frame(width: 6, height: 6).accessibilityHidden(true)
            Text(CockpitDevice.pillText(model.connection, lastUpdated: model.lastUpdated, now: now))
                .veetType(.meta, weight: .medium)
                .lineLimit(1)
        }
        .foregroundColor(live ? AppTheme.tealText : AppTheme.inkSoft)
        .padding(.horizontal, VeetSpace.s2)
        .frame(height: 22)
        .background(Capsule().fill(live ? AppTheme.tealWash : AppTheme.field))
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("cockpit.connection")
    }

    @ViewBuilder
    private func summary(now: Date) -> some View {
        let stale = model.snapshot.map { CockpitDevice.staleCount($0, now: now) } ?? 0
        let (attention, staleText) = CockpitDevice.summary(needYou: needYou, stale: stale)
        if attention != nil || staleText != nil {
            HStack(spacing: 6) {
                if attention != nil {
                    Circle().fill(AppTheme.amber).frame(width: 7, height: 7).accessibilityHidden(true)
                }
                Text("\(Text(attention ?? "").fontWeight(.semibold).foregroundColor(AppTheme.amberText))\(Text(staleText ?? "").foregroundColor(AppTheme.inkSoft))")
                    .veetType(.meta)
                    .lineLimit(1)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("cockpit.summary")
        }
    }
}

/// Appearance in three steps, as the design's toolbar button cycles it:
/// light, dark, then night watch, then light again.
struct CockpitAppearanceButton: View {
    @EnvironmentObject private var appearance: AppearancePreferences
    @Environment(\.colorScheme) private var colorScheme
    @AppStorage("cockpit.nightWatch") private var nightWatch = false

    private var current: String { nightWatch ? "Night watch" : colorScheme == .dark ? "Dark" : "Light" }

    var body: some View {
        Button {
            if nightWatch {
                nightWatch = false
                appearance.theme = .light
            } else if colorScheme == .dark {
                nightWatch = true
            } else {
                appearance.theme = .dark
            }
        } label: {
            Image(systemName: nightWatch ? "moon.stars" : colorScheme == .dark ? "moon" : "sun.max")
                .foregroundColor(AppTheme.inkSoft)
        }
        .help("Appearance: \(current)")
        .accessibilityLabel("Appearance: \(current)")
        .accessibilityIdentifier("cockpit.appearance")
    }
}

/// The design's Arrange button, which opens the arrange sheet.
struct CockpitArrangeButton: View {
    @ObservedObject var model: CockpitViewModel

    var body: some View {
        Button("Arrange") { model.isArranging = true }
            .buttonStyle(SecondaryCapsuleButtonStyle())
            .disabled(model.availability != .available || model.snapshot == nil)
            .accessibilityIdentifier("cockpit.arrange")
    }
}

/// The details panel's toolbar button: closes an open panel, or opens the
/// first widget's details (⌥⌘I, as the other modes' panels).
struct CockpitPanelToggle: View {
    @ObservedObject var model: CockpitViewModel

    var body: some View {
        let open = model.selectedSlug != nil
        Button {
            if open {
                model.closeDetail()
            } else if let snapshot = model.snapshot,
                      let first = CockpitBoard.placements(snapshot, deviceClass: CockpitDevice.deviceClass).first {
                Task { await model.openDetail(first.widget.slug) }
            }
        } label: {
            Image(systemName: "sidebar.trailing").foregroundColor(open ? AppTheme.ink : AppTheme.inkSoft)
        }
        .keyboardShortcut("i", modifiers: [.command, .option])
        .disabled(model.availability != .available || model.snapshot == nil)
        .help(open ? "Hide details (⌥⌘I)" : "Show details (⌥⌘I)")
        .accessibilityLabel(open ? "Hide details" : "Show details")
        .accessibilityIdentifier("cockpit.panel-toggle")
    }
}
#endif
