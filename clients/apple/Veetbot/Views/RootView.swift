import SwiftUI

#if os(macOS)
import AppKit
#endif

struct VeetbotSceneRoot: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject var appearance: AppearancePreferences

    var body: some View {
        RootView(model: model)
            .environmentObject(appearance)
            .appTypography(appearance)
            .tint(AppTheme.turquoise)
        #if os(macOS)
            .frame(minWidth: 780, minHeight: 560)
        #endif
    }
}

public struct RootView: View {
    @ObservedObject var model: ChatViewModel
    #if !os(macOS)
    @State private var showingSettings = false
    #endif
    @Environment(\.scenePhase) private var scenePhase
    @EnvironmentObject private var appearance: AppearancePreferences
    #if os(macOS)
    @StateObject private var settingsWindowPresenter = SettingsWindowPresenter()
    #endif

    public init(model: ChatViewModel) {
        self.model = model
    }

    public var body: some View {
        Group {
            if model.isConfigured {
                configuredContent
            } else {
                ConnectionSettingsView(model: model, embedded: true)
            }
        }
        #if !os(macOS)
        .sheet(isPresented: $showingSettings) {
            ConnectionSettingsView(model: model, embedded: false)
        }
        #endif
        .onChange(of: model.requiresReauthentication) { required in
            if required { presentSettings() }
        }
        .onChange(of: scenePhase) { phase in
            if phase != .active {
                Task { await model.clearCachedArtifacts() }
            }
        }
        .task(id: model.isConfigured && scenePhase == .active) {
            guard model.isConfigured, scenePhase == .active else { return }
            while !Task.isCancelled {
                await model.synchronizeHistory()
                do {
                    try await Task.sleep(nanoseconds: 30_000_000_000)
                } catch {
                    return
                }
            }
        }
        .alert(
            "Veetbot",
            isPresented: Binding(
                get: { model.errorMessage != nil },
                set: { if !$0 { model.clearError() } }
            )
        ) {
            Button("OK") { model.clearError() }
        } message: {
            Text(model.errorMessage ?? "")
        }
        #if os(macOS)
        .background(MainWindowFrameAutosaveView())
        #endif
    }

    @ViewBuilder
    private var configuredContent: some View {
        if #available(iOS 16.0, macOS 13.0, *) {
            NavigationSplitView {
                SessionSidebar(
                    model: model,
                    openSettings: presentSettings
                )
            } detail: {
                ChatView(model: model)
            }
        } else {
            NavigationView {
                SessionSidebar(
                    model: model,
                    openSettings: presentSettings
                )
                ChatView(model: model)
            }
        }
    }

    private func presentSettings() {
        #if os(macOS)
        settingsWindowPresenter.show(model: model, appearance: appearance)
        #else
        showingSettings = true
        #endif
    }
}

enum SessionSidebarDestination: Hashable, Sendable {
    case newConversation(UUID)
    case session(UUID)

    static func freshConversation() -> Self {
        .newConversation(UUID())
    }
}

private struct SessionSidebar: View {
    @ObservedObject var model: ChatViewModel
    let openSettings: () -> Void
    @State private var deletionCandidate: SessionHistoryEntry?
    @State private var newConversationDestination =
        SessionSidebarDestination.freshConversation()

    var body: some View {
        Group {
            if #available(iOS 16.0, macOS 13.0, *) {
                modernList
            } else {
                legacyList
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("Veetbot")
        .confirmationDialog(
            "Delete conversation everywhere?",
            isPresented: Binding(
                get: { deletionCandidate != nil },
                set: { if !$0 { deletionCandidate = nil } }
            ),
            titleVisibility: .visible,
            presenting: deletionCandidate
        ) { entry in
            Button("Delete Everywhere", role: .destructive) {
                deletionCandidate = nil
                Task { await model.deleteSessionEverywhere(entry) }
            }
            Button("Cancel", role: .cancel) {
                deletionCandidate = nil
            }
        } message: { _ in
            Text(
                "This permanently deletes the conversation and its associated data from the server and all synchronized devices. This cannot be undone."
            )
        }
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button(action: openSettings) {
                    Image(systemName: "gearshape")
                        .foregroundColor(AppTheme.orange)
                }
                .accessibilityLabel("Settings")
            }
        }
    }

    @available(iOS 16.0, macOS 13.0, *)
    @ViewBuilder
    private var modernList: some View {
        #if os(macOS)
        List {
            Button {
                activate(newConversationDestination)
            } label: {
                newConversationLabel
            }
            .buttonStyle(.plain)
            .listRowBackground(AppTheme.brandGradient)

            Section("History") {
                ForEach(model.history) { entry in
                    HStack(spacing: 8) {
                        Button {
                            activate(.session(entry.sessionID))
                        } label: {
                            historyLabel(entry)
                        }
                        .buttonStyle(.plain)

                        deleteButton(for: entry)
                    }
                    .listRowBackground(
                        entry.sessionID == model.selectedSessionID
                            ? AppTheme.turquoise.opacity(0.15)
                            : Color.clear
                    )
                }
            }
        }
        #else
        List {
            NavigationLink {
                ChatDestination(model: model, entry: nil)
            } label: {
                newConversationLabel
            }
            .accessibilityIdentifier("sidebar.new-conversation")
            .listRowBackground(AppTheme.brandGradient)

            Section("History") {
                ForEach(model.history) { entry in
                    HStack(spacing: 8) {
                        NavigationLink {
                            ChatDestination(model: model, entry: entry)
                        } label: {
                            historyLabel(entry)
                        }
                        .accessibilityIdentifier("sidebar.session.\(entry.sessionID.uuidString)")
                        .buttonStyle(.plain)

                        deleteButton(for: entry)
                    }
                    .listRowBackground(
                        entry.sessionID == model.selectedSessionID
                            ? AppTheme.turquoise.opacity(0.15)
                            : Color.clear
                    )
                }
            }
        }
        #endif
    }

    private var legacyList: some View {
        List {
            NavigationLink {
                ChatDestination(model: model, entry: nil)
            } label: {
                newConversationLabel
            }
            .accessibilityIdentifier("sidebar.new-conversation")
            .listRowBackground(AppTheme.brandGradient)

            Section("History") {
                ForEach(model.history) { entry in
                    HStack(spacing: 8) {
                        NavigationLink {
                            ChatDestination(model: model, entry: entry)
                        } label: {
                            historyLabel(entry)
                        }
                        .accessibilityIdentifier("sidebar.session.\(entry.sessionID.uuidString)")
                        .buttonStyle(.plain)

                        deleteButton(for: entry)
                    }
                    .listRowBackground(
                        entry.sessionID == model.selectedSessionID
                            ? AppTheme.turquoise.opacity(0.15)
                            : Color.clear
                    )
                }
            }
        }
    }

    private var newConversationLabel: some View {
        HStack(spacing: 10) {
            Image(systemName: "square.and.pencil")
                .foregroundColor(AppTheme.orange)
            Text("New conversation")
                .appFont(.headline)
            Spacer()
        }
        .padding(.vertical, 4)
    }

    private func historyLabel(_ entry: SessionHistoryEntry) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(entry.title)
                .lineLimit(2)
                .foregroundColor(.primary)
            ConversationAgeText(updatedAt: entry.updatedAt)
                .appFont(.caption)
                .foregroundColor(.secondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func deleteButton(for entry: SessionHistoryEntry) -> some View {
        Button(role: .destructive) {
            deletionCandidate = entry
        } label: {
            Image(systemName: "trash")
                .foregroundColor(.secondary)
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Delete \(entry.title) everywhere")
    }

    private func activate(_ destination: SessionSidebarDestination) {
        switch destination {
        case .newConversation:
            model.newSession()
            newConversationDestination = .freshConversation()
        case .session(let sessionID):
            guard let entry = model.history.first(where: { $0.sessionID == sessionID }) else {
                return
            }
            Task { await model.selectSession(entry) }
        }
    }
}

private struct ChatDestination: View {
    @ObservedObject var model: ChatViewModel
    let entry: SessionHistoryEntry?
    @State private var hasActivated = false

    var body: some View {
        ChatView(model: model)
            .task {
                guard !hasActivated else { return }
                hasActivated = true
                if let entry {
                    await model.selectSession(entry)
                } else {
                    model.newSession()
                }
            }
    }
}

#if os(macOS)
enum MainWindowConfiguration {
    static let frameName: NSWindow.FrameAutosaveName = "VeetbotMainWindow"

    @MainActor
    static func apply(to window: NSWindow) {
        guard window.frameAutosaveName != frameName else { return }
        window.setFrameUsingName(frameName)
        window.setFrameAutosaveName(frameName)
    }
}

enum SettingsWindowConfiguration {
    static let frameName: NSWindow.FrameAutosaveName = "VeetbotSettingsWindow"
    static let minimumSize = NSSize(width: 520, height: 440)
    static let initialSize = NSSize(width: 720, height: 680)
    static let maximumSize = NSSize(width: 10_000, height: 10_000)

    @MainActor
    static func apply(to window: NSWindow) {
        window.styleMask.insert(.resizable)
        window.contentMinSize = minimumSize
        window.contentMaxSize = maximumSize
    }
}

@MainActor
private final class SettingsWindowPresenter: NSObject, ObservableObject, NSWindowDelegate {
    private var controller: NSWindowController?

    func show(model: ChatViewModel, appearance: AppearancePreferences) {
        if let window = controller?.window {
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }

        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: SettingsWindowConfiguration.initialSize),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        SettingsWindowConfiguration.apply(to: window)
        window.title = "Veetbot Settings"
        window.isReleasedWhenClosed = false
        window.delegate = self
        let restoredFrame = window.setFrameUsingName(SettingsWindowConfiguration.frameName)
        window.setFrameAutosaveName(SettingsWindowConfiguration.frameName)

        let settings = ConnectionSettingsView(
            model: model,
            embedded: false,
            onClose: { [weak window] in window?.close() }
        )
        .environmentObject(appearance)
        .appTypography(appearance)
        .tint(AppTheme.turquoise)
        window.contentViewController = NSHostingController(rootView: settings)

        let controller = NSWindowController(window: window)
        self.controller = controller
        if !restoredFrame { window.center() }
        controller.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        guard notification.object as? NSWindow === controller?.window else { return }
        controller = nil
    }
}

private struct MainWindowFrameAutosaveView: NSViewRepresentable {
    func makeNSView(context: Context) -> MainWindowFrameAutosaveNSView {
        MainWindowFrameAutosaveNSView()
    }

    func updateNSView(_ view: MainWindowFrameAutosaveNSView, context: Context) {
        view.applyConfigurationIfPossible()
    }
}

private final class MainWindowFrameAutosaveNSView: NSView {
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        applyConfigurationIfPossible()
    }

    func applyConfigurationIfPossible() {
        guard let window else { return }
        MainWindowConfiguration.apply(to: window)
    }
}
#endif

private struct ConversationAgeText: View {
    let updatedAt: Date

    var body: some View {
        TimelineView(ConversationAgeSchedule(updatedAt: updatedAt)) { context in
            Text(
                ConversationAgeFormatter.string(
                    since: updatedAt,
                    relativeTo: context.date
                )
            )
        }
    }
}

enum ConversationAgeFormatter {
    static func string(
        since updatedAt: Date,
        relativeTo now: Date,
        locale: Locale = .current
    ) -> String {
        guard updatedAt < now else { return "now" }
        let formatter = RelativeDateTimeFormatter()
        formatter.dateTimeStyle = .numeric
        formatter.unitsStyle = .abbreviated
        formatter.locale = locale
        return formatter.localizedString(for: updatedAt, relativeTo: now)
    }
}

private struct ConversationAgeSchedule: TimelineSchedule {
    let updatedAt: Date

    func entries(from startDate: Date, mode: Mode) -> Entries {
        Entries(nextDate: startDate, updatedAt: updatedAt)
    }

    struct Entries: Sequence, IteratorProtocol {
        var nextDate: Date
        let updatedAt: Date

        mutating func next() -> Date? {
            let result = nextDate
            let elapsed = Swift.max(0, result.timeIntervalSince(updatedAt))
            let refreshInterval: TimeInterval
            if elapsed < 60 {
                refreshInterval = 1
            } else if elapsed < 3_600 {
                refreshInterval = 60
            } else if elapsed < 86_400 {
                refreshInterval = 3_600
            } else {
                refreshInterval = 86_400
            }
            nextDate.addTimeInterval(refreshInterval)
            return result
        }
    }
}
