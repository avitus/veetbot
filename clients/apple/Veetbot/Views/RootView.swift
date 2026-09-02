import SwiftUI

#if os(macOS)
import AppKit
#endif

struct VeetbotSceneRoot: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject var appearance: AppearancePreferences
    @ObservedObject var smsIntegration: SmsIntegrationPreferences

    var body: some View {
        RootView(model: model)
            .environmentObject(appearance)
            .environmentObject(smsIntegration)
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
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    #endif
    @Environment(\.scenePhase) private var scenePhase
    @EnvironmentObject private var appearance: AppearancePreferences
    #if os(iOS)
    @EnvironmentObject private var smsIntegration: SmsIntegrationPreferences
    @State private var composingInvocation: SmsInvocation?
    @State private var answeredSmsInvocationIDs: Set<UUID> = []
    #endif
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
        #if os(iOS)
        .sheet(item: composedInvocation, onDismiss: { presentNextSmsInvocation() }) { invocation in
            SmsComposeSheet(invocation: invocation) { result in
                answerSmsInvocation(invocation, with: DeviceInvocationResult(composeResult: result))
            }
            .ignoresSafeArea()
        }
        .onChange(of: model.pendingSmsInvocation) { _ in presentNextSmsInvocation() }
        .task(id: smsRecoveryKey) {
            guard scenePhase == .active, smsIntegration.integrationEnabled else { return }
            await model.refreshPendingSmsInvocations()
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
                    usesDirectActivation: usesDirectSidebarActivation,
                    openSettings: presentSettings
                )
            } detail: {
                ChatView(model: model)
            }
        } else {
            NavigationView {
                SessionSidebar(
                    model: model,
                    usesDirectActivation: usesDirectSidebarActivation,
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

    private var usesDirectSidebarActivation: Bool {
        #if os(macOS)
        true
        #else
        horizontalSizeClass == .regular
        #endif
    }

    #if os(iOS)
    /// The invocation this sheet is showing — the view's own state, not the
    /// head of the model's queue, which may already have advanced. Dismissing
    /// the sheet by hand never reaches the compose delegate, so that path
    /// posts the cancellation for the invocation that was on screen.
    private var composedInvocation: Binding<SmsInvocation?> {
        Binding(
            get: { composingInvocation },
            set: { newValue in
                guard newValue == nil, let dismissed = composingInvocation else { return }
                answerSmsInvocation(dismissed, with: .cancelled)
            }
        )
    }

    /// Records the owner's outcome for the invocation that was on screen. The
    /// id is retired here, synchronously, because the sheet's dismissal can
    /// reach `onDismiss` before the view model has settled the queue — and a
    /// dismissal must never bring the answered invocation back.
    private func answerSmsInvocation(
        _ invocation: SmsInvocation,
        with result: DeviceInvocationResult
    ) {
        answeredSmsInvocationIDs.insert(invocation.id)
        composingInvocation = nil
        Task { await model.completeSmsInvocation(invocation, with: result) }
    }

    /// Shows the next unanswered invocation once nothing is on screen. A
    /// device that cannot send text reports the failure instead of presenting
    /// a sheet the owner could not use, and a head whose deadline has already
    /// passed — because it queued up behind a sheet the owner sat on for too
    /// long — reports expired the same way, rather than presenting a sheet
    /// whose eventual send the server can only refuse.
    private func presentNextSmsInvocation() {
        guard composingInvocation == nil else { return }
        switch SmsInvocationDisposition.resolve(
            model.pendingSmsInvocation,
            canSendText: SmsComposeSheet.canSend
        ) {
        case .idle:
            break
        case .compose(let invocation):
            guard !answeredSmsInvocationIDs.contains(invocation.id) else { return }
            composingInvocation = invocation
        case .unsupported(let invocation):
            guard !answeredSmsInvocationIDs.contains(invocation.id) else { return }
            answerSmsInvocation(invocation, with: .failed)
        case .expired(let invocation):
            guard !answeredSmsInvocationIDs.contains(invocation.id) else { return }
            answerSmsInvocation(invocation, with: .expired)
        }
    }

    /// Re-runs the recovery fetch when the app comes forward, when the owner
    /// switches the integration on, and once this device knows its own id.
    private struct SmsRecoveryKey: Equatable {
        let deviceID: UUID?
        let isActive: Bool
        let isEnabled: Bool
    }

    private var smsRecoveryKey: SmsRecoveryKey {
        SmsRecoveryKey(
            deviceID: model.registeredDeviceID,
            isActive: scenePhase == .active,
            isEnabled: smsIntegration.integrationEnabled
        )
    }
    #endif
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
    let usesDirectActivation: Bool
    let openSettings: () -> Void
    @State private var deletionCandidate: SessionHistoryEntry?
    @State private var newConversationDestination =
        SessionSidebarDestination.freshConversation()
    @StateObject private var memoryViewModel = MemoryViewModel()
    @StateObject private var personaViewModel = PersonaViewModel()
    @State private var showingMemoryBrowser = false
    @State private var showingPersonaEditor = false
    @StateObject private var scheduleViewModel = ScheduleViewModel()
    @State private var showingScheduleBrowser = false

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
            ToolbarItem(placement: .automatic) {
                Button {
                    showingMemoryBrowser = true
                } label: {
                    Image(systemName: "brain.head.profile")
                        .foregroundColor(AppTheme.turquoise)
                }
                .accessibilityLabel("Memory")
                .accessibilityIdentifier("sidebar.memory")
            }
            ToolbarItem(placement: .automatic) {
                Button {
                    showingPersonaEditor = true
                } label: {
                    Image(systemName: "person.crop.circle")
                        .foregroundColor(AppTheme.turquoise)
                }
                .accessibilityLabel("Persona")
                .accessibilityIdentifier("sidebar.persona")
            }
            ToolbarItem(placement: .automatic) {
                Button {
                    showingScheduleBrowser = true
                } label: {
                    Image(systemName: "calendar")
                        .foregroundColor(AppTheme.orange)
                }
                .accessibilityLabel("Schedules")
                .accessibilityIdentifier("sidebar.schedules")
            }
        }
        .sheet(isPresented: $showingMemoryBrowser) {
            MemoryBrowserView(model: memoryViewModel)
        }
        .sheet(isPresented: $showingPersonaEditor) {
            PersonaEditorView(model: personaViewModel)
        }
        .sheet(isPresented: $showingScheduleBrowser) {
            ScheduleBrowserView(model: scheduleViewModel)
        }
    }

    @available(iOS 16.0, macOS 13.0, *)
    @ViewBuilder
    private var modernList: some View {
        #if os(macOS)
        directlyActivatingList
        #else
        Group {
            if usesDirectActivation {
                directlyActivatingList
            } else {
                pushingList
            }
        }
        #endif
    }

    private var directlyActivatingList: some View {
        List {
            Button {
                activate(newConversationDestination)
            } label: {
                newConversationLabel
            }
            .accessibilityIdentifier("sidebar.new-conversation")
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
                        .accessibilityIdentifier(
                            "sidebar.session.\(entry.sessionID.uuidString)"
                        )
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

    @available(iOS 16.0, macOS 13.0, *)
    private var pushingList: some View {
        navigationList
            .navigationDestination(isPresented: notificationNavigationBinding) {
                ChatView(model: model)
            }
    }

    private var legacyList: some View {
        navigationList
        #if !os(macOS)
        .background {
            NavigationLink(
                isActive: notificationNavigationBinding
            ) {
                ChatView(model: model)
            } label: {
                EmptyView()
            }
            .hidden()
        }
        #endif
    }

    private var navigationList: some View {
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

    private var notificationNavigationBinding: Binding<Bool> {
        Binding(
            get: { model.notificationNavigationID != nil },
            set: { active in
                if !active { model.acknowledgeNotificationNavigation() }
            }
        )
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
    static let storageKey = "veetbot.mainWindow.frame"
    @MainActor private static let persistenceByWindow =
        NSMapTable<NSWindow, FramePersistence>.weakToStrongObjects()

    @MainActor
    static func apply(to window: NSWindow) {
        if persistenceByWindow.object(forKey: window) == nil {
            persistenceByWindow.setObject(FramePersistence(window: window), forKey: window)
        }
        applyImmediately(to: window)
    }

    @MainActor
    private static func applyImmediately(to window: NSWindow) {
        if
            let value = UserDefaults.standard.string(forKey: storageKey),
            let frame = validFrame(from: value)
        {
            let screen = NSScreen.screens.first(where: { $0.frame.intersects(frame) })
                ?? window.screen
                ?? NSScreen.main
            window.setFrame(window.constrainFrameRect(frame, to: screen), display: false)
        }
        if window.frameAutosaveName != frameName {
            window.setFrameAutosaveName(frameName)
        }
    }

    @MainActor
    static func saveFrame(of window: NSWindow) {
        // SwiftUI.AppKitWindow owns AppKit autosave and ignores a caller-supplied
        // save name, so keep the frame in an application-owned preference.
        UserDefaults.standard.set(NSStringFromRect(window.frame), forKey: storageKey)
    }

    private static func validFrame(from value: String) -> NSRect? {
        let frame = NSRectFromString(value)
        guard
            frame.origin.x.isFinite,
            frame.origin.y.isFinite,
            frame.width.isFinite,
            frame.height.isFinite,
            frame.width > 0,
            frame.height > 0
        else { return nil }
        return frame
    }

    @MainActor
    private final class FramePersistence: NSObject {
        private weak var window: NSWindow?
        private var isReadyForUserChanges: Bool

        init(window: NSWindow) {
            self.window = window
            isReadyForUserChanges = window.isMainWindow || window.isKeyWindow
            super.init()
            let center = NotificationCenter.default
            center.addObserver(
                self,
                selector: #selector(windowBecameReady(_:)),
                name: NSWindow.didBecomeMainNotification,
                object: window
            )
            center.addObserver(
                self,
                selector: #selector(windowBecameReady(_:)),
                name: NSWindow.didBecomeKeyNotification,
                object: window
            )
            center.addObserver(
                self,
                selector: #selector(windowFrameChanged(_:)),
                name: NSWindow.didMoveNotification,
                object: window
            )
            center.addObserver(
                self,
                selector: #selector(windowFrameChanged(_:)),
                name: NSWindow.didResizeNotification,
                object: window
            )
        }

        deinit {
            NotificationCenter.default.removeObserver(self)
        }

        @objc private func windowBecameReady(_ notification: Notification) {
            guard let window, notification.object as? NSWindow === window else { return }
            isReadyForUserChanges = false
            MainWindowConfiguration.applyImmediately(to: window)
            DispatchQueue.main.async { [weak self, weak window] in
                guard let self, let window, self.window === window else { return }
                MainWindowConfiguration.applyImmediately(to: window)
                self.isReadyForUserChanges = true
            }
        }

        @objc private func windowFrameChanged(_ notification: Notification) {
            guard
                isReadyForUserChanges,
                let window,
                notification.object as? NSWindow === window
            else { return }
            MainWindowConfiguration.saveFrame(of: window)
        }
    }
}

enum SettingsWindowConfiguration {
    static let frameName: NSWindow.FrameAutosaveName = "VeetbotSettingsWindow"
    static let storageKey = "veetbot.settingsWindow.contentSize"
    static let minimumSize = NSSize(width: 520, height: 440)
    static let initialSize = NSSize(width: 720, height: 680)
    static let maximumSize = NSSize(width: 10_000, height: 10_000)

    @MainActor
    static func apply(to window: NSWindow) {
        window.styleMask.insert(.resizable)
        window.contentMinSize = minimumSize
        window.contentMaxSize = maximumSize
        PopupWindowContentSizeStore.restore(
            window: window,
            key: storageKey,
            minimumSize: minimumSize,
            maximumSize: maximumSize
        )
    }
}

enum PopupWindowContentSizeStore {
    @MainActor
    static func save(window: NSWindow, key: String) {
        guard let size = window.contentView?.frame.size else { return }
        UserDefaults.standard.set(NSStringFromSize(size), forKey: key)
    }

    @MainActor
    static func restore(
        window: NSWindow,
        key: String,
        minimumSize: NSSize,
        maximumSize: NSSize
    ) {
        guard
            let value = UserDefaults.standard.string(forKey: key),
            let size = validSize(from: value)
        else { return }

        window.setContentSize(
            NSSize(
                width: min(max(size.width, minimumSize.width), maximumSize.width),
                height: min(max(size.height, minimumSize.height), maximumSize.height)
            )
        )
    }

    private static func validSize(from value: String) -> NSSize? {
        let size = NSSizeFromString(value)
        guard
            size.width.isFinite,
            size.height.isFinite,
            size.width > 0,
            size.height > 0
        else { return nil }
        return size
    }
}

@MainActor
final class PopupWindowResizePersistence: NSObject {
    private weak var window: NSWindow?
    private let key: String

    init(window: NSWindow, key: String) {
        self.window = window
        self.key = key
        super.init()
        let center = NotificationCenter.default
        center.addObserver(
            self,
            selector: #selector(windowDidResize(_:)),
            name: NSWindow.didResizeNotification,
            object: window
        )
        center.addObserver(
            self,
            selector: #selector(windowDidEndLiveResize(_:)),
            name: NSWindow.didEndLiveResizeNotification,
            object: window
        )
    }

    deinit {
        NotificationCenter.default.removeObserver(self)
    }

    @objc private func windowDidResize(_ notification: Notification) {
        guard
            let window,
            notification.object as? NSWindow === window,
            window.inLiveResize
        else { return }
        PopupWindowContentSizeStore.save(window: window, key: key)
    }

    @objc private func windowDidEndLiveResize(_ notification: Notification) {
        guard let window, notification.object as? NSWindow === window else { return }
        PopupWindowContentSizeStore.save(window: window, key: key)
    }
}

@MainActor
private final class SettingsWindowPresenter: NSObject, ObservableObject, NSWindowDelegate {
    private var controller: NSWindowController?
    private var resizePersistence: PopupWindowResizePersistence?

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
        window.title = "Veetbot Settings"
        window.isReleasedWhenClosed = false
        let restoredFrame = window.setFrameUsingName(SettingsWindowConfiguration.frameName)
        window.setFrameAutosaveName(SettingsWindowConfiguration.frameName)
        SettingsWindowConfiguration.apply(to: window)

        let settings = ConnectionSettingsView(
            model: model,
            embedded: false,
            onClose: { [weak window] in window?.close() }
        )
        .environmentObject(appearance)
        .appTypography(appearance)
        .tint(AppTheme.turquoise)
        window.contentViewController = NSHostingController(rootView: settings)
        SettingsWindowConfiguration.apply(to: window)
        window.delegate = self

        let controller = NSWindowController(window: window)
        self.controller = controller
        resizePersistence = PopupWindowResizePersistence(
            window: window,
            key: SettingsWindowConfiguration.storageKey
        )
        if !restoredFrame { window.center() }
        controller.showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func windowWillClose(_ notification: Notification) {
        guard notification.object as? NSWindow === controller?.window else { return }
        resizePersistence = nil
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
    #if DEBUG
    private var scheduledUITestFrame = false
    #endif

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        applyConfigurationIfPossible()
    }

    func applyConfigurationIfPossible() {
        guard let window else { return }
        MainWindowConfiguration.apply(to: window)
        #if DEBUG
        scheduleUITestFrameIfRequested(on: window)
        #endif
    }

    #if DEBUG
    private func scheduleUITestFrameIfRequested(on window: NSWindow) {
        guard !scheduledUITestFrame else { return }
        guard ProcessInfo.processInfo.arguments.contains(
            "--ui-testing-conversation-navigation"
        ) else { return }
        guard
            let value = ProcessInfo.processInfo.environment[
                "VEETBOT_UI_TEST_MAIN_WINDOW_FRAME"
            ]
        else { return }
        let components = value.split(separator: ",", omittingEmptySubsequences: false)
        guard
            components.count == 2,
            let width = Double(components[0]),
            let height = Double(components[1]),
            width.isFinite,
            height.isFinite,
            width > 0,
            height > 0
        else { return }

        scheduledUITestFrame = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak window] in
            guard let window else { return }
            window.setFrame(
                NSRect(
                    origin: window.frame.origin,
                    size: NSSize(width: width, height: height)
                ),
                display: true
            )
        }
    }
    #endif
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
