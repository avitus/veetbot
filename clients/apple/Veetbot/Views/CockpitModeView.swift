import SwiftUI

/// Cockpit mode (docs/plan/cockpit.md, "The native experience"; MagicPath
/// merrily-city-4157). On the Mac its left pane leads with the mode switch,
/// as every mode's does. No server offers Cockpit's routes yet, so the board
/// shows the native unavailable state; the web view, the stream and the
/// board's chrome arrive with them (build step 10).
struct CockpitModeView: View {
    var chooseMode: (ClientMode) -> Void = { _ in }

    var body: some View {
        #if os(macOS)
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: VeetSpace.s2) {
                ModeSegment(selected: .cockpit, choose: chooseMode)
                    .frame(maxWidth: .infinity)
                SidebarSectionLabel(title: "Widgets")
                Text("Your board's widgets appear here once this server offers Cockpit.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkFaint)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, VeetSpace.s2)
                Spacer(minLength: 0)
            }
            .padding(.horizontal, A2Metrics.sidebarGutter)
            .padding(.top, VeetSpace.s3)
            .frame(width: A2Metrics.sidebarWidth)
            .frame(maxHeight: .infinity)
            .background(AppTheme.paperDeep)
            Rectangle().fill(AppTheme.hairline).frame(width: 1).accessibilityHidden(true)
            unavailable
        }
        #else
        unavailable
        #endif
    }

    /// The board for a server without Cockpit's routes (gate 15: off means absent).
    private var unavailable: some View {
        VStack(spacing: VeetSpace.s2) {
            Image(systemName: ClientMode.cockpit.symbol)
                .veetIcon(.large)
                .foregroundColor(AppTheme.inkFaint)
                .accessibilityHidden(true)
            Text("Cockpit isn't available yet").veetType(.title)
            Text("This server doesn't offer Cockpit. When it does, the widgets on your board appear here, live.")
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
        .accessibilityIdentifier("cockpit.unavailable")
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
