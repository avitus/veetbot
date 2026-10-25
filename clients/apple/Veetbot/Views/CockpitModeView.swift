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
    @AppStorage("cockpit.nightWatch") private var nightWatch = false
    @AppStorage("cockpit.keepAwake") private var keepAwake = false
    @Environment(\.colorScheme) private var colorScheme
    @State private var arranging = false

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
            .sheet(isPresented: $arranging) {
                CockpitArrangeView(model: model, deviceClass: CockpitDevice.deviceClass)
            }
    }

    @ViewBuilder
    private var content: some View {
        #if os(macOS)
        HStack(spacing: 0) {
            sidebar
            Rectangle().fill(AppTheme.hairline).frame(width: 1).accessibilityHidden(true)
            board
            if let slug = model.selectedSlug, model.availability == .available {
                Rectangle().fill(AppTheme.hairline).frame(width: 1).accessibilityHidden(true)
                CockpitWidgetDetailView(model: model, slug: slug, close: model.closeDetail)
                    .frame(width: 340)
                    .background(AppTheme.paper)
            }
        }
        #else
        VStack(spacing: 0) {
            statusBar
            Rectangle().fill(AppTheme.hairline).frame(height: 1).accessibilityHidden(true)
            board
        }
        .sheet(item: selectionBinding) { selection in
            NavigationStack {
                CockpitWidgetDetailView(model: model, slug: selection.slug, close: model.closeDetail)
            }
            .presentationDetents([.medium, .large])
        }
        #endif
    }

    #if os(macOS)
    private var sidebar: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            ModeSegment(selected: .cockpit, choose: chooseMode)
                .frame(maxWidth: .infinity)
            SidebarSectionLabel(title: "Widgets")
            if model.availability == .available, let snapshot = model.snapshot {
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(CockpitBoard.placements(snapshot, deviceClass: CockpitDevice.deviceClass), id: \.widget.slug) { placed in
                            widgetRow(placed.widget, snapshot: snapshot)
                        }
                    }
                }
                Spacer(minLength: 0)
                sidebarControls
            } else {
                Text("Your board's widgets appear here once this server offers Cockpit.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, VeetSpace.s2)
                Spacer(minLength: 0)
            }
        }
        .padding(.horizontal, A2Metrics.sidebarGutter)
        .padding(.vertical, VeetSpace.s3)
        .frame(width: A2Metrics.sidebarWidth)
        .frame(maxHeight: .infinity)
        .background(AppTheme.paperDeep)
    }

    private func widgetRow(_ widget: CockpitWidget, snapshot: CockpitSnapshot) -> some View {
        let selected = model.selectedSlug == widget.slug
        return Button {
            Task { await model.openDetail(widget.slug) }
        } label: {
            HStack(spacing: VeetSpace.s2) {
                CockpitFreshnessDot(level: CockpitDevice.freshness(of: widget, in: snapshot))
                Text(widget.title).veetType(.ui).foregroundColor(AppTheme.ink).lineLimit(1)
                Spacer(minLength: 0)
                if model.quarantined.contains(widget.slug) {
                    Text("Paused").veetType(.meta).foregroundColor(AppTheme.amberText)
                }
            }
            .padding(.horizontal, VeetSpace.s2)
            .frame(minHeight: A2Metrics.rowHeight)
            .background(
                RoundedRectangle(cornerRadius: A2Metrics.rowRadius, style: .continuous)
                    .fill(selected ? AppTheme.selected : Color.clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityIdentifier("cockpit.widget.\(widget.slug)")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var sidebarControls: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            connectionLine
            HStack {
                Toggle("Night watch", isOn: $nightWatch)
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .accessibilityIdentifier("cockpit.night")
                Spacer()
                Button("Arrange") { arranging = true }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .accessibilityIdentifier("cockpit.arrange")
            }
        }
    }
    #else
    private var statusBar: some View {
        HStack(spacing: VeetSpace.s3) {
            connectionLine
            Spacer(minLength: 0)
            if model.availability == .available {
                Menu {
                    Toggle("Night watch", isOn: $nightWatch)
                    if CockpitDevice.platform == "ipad" {
                        Toggle("Keep screen on", isOn: $keepAwake)
                    }
                    Button("Arrange board") { arranging = true }
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
                            excluded: model.quarantined, selected: model.selectedSlug
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

    static func age(from date: Date, to now: Date) -> String {
        let seconds = max(0, now.timeIntervalSince(date))
        if seconds < 60 { return "just now" }
        if seconds < 3600 { return "\(Int(seconds / 60)) min ago" }
        if seconds < 86_400 { return "\(Int(seconds / 3600)) h ago" }
        return "\(Int(seconds / 86_400)) d ago"
    }
}

#if os(macOS)
/// The Mac window's title while Cockpit is showing.
struct CockpitToolbarTitle: View {
    var body: some View {
        Text("Cockpit").fontWeight(.semibold).veetType(.ui).lineLimit(1)
            .accessibilityIdentifier("cockpit.toolbar-title")
    }
}
#endif
