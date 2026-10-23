import SwiftUI

enum ConnectionSettingsSection: String, CaseIterable, Identifiable {
    case connection
    case models
    case websiteAccess
    case notifications
    case appearance
    case smsIntegration
    case dataAndPrivacy

    var id: String { rawValue }

    var title: String { Self.titles[self] ?? rawValue }
    var systemImage: String { Self.systemImages[self] ?? "gearshape" }

    // Dictionaries rather than switches: the layout tests slice this file
    // at each group's `case` in the page switch below.
    private static let titles: [ConnectionSettingsSection: String] = [
        .connection: "Connection", .models: "Models", .websiteAccess: "Website access",
        .notifications: "Notifications", .appearance: "Appearance",
        .smsIntegration: "SMS integration", .dataAndPrivacy: "Data & privacy",
    ]

    private static let systemImages: [ConnectionSettingsSection: String] = [
        .connection: "key", .models: "wand.and.stars", .websiteAccess: "globe",
        .notifications: "bell", .appearance: "paintpalette",
        .smsIntegration: "message", .dataAndPrivacy: "lock.shield",
    ]

    /// The sections this platform offers. Sending a text rides the phone's own
    /// compose sheet, so the SMS integration exists only where that sheet does.
    static var visibleCases: [ConnectionSettingsSection] {
        #if os(iOS)
        return allCases
        #else
        return allCases.filter { $0 != .smsIntegration }
        #endif
    }
}

struct ClientBuildIdentity: Equatable {
    let version: String
    let build: String

    init(infoDictionary: [String: Any]?) {
        version = infoDictionary?["CFBundleShortVersionString"] as? String ?? "Development"
        build = infoDictionary?["CFBundleVersion"] as? String ?? "local"
    }

    static var current: ClientBuildIdentity {
        ClientBuildIdentity(infoDictionary: Bundle.main.infoDictionary)
    }

    var displayName: String { "Version \(version) (\(build))" }
}

public struct ConnectionSettingsView: View {
    @ObservedObject var model: ChatViewModel
    let embedded: Bool
    private let closeAction: (() -> Void)?
    @Environment(\.dismiss) private var dismiss
    @Environment(\.openURL) private var openURL
    @Environment(\.scenePhase) private var scenePhase
    @EnvironmentObject private var appearance: AppearancePreferences
    #if os(iOS)
    @EnvironmentObject private var smsIntegration: SmsIntegrationPreferences
    #endif
    @State private var baseURL = ""
    @State private var token = ""
    @State private var isSaving = false
    @State private var websiteURL = ""
    @State private var taskScopeURL = ""
    @State private var confirmingTaskScope = false
    @State private var selectedSection: ConnectionSettingsSection = .connection
    @State private var showsToken = false
    @State private var confirmingClearCache = false
    @StateObject private var notificationSettings: NotificationSettingsViewModel
    private let onConnecting: (() -> Void)?

    public init(
        model: ChatViewModel,
        embedded: Bool,
        onClose: (() -> Void)? = nil,
        onConnecting: (() -> Void)? = nil
    ) {
        self.model = model
        self.embedded = embedded
        closeAction = onClose
        self.onConnecting = onConnecting
        _notificationSettings = StateObject(wrappedValue: NotificationSettingsViewModel(
            makeAPIClient: { [weak model] in model?.currentAPIClient },
            installationID: { try? await KeychainInstallationIdentityStore().readOrCreateInstallationID() }
        ))
    }

    public var body: some View {
        content
        .background(AppTheme.paper)
        .tint(AppTheme.tealFill)
        .toastHost()
        #if os(macOS)
        .frame(
            minWidth: SettingsWindowConfiguration.minimumSize.width,
            minHeight: SettingsWindowConfiguration.minimumSize.height
        )
        #endif
        .onAppear {
            if baseURL.isEmpty { baseURL = model.baseURL?.absoluteString ?? "" }
            if model.isConfigured {
                Task {
                    await model.refreshBrowserProfiles()
                    await model.refreshOpenRemoteAuthentication()
                    await model.refreshTaskPermissions()
                    await model.refreshTaskScopes()
                }
            }
        }
        // A remote sign-in finished in the web browser is read back when the
        // app returns, without polling (ADR-0128 D16).
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                Task {
                    await model.refreshOpenRemoteAuthentication()
                    await model.refreshTaskScopes()
                    await model.refreshTaskPermissions()
                }
            }
        }
        .deviceSignInPresentation(model: model)
    }

    /// First run is Connect's first step alone. Once connected, each group
    /// is a page of its own: picked from a sidebar on the Mac and iPad, and
    /// pushed from a list of groups on the iPhone.
    @ViewBuilder
    private var content: some View {
        if embedded {
            VStack(spacing: 0) {
                header
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) { sectionView(.connection) }
                        .frame(maxWidth: 560, alignment: .leading)
                        .padding(24)
                        .frame(maxWidth: .infinity, alignment: .topLeading)
                }
            }
        } else if usesGroupSidebar {
            HStack(spacing: 0) {
                groupSidebar
                Rectangle().fill(AppTheme.hairline).frame(width: 1).accessibilityHidden(true)
                VStack(spacing: 0) {
                    groupPage(selectedSection)
                    HairlineDivider()
                    actionBar
                }
            }
        } else {
            VStack(spacing: 0) {
                NavigationStack {
                    groupList
                        .navigationDestination(for: ConnectionSettingsSection.self) { section in
                            groupPage(section)
                                .background(AppTheme.paper)
                                .navigationTitle(section.title)
                                #if os(iOS)
                                .navigationBarTitleDisplayMode(.inline)
                                #endif
                        }
                }
                HairlineDivider()
                actionBar
            }
        }
    }

    /// The Mac and the iPad pick a group from a sidebar; the iPhone pushes it.
    private var usesGroupSidebar: Bool {
        #if os(macOS)
        return true
        #else
        return UIDevice.current.userInterfaceIdiom == .pad
        #endif
    }

    /// A website login that waits for the owner marks its group with amber.
    private var websiteAccessNeedsAttention: Bool {
        model.browserProfiles.contains { $0.status == .needsUser || $0.status == .authenticationRequired }
    }

    private var groupSidebar: some View {
        VStack(alignment: .leading, spacing: 2) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Settings").veetType(.title)
                Text("This \(deviceName) and your Veetbot server.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.horizontal, 8)
            .padding(.bottom, 12)
            Text("Groups")
                .veetType(.meta, weight: .semibold)
                .foregroundColor(AppTheme.inkSoft)
                .padding(.horizontal, 8)
                .padding(.bottom, 4)
                .accessibilityAddTraits(.isHeader)
            ForEach(ConnectionSettingsSection.visibleCases) { section in
                Button { selectedSection = section } label: {
                    HStack(spacing: 10) {
                        Image(systemName: section.systemImage)
                            .frame(width: 18)
                            .foregroundColor(AppTheme.inkSoft)
                            .accessibilityHidden(true)
                        Text(section.title).foregroundColor(AppTheme.ink)
                        Spacer(minLength: 6)
                        if section == .websiteAccess && websiteAccessNeedsAttention {
                            Circle().fill(AppTheme.amber).frame(width: 7, height: 7)
                                .accessibilityLabel("Needs sign-in")
                        }
                    }
                    .veetType(.ui)
                    .padding(.horizontal, 8)
                    .frame(minHeight: A2Metrics.rowHeight)
                    .background(
                        RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .fill(selectedSection == section ? AppTheme.selected : Color.clear)
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(selectedSection == section ? .isSelected : [])
                .accessibilityIdentifier("settings.group.\(section.rawValue)")
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .frame(width: 232, alignment: .topLeading)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(AppTheme.paperDeep)
    }

    private var groupList: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                header
                VStack(spacing: 0) {
                    ForEach(Array(ConnectionSettingsSection.visibleCases.enumerated()), id: \.element) { index, section in
                        if index > 0 { HairlineDivider() }
                        NavigationLink(value: section) {
                            HStack(spacing: 12) {
                                Image(systemName: section.systemImage)
                                    .frame(width: 22)
                                    .foregroundColor(AppTheme.inkSoft)
                                    .accessibilityHidden(true)
                                Text(section.title).foregroundColor(AppTheme.ink)
                                Spacer(minLength: 8)
                                Text(groupSummary(section))
                                    .foregroundColor(AppTheme.inkSoft)
                                    .lineLimit(1)
                                if section == .websiteAccess && websiteAccessNeedsAttention {
                                    Circle().fill(AppTheme.amber).frame(width: 7, height: 7)
                                        .accessibilityLabel("Needs sign-in")
                                }
                                Image(systemName: "chevron.right")
                                    .veetType(.meta, weight: .semibold)
                                    .foregroundColor(AppTheme.inkFaint)
                                    .accessibilityHidden(true)
                            }
                            .veetType(.ui)
                            .padding(.horizontal, 14)
                            .frame(minHeight: A2Metrics.rowHeight)
                            .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                        .accessibilityIdentifier("settings.group.\(section.rawValue)")
                    }
                }
                .a2Card()
                .padding(.horizontal, 16)
                Text("Models and website access are saved on the server; everything else stays on this \(deviceName).")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                    .padding(.horizontal, 20)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(.bottom, 24)
        }
        .background(AppTheme.paper)
    }

    /// The short value the iPhone list shows beside a group.
    private func groupSummary(_ section: ConnectionSettingsSection) -> String {
        if section == .connection { return model.baseURL?.host ?? "" }
        if section == .websiteAccess {
            let count = model.browserProfiles.count
            return count == 0 ? "" : "\(count) \(count == 1 ? "site" : "sites")"
        }
        if section == .appearance { return appearance.theme.label }
        #if os(iOS)
        if section == .smsIntegration { return smsIntegration.integrationEnabled ? "On" : "Off" }
        #endif
        if section == .dataAndPrivacy { return ClientBuildIdentity.current.version }
        return ""
    }

    private func groupPage(_ section: ConnectionSettingsSection) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) { sectionView(section) }
                .frame(maxWidth: 680, alignment: .leading)
                .padding(24)
                .frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .background(AppTheme.paper)
    }

    private var taskApprovalWebsites: some View {
        DisclosureGroup("Task approval websites") {
            VStack(alignment: .leading, spacing: 10) {
                Text("Saved on the server and shared across your devices. These websites can offer Allow All Actions for this Task: up to 30 minutes and 200 actions. Sensitive actions still need separate approval.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                if let policy = model.taskScopePolicy {
                    if policy.scopes.isEmpty { Text("No websites added.").foregroundColor(AppTheme.inkSoft) }
                    ForEach(policy.scopes) { scope in
                        HStack {
                            Text(scope.id).veetMono().foregroundColor(AppTheme.ink).textSelection(.enabled)
                            Spacer()
                            Button("Remove", role: .destructive) {
                                Task { _ = await model.removeTaskScope(scope) }
                            }
                            .buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red))
                            .disabled(model.isSavingTaskScopes)
                            .accessibilityIdentifier("task-scopes.remove.\(scope.id)")
                        }
                    }
                    Text("Removing a website ends its active task permissions.")
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    TextField("https://www.duolingo.com/lesson", text: $taskScopeURL)
                        .textFieldStyle(.plain)
                        .a2Field(radius: 12)
                        .accessibilityIdentifier("task-scopes.url")
                        #if os(iOS)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                        #endif
                    Button("Add website") { confirmingTaskScope = true }
                        .buttonStyle(SecondaryCapsuleButtonStyle())
                        .disabled(model.isSavingTaskScopes || policy.scopes.count >= 16 || TaskGrantEcho.website(taskScopeURL) == nil)
                        .accessibilityIdentifier("task-scopes.add")
                        .confirmationDialog("Allow task approval for this website?", isPresented: $confirmingTaskScope, titleVisibility: .visible) {
                            Button("Add website") {
                                if let scope = TaskGrantEcho.website(taskScopeURL) {
                                    Task { if await model.addTaskScope(scope) { taskScopeURL = "" } }
                                }
                            }
                        } message: {
                            Text("\(taskScopeURL) and paths below it can offer task permission. Each task still requires your approval.")
                        }
                }
                if let error = model.taskScopeError {
                    Text(error).foregroundColor(AppTheme.red).veetType(.meta)
                        .accessibilityIdentifier("task-scopes.error")
                }
                Button("Refresh websites") { Task { await model.refreshTaskScopes() } }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .disabled(model.isSavingTaskScopes)
                    .accessibilityIdentifier("task-scopes.refresh")
            }.padding(.top, 8)
        }
    }

    /// No second sign-in starts while a remote one is open (either mode).
    private var websiteAccessBlocked: Bool {
        model.isManagingWebsiteAccess
            || websiteURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            || (model.browserAuthentication.map { $0.status != .ready } ?? false)
    }

    /// The device this app runs on, as the Connect page names it.
    private var deviceName: String {
        #if os(macOS)
        return "Mac"
        #else
        return UIDevice.current.userInterfaceIdiom == .pad ? "iPad" : "iPhone"
        #endif
    }

    /// First run opens with the brand mark and the Connect page's title; the
    /// configured surface keeps a short "Settings" header.
    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 10) {
                VeetbotBrandMark(size: 22)
                Text("VEETBOT")
                    .veetType(.meta, weight: .semibold)
                    .tracking(0.8)
                    .foregroundColor(AppTheme.inkSoft)
                Spacer(minLength: 12)
            }
            if embedded {
                Text("Connect this \(deviceName) to your Veetbot.")
                    .veetType(.display)
                    .fixedSize(horizontal: false, vertical: true)
                Text(
                    "Your assistant runs on a server you control. Enter its address and the access token it gave you. The token is kept in this \(deviceName)'s Keychain and sent only to that server, to sign you in."
                )
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
            } else {
                Text("Settings")
                    .veetType(.display)
                Text("This \(deviceName) and your Veetbot server.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
            }
        }
        .frame(maxWidth: embedded ? 560 : 680, alignment: .leading)
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private func sectionView(_ section: ConnectionSettingsSection) -> some View {
        switch section {
        case .connection:
            SettingsCard(
                title: "Connection",
                summary: "The server Veetbot uses and the credential for this device.",
                systemImage: "network",
                tint: AppTheme.tealText
            ) {
                VStack(alignment: .leading, spacing: 16) {
                    if model.isConfigured { connectionStatusRows }
                    settingsField(
                        title: "Server address",
                        help:
                            "HTTPS is required. Queries, fragments, and embedded credentials are not allowed."
                    ) {
                        TextField("https://agent.example.com", text: $baseURL)
                            .textFieldStyle(.plain)
                            .veetMono()
                            .a2Field(radius: 12)
                            #if os(iOS)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                            #endif
                    }

                    settingsField(
                        title: "Bearer token",
                        help: "Stored only in Keychain; it is never written to preferences."
                    ) {
                        HStack(spacing: 6) {
                            Group {
                                if showsToken {
                                    TextField("Static bearer token", text: $token)
                                        .veetMono()
                                        .autocorrectionDisabled()
                                        #if os(iOS)
                                        .textInputAutocapitalization(.never)
                                        #endif
                                } else {
                                    SecureField("Static bearer token", text: $token)
                                }
                            }
                            .textContentType(.password)
                            .textFieldStyle(.plain)
                            Button { showsToken.toggle() } label: {
                                Image(systemName: showsToken ? "eye.slash" : "eye")
                            }
                            .buttonStyle(QuietIconButtonStyle())
                            .accessibilityLabel(showsToken ? "Hide token" : "Show token")
                            .accessibilityIdentifier("settings.token.visibility")
                        }
                        .a2Field(radius: 12)
                    }

                    if model.requiresReauthentication {
                        Label(
                            "The server rejected the saved credential. Enter a valid token.",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .veetType(.ui, weight: .medium)
                        .foregroundColor(AppTheme.red)
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(AppTheme.red.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                    }

                    HStack {
                        Spacer()
                        connectionAction
                    }
                }
            }

        case .models:
            ModelSettingsCard(isConfigured: model.isConfigured)

        case .websiteAccess:
            SettingsCard(
                title: "Website Access",
                summary: "Create a dedicated login profile for sites Veetbot may use.",
                systemImage: "globe.badge.chevron.backward",
                tint: AppTheme.amberText
            ) {
                if model.isConfigured {
                    VStack(alignment: .leading, spacing: 16) {
                        taskApprovalWebsites
                        settingsField(
                            title: "Website URL",
                            help:
                                "Enter the website’s home page or login page. Sign in on this device opens it in a private window here; Veetbot's remote browser opens it in an isolated browser instead. HTTPS is added if omitted."
                        ) {
                            TextField("example.com or https://example.com/login", text: $websiteURL)
                                .textFieldStyle(.plain)
                                .a2Field(radius: 12)
                                .accessibilityIdentifier("website-access.url")
                                #if os(iOS)
                            .textContentType(.URL)
                            .textInputAutocapitalization(.never)
                            .keyboardType(.URL)
                                #endif
                        }
                        Text(
                            "Veetbot reuses your saved browser session. Sign in again when the website requires it; Veetbot does not save your password for automatic login."
                        )
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                        Button {
                            model.beginDeviceSignIn(websiteURL: websiteURL)
                        } label: {
                            Label("Sign in on this device", systemImage: "person.badge.key.fill")
                        }
                        .primaryCapsule()
                        .disabled(websiteAccessBlocked)
                        .accessibilityIdentifier("website-access.sign-in-on-device")
                        Button {
                            addWebsiteAccess()
                        } label: {
                            Label("Use Veetbot's remote browser", systemImage: "globe")
                        }
                        .buttonStyle(SecondaryCapsuleButtonStyle())
                        .disabled(websiteAccessBlocked)
                        .accessibilityIdentifier("website-access.remote-browser")

                        if let authenticationLaunchURL = model.websiteAuthenticationLaunchURL {
                            VStack(alignment: .leading, spacing: 10) {
                                Label("Secure login ready", systemImage: "lock.shield.fill")
                                    .veetType(.ui, weight: .semibold)
                                    .foregroundColor(AppTheme.tealText)
                                Text(
                                    "Continue in your web browser, then use the remote-browser image to sign in. Keep that page open—reloading or copying its one-time link requires starting over."
                                )
                                .veetType(.meta)
                                .foregroundColor(AppTheme.inkSoft)
                                .fixedSize(horizontal: false, vertical: true)
                                Button {
                                    openWebsiteAuthentication(authenticationLaunchURL)
                                } label: {
                                    Label("Continue in web browser", systemImage: "safari")
                                }
                                .primaryCapsule()
                            }
                            .padding(14)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(
                                RoundedRectangle(cornerRadius: 12, style: .continuous).fill(AppTheme.paperDeep)
                            )
                        }

                        if let authentication = model.browserAuthentication {
                            HairlineDivider()
                            HStack(spacing: 10) {
                                Label(
                                    authentication.status.displayName,
                                    systemImage: authentication.status.systemImage
                                )
                                .veetType(.meta, weight: .semibold)
                                .foregroundColor(AppTheme.inkSoft)
                                Spacer()
                                if !authentication.status.isTerminal {
                                    Button("Check login status") {
                                        Task { await model.refreshBrowserAuthentication() }
                                    }
                                    .buttonStyle(SecondaryCapsuleButtonStyle())
                                    .disabled(model.isManagingWebsiteAccess)
                                }
                                if authentication.status != .ready {
                                    Button("Start over") {
                                        Task { await model.cancelWebsiteAccessSetup() }
                                    }
                                    .buttonStyle(QuietCapsuleButtonStyle())
                                    .disabled(model.isManagingWebsiteAccess)
                                }
                            }
                            if !authentication.status.isTerminal {
                                Text(
                                    "Finish signing in on the secure browser page, return here, and check the status. Start over if the page was closed, reloaded, or expired."
                                )
                                .veetType(.meta)
                                .foregroundColor(AppTheme.inkSoft)
                                .fixedSize(horizontal: false, vertical: true)
                            }
                        }

                        HairlineDivider()
                        if model.browserProfiles.isEmpty {
                            Text("No website logins have been added yet.")
                                .veetType(.meta)
                                .foregroundColor(AppTheme.inkSoft)
                        } else {
                            ForEach(model.browserProfiles) { profile in
                                websiteProfileRow(profile)
                            }
                        }
                        // ADR-0129: a compact row, not a List section, so no
                        // row the UI tests assert moves (CS13).
                        if !model.activeTaskGrants.isEmpty {
                            HStack(spacing: 10) {
                                Image(systemName: "checkmark.shield.fill")
                                    .foregroundColor(AppTheme.tealText)
                                Text("Task permissions")
                                    .veetType(.ui, weight: .semibold)
                                    .foregroundColor(AppTheme.ink)
                                Spacer(minLength: 8)
                                Menu {
                                    ForEach(model.activeTaskGrants) { grant in
                                        Button(role: .destructive) {
                                            Task { await model.stopTaskGrant(grant.id) }
                                        } label: {
                                            Text("Stop: \(grant.bannerText(now: Date()))")
                                        }
                                    }
                                } label: {
                                    Text("\(model.activeTaskGrants.count) active")
                                }
                                .fixedSize()
                            }
                            .padding(12)
                            .background(
                                RoundedRectangle(cornerRadius: 12, style: .continuous).fill(AppTheme.paperDeep)
                            )
                            .accessibilityIdentifier("website-access.task-permissions")
                        }
                    }
                } else {
                    Text("Connect this app to Veetbot before adding a website login.")
                        .veetType(.ui)
                        .foregroundColor(AppTheme.inkSoft)
                }
            }

        case .smsIntegration:
            smsIntegrationSection

        case .notifications:
            SettingsCard(
                title: "Notifications",
                summary: "The devices your Veetbot notifies when an approval, a question or a report is waiting.",
                systemImage: "bell",
                tint: AppTheme.tealText
            ) {
                if model.isConfigured {
                    NotificationDevicesSection(model: notificationSettings, chat: model)
                } else {
                    Text("Connect this app to Veetbot before managing notifications.")
                        .veetType(.ui)
                        .foregroundColor(AppTheme.inkSoft)
                }
            }

        case .appearance:
            SettingsCard(
                title: "Appearance",
                summary: "Saved on this device and applied at once.",
                systemImage: "textformat",
                tint: AppTheme.amberText
            ) {
                VStack(alignment: .leading, spacing: 16) {
                    settingsField(
                        title: "Theme",
                        help: "System follows this device's light or dark setting."
                    ) {
                        Picker("Theme", selection: $appearance.theme) {
                            ForEach(AppThemePreference.allCases) { theme in
                                Text(theme.label).tag(theme)
                            }
                        }
                        .labelsHidden()
                        .pickerStyle(.segmented)
                        .frame(maxWidth: 320)
                        .accessibilityIdentifier("settings.appearance.theme")
                    }

                    settingsField(
                        title: "Text size",
                        help: VeetType.Platform.current == .mac
                            ? "Sizes conversation text and both panels. System uses the design's standard sizes."
                            : "System follows the device accessibility setting."
                    ) {
                        Picker("Text size", selection: $appearance.textSize) {
                            ForEach(AppTextSize.allCases) { size in
                                Text(size.label).tag(size)
                            }
                        }
                        .labelsHidden()
                        .settingsPickerStyle()
                    }

                    settingsField(
                        title: "Typeface",
                        help: "Changes conversation text. Panels keep the system face; code remains monospaced."
                    ) {
                        Picker("Typeface", selection: $appearance.fontStyle) {
                            ForEach(AppFontStyle.allCases) { style in
                                Text(style.label).tag(style)
                            }
                        }
                        .labelsHidden()
                        .settingsPickerStyle()
                    }

                    VStack(alignment: .leading, spacing: 6) {
                        Text("Preview")
                            .veetType(.meta, weight: .semibold)
                            .foregroundColor(AppTheme.inkSoft)
                        Text("The quick brown fox meets a helpful bot.")
                            .veetType(.ui)
                            .foregroundColor(AppTheme.ink)
                        Text("Clear, comfortable, and ready to work.")
                            .veetType(.meta)
                            .foregroundColor(AppTheme.inkSoft)
                    }
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(
                        RoundedRectangle(cornerRadius: 12, style: .continuous).fill(AppTheme.paperDeep)
                    )
                }
            }

        case .dataAndPrivacy:
            SettingsCard(
                title: "Data & Privacy",
                summary: "A quick view of what this client keeps on the device.",
                systemImage: "lock.shield",
                tint: AppTheme.tealText
            ) {
                VStack(alignment: .leading, spacing: 14) {
                    SettingsInfoRow(
                        icon: "key.fill",
                        title: "Credential",
                        detail: "The bearer token is device-local and protected by Keychain."
                    )
                    SettingsInfoRow(
                        icon: "textformat.size",
                        title: "Preferences",
                        detail: "Text size, typeface, and the server address stay on this device."
                    )
                    SettingsInfoRow(
                        icon: "info.circle.fill",
                        title: "Client build",
                        detail: ClientBuildIdentity.current.displayName
                    )

                    SettingsInfoRow(
                        icon: "clock.arrow.circlepath",
                        title: "Local history",
                        detail: "A cache of the server's chats, reconciled when this app connects."
                    )

                    if model.isConfigured {
                        HairlineDivider()
                        clearCacheControl
                        Button(role: .destructive) {
                            forgetConnection()
                        } label: {
                            Label("Forget saved connection", systemImage: "trash")
                        }
                        .buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red))
                        .disabled(isSaving)
                    }
                }
            }
        }
    }

    /// The owner's switch for the device SMS integration. Off by default, and
    /// off means the device never declares the capability, so the tool never
    /// registers (docs/plan/device-channel-and-sms.md).
    @ViewBuilder
    private var smsIntegrationSection: some View {
        #if os(iOS)
        SettingsCard(
            title: "SMS Integration",
            summary: "Let Veetbot prepare texts you send from this iPhone.",
            systemImage: "message",
            tint: AppTheme.tealText
        ) {
            VStack(alignment: .leading, spacing: 16) {
                settingsField(
                    title: "Send texts from this iPhone",
                    help:
                        "Veetbot opens the Messages compose sheet with the text prepared. Nothing is sent until you tap Send."
                ) {
                    Toggle(
                        "Send texts from this iPhone",
                        isOn: $smsIntegration.integrationEnabled
                    )
                    .labelsHidden()
                    .accessibilityIdentifier("sms-integration.enabled")
                    .onChange(of: smsIntegration.integrationEnabled) {
                        model.requestDeviceCapabilityRegistration()
                    }
                }
                SettingsInfoRow(
                    icon: "hand.tap.fill",
                    title: "You approve every message",
                    detail:
                        "The recipient and the text appear in the system compose sheet before anything is sent."
                )
            }
        }
        #else
        EmptyView()
        #endif
    }

    /// Where this device talks to, and when the token last worked.
    private var connectionStatusRows: some View {
        VStack(alignment: .leading, spacing: 0) {
            statusRow("Server") {
                Text(model.baseURL?.absoluteString ?? "").veetMono().textSelection(.enabled)
            }
            HairlineDivider()
            statusRow("Token") {
                Text(["Saved in Keychain", ConnectionVerification.phrase(for: model.lastVerifiedAt, now: Date())]
                    .compactMap { $0 }.joined(separator: " · "))
            }
            HairlineDivider()
            statusRow("Status") {
                if model.requiresReauthentication {
                    Label("The server rejected the token", systemImage: "exclamationmark.triangle")
                        .foregroundColor(AppTheme.red)
                } else {
                    Label("Connected", systemImage: "checkmark").foregroundColor(AppTheme.ink)
                }
            }
        }
        .a2Card()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("settings.connection.status")
    }

    private func statusRow<Value: View>(_ key: String, @ViewBuilder value: () -> Value) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(key).foregroundColor(AppTheme.inkSoft).frame(width: 72, alignment: .leading)
            value().frame(maxWidth: .infinity, alignment: .leading)
        }
        .veetType(.ui)
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    /// Clearing asks first, in place: Clear in red, Keep beside it.
    @ViewBuilder
    private var clearCacheControl: some View {
        if confirmingClearCache {
            VStack(alignment: .leading, spacing: 8) {
                Text("Clear the local cache? Chats stay on the server and reload.")
                    .veetType(.ui)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("Clear") {
                        confirmingClearCache = false
                        Task { await model.clearLocalCache() }
                    }
                    .buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red))
                    .accessibilityIdentifier("settings.clear-cache.confirm")
                    Button("Keep") { confirmingClearCache = false }
                        .buttonStyle(QuietCapsuleButtonStyle())
                }
            }
        } else {
            Button {
                confirmingClearCache = true
            } label: {
                Label("Clear local cache", systemImage: "arrow.counterclockwise")
            }
            .buttonStyle(QuietCapsuleButtonStyle())
            .accessibilityIdentifier("settings.clear-cache")
        }
    }

    private func settingsField<Content: View>(
        title: String,
        help: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title)
                .veetType(.meta, weight: .semibold)
                .foregroundColor(AppTheme.inkSoft)
            content()
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(help)
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func websiteProfileRow(_ profile: BrowserProfileView) -> some View {
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: "globe")
                .foregroundColor(AppTheme.inkSoft)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(profile.allowedOrigins.joined(separator: ", "))
                    .veetMono()
                    .foregroundColor(AppTheme.ink)
                Text(profile.status.displayName)
                    .veetType(.meta)
                    .foregroundColor(profile.status == .needsUser ? AppTheme.amberText : AppTheme.inkSoft)
            }
            Spacer(minLength: 8)
            if WebsiteAccessActions.offersSignInAgain(profile.status) {
                Button("Sign in again") {
                    model.beginDeviceSignIn(profile: profile)
                }
                .buttonStyle(SecondaryCapsuleButtonStyle())
                .disabled(model.isManagingWebsiteAccess)
                .accessibilityIdentifier("website-access.sign-in-again.\(profile.id.uuidString)")
            }
            if profile.status == .ready {
                if model.selectedBrowserProfileID == profile.id {
                    Label("Used for new chats", systemImage: "checkmark.circle.fill")
                        .veetType(.meta, weight: .semibold)
                        .foregroundColor(AppTheme.tealText)
                } else {
                    Button("Use for new chats") {
                        Task { await model.selectBrowserProfile(profile.id) }
                    }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .disabled(model.isManagingWebsiteAccess)
                }
            }
            Button(role: .destructive) {
                Task { await model.removeBrowserProfile(profile.id) }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(QuietIconButtonStyle())
            .accessibilityLabel("Remove")
            .disabled(model.isManagingWebsiteAccess)
        }
        .padding(.vertical, 6)
    }

    private var connectionAction: some View {
        Button {
            saveConnection()
        } label: {
            Label(
                isSaving ? "Saving…" : (model.isConfigured ? "Update Connection" : "Connect"),
                systemImage: isSaving ? "hourglass" : "checkmark.circle.fill"
            )
        }
        .primaryCapsule()
        .keyboardShortcut(.defaultAction)
        .disabled(
            isSaving
                || baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        )
    }

    private var actionBar: some View {
        HStack {
            Spacer()
            Button("Close") { close() }
                .buttonStyle(QuietCapsuleButtonStyle())
                .keyboardShortcut(.cancelAction)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .background(AppTheme.paper)
    }

    private func close() {
        if let closeAction {
            closeAction()
        } else {
            dismiss()
        }
    }

    private func forgetConnection() {
        isSaving = true
        Task {
            await model.forgetCredentials()
            isSaving = false
            token = ""
            if !embedded { close() }
        }
    }

    private func saveConnection() {
        isSaving = true
        if embedded { onConnecting?() }
        Task {
            let configured = await model.configure(baseURLString: baseURL, token: token)
            isSaving = false
            if configured {
                token = ""
                if !embedded { close() }
            }
        }
    }

    private func addWebsiteAccess() {
        Task {
            await model.createWebsiteAccess(
                websiteURL: websiteURL
            )
        }
    }

    private func openWebsiteAuthentication(_ launchURL: URL) {
        openURL(launchURL) { accepted in
            if accepted {
                model.websiteAuthenticationLaunchOpened()
                websiteURL = ""
            } else {
                Task { await model.websiteAuthenticationLaunchFailed() }
            }
        }
    }
}

extension View {
    /// Frames settings presented as a sheet from Chat's Website Access button:
    /// on the Mac, resizable and opening at the settings window's size.
    @ViewBuilder
    func websiteAccessSheetFrame() -> some View {
        #if os(macOS)
        sheetFrame(
            minWidth: SettingsWindowConfiguration.minimumSize.width,
            idealWidth: SettingsWindowConfiguration.initialSize.width,
            minHeight: SettingsWindowConfiguration.minimumSize.height,
            idealHeight: SettingsWindowConfiguration.initialSize.height
        )
        #else
        self
        #endif
    }
}

extension BrowserProfileStatus {
    var displayName: String {
        switch self {
        case .provisioning: return "Provisioning"
        case .authenticationRequired: return "Login required"
        case .ready: return "Ready"
        case .needsUser: return "Needs your attention"
        case .revoked: return "Revoked"
        }
    }
}

private extension BrowserAuthenticationStatus {
    var displayName: String {
        switch self {
        case .authenticationRequired: return "Waiting for login"
        case .needsUser: return "The login needs your attention"
        case .ready: return "Login saved"
        case .expired: return "Login window expired"
        case .cancelled: return "Login cancelled"
        }
    }

    var systemImage: String {
        switch self {
        case .ready: return "checkmark.shield.fill"
        case .expired, .cancelled: return "xmark.circle"
        case .authenticationRequired, .needsUser: return "person.crop.circle.badge.clock"
        }
    }
}

/// The owner's chat and memory models (`/v1/settings/models`). The card owns
/// its view model: a published change re-renders only the card, never the
/// Settings form around it. Each picker change saves the whole state at once;
/// the pickers lock while a save is in flight, and a failed save falls back to
/// the server's last values.
private struct ModelSettingsCard: View {
    let isConfigured: Bool
    @StateObject private var modelSettings = ModelSettingsViewModel()

    var body: some View {
        SettingsCard(
            title: "Models",
            summary: "Choose the models Veetbot uses to chat and to form memories.",
            systemImage: "cpu",
            tint: AppTheme.tealText
        ) {
            if isConfigured {
                VStack(alignment: .leading, spacing: 16) {
                    if modelSettings.chat != nil, modelSettings.memory != nil,
                        !modelSettings.unavailable
                    {
                        modelGroup(
                            title: "Chat",
                            footnote:
                                "A model change applies to new chats. Reasoning applies from your next message."
                        ) {
                            modelRow("Model") {
                                Picker("Chat model", selection: chatModelSelection) {
                                    ForEach(modelSettings.chatModelChoices) { choice in
                                        Text(choice.displayName).tag(choice.modelPolicy)
                                    }
                                }
                                .labelsHidden()
                                .pickerStyle(.menu)
                                .disabled(!modelSettings.isEditable)
                                .accessibilityIdentifier("settings.models.chat.model")
                            }
                            modelRow("Reasoning") {
                                Picker("Chat reasoning", selection: chatEffortSelection) {
                                    ForEach(modelSettings.chatEffortPickerValues, id: \.self) { effort in
                                        Text(ReasoningEffort.displayName(for: effort)).tag(effort)
                                    }
                                }
                                .labelsHidden()
                                .pickerStyle(.menu)
                                .disabled(
                                    !modelSettings.isEditable
                                        || modelSettings.chatEffortChoices.isEmpty
                                )
                                .accessibilityIdentifier("settings.models.chat.effort")
                            }
                        }

                        HairlineDivider()

                        modelGroup(
                            title: "Memory",
                            footnote:
                                "Only combinations that passed Veetbot's memory evaluation are offered."
                        ) {
                            modelRow("Model") {
                                Picker("Memory model", selection: memoryModelSelection) {
                                    ForEach(modelSettings.memoryModelChoices) { choice in
                                        Text(choice.displayName).tag(choice.modelPolicy)
                                    }
                                }
                                .labelsHidden()
                                .pickerStyle(.menu)
                                .disabled(!modelSettings.isEditable)
                                .accessibilityIdentifier("settings.models.memory.model")
                            }
                            modelRow("Reasoning") {
                                Picker("Memory reasoning", selection: memoryEffortSelection) {
                                    ForEach(modelSettings.memoryEffortPickerValues, id: \.self) { effort in
                                        Text(ReasoningEffort.displayName(for: effort)).tag(effort)
                                    }
                                }
                                .labelsHidden()
                                .pickerStyle(.menu)
                                .disabled(
                                    !modelSettings.isEditable
                                        || modelSettings.memoryEffortChoices.count < 2
                                )
                                .accessibilityIdentifier("settings.models.memory.effort")
                            }
                        }
                    } else if modelSettings.isLoading {
                        ProgressView()
                            .controlSize(.small)
                    }

                    if let status = modelSettings.statusMessage {
                        Text(status)
                            .veetType(.meta)
                            .foregroundColor(AppTheme.inkSoft)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("settings.models.status")
                    }
                }
            } else {
                Text("Connect this app to Veetbot before choosing models.")
                    .veetType(.ui)
                    .foregroundColor(AppTheme.inkSoft)
            }
        }
        .onAppear {
            if isConfigured {
                Task { await modelSettings.load() }
            }
        }
    }

    private var chatModelSelection: Binding<String> {
        Binding(
            get: { modelSettings.chat?.modelPolicy ?? "" },
            set: { policy in Task { await modelSettings.selectChatModel(policy) } }
        )
    }

    private var chatEffortSelection: Binding<ReasoningEffort?> {
        Binding(
            get: { modelSettings.chat?.reasoningEffort },
            set: { effort in Task { await modelSettings.selectChatEffort(effort) } }
        )
    }

    private var memoryModelSelection: Binding<String> {
        Binding(
            get: { modelSettings.memory?.modelPolicy ?? "" },
            set: { policy in Task { await modelSettings.selectMemoryModel(policy) } }
        )
    }

    private var memoryEffortSelection: Binding<ReasoningEffort?> {
        Binding(
            get: { modelSettings.memory?.reasoningEffort },
            set: { effort in Task { await modelSettings.selectMemoryEffort(effort) } }
        )
    }

    private func modelGroup<Content: View>(
        title: String,
        footnote: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            A2SectionHeading(title: title)
            content()
            Text(footnote)
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func modelRow<Content: View>(
        _ label: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        HStack(spacing: 12) {
            Text(label)
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
                .frame(width: 96, alignment: .leading)
            content()
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct SettingsCard<Content: View>: View {
    let title: String
    let summary: String
    let systemImage: String
    let tint: Color
    let content: Content

    init(
        title: String,
        summary: String,
        systemImage: String,
        tint: Color,
        @ViewBuilder content: () -> Content
    ) {
        self.title = title
        self.summary = summary
        self.systemImage = systemImage
        self.tint = tint
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Image(systemName: systemImage)
                    .foregroundColor(tint)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .veetType(.title)
                        .foregroundColor(AppTheme.ink)
                    Text(summary)
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            content
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .a2Card()
    }
}

private struct SettingsInfoRow: View {
    let icon: String
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: icon)
                .foregroundColor(AppTheme.inkSoft)
                .frame(width: 20)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .veetType(.ui, weight: .semibold)
                    .foregroundColor(AppTheme.ink)
                Text(detail)
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

extension View {
    @ViewBuilder
    fileprivate func settingsPickerStyle() -> some View {
        #if os(macOS)
        pickerStyle(.segmented)
        #else
        pickerStyle(.menu)
        #endif
    }
}
