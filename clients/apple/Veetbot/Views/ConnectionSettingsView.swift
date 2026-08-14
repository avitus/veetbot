import SwiftUI

public struct ConnectionSettingsView: View {
    @ObservedObject var model: ChatViewModel
    let embedded: Bool
    @Environment(\.dismiss) private var dismiss
    @EnvironmentObject private var appearance: AppearancePreferences
    @State private var baseURL = ""
    @State private var token = ""
    @State private var isSaving = false

    public init(model: ChatViewModel, embedded: Bool) {
        self.model = model
        self.embedded = embedded
    }

    public var body: some View {
        Group {
            if embedded {
                settingsNavigation
            } else {
                settingsNavigation.toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { dismiss() }
                    }
                }
            }
        }
        #if os(macOS)
        .frame(minWidth: 620, idealWidth: 680, minHeight: 580, idealHeight: 660)
        #endif
    }

    private var settingsNavigation: some View {
        NavigationView {
            VStack(spacing: 0) {
                form
                Divider()
                actionBar
            }
            .navigationTitle(embedded ? "Set up Veetbot" : "Settings")
        }
    }

    private var form: some View {
        Form {
            Section {
                HStack(spacing: 18) {
                    VeetbotBrandMark(size: 34)
                        .frame(width: 86)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(embedded ? "Welcome" : "Make Veetbot yours")
                            .appFont(.headline)
                        Text("Connection and appearance settings stay on this device.")
                            .appFont(.caption)
                            .foregroundColor(.secondary)
                    }
                }
                .padding(.vertical, 8)
            }
            .listRowBackground(AppTheme.brandGradient)

            Section {
                TextField("https://agent.example.com", text: $baseURL)
                    #if os(iOS)
                    .textContentType(.URL)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
                    #endif
                Text("HTTPS is required. Plaintext http:// targets are rejected.")
                    .appFont(.caption)
                    .foregroundColor(.secondary)
            } header: {
                Label("Server", systemImage: "network")
                    .foregroundColor(AppTheme.turquoise)
            }

            Section {
                SecureField("Static bearer token", text: $token)
                    .textContentType(.password)
                Text("The token is stored in Keychain and never in UserDefaults.")
                    .appFont(.caption)
                    .foregroundColor(.secondary)
            } header: {
                Label("Bearer token", systemImage: "key.fill")
                    .foregroundColor(AppTheme.orange)
            }

            if model.requiresReauthentication {
                Section {
                    Label(
                        "The server rejected the saved credential. Enter a valid token.",
                        systemImage: "exclamationmark.triangle.fill"
                    )
                    .foregroundColor(.red)
                }
            }

            Section {
                Picker("Text size", selection: $appearance.textSize) {
                    ForEach(AppTextSize.allCases) { size in
                        Text(size.label).tag(size)
                    }
                }
                Picker("Font style", selection: $appearance.fontStyle) {
                    ForEach(AppFontStyle.allCases) { style in
                        Text(style.label).tag(style)
                    }
                }

                VStack(alignment: .leading, spacing: 5) {
                    Text("The quick brown fox meets a helpful bot.")
                        .appFont(.body)
                    Text("Changes are saved automatically and applied throughout the client.")
                        .appFont(.caption)
                        .foregroundColor(.secondary)
                }
                .padding(.vertical, 4)
            } header: {
                Label("Appearance", systemImage: "textformat")
                    .foregroundColor(AppTheme.turquoise)
            }
        }
    }

    private var actionBar: some View {
        HStack(spacing: 12) {
            if model.isConfigured {
                Button(role: .destructive) {
                    Task {
                        await model.forgetCredentials()
                        if !embedded { dismiss() }
                    }
                } label: {
                    Label("Forget token", systemImage: "trash")
                }
                .buttonStyle(.bordered)
                .frame(maxWidth: .infinity)
            }

            Button {
                saveConnection()
            } label: {
                Label(
                    isSaving ? "Saving…" : (model.isConfigured ? "Save connection" : "Connect"),
                    systemImage: isSaving ? "hourglass" : "checkmark.circle.fill"
                )
            }
            .buttonStyle(.borderedProminent)
            .tint(AppTheme.turquoise)
            .frame(maxWidth: .infinity)
            .disabled(
                isSaving
                    || baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            )
        }
        .padding(.horizontal)
        .padding(.vertical, 12)
        .background(.regularMaterial)
        .onAppear {
            if baseURL.isEmpty { baseURL = model.baseURL?.absoluteString ?? "" }
        }
    }

    private func saveConnection() {
        isSaving = true
        Task {
            await model.configure(baseURLString: baseURL, token: token)
            isSaving = false
            if model.isConfigured {
                token = ""
                if !embedded { dismiss() }
            }
        }
    }
}
