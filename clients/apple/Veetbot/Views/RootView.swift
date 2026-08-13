import SwiftUI

public struct RootView: View {
    @ObservedObject var model: ChatViewModel
    @State private var showingSettings = false

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
        .sheet(isPresented: $showingSettings) {
            ConnectionSettingsView(model: model, embedded: false)
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
    }

    @ViewBuilder
    private var configuredContent: some View {
        if #available(iOS 16.0, macOS 13.0, *) {
            NavigationSplitView {
                SessionSidebar(model: model, showingSettings: $showingSettings)
            } detail: {
                ChatView(model: model)
            }
        } else {
            NavigationView {
                SessionSidebar(model: model, showingSettings: $showingSettings)
                ChatView(model: model)
            }
        }
    }
}

private struct SessionSidebar: View {
    @ObservedObject var model: ChatViewModel
    @Binding var showingSettings: Bool
    @State private var removalCandidate: SessionHistoryEntry?

    var body: some View {
        List {
            Button {
                model.newSession()
            } label: {
                Label("New conversation", systemImage: "square.and.pencil")
            }

            Section("History") {
                ForEach(model.history) { entry in
                    HStack(spacing: 8) {
                        Button {
                            Task { await model.selectSession(entry) }
                        } label: {
                            VStack(alignment: .leading, spacing: 3) {
                                Text(entry.title)
                                    .lineLimit(2)
                                    .foregroundColor(.primary)
                                Text(entry.updatedAt, style: .relative)
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        .buttonStyle(.plain)

                        Button(role: .destructive) {
                            removalCandidate = entry
                        } label: {
                            Image(systemName: "trash")
                                .foregroundColor(.secondary)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Remove \(entry.title) from history")
                    }
                    .listRowBackground(
                        entry.sessionID == model.selectedSessionID
                            ? Color.accentColor.opacity(0.12)
                            : Color.clear
                    )
                }
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("Veetbot")
        .confirmationDialog(
            "Remove conversation from history?",
            isPresented: Binding(
                get: { removalCandidate != nil },
                set: { if !$0 { removalCandidate = nil } }
            ),
            titleVisibility: .visible,
            presenting: removalCandidate
        ) { entry in
            Button("Remove from History", role: .destructive) {
                removalCandidate = nil
                Task { await model.removeSessionFromHistory(entry) }
            }
            Button("Cancel", role: .cancel) {
                removalCandidate = nil
            }
        } message: { _ in
            Text("This removes the conversation from this device. Server data is not deleted.")
        }
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button { showingSettings = true } label: {
                    Image(systemName: "gearshape")
                }
                .accessibilityLabel("Connection settings")
            }
        }
    }
}
