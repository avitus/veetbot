import SwiftUI

public struct ConnectionSettingsView: View {
    @ObservedObject var model: ChatViewModel
    let embedded: Bool
    @Environment(\.dismiss) private var dismiss
    @State private var baseURL = ""
    @State private var token = ""

    public init(model: ChatViewModel, embedded: Bool) {
        self.model = model
        self.embedded = embedded
    }

    public var body: some View {
        NavigationView {
            if embedded {
                form
            } else {
                form.toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { dismiss() }
                    }
                }
            }
        }
    }

    private var form: some View {
        Form {
            Section("Server") {
                TextField("https://agent.example.com", text: $baseURL)
#if os(iOS)
                    .textContentType(.URL)
                    .textInputAutocapitalization(.never)
                    .keyboardType(.URL)
#endif
                Text("HTTPS is required. Plaintext http:// targets are rejected.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            Section("Bearer token") {
                SecureField("Static bearer token", text: $token)
                    .textContentType(.password)
                Text("The token is stored in Keychain and never in UserDefaults.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
            if model.requiresReauthentication {
                Text("The server rejected the saved credential. Enter a valid token.")
                    .foregroundColor(.red)
            }
            Section {
                Button("Save connection") {
                    Task {
                        await model.configure(baseURLString: baseURL, token: token)
                        if model.isConfigured && !embedded { dismiss() }
                    }
                }
                .disabled(baseURL.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                if model.isConfigured {
                    Button("Forget bearer token", role: .destructive) {
                        Task { await model.forgetCredentials() }
                    }
                }
            }
        }
        .navigationTitle("Connection")
        .onAppear {
            if baseURL.isEmpty { baseURL = model.baseURL?.absoluteString ?? "" }
        }
    }
}
