import AuthenticationServices
import SwiftUI

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

/// Settings → MCP servers (ADR-0255), inside the group's card, shown once
/// this app is connected. The section owns its view model, as the Models
/// card does, so a published change re-renders only the section. Rows keep
/// every text a test reads beside their buttons, never inside a label.
struct MCPServerSettingsSection: View {
    @StateObject private var settings = MCPServerSettingsViewModel()
    @Environment(\.scenePhase) private var scenePhase
    @State private var name = ""
    @State private var url = ""
    @State private var access: MCPServerAccess = .changes
    @State private var configuration = ""
    @State private var showsConfiguration = false
    @State private var confirmingRemoval: String?

    var body: some View {
        content
            .task { await settings.load() }
            // A sign-in finished in the browser, or a server added on another
            // device, is read back when the app returns.
            .onChange(of: scenePhase) { _, phase in
                if phase == .active, !settings.isBusy {
                    Task { await settings.load() }
                }
            }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(
                "Saved on the server and shared across your devices. New chats can use a server once it's ready. Read-only servers run without asking; the others ask before every call."
            )
            .veetType(.meta)
            .foregroundColor(AppTheme.inkSoft)
            .fixedSize(horizontal: false, vertical: true)

            if settings.notEnabled {
                Label(MCPServerSettingsViewModel.notEnabledMessage, systemImage: "info.circle")
                    .veetType(.ui)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("mcp-servers.not-enabled")
            } else {
                serverList
                if let error = settings.errorMessage {
                    Text(error)
                        .veetType(.meta)
                        .foregroundColor(AppTheme.red)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("mcp-servers.error")
                }
                if settings.hasLoaded {
                    HairlineDivider()
                    addForm
                }
            }
        }
    }

    // MARK: The list

    @ViewBuilder
    private var serverList: some View {
        if settings.servers.isEmpty {
            if settings.hasLoaded || settings.isLoading || settings.errorMessage == nil {
                Text(settings.hasLoaded ? "No MCP servers yet." : "Loading servers…")
                    .veetType(.ui)
                    .foregroundColor(AppTheme.inkSoft)
            } else {
                Button("Try again") { Task { await settings.load() } }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .accessibilityIdentifier("mcp-servers.reload")
            }
        } else {
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array(settings.servers.enumerated()), id: \.element.id) { index, server in
                    if index > 0 { HairlineDivider() }
                    serverRow(server)
                }
            }
            .a2Card()
        }
    }

    private func serverRow(_ server: MCPServerView) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(server.id)
                    .veetMono()
                    .foregroundColor(AppTheme.ink)
                    .textSelection(.enabled)
                    .accessibilityIdentifier("mcp-servers.name.\(server.id)")
                if !server.isOwnerManaged {
                    Text("Built in")
                        .veetType(.meta, weight: .semibold)
                        .foregroundColor(AppTheme.tealText)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(Capsule().fill(AppTheme.tealWash))
                        .accessibilityIdentifier("mcp-servers.built-in.\(server.id)")
                }
                Spacer(minLength: 8)
                Text(server.status.label)
                    .veetType(.meta, weight: .semibold)
                    .foregroundColor(server.status == .ready ? AppTheme.tealText : AppTheme.amberText)
                    .accessibilityIdentifier("mcp-servers.status.\(server.id)")
            }
            Text(detail(for: server))
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("mcp-servers.detail.\(server.id)")
            if server.isOwnerManaged {
                ViewThatFits(in: .horizontal) {
                    HStack(spacing: 8) { rowActions(server) }
                    VStack(alignment: .leading, spacing: 6) { rowActions(server) }
                }
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
    }

    /// The host, the access level, and when a sign-in last succeeded.
    private func detail(for server: MCPServerView) -> String {
        var parts: [String] = []
        if let host = server.host { parts.append(host) }
        parts.append(server.access.label)
        if server.usesSignIn {
            if let signedInAt = server.signedInAt, server.status == .ready {
                parts.append("Signed in \(signedInAt.formatted(.relative(presentation: .named)))")
            } else if let signInHost = server.signInHost {
                parts.append("Signs in at \(signInHost)")
            }
        }
        return parts.joined(separator: " · ")
    }

    @ViewBuilder
    private func rowActions(_ server: MCPServerView) -> some View {
        let busy = settings.isBusy
        if server.usesSignIn {
            if server.status == .ready {
                Button("Sign out") { Task { await settings.signOut(server) } }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .disabled(busy)
                    .accessibilityIdentifier("mcp-servers.sign-out.\(server.id)")
            } else {
                Button("Sign in") { Task { await settings.signIn(server) } }
                    .buttonStyle(SecondaryCapsuleButtonStyle())
                    .disabled(busy)
                    .accessibilityIdentifier("mcp-servers.sign-in.\(server.id)")
            }
        }
        Menu {
            ForEach(MCPServerAccess.choices) { choice in
                Button {
                    Task { await settings.setAccess(choice, for: server) }
                } label: {
                    if choice == server.access {
                        Label(choice.label, systemImage: "checkmark")
                    } else {
                        Text(choice.label)
                    }
                }
            }
        } label: {
            Text("Access")
        }
        .fixedSize()
        .disabled(busy)
        .accessibilityIdentifier("mcp-servers.access.\(server.id)")
        if confirmingRemoval == server.id {
            Text("Remove \(server.id)?")
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
            Button("Remove") {
                confirmingRemoval = nil
                Task { await settings.remove(server) }
            }
            .buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red))
            .disabled(busy)
            .accessibilityIdentifier("mcp-servers.remove.confirm.\(server.id)")
            Button("Keep") { confirmingRemoval = nil }
                .buttonStyle(QuietCapsuleButtonStyle())
        } else {
            Button("Remove", role: .destructive) { confirmingRemoval = server.id }
                .buttonStyle(QuietCapsuleButtonStyle(tint: AppTheme.red))
                .disabled(busy)
                .accessibilityIdentifier("mcp-servers.remove.\(server.id)")
        }
    }

    // MARK: Adding

    private var normalizedName: String { MCPServerInput.normalizedName(name) }

    private var addForm: some View {
        VStack(alignment: .leading, spacing: 14) {
            A2SectionHeading(title: "Add a server")
            field(
                "Name",
                help: normalizedName.isEmpty || normalizedName == name
                    ? "Lowercase letters, digits and underscores, starting with a letter."
                    : "Saved as \(normalizedName)."
            ) {
                TextField("Name", text: $name, prompt: .a2Prompt("magicpath"))
                    .textFieldStyle(.plain)
                    .veetMono()
                    .autocorrectionDisabled()
                    .a2Field(radius: 12)
                    .accessibilityIdentifier("mcp-servers.add.name")
                    #if os(iOS)
                    .textInputAutocapitalization(.never)
                    #endif
            }
            field("URL", help: "The server's https address, such as https://api.magicpath.ai/mcp.") {
                TextField("URL", text: $url, prompt: .a2Prompt("https://example.com/mcp"))
                    .textFieldStyle(.plain)
                    .veetMono()
                    .autocorrectionDisabled()
                    .a2Field(radius: 12)
                    .accessibilityIdentifier("mcp-servers.add.url")
                    #if os(iOS)
                    .textContentType(.URL)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    #endif
            }
            field(
                "Access",
                help: "Read-only suits servers that only look things up. Asks before each call is safer for a server that can change things."
            ) {
                Picker("Access", selection: $access) {
                    ForEach(MCPServerAccess.choices) { choice in
                        Text(choice.label).tag(choice)
                    }
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .fixedSize()
                .accessibilityIdentifier("mcp-servers.add.access")
            }
            Button {
                Task {
                    if await settings.add(name: name, url: url, access: access) {
                        name = ""
                        url = ""
                    }
                }
            } label: {
                Label(settings.isAdding ? "Adding…" : "Add server", systemImage: "plus")
            }
            .primaryCapsule()
            .disabled(
                settings.isBusy || !settings.canAddMore
                    || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                    || url.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            )
            .accessibilityIdentifier("mcp-servers.add")

            DisclosureGroup("Paste a configuration", isExpanded: $showsConfiguration) {
                configurationForm.padding(.top, 8)
            }
            .accessibilityIdentifier("mcp-servers.paste")

            if !settings.canAddMore {
                Text(MCPServerSettingsViewModel.limitMessage)
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
            }
            if let error = settings.addErrorMessage {
                Text(error)
                    .veetType(.meta)
                    .foregroundColor(AppTheme.red)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("mcp-servers.add.error")
            }
        }
    }

    private var configurationForm: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(
                "Paste the JSON a service gives for its MCP server, such as {\"mcpServers\": {\"magicpath\": {\"url\": \"https://api.magicpath.ai/mcp\"}}}. Each server is added with the access chosen above."
            )
            .veetType(.meta)
            .foregroundColor(AppTheme.inkSoft)
            .fixedSize(horizontal: false, vertical: true)
            TextField(
                "Configuration", text: $configuration,
                prompt: .a2Prompt("{\"mcpServers\": { … }}"), axis: .vertical
            )
            .lineLimit(3...8)
            .textFieldStyle(.plain)
            .veetMono()
            .autocorrectionDisabled()
            .a2Field(radius: 12)
            .accessibilityIdentifier("mcp-servers.paste.text")
            #if os(iOS)
            .textInputAutocapitalization(.never)
            #endif
            if case .success(let drafts) = MCPServerConfiguration.parse(configuration) {
                Text("Found \(drafts.map(\.name).joined(separator: ", ")).")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                    .accessibilityIdentifier("mcp-servers.paste.found")
            }
            Button("Add from configuration") {
                Task {
                    if await settings.addConfiguration(configuration, access: access) {
                        configuration = ""
                    }
                }
            }
            .buttonStyle(SecondaryCapsuleButtonStyle())
            .disabled(
                settings.isBusy || !settings.canAddMore
                    || configuration.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            )
            .accessibilityIdentifier("mcp-servers.paste.add")
        }
    }

    private func field<Content: View>(
        _ title: String, help: String, @ViewBuilder content: () -> Content
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
}

// MARK: - The authentication session

/// Opens the authorization page in the system's web authentication session
/// and returns the `veetbot://mcp-sign-in` URL it ends at. The shared browser
/// session is kept, so a sign-in the browser already holds carries over.
@MainActor
final class WebAuthenticationSessionPresenter: NSObject, MCPSignInPresenting,
    ASWebAuthenticationPresentationContextProviding
{
    private var session: ASWebAuthenticationSession?
    /// The window the session presents over, found before it starts; the
    /// session asks for it only after `start()`.
    private var anchor: ASPresentationAnchor!

    func authenticate(url: URL, callbackScheme: String) async throws -> URL {
        session?.cancel()
        guard let anchor = Self.currentWindow() else { throw MCPSignInPresentationError.couldNotStart }
        self.anchor = anchor
        return try await withCheckedThrowingContinuation { continuation in
            let resume = ResumeOnce(continuation)
            let session = ASWebAuthenticationSession(
                url: url, callback: .customScheme(callbackScheme)
            ) { [weak self] callbackURL, error in
                Task { @MainActor in self?.session = nil }
                if let callbackURL {
                    resume.resume(returning: callbackURL)
                } else if let error = error as? ASWebAuthenticationSessionError,
                    error.code == .canceledLogin
                {
                    resume.resume(throwing: MCPSignInPresentationError.cancelled)
                } else {
                    resume.resume(throwing: error ?? MCPSignInPresentationError.couldNotStart)
                }
            }
            session.presentationContextProvider = self
            session.prefersEphemeralWebBrowserSession = false
            self.session = session
            if !session.start() {
                self.session = nil
                resume.resume(throwing: MCPSignInPresentationError.couldNotStart)
            }
        }
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        anchor
    }

    /// The key window: on the Mac the Settings window the owner tapped in.
    private static func currentWindow() -> ASPresentationAnchor? {
        #if os(macOS)
        return NSApplication.shared.keyWindow
            ?? NSApplication.shared.mainWindow
            ?? NSApplication.shared.windows.first { $0.isVisible }
        #else
        let scenes = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
        let windows = scenes.flatMap(\.windows)
        if let window = windows.first(where: \.isKeyWindow) ?? windows.first { return window }
        return scenes.first.map { ASPresentationAnchor(windowScene: $0) }
        #endif
    }
}

/// The session may report both a failed start and a completion; the caller
/// hears only the first.
private final class ResumeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<URL, Error>?

    init(_ continuation: CheckedContinuation<URL, Error>) { self.continuation = continuation }

    func resume(returning url: URL) { take()?.resume(returning: url) }
    func resume(throwing error: Error) { take()?.resume(throwing: error) }

    private func take() -> CheckedContinuation<URL, Error>? {
        lock.withLock {
            defer { continuation = nil }
            return continuation
        }
    }
}
