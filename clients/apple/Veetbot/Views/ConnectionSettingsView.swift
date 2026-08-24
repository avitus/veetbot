import SwiftUI

enum ConnectionSettingsSection: String, CaseIterable, Identifiable {
    case connection
    case websiteAccess
    case appearance
    case dataAndPrivacy

    var id: String { rawValue }
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
    @EnvironmentObject private var appearance: AppearancePreferences
    @State private var baseURL = ""
    @State private var token = ""
    @State private var isSaving = false
    @State private var websiteOrigin = ""
    @State private var websiteLoginURL = ""

    public init(
        model: ChatViewModel,
        embedded: Bool,
        onClose: (() -> Void)? = nil
    ) {
        self.model = model
        self.embedded = embedded
        closeAction = onClose
    }

    public var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 18) {
                    ForEach(ConnectionSettingsSection.allCases) { section in
                        sectionView(section)
                    }
                }
                .padding(24)
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            Divider()
            actionBar
        }
        .background(Color.primary.opacity(0.025))
        #if os(macOS)
        .frame(
            minWidth: SettingsWindowConfiguration.minimumSize.width,
            minHeight: SettingsWindowConfiguration.minimumSize.height
        )
        #endif
        .onAppear {
            if baseURL.isEmpty { baseURL = model.baseURL?.absoluteString ?? "" }
            if model.isConfigured {
                Task { await model.refreshBrowserProfiles() }
            }
        }
    }

    private var header: some View {
        HStack(spacing: 14) {
            VeetbotBrandMark(size: 27)
                .frame(width: 58)
            VStack(alignment: .leading, spacing: 2) {
                Text(embedded ? "Set up Veetbot" : "Settings")
                    .appFont(.title2, weight: .semibold)
                Text(
                    embedded
                        ? "Connect securely, then choose how Veetbot looks."
                        : "Connection, appearance, and device-local data."
                )
                .appFont(.caption)
                .foregroundColor(.secondary)
            }
            Spacer(minLength: 12)
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 16)
        .background(AppTheme.brandGradient.opacity(0.6))
    }

    @ViewBuilder
    private func sectionView(_ section: ConnectionSettingsSection) -> some View {
        switch section {
        case .connection:
            SettingsCard(
                title: "Connection",
                summary: "The server Veetbot uses and the credential for this device.",
                systemImage: "network",
                tint: AppTheme.turquoise
            ) {
                VStack(alignment: .leading, spacing: 16) {
                    settingsField(
                        title: "Server address",
                        help:
                            "HTTPS is required. Queries, fragments, and embedded credentials are not allowed."
                    ) {
                        TextField("https://agent.example.com", text: $baseURL)
                            .textFieldStyle(.roundedBorder)
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
                        SecureField("Static bearer token", text: $token)
                            .textContentType(.password)
                            .textFieldStyle(.roundedBorder)
                    }

                    if model.requiresReauthentication {
                        Label(
                            "The server rejected the saved credential. Enter a valid token.",
                            systemImage: "exclamationmark.triangle.fill"
                        )
                        .foregroundColor(.red)
                        .padding(12)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.red.opacity(0.08))
                        .clipShape(RoundedRectangle(cornerRadius: 9))
                    }
                }
            }

        case .websiteAccess:
            SettingsCard(
                title: "Website Access",
                summary: "Create a dedicated login profile for sites Veetbot may use.",
                systemImage: "globe.badge.chevron.backward",
                tint: AppTheme.orange
            ) {
                if model.isConfigured {
                    VStack(alignment: .leading, spacing: 16) {
                        settingsField(
                            title: "Website origin",
                            help: "Use one exact HTTPS origin, such as https://example.com."
                        ) {
                            TextField("https://example.com", text: $websiteOrigin)
                                .textFieldStyle(.roundedBorder)
                                .accessibilityIdentifier("website-access.origin")
                                #if os(iOS)
                            .textContentType(.URL)
                            .textInputAutocapitalization(.never)
                            .keyboardType(.URL)
                                #endif
                        }
                        settingsField(
                            title: "Login page",
                            help:
                                "Veetbot opens this page in an isolated browser. Enter your username, password, passkey, or MFA there—not in chat or these settings."
                        ) {
                            TextField("https://example.com/login", text: $websiteLoginURL)
                                .textFieldStyle(.roundedBorder)
                                .accessibilityIdentifier("website-access.login-url")
                                #if os(iOS)
                            .textContentType(.URL)
                            .textInputAutocapitalization(.never)
                            .keyboardType(.URL)
                                #endif
                        }
                        Button {
                            addWebsiteAccess()
                        } label: {
                            Label("Create secure login", systemImage: "person.badge.key.fill")
                        }
                        .buttonStyle(.borderedProminent)
                        .tint(AppTheme.turquoise)
                        .disabled(
                            model.isManagingWebsiteAccess
                                || websiteOrigin.trimmingCharacters(in: .whitespacesAndNewlines)
                                .isEmpty
                                || websiteLoginURL.trimmingCharacters(in: .whitespacesAndNewlines)
                                .isEmpty
                                || (model.browserAuthentication.map { $0.status != .ready } ?? false)
                        )

                        if let authenticationLaunchURL = model.websiteAuthenticationLaunchURL {
                            VStack(alignment: .leading, spacing: 10) {
                                Label("Secure login ready", systemImage: "lock.shield.fill")
                                    .appFont(.headline)
                                    .foregroundColor(AppTheme.turquoise)
                                Text(
                                    "Continue in your web browser, then use the remote-browser image to sign in. Keep that page open—reloading or copying its one-time link requires starting over."
                                )
                                .appFont(.caption)
                                .foregroundColor(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                                Button {
                                    openWebsiteAuthentication(authenticationLaunchURL)
                                } label: {
                                    Label("Continue in web browser", systemImage: "safari")
                                }
                                .buttonStyle(.borderedProminent)
                                .tint(AppTheme.orange)
                            }
                            .padding(14)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(AppTheme.turquoise.opacity(0.08))
                            .clipShape(RoundedRectangle(cornerRadius: 10))
                        }

                        if let authentication = model.browserAuthentication {
                            Divider()
                            HStack(spacing: 10) {
                                Label(
                                    authentication.status.displayName,
                                    systemImage: authentication.status.systemImage
                                )
                                .appFont(.caption, weight: .semibold)
                                Spacer()
                                if !authentication.status.isTerminal {
                                    Button("Check login status") {
                                        Task { await model.refreshBrowserAuthentication() }
                                    }
                                    .disabled(model.isManagingWebsiteAccess)
                                }
                                if authentication.status != .ready {
                                    Button("Start over") {
                                        Task { await model.cancelWebsiteAccessSetup() }
                                    }
                                    .disabled(model.isManagingWebsiteAccess)
                                }
                            }
                            if !authentication.status.isTerminal {
                                Text(
                                    "Finish signing in on the secure browser page, return here, and check the status. Start over if the page was closed, reloaded, or expired."
                                )
                                .appFont(.caption)
                                .foregroundColor(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                            }
                        }

                        Divider()
                        if model.browserProfiles.isEmpty {
                            Text("No website logins have been added yet.")
                                .appFont(.caption)
                                .foregroundColor(.secondary)
                        } else {
                            ForEach(model.browserProfiles) { profile in
                                websiteProfileRow(profile)
                            }
                        }
                    }
                } else {
                    Text("Connect this app to Veetbot before adding a website login.")
                        .appFont(.body)
                        .foregroundColor(.secondary)
                }
            }

        case .appearance:
            SettingsCard(
                title: "Appearance",
                summary: "Typography changes are saved automatically and apply immediately.",
                systemImage: "textformat",
                tint: AppTheme.orange
            ) {
                VStack(alignment: .leading, spacing: 16) {
                    settingsField(
                        title: "Text size",
                        help: "System follows the device accessibility setting."
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
                        help: "Code and terminal output remain monospaced."
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
                            .appFont(.caption, weight: .semibold)
                            .foregroundColor(.secondary)
                        Text("The quick brown fox meets a helpful bot.")
                            .appFont(.body)
                        Text("Clear, comfortable, and ready to work.")
                            .appFont(.caption)
                            .foregroundColor(.secondary)
                    }
                    .padding(14)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(AppTheme.brandGradient.opacity(0.38))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                }
            }

        case .dataAndPrivacy:
            SettingsCard(
                title: "Data & Privacy",
                summary: "A quick view of what this client keeps on the device.",
                systemImage: "lock.shield",
                tint: AppTheme.turquoise
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

                    if model.isConfigured {
                        Divider()
                        Button(role: .destructive) {
                            forgetConnection()
                        } label: {
                            Label("Forget saved connection", systemImage: "trash")
                        }
                        .disabled(isSaving)
                    }
                }
            }
        }
    }

    private func settingsField<Content: View>(
        title: String,
        help: String,
        @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title)
                .appFont(.headline)
            content()
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(help)
                .appFont(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func websiteProfileRow(_ profile: BrowserProfileView) -> some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 4) {
                Text(profile.allowedOrigins.joined(separator: ", "))
                    .appFont(.body, weight: .semibold)
                Text(profile.status.displayName)
                    .appFont(.caption)
                    .foregroundColor(.secondary)
            }
            Spacer(minLength: 8)
            if profile.status == .ready {
                if model.selectedBrowserProfileID == profile.id {
                    Label("Used for new chats", systemImage: "checkmark.circle.fill")
                        .appFont(.caption, weight: .semibold)
                        .foregroundColor(AppTheme.turquoise)
                } else {
                    Button("Use") {
                        Task { await model.selectBrowserProfile(profile.id) }
                    }
                    .disabled(model.isManagingWebsiteAccess)
                }
            }
            Button(role: .destructive) {
                Task { await model.removeBrowserProfile(profile.id) }
            } label: {
                Image(systemName: "trash")
            }
            .buttonStyle(.borderless)
            .disabled(model.isManagingWebsiteAccess)
        }
        .padding(12)
        .background(Color.primary.opacity(0.035))
        .clipShape(RoundedRectangle(cornerRadius: 9))
    }

    private var actionBar: some View {
        HStack(spacing: 12) {
            if !embedded {
                Button("Close") { close() }
                    .keyboardShortcut(.cancelAction)
            }
            Spacer()
            Button {
                saveConnection()
            } label: {
                Label(
                    isSaving ? "Saving…" : (model.isConfigured ? "Save Connection" : "Connect"),
                    systemImage: isSaving ? "hourglass" : "checkmark.circle.fill"
                )
            }
            .buttonStyle(.borderedProminent)
            .tint(AppTheme.turquoise)
            .keyboardShortcut(.defaultAction)
            .disabled(
                isSaving
                    || baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            )
        }
        .padding(.horizontal, 24)
        .padding(.vertical, 12)
        .background(.regularMaterial)
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
                origin: websiteOrigin,
                loginURL: websiteLoginURL
            )
        }
    }

    private func openWebsiteAuthentication(_ launchURL: URL) {
        openURL(launchURL) { accepted in
            if accepted {
                model.websiteAuthenticationLaunchOpened()
                websiteOrigin = ""
                websiteLoginURL = ""
            } else {
                Task { await model.websiteAuthenticationLaunchFailed() }
            }
        }
    }
}

private extension BrowserProfileStatus {
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
            HStack(alignment: .top, spacing: 12) {
                Image(systemName: systemImage)
                    .foregroundColor(tint)
                    .frame(width: 30, height: 30)
                    .background(tint.opacity(0.12))
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                VStack(alignment: .leading, spacing: 3) {
                    Text(title)
                        .appFont(.title3, weight: .semibold)
                    Text(summary)
                        .appFont(.caption)
                        .foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            content
        }
        .padding(20)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.045))
        .clipShape(RoundedRectangle(cornerRadius: 14))
        .overlay(
            RoundedRectangle(cornerRadius: 14)
                .stroke(Color.primary.opacity(0.09))
        )
    }
}

private struct SettingsInfoRow: View {
    let icon: String
    let title: String
    let detail: String

    var body: some View {
        HStack(alignment: .top, spacing: 11) {
            Image(systemName: icon)
                .foregroundColor(.secondary)
                .frame(width: 20)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .appFont(.headline)
                Text(detail)
                    .appFont(.caption)
                    .foregroundColor(.secondary)
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
