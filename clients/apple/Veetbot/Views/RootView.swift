import SwiftUI

private struct ActiveClientModeKey: EnvironmentKey {
    static let defaultValue = ClientMode.chat
}

extension EnvironmentValues {
    /// Identifies which mounted client mode owns visible titles and toolbar actions.
    var activeClientMode: ClientMode {
        get { self[ActiveClientModeKey.self] }
        set { self[ActiveClientModeKey.self] = newValue }
    }
}

#if os(macOS)
import AppKit
#endif

struct VeetbotSceneRoot: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject var appearance: AppearancePreferences
    @ObservedObject var smsIntegration: SmsIntegrationPreferences
    @ObservedObject var folderSidebar: FolderSidebarPreferences

    /// Applies shared appearance and platform bounds without changing the scene's autosave identity.
    var body: some View {
        RootView(model: model)
            .onAppear { AppThemeApplier.apply(appearance.theme) }
            .onChange(of: appearance.theme) { AppThemeApplier.apply(appearance.theme) }
            .environmentObject(appearance)
            .environmentObject(smsIntegration)
            .environmentObject(folderSidebar)
            .appTypography(appearance)
            .tint(AppTheme.teal)
        #if DEBUG && !SWIFT_PACKAGE
            .preferredColorScheme(
                ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.launchArgument)
                    && ProcessInfo.processInfo.environment["VEETBOT_UI_TEST_COLOR_SCHEME"] == "dark"
                    ? .dark : nil
            )
        #endif
        #if DEBUG && os(iOS)
            .transformEnvironment(\.horizontalSizeClass) { value in
                if ProcessInfo.processInfo.arguments.contains(ConversationNavigationUITestFixture.launchArgument),
                   ProcessInfo.processInfo.environment["VEETBOT_UI_TEST_SIZE_CLASS"] == "compact" {
                    value = .compact
                }
            }
        #endif
        #if os(macOS)
            .frame(minWidth: 780, minHeight: 560)
        #endif
    }
}

public struct RootView: View {
    @ObservedObject var model: ChatViewModel
    @StateObject private var coordinator: AppCoordinator
    @StateObject private var globalMemory = MemoryViewModel()
    @StateObject private var globalPersona = PersonaViewModel()
    @StateObject private var globalSchedules = ScheduleViewModel()
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    /// The Mac right panel in each mode (ADR-0237). Held without observing:
    /// a width drag redraws the panel and its toolbar button, not the root.
    @State private var inspector = InspectorModel()
    @State private var globalPeopleLookup: PeopleLookup?
    @State private var showingGlobalMemory = false
    @State private var showingGlobalPersona = false
    @State private var showingGlobalSchedules = false
    /// Connect's steps 2 and 3, shown once the first-run connection succeeds.
    @State private var onboarding: ConnectOnboardingStep?
    #if !os(macOS)
    @State private var showingSettings = false
    @State private var chatListPresented = false
    @Environment(\.horizontalSizeClass) private var horizontalSizeClass
    #endif
    @Environment(\.scenePhase) private var scenePhase
    #if os(macOS)
    @Environment(\.controlActiveState) private var controlActiveState
    #endif
    @EnvironmentObject private var appearance: AppearancePreferences
    #if os(iOS)
    @EnvironmentObject private var smsIntegration: SmsIntegrationPreferences
    @State private var composingInvocation: SmsInvocation?
    @State private var answeredSmsInvocationIDs: Set<UUID> = []
    #endif
    #if os(macOS)
    @StateObject private var settingsWindowPresenter = SettingsWindowPresenter()
    @State private var showingEmailLearning = false
    @State private var showingEmailSubscriptions = false
    #endif

    public init(model: ChatViewModel) {
        self.model = model
        _coordinator = StateObject(wrappedValue: AppCoordinator(chat: model))
    }

    private var presentedContent: some View {
        Group {
            if onboarding != nil, model.isConfigured {
                ConnectOnboardingView(model: model, step: $onboarding)
            } else if model.isBootstrapping || model.isConfigured {
                configuredContent
            } else {
                ConnectionSettingsView(model: model, embedded: true, onConnecting: {
                    // Step 2 asks about notifications, so connecting must not
                    // raise the system prompt first.
                    NotificationPermissionGate.askLater = true
                    onboarding = .notifications
                })
            }
        }
        // Clears the composer at the foot of Chat.
        .toastHost(bottomInset: 88)
        .sheet(item: $globalPeopleLookup) { PeopleLookupSheet(lookup: $0, sessionID: model.selectedSessionID) }
        .sheet(isPresented: $showingGlobalMemory) { MemoryBrowserView(model: globalMemory, sessionID: model.selectedSessionID) }
        .sheet(isPresented: $showingGlobalPersona) { PersonaEditorView(model: globalPersona) }
        .sheet(isPresented: $showingGlobalSchedules) {
            ScheduleBrowserView(model: globalSchedules)
                .environment(\.openScheduleReport) { sessionID in
                    showingGlobalSchedules = false
                    Task { await model.openSharedSession(sessionID) }
                }
        }
    }

    private var activityContent: some View {
        presentedContent
        .onChange(of: coordinator.mode) { updateEmailActivity() }
        .onChange(of: model.isConfigured) { updateEmailActivity() }
        .onChange(of: model.connectionGeneration) {
            showingGlobalMemory = false
            NotificationCenter.default.post(name: .peopleConnectionChanged, object: nil)
            updateEmailActivity()
        }
        .onChange(of: model.isReconfiguring) { updateEmailActivity() }
        .onChange(of: model.errorMessage) { updateEmailActivity() }
        .onChange(of: scenePhase) { _, phase in updateEmailActivity(scenePhase: phase) }
        .onAppear { updateEmailActivity() }
        .onChange(of: globalPeopleLookup != nil) { updateEmailActivity() }
        .onChange(of: showingGlobalMemory) { updateEmailActivity() }
        .onChange(of: showingGlobalPersona) { updateEmailActivity() }
        .onChange(of: showingGlobalSchedules) { updateEmailActivity() }
        #if os(macOS)
        .onChange(of: controlActiveState) { _, state in updateEmailActivity(keyWindow: state == .key) }
        #else
        .onChange(of: showingSettings) { updateEmailActivity() }
        #endif
    }

    public var body: some View {
        activityContent
        .task(id: "\(model.connectionGeneration)-\(model.isConfigured)-\(scenePhase == .active)") {
            guard model.isConfigured, scenePhase == .active else { return }
            while !Task.isCancelled {
                await model.refreshScheduledReportHistory()
                await model.synchronizeNotifications()
                await model.refreshSessionsAwaitingApproval()
                do { try await Task.sleep(nanoseconds: 10_000_000_000) } catch { return }
            }
        }
        #if !os(macOS)
        .sheet(isPresented: $showingSettings) {
            // The iPad gives the group sidebar and its page a full page.
            ConnectionSettingsView(model: model, embedded: false)
                .presentationSizing(.page)
        }
        #endif
        #if os(iOS)
        .sheet(item: composedInvocation, onDismiss: { presentNextSmsInvocation() }) { invocation in
            VStack(alignment: .leading, spacing: 0) {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Veetbot wants to send a text").veetType(.display)
                    Text("To \(invocation.recipient)").veetType(.ui, weight: .medium)
                    Text("Texts go from this iPhone's number. Nothing is sent until you tap Send in Messages below.")
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                }
                .padding(20).frame(maxWidth: .infinity, alignment: .leading)
                .background(AppTheme.paper)
                .accessibilityIdentifier("sms.compose-context")
                SmsComposeSheet(invocation: invocation) { result in
                    answerSmsInvocation(invocation, with: DeviceInvocationResult(composeResult: result))
                }
                .ignoresSafeArea(edges: .bottom)
            }
        }
        .onChange(of: model.pendingSmsInvocation) { presentNextSmsInvocation() }
        .task(id: smsRecoveryKey) {
            guard scenePhase == .active, smsIntegration.integrationEnabled else { return }
            await model.refreshPendingSmsInvocations()
        }
        #endif
        .onChange(of: model.requiresReauthentication) { _, required in
            if required { presentSettings() }
        }
        .onChange(of: scenePhase) { _, phase in
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
            isPresented: errorPresented
        ) {
            Button("OK") { model.clearError() }
        } message: {
            Text(model.errorMessage ?? "")
        }
        #if os(macOS)
        .background(MainWindowFrameAutosaveView())
        #endif
    }

    private var errorPresented: Binding<Bool> {
        Binding(get: { model.errorMessage != nil }, set: { if !$0 { model.clearError() } })
    }

    @ViewBuilder
    private var configuredContent: some View {
        VStack(spacing: 0) {
            if coordinator.mode != .email || !usesDirectSidebarActivation {
            HStack {
                ModeSegment(selected: coordinator.mode) { coordinator.mode = $0 }
                Spacer()
                if coordinator.mode == .email {
                    Menu {
                        Button("Memory") { showingGlobalMemory = true }
                        Button("Persona") { showingGlobalPersona = true }
                        Button("Schedules") { showingGlobalSchedules = true }
                        Button("Settings", action: presentSettings)
                    } label: { Image(systemName: "ellipsis.circle").padding(10) }
                    .accessibilityLabel("More")
                    .accessibilityIdentifier("email.more")
                }
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            .background(AppTheme.paperDeep)
            Rectangle().fill(AppTheme.hairline).frame(height: 1)
            }
            // Keeping each navigation tree mounted preserves compact navigation,
            // scroll position and live Chat rendering across a mode switch.
            GeometryReader { geometry in
                ZStack {
                    chatContent
                        .frame(width: geometry.size.width, height: geometry.size.height)
                        .clipped()
                        .opacity(coordinator.mode == .chat ? 1 : 0)
                        .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: coordinator.mode)
                        .allowsHitTesting(coordinator.mode == .chat)
                        .accessibilityHidden(coordinator.mode != .chat)
                    EmailModeView(
                        model: coordinator.email, viewportHeight: geometry.size.height,
                        discussInChat: { await coordinator.discussSelectedThread() },
                        openChat: { coordinator.mode = .chat },
                        newChat: { model.newSession(); coordinator.mode = .chat },
                        openMemory: { showingGlobalMemory = true },
                        openPeople: { globalPeopleLookup = PeopleLookup(text: "", asOf: nil) },
                        openSchedules: { showingGlobalSchedules = true },
                        openPersona: { showingGlobalPersona = true },
                        openSettings: presentSettings,
                        chatAttention: SidebarAttention.rows(history: model.history,
                            sessionsAwaitingApproval: model.sessionsNeedingApproval, hasUnreadReport: model.hasUnreadReport),
                        openAttention: { sessionID in
                            coordinator.mode = .chat
                            Task { await model.openSharedSession(sessionID) }
                        },
                        inspector: inspector,
                        runFrames: { model.runFrames($0) }
                    )
                    .frame(width: geometry.size.width, height: geometry.size.height)
                    .clipped()
                    .opacity(coordinator.mode == .email ? 1 : 0)
                    .animation(reduceMotion ? nil : .easeInOut(duration: 0.22), value: coordinator.mode)
                    .allowsHitTesting(coordinator.mode == .email)
                    .accessibilityHidden(coordinator.mode != .email)
                }
                // Mounted navigation trees must fit the available window instead
                // of moving the mode controls outside a smaller Mac window.
                .frame(width: geometry.size.width, height: geometry.size.height)
                #if os(macOS)
                // Too narrow a window collapses a mode's panel (ADR-0237).
                .onAppear { inspector.windowWidthChanged(geometry.size.width) }
                .onChange(of: geometry.size.width) { _, width in inspector.windowWidthChanged(width) }
                .modifier(InspectorRevealEnvironment(inspector: inspector))
                #endif
            }
        }
        // Clipping each tree to the space above the home indicator also clips
        // its own full-bleed paper, so the paper under them reaches the edge.
        .background(AppTheme.paper.ignoresSafeArea(edges: .bottom))
        .environment(\.activeClientMode, coordinator.mode)
        #if os(macOS)
        // One window-level owner prevents the hidden navigation tree from replacing active controls.
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                if coordinator.mode == .chat {
                    Button(action: presentSettings) {
                        Image(systemName: "gearshape")
                            .foregroundColor(AppTheme.inkSoft)
                    }
                    .accessibilityLabel("Settings")
                    .accessibilityIdentifier("sidebar.settings")
                    Button {
                        showingGlobalMemory = true
                    } label: {
                        Image(systemName: "brain.head.profile")
                            .foregroundColor(AppTheme.inkSoft)
                    }
                    .accessibilityLabel("Memory")
                    .accessibilityIdentifier("sidebar.memory")
                    Button {
                        showingGlobalPersona = true
                    } label: {
                        Image(systemName: "person.crop.circle")
                            .foregroundColor(AppTheme.inkSoft)
                    }
                    .accessibilityLabel("Persona")
                    .accessibilityIdentifier("sidebar.persona")
                    Button {
                        showingGlobalSchedules = true
                    } label: {
                        Image(systemName: "calendar")
                            .foregroundColor(AppTheme.inkSoft)
                    }
                    .accessibilityLabel("Schedules")
                    .accessibilityIdentifier("sidebar.schedules")
                    ChatToolbarActions(model: model)
                    InspectorToggleButton(inspector: inspector, mode: .chat)
                } else {
                    // The entry exists only where an account advertises support.
                    if coordinator.email.unsubscribeAvailable {
                        Button { showingEmailSubscriptions = true } label: {
                            Image(systemName: "tray.full")
                                .foregroundColor(AppTheme.inkSoft)
                        }
                        .accessibilityLabel("Subscriptions").help("Subscriptions")
                        .accessibilityIdentifier("email.subscriptions.open")
                    }
                    Button { showingEmailLearning = true } label: {
                        Image(systemName: "slider.horizontal.3")
                    }
                    .accessibilityLabel("Email learning").help("Email learning")
                    .accessibilityIdentifier("email.learning")
                    EmailRefreshButton(model: coordinator.email)
                    InspectorToggleButton(inspector: inspector, mode: .email)
                }
            }
        }
        .sheet(isPresented: $showingEmailLearning) { EmailLearningScreen(model: coordinator.email) }
        .sheet(isPresented: $showingEmailSubscriptions) {
            EmailSubscriptionsScreen(model: coordinator.email.subscriptions, accounts: coordinator.email.accounts)
        }
        .onChange(of: coordinator.email.unsubscribeAvailable) { _, available in
            if !available { showingEmailSubscriptions = false }
        }
        #endif
    }

    /// `onChange` callers pass the changed phase from the action's `newValue`, so
    /// the result never depends on which render's environment the closure read.
    private func updateEmailActivity(scenePhase newPhase: ScenePhase? = nil, keyWindow: Bool? = nil) {
        let active = (newPhase ?? scenePhase) == .active
        model.notificationSyncActive = model.isConfigured && active
        var visible = model.isConfigured && !model.isReconfiguring && active
            && model.errorMessage == nil && coordinator.mode == .chat
            && !showingGlobalMemory && !showingGlobalPersona && !showingGlobalSchedules && globalPeopleLookup == nil
        #if os(macOS)
        visible = visible && (keyWindow ?? (controlActiveState == .key))
        #else
        visible = visible && !showingSettings
        #endif
        model.notificationAttentionEnabled = visible
        if visible { Task { await model.synchronizeNotifications() } }
        coordinator.email.setActive(model.isConfigured && !model.isReconfiguring && active && coordinator.mode == .email)
    }

    @ViewBuilder
    private var chatContent: some View {
        #if os(iOS)
        if horizontalSizeClass == .compact {
            compactChat
        } else {
            splitChat
        }
        #else
        splitChat
        #endif
    }

    /// The window toolbar sits above the mode bar, not over the split view's
    /// columns, yet on macOS 26 its glass still draws a soft edge across the
    /// top of each column and blurs the conversation's title. Hiding the
    /// toolbar's own background removes that edge; the title bar keeps the
    /// window's colour.
    private var splitChat: some View {
        NavigationSplitView {
            SessionSidebar(
                model: model,
                usesDirectActivation: usesDirectSidebarActivation,
                openSettings: presentSettings
            )
            .navigationSplitViewColumnWidth(
                min: A2Metrics.sidebarWidth - VeetSpace.s7, ideal: A2Metrics.sidebarWidth,
                max: A2Metrics.sidebarWidth + VeetSpace.s7 * 2
            )
        } detail: {
            #if os(macOS)
            ChatView(model: model)
                .macInspector(inspector, mode: .chat) {
                    ChatInspectorPanel(model: model, inspector: inspector)
                }
            #else
            ChatView(model: model)
            #endif
        }
        #if os(macOS)
        .toolbarBackground(.hidden, for: .windowToolbar)
        #endif
    }

    #if os(iOS)
    /// A compact window shows the conversation itself, and the chat list
    /// slides over it from the leading edge: opened from the navigation bar,
    /// closed by choosing a conversation, tapping the paper beside it, or
    /// swiping it away. It opens by itself while no conversation is chosen,
    /// so the owner lands on their conversations, not on an empty page.
    private var compactChat: some View {
        NavigationStack {
            ChatView(model: model)
                .toolbar {
                    ToolbarItem(placement: .navigationBarLeading) {
                        Button {
                            withAnimation(.easeOut(duration: 0.26)) { chatListPresented = true }
                        } label: {
                            Image(systemName: "sidebar.leading")
                                .frame(minWidth: 44, minHeight: 44)
                        }
                        .accessibilityLabel("Chats")
                        .accessibilityHint("Shows your conversations")
                        .accessibilityIdentifier("sidebar.toggle")
                    }
                }
        }
        .overlay {
            ChatListDrawer(isPresented: $chatListPresented) {
                SessionSidebar(
                    model: model,
                    usesDirectActivation: true,
                    inDrawer: true,
                    openSettings: presentSettings,
                    onActivate: { withAnimation(.easeOut(duration: 0.22)) { chatListPresented = false } }
                )
            }
        }
        .environment(\.chatListOverlayPresented, chatListPresented)
        // Removing the drawer also dismisses sheets owned by its sidebar.
        .onChange(of: chatListPresented) { _, presented in
            if !presented { model.notificationOverlayPresented = false }
        }
        .onAppear {
            var opensList = model.selectedSessionID == nil
            #if DEBUG && !SWIFT_PACKAGE
            if ConversationNavigationUITestFixture.opensSessionAtLaunch { opensList = false }
            #endif
            if opensList { chatListPresented = true }
        }
        // A notification's conversation is already selected; showing it means
        // getting the list out of the way.
        .onChange(of: model.notificationNavigationID) { _, id in
            guard id != nil else { return }
            withAnimation(.easeOut(duration: 0.22)) { chatListPresented = false }
            model.acknowledgeNotificationNavigation()
        }
    }
    #endif

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
    @Environment(\.activeClientMode) private var activeMode
    @ObservedObject var model: ChatViewModel
    /// Observed for the open conversation's pending approvals, which the
    /// Needs you group reads through `model.sessionsNeedingApproval`.
    @ObservedObject private var runState: RunStateReducer
    let usesDirectActivation: Bool
    /// Sliding over the conversation on a compact window, with no navigation
    /// bar of its own: the overflow menu sits in its header instead.
    var inDrawer = false
    let openSettings: () -> Void
    /// Called after a row opens a conversation, so a drawer can close.
    var onActivate: () -> Void = {}
    @State private var deletionCandidate: SessionHistoryEntry?
    /// The sidebar search: rows filter by title while it holds text.
    @State private var query = ""
    @State private var newConversationDestination =
        SessionSidebarDestination.freshConversation()
    @StateObject private var memoryViewModel = MemoryViewModel()
    @StateObject private var personaViewModel = PersonaViewModel()
    @State private var showingMemoryBrowser = false
    @State private var showingPersonaEditor = false
    @StateObject private var scheduleViewModel = ScheduleViewModel()
    @State private var showingScheduleBrowser = false
    @EnvironmentObject private var folderSidebar: FolderSidebarPreferences
    @StateObject private var scheduleGroups = ScheduleGroupSidebarPreferences()
    @State private var folderEditor: FolderEditorRequest?
    @State private var folderDeletionCandidate: FolderView?

    init(
        model: ChatViewModel,
        usesDirectActivation: Bool,
        inDrawer: Bool = false,
        openSettings: @escaping () -> Void,
        onActivate: @escaping () -> Void = {}
    ) {
        self.model = model
        self.runState = model.runState
        self.usesDirectActivation = usesDirectActivation
        self.inDrawer = inDrawer
        self.openSettings = openSettings
        self.onActivate = onActivate
    }

    /// Presents session navigation and the iOS Chat overflow menu.
    var body: some View {
        VStack(spacing: 0) {
            #if os(iOS)
            if inDrawer { drawerHeader }
            #endif
            sidebarTop
            sidebarList
                .scrollContentBackground(.hidden)
                .listStyle(.sidebar)
                .environment(\.defaultMinListRowHeight, 0)
        }
        .background(AppTheme.paperDeep.ignoresSafeArea())
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
        .confirmationDialog(
            "Delete folder?",
            isPresented: Binding(
                get: { folderDeletionCandidate != nil },
                set: { if !$0 { folderDeletionCandidate = nil } }
            ),
            titleVisibility: .visible,
            presenting: folderDeletionCandidate
        ) { folder in
            Button("Delete Folder", role: .destructive) {
                folderDeletionCandidate = nil
                Task { await model.deleteFolder(folder.id) }
            }
            Button("Cancel", role: .cancel) {
                folderDeletionCandidate = nil
            }
        } message: { _ in
            Text("The conversations in this folder return to History. Nothing is deleted.")
        }
        .onChange(of: showingMemoryBrowser || showingPersonaEditor || showingScheduleBrowser || folderEditor != nil) { _, covered in
            model.notificationOverlayPresented = covered
        }
        .sheet(item: $folderEditor) { request in
            FolderNameSheet(request: request, model: model)
        }
        .onChange(of: model.folders.map(\.id)) { _, folderIDs in
            guard model.foldersAvailable else { return }
            folderSidebar.expansion.retain(only: Set(folderIDs))
        }
        #if os(iOS)
        .toolbar {
            ToolbarItem(placement: .automatic) {
                if activeMode == .chat, !inDrawer { moreMenu }
            }
        }
        #endif
        .sheet(isPresented: $showingMemoryBrowser) {
            MemoryBrowserView(model: memoryViewModel, sessionID: model.selectedSessionID)
        }
        .onChange(of: model.connectionGeneration) { showingMemoryBrowser = false }
        .sheet(isPresented: $showingPersonaEditor) {
            PersonaEditorView(model: personaViewModel)
        }
        .sheet(isPresented: $showingScheduleBrowser) {
            ScheduleBrowserView(model: scheduleViewModel)
                .environment(\.openScheduleReport) { sessionID in
                    showingScheduleBrowser = false
                    Task { await model.openSharedSession(sessionID) }
                }
        }
    }

    #if os(iOS)
    /// The drawer's own top: the app's name in the display face and the
    /// overflow menu the navigation bar would otherwise hold.
    private var drawerHeader: some View {
        HStack(spacing: VeetSpace.s2) {
            Text("Veetbot")
                .veetType(.title)
                .foregroundColor(AppTheme.ink)
            Spacer()
            moreMenu
        }
        .padding(.leading, A2Metrics.sidebarGutter)
        .padding(.trailing, VeetSpace.s2)
        .padding(.top, VeetSpace.s2)
    }

    private var moreMenu: some View {
        Menu {
            Button {
                showingMemoryBrowser = true
            } label: {
                Label("Memory", systemImage: "brain.head.profile")
            }
            .accessibilityIdentifier("sidebar.memory")

            Button {
                showingScheduleBrowser = true
            } label: {
                Label("Schedules", systemImage: "calendar")
            }
            .accessibilityIdentifier("sidebar.schedules")

            Button {
                showingPersonaEditor = true
            } label: {
                Label("Persona", systemImage: "person.crop.circle")
            }
            .accessibilityIdentifier("sidebar.persona")

            Divider()

            Button(action: openSettings) {
                Label("Settings", systemImage: "gearshape")
            }
            .accessibilityIdentifier("sidebar.settings")
        } label: {
            Image(systemName: "ellipsis.circle")
                .foregroundColor(AppTheme.inkSoft)
                .frame(minWidth: 44, minHeight: 44)
        }
        .accessibilityLabel("More")
        .accessibilityHint("Shows Memory, Schedules, Persona, and Settings")
        .accessibilityIdentifier("sidebar.more")
    }
    #endif

    @ViewBuilder
    private var sidebarList: some View {
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

    /// The top of the sidebar: search, and New chat as a teal compose
    /// button beside it.
    private var sidebarTop: some View {
        HStack(spacing: VeetSpace.s2) {
            SidebarSearchField(text: $query)
            newConversationButton
        }
        .padding(.horizontal, A2Metrics.sidebarGutter)
        .padding(.top, VeetSpace.s2)
        .padding(.bottom, VeetSpace.s1)
    }

    @ViewBuilder
    private var newConversationButton: some View {
        if usesDirectActivation {
            Button {
                activate(newConversationDestination)
            } label: {
                ComposeButton { Image(systemName: "square.and.pencil") }
            }
            .buttonStyle(.plain)
            .help("New chat")
            .accessibilityLabel("New chat")
            .accessibilityIdentifier("sidebar.new-conversation")
        } else {
            NavigationLink {
                ChatDestination(model: model, entry: nil)
            } label: {
                ComposeButton { Image(systemName: "square.and.pencil") }
            }
            .buttonStyle(.plain)
            .accessibilityLabel("New chat")
            .accessibilityIdentifier("sidebar.new-conversation")
        }
    }

    private var directlyActivatingList: some View {
        List {
            needsYouSection { entry in
                activate(.session(entry.sessionID))
            }

            historySections { entry, isRecent, nested in
                chatRow(entry, isRecent: isRecent, nested: nested) { label in
                    Button {
                        activate(.session(entry.sessionID))
                    } label: {
                        label
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    /// What needs the owner, ahead of every conversation: an approval waiting
    /// or a report not yet read, each naming its conversation.
    @ViewBuilder
    private func needsYouSection(open: @escaping (SessionHistoryEntry) -> Void) -> some View {
        let rows = SidebarAttention.rows(
            history: model.history,
            sessionsAwaitingApproval: model.sessionsNeedingApproval,
            hasUnreadReport: model.hasUnreadReport
        ).filter { matches($0.entry.title) }
        if !rows.isEmpty {
            SidebarSectionLabel(title: "Needs you")
                .sidebarRow()
            ForEach(rows) { row in
                Button {
                    open(row.entry)
                } label: {
                    SidebarRow {
                        AttentionDot(kind: row.kind)
                    } title: {
                        Text("\(Text(row.label).fontWeight(.medium))\(Text(verbatim: " · \(row.entry.title)").foregroundColor(AppTheme.inkFaint))")
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(row.label), \(row.entry.title)")
                .accessibilityIdentifier("sidebar.needs-you.\(row.id)")
                .sidebarRow(selected: row.entry.sessionID == model.selectedSessionID)
            }
        }
    }

    private var pushingList: some View {
        navigationList
            .navigationDestination(isPresented: notificationNavigationBinding) {
                ChatView(model: model)
            }
    }

    private var navigationList: some View {
        List {
            needsYouSection { entry in
                Task { await model.selectSession(entry) }
            }

            historySections { entry, isRecent, nested in
                chatRow(entry, isRecent: isRecent, nested: nested) { label in
                    NavigationLink {
                        ChatDestination(model: model, entry: entry)
                    } label: {
                        label
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }

    /// A conversation's row: the shared row anatomy (an unread report's dot
    /// in the icon column), History rows dated in the trailing slot, and the
    /// move and delete actions replacing that slot on hover. The same actions
    /// sit in the context menu, a right-click on the Mac and a long press on
    /// touch, where nothing hovers.
    private func chatRow<Open: View>(
        _ entry: SessionHistoryEntry,
        isRecent: Bool,
        nested: Bool,
        @ViewBuilder open: @escaping (AnyView) -> Open
    ) -> some View {
        SidebarChatRow(
            nested: nested,
            hasUnreadReport: model.hasUnreadReport(entry),
            title: entry.title,
            date: isRecent || nested ? nil : entry.updatedAt,
            open: { label in
                open(label)
                    .accessibilityIdentifier(
                        isRecent
                            ? "sidebar.recent.session.\(entry.sessionID.uuidString)"
                            : "sidebar.session.\(entry.sessionID.uuidString)"
                    )
            },
            actions: {
                HStack(spacing: VeetSpace.s1) {
                    moveMenu(for: entry, isRecent: isRecent)
                    deleteButton(for: entry)
                }
            }
        )
        .contextMenu { rowContextMenu(for: entry, isRecent: isRecent) }
        .sidebarRow(selected: entry.sessionID == model.selectedSessionID)
    }

    /// One builder for both list variants, so folder sections cannot drift
    /// between the direct-activation and the compact-navigation sidebars.
    /// Order (design system revision 2): Needs you (above), Recent, suggested
    /// folders, Folders, Scheduled, then History. With folders unavailable no
    /// folder section or control renders, while schedule groups still do
    /// (ADR-0113). Every label is a row in one list, aligned with the icon
    /// column, so no per-section gap separates a header from its rows. While
    /// the owner searches, rows filter by title and a folder or schedule with
    /// a match shows its matches open.
    @ViewBuilder
    private func historySections<Row: View>(
        @ViewBuilder row: @escaping (SessionHistoryEntry, _ isRecent: Bool, _ nested: Bool) -> Row
    ) -> some View {
        let grouped = model.groupedHistory
        let searching = !trimmedQuery.isEmpty
        let recent = grouped.recent.filter { matches($0.title) }
        if !recent.isEmpty {
            RecentChatsHeaderRow(
                isExpanded: folderSidebar.recentChatsExpanded,
                onToggle: {
                    withAnimation(.easeInOut(duration: 0.2)) {
                        folderSidebar.recentChatsExpanded.toggle()
                    }
                }
            )
            .sidebarRow()
            if folderSidebar.recentChatsExpanded {
                ForEach(SidebarKeyedEntry.keyed(recent, in: "recent")) { keyed in
                    row(keyed.entry, true, false)
                }
            }
        }
        if model.foldersAvailable, !model.suggestedFolders.isEmpty, !searching {
            SidebarSectionLabel(title: "Suggested folders")
                .sidebarRow()
            ForEach(model.suggestedFolders) { proposal in
                SuggestedFolderRow(proposal: proposal, model: model) {
                    folderEditor = .acceptProposal(proposal)
                }
                .sidebarRow()
            }
        }
        let folders = grouped.folders.filter { section in
            !searching || matches(section.folder.name) || section.entries.contains { matches($0.title) }
        }
        if model.foldersAvailable || !folders.isEmpty {
            SidebarSectionLabel(title: "Folders") {
                if model.foldersAvailable {
                    SidebarSectionAction(systemImage: "plus", label: "New folder") {
                        folderEditor = .create(moving: nil)
                    }
                    .accessibilityIdentifier("sidebar.new-folder")
                }
            }
            .sidebarRow()
            ForEach(folders) { section in
                let expanded = folderSidebar.expansion.isExpanded(section.id)
                FolderHeaderRow(
                    folder: section.folder,
                    count: section.entries.count,
                    isExpanded: expanded || searching,
                    solo: folderSidebar.expansion.solo,
                    onToggle: { setFolder(section.id, expanded: !expanded) },
                    onRename: { folderEditor = .rename(section.folder) },
                    onDelete: { folderDeletionCandidate = section.folder },
                    onSetSolo: { solo in
                        folderSidebar.expansion.setSolo(solo, order: grouped.folders.map(\.id))
                    }
                )
                .sidebarRow()
                if expanded || searching {
                    ForEach(SidebarKeyedEntry.keyed(section.entries.filter { matches($0.title) }, in: "folder")) { keyed in
                        row(keyed.entry, false, true)
                    }
                }
            }
        }
        let schedules = grouped.schedules.filter { group in
            !searching || matches(group.title) || group.entries.contains { matches($0.title) }
        }
        if !schedules.isEmpty {
            SidebarSectionLabel(title: "Scheduled")
                .sidebarRow()
            ForEach(schedules) { group in
                let expanded = scheduleGroups.isExpanded(group.id)
                ScheduleGroupHeaderRow(
                    group: group,
                    isExpanded: expanded || searching,
                    hasUnreadReport: group.entries.contains { model.hasUnreadReport($0) },
                    onToggle: { setScheduleGroup(group.id, expanded: !expanded) }
                )
                .sidebarRow()
                if expanded || searching {
                    ForEach(SidebarKeyedEntry.keyed(group.entries.filter { matches($0.title) }, in: "schedule")) { keyed in
                        row(keyed.entry, false, true)
                    }
                }
            }
        }
        let history = grouped.uncategorized.filter { matches($0.title) }
        if !history.isEmpty {
            SidebarSectionLabel(title: "History")
                .sidebarRow()
            ForEach(SidebarKeyedEntry.keyed(history, in: "history")) { keyed in
                row(keyed.entry, false, false)
            }
        }
    }

    private var trimmedQuery: String {
        query.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func matches(_ title: String) -> Bool {
        trimmedQuery.isEmpty || title.localizedCaseInsensitiveContains(trimmedQuery)
    }

    /// Only the owner's own toggle changes what is expanded; in solo mode,
    /// expanding one folder collapses the one that was open.
    private func setFolder(_ folderID: UUID, expanded: Bool) {
        withAnimation(.easeInOut(duration: 0.2)) {
            folderSidebar.expansion.setExpanded(expanded, folder: folderID)
        }
    }

    /// Only the owner's own toggle changes which schedule groups are open.
    private func setScheduleGroup(_ scheduleID: UUID, expanded: Bool) {
        withAnimation(.easeInOut(duration: 0.2)) {
            scheduleGroups.setExpanded(expanded, schedule: scheduleID)
        }
    }

    /// A scheduled session is never a chat conversation, and the move route
    /// refuses it, so it offers no move menu.
    @ViewBuilder
    private func moveMenu(for entry: SessionHistoryEntry, isRecent: Bool) -> some View {
        if model.foldersAvailable, entry.scheduleID == nil {
            Menu {
                moveMenuItems(for: entry)
            } label: {
                Image(systemName: "folder")
                    .veetIcon(.small)
                    .foregroundColor(AppTheme.inkFaint)
            }
            .menuIndicator(.hidden)
            #if os(macOS)
            .menuStyle(.borderlessButton)
            .fixedSize()
            #endif
            .accessibilityLabel("Move \(entry.title) to a folder")
            .accessibilityIdentifier(
                isRecent
                    ? "sidebar.recent.session.move.\(entry.sessionID.uuidString)"
                    : "sidebar.session.move.\(entry.sessionID.uuidString)"
            )
        }
    }

    @ViewBuilder
    private func moveMenuItems(for entry: SessionHistoryEntry) -> some View {
        ForEach(model.folders) { folder in
            Button {
                Task { await model.moveSession(entry.sessionID, toFolder: folder.id) }
            } label: {
                if entry.folderID == folder.id {
                    Label(folder.name, systemImage: "checkmark")
                } else {
                    Text(folder.name)
                }
            }
            .accessibilityIdentifier("sidebar.session.move.to.\(folder.id.uuidString)")
        }
        if entry.folderID != nil {
            Button("Remove from folder") {
                Task { await model.moveSession(entry.sessionID, toFolder: nil) }
            }
            .accessibilityIdentifier("sidebar.session.move.none")
        }
        Divider()
        Button("New folder…") {
            folderEditor = .create(moving: entry.sessionID)
        }
        .accessibilityIdentifier("sidebar.session.move.new")
    }

    /// The row's actions where nothing hovers: Move to Folder and Delete.
    @ViewBuilder
    private func rowContextMenu(for entry: SessionHistoryEntry, isRecent: Bool) -> some View {
        if model.foldersAvailable, entry.scheduleID == nil {
            #if os(iOS)
            Menu("Move to Folder") { moveMenuItems(for: entry) }
                .accessibilityIdentifier(
                    isRecent
                        ? "sidebar.recent.session.move.\(entry.sessionID.uuidString)"
                        : "sidebar.session.move.\(entry.sessionID.uuidString)"
                )
            #else
            Menu("Move to Folder") { moveMenuItems(for: entry) }
            #endif
        }
        Button("Delete Everywhere…", role: .destructive) {
            deletionCandidate = entry
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

    private func deleteButton(for entry: SessionHistoryEntry) -> some View {
        Button(role: .destructive) {
            deletionCandidate = entry
        } label: {
            Image(systemName: "trash")
                .veetIcon(.small)
                .foregroundColor(AppTheme.inkFaint)
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
        onActivate()
    }
}

/// A conversation's row identity within one sidebar section. Recent repeats
/// conversations that also sit in a folder or History, and with every row in
/// one list the Mac reused a History row for its Recent twin unless each
/// section keys its own.
private struct SidebarKeyedEntry: Identifiable {
    let section: String
    let entry: SessionHistoryEntry
    var id: String { "\(section).\(entry.id)" }

    static func keyed(_ entries: [SessionHistoryEntry], in section: String) -> [SidebarKeyedEntry] {
        entries.map { SidebarKeyedEntry(section: section, entry: $0) }
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
    // The group sidebar (232 pt) sits beside each group's page.
    static let minimumSize = NSSize(width: 720, height: 480)
    static let initialSize = NSSize(width: 920, height: 680)
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
        .tint(AppTheme.teal)
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
            if ProcessInfo.processInfo.environment["VEETBOT_UI_TEST_MAIN_WINDOW_CENTER"] == "1",
               let screen = NSScreen.screens.first {
                let frame = screen.visibleFrame
                window.setFrameOrigin(NSPoint(
                    x: frame.midX - window.frame.width / 2,
                    y: frame.midY - window.frame.height / 2
                ))
            }
        }
    }
    #endif
}
#endif

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

struct ConversationAgeSchedule: TimelineSchedule {
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

/// Chat or Email: a two-way segment in a sunken field, the chosen one lifted
/// onto a white pill, as the reference draws it.
struct ModeSegment: View {
    let selected: ClientMode
    let choose: (ClientMode) -> Void

    var body: some View {
        HStack(spacing: 2) {
            ForEach(ClientMode.allCases) { mode in
                Button { choose(mode) } label: {
                    Label(mode.title, systemImage: mode.symbol)
                        .labelStyle(.titleOnly)
                        .veetType(.meta, weight: .medium)
                        .foregroundColor(selected == mode ? AppTheme.ink : AppTheme.inkSoft)
                        .padding(.horizontal, 18)
                        .frame(minHeight: 24)
                        .background(
                            RoundedRectangle(cornerRadius: 6, style: .continuous)
                                .fill(selected == mode ? AppTheme.surface : Color.clear)
                                .shadow(color: AppTheme.ink.opacity(selected == mode ? 0.14 : 0), radius: 1, y: 1)
                        )
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("mode.\(mode.rawValue)")
                .accessibilityAddTraits(selected == mode ? .isSelected : [])
            }
        }
        .padding(2)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(AppTheme.field))
    }
}

#if os(iOS)
/// The chat list sliding over a compact conversation from the leading edge,
/// with a scrim on the rest. The scrim, or a swipe towards the edge, closes it.
struct ChatListDrawer<Content: View>: View {
    @Binding var isPresented: Bool
    @ViewBuilder let content: () -> Content
    @State private var dragOffset: CGFloat = 0

    var body: some View {
        GeometryReader { geometry in
            let width = min(geometry.size.width * 0.86, 360)
            ZStack(alignment: .leading) {
                if isPresented {
                    AppTheme.ink.opacity(0.32)
                        .ignoresSafeArea()
                        .onTapGesture { close() }
                        .accessibilityLabel("Close chats")
                        .accessibilityAddTraits(.isButton)
                        .accessibilityIdentifier("sidebar.scrim")
                        .transition(.opacity)
                    content()
                        .frame(width: width)
                        .background(AppTheme.paperDeep.ignoresSafeArea())
                        .offset(x: min(0, dragOffset))
                        .gesture(
                            DragGesture(minimumDistance: 20)
                                .onChanged { value in
                                    if abs(value.translation.width) > abs(value.translation.height) {
                                        dragOffset = value.translation.width
                                    }
                                }
                                .onEnded { value in
                                    let shouldClose = value.translation.width < -width / 3
                                        || value.predictedEndTranslation.width < -width
                                    dragOffset = 0
                                    if shouldClose { close() }
                                }
                        )
                        .transition(.move(edge: .leading))
                        .accessibilityAction(.escape) { close() }
                }
            }
            .frame(width: geometry.size.width, height: geometry.size.height, alignment: .leading)
        }
        .allowsHitTesting(isPresented)
        .accessibilityHidden(!isPresented)
    }

    private func close() {
        withAnimation(.easeOut(duration: 0.22)) { isPresented = false }
    }
}
#endif
