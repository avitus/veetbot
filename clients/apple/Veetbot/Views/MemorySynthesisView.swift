import SwiftUI

/// An evidence trail, from the derived claim to its retained originals.
struct MemorySynthesisBrowserView: View {
    @StateObject private var model = MemorySynthesisViewModel()
    @State private var selectedID: UUID?
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        List {
            Section {
                Label("Ideas, with their evidence", systemImage: "point.3.connected.trianglepath.dotted")
                    .appFont(.headline)
                Text("Explore summaries, tentative connections, conflicts and duplicate merges. Open a claim to see why it exists; your original memories are kept.")
                    .appFont(.caption).foregroundColor(.secondary)
                Picker("Synthesis kind", selection: Binding(get: { model.kind ?? "all" }, set: { value in
                    Task { await model.select(kind: value == "all" ? nil : value, state: model.state) }
                })) {
                    Text("All").tag("all")
                    Text("Summaries").tag("summary")
                    Text("Connections").tag("hypothesis")
                    Text("Conflicts").tag("conflict")
                    Text("Merges").tag("merge")
                }.pickerStyle(.menu).accessibilityIdentifier("memory.synthesis.kind")
                Picker("History", selection: Binding(get: { model.state ?? "all" }, set: { value in
                    Task { await model.select(kind: model.kind, state: value == "all" ? nil : value) }
                })) {
                    Text("All history").tag("all")
                    Text("Committed").tag("committed")
                    Text("Invalidated").tag("invalidated")
                    Text("Undone").tag("undone")
                }.accessibilityIdentifier("memory.synthesis.state")
                Button("Refresh journal") { Task { await model.reload() } }
            }
            ForEach(model.items) { operation in
                Button { selectedID = operation.id } label: {
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Label(operation.title, systemImage: operation.kind == "merge" ? "square.on.square" : "sparkles")
                                .appFont(.caption, weight: .semibold)
                            Spacer()
                            Text(memoryDisplayText(operation.state)).appFont(.caption)
                        }
                        if let content = operation.content {
                            Text(content.statement).appFont(.body).lineLimit(4)
                            Text("\(operation.sources.count) original memories · \(Set(operation.sources.map(\.sessionID)).count) conversations")
                                .appFont(.caption).foregroundColor(.secondary)
                        } else {
                            Text("Source details unavailable").foregroundColor(.secondary)
                        }
                        Text(operation.createdAt, style: .date).appFont(.caption2).foregroundColor(.secondary)
                    }.padding(.vertical, 6)
                }.accessibilityIdentifier("memory.synthesis.row.\(operation.id.uuidString)")
            }
            if model.isLoading { ProgressView("Loading synthesis…") }
            else if let error = model.errorMessage {
                Text(error).foregroundColor(.secondary)
                Button("Retry") { Task { await retryRead() } }
            } else if model.items.isEmpty {
                Text("No synthesis in this view yet.").foregroundColor(.secondary)
                Text("Original memories remain in Memories. Browsing this journal does not start dreaming.")
                    .appFont(.caption).foregroundColor(.secondary)
            }
            if model.hasMore && !model.isLoading {
                Button("Load more history") { Task { await model.loadMore() } }
                    .accessibilityIdentifier("memory.synthesis.more")
            }
        }
        .accessibilityIdentifier("memory.synthesis")
        .background {
            NavigationLink(isActive: Binding(get: { selectedID != nil }, set: { if !$0 { selectedID = nil } })) {
                if let selectedID { MemorySynthesisDetailView(id: selectedID) }
            } label: { EmptyView() }
        }
        .task { await model.reload() }
        .onChange(of: selectedID) { selection in
            if selection == nil { Task { await model.reload() } }
        }
        .onChange(of: scenePhase) { phase in
            if phase == .active { Task { await model.reload() } } else { model.clearContent() }
        }
        .onReceive(NotificationCenter.default.publisher(for: .memorySynthesisChanged)) { _ in
            model.clearContent()
        }
    }
    private func retryRead() async {
        if model.hasMore { await model.loadMore() } else { await model.reload() }
    }
}

struct MemorySynthesisDetailView: View {
    let id: UUID
    @StateObject private var model = MemorySynthesisViewModel()
    @State private var previewUndo = false
    @State private var confirmDelete = false
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        List {
            if model.isLoading { ProgressView("Checking current evidence…") }
            if let summary = model.summary {
                Section(summary.recordKind == "hypothesis" ? "Tentative connection · derived memory" : "Related summary · derived memory") {
                    if summary.flaggedForReview { Label("Needs review", systemImage: "flag.fill") }
                    if let content = summary.content {
                        Text(content.statement).appFont(.headline)
                        MemorySensitivityBadge(sensitivity: content.sensitivity, kind: MemorySensitivityKind(rawValue: content.sensitivity))
                        Text("Evidence last observed \(content.lastEvidenceAt.formatted()). Combining it does not make it new evidence.")
                            .appFont(.caption).foregroundColor(.secondary)
                        Text("\(memoryDisplayText(content.authority)) authority · \(memoryDisplayText(content.portability))")
                            .appFont(.caption).foregroundColor(.secondary)
                    } else { Text("Synthesis content is no longer available.") }
                }
                if let content = summary.content {
                    claims(content.clauses, sources: summary.sources)
                    originals(summary.sources)
                }
                Section("Your control") {
                    if summary.status == "active", summary.content != nil {
                        if summary.flaggedForReview {
                            Button("Mark reviewed") { Task { await model.review(.dismiss) } }
                                .accessibilityIdentifier("memory.synthesis.review")
                        }
                        Button("Not true") { Task { await model.review(.untrue) } }
                            .accessibilityIdentifier("memory.synthesis.untrue")
                        if summary.content?.portability != "local" {
                            Button("Not relevant here") { Task { await model.review(.notHere) } }
                        }
                    }
                    Button("Delete synthesis…", role: .destructive) { confirmDelete = true }
                        .accessibilityIdentifier("memory.synthesis.delete")
                    Text("These actions affect this synthesis. Its original memories are kept.")
                        .appFont(.caption).foregroundColor(.secondary)
                }.disabled(model.isSaving || model.canRetryWrite)
            } else if let operation = model.operation {
                Section(operation.title) {
                    if let content = operation.content {
                        Text(content.statement).appFont(.headline)
                    } else {
                        Text("Source details unavailable")
                        Text("This history entry keeps the outcome, without retaining unavailable memory content.")
                            .appFont(.caption).foregroundColor(.secondary)
                    }
                    Text(memoryDisplayText(operation.state)).appFont(.caption, weight: .semibold)
                }
                if operation.content != nil {
                    claims(operation.content?.clauses ?? [], sources: operation.sources)
                    originals(operation.sources)
                }
                if operation.canUndo {
                    Section {
                        Button("Preview undo merge") { previewUndo = true }
                            .accessibilityIdentifier("memory.synthesis.undo.preview")
                            .disabled(model.isSaving || model.canRetryWrite)
                    }
                }
            }
            if let operation = model.operation {
                Section("History") {
                    Text("Created \(operation.createdAt.formatted())")
                    Text("Outcome: \(memoryDisplayText(operation.reason))")
                    if let time = operation.undoneAt { Text("Undone \(time.formatted())") }
                    if let time = operation.invalidatedAt { Text("Invalidated \(time.formatted())") }
                    DisclosureGroup("Technical details") {
                        Text("Policy: \(operation.policy)")
                        Text("Model: \(operation.modelIdentity)")
                        Text("Revision: \(operation.revision)")
                    }
                }.appFont(.caption)
            }
            if !model.canRetryWrite && !model.isLoading && !model.isSaving {
                Button("Refresh evidence") { Task { await model.open(id) } }
            }
        }
        .accessibilityIdentifier("memory.synthesis.detail")
        .safeAreaInset(edge: .bottom) { actionFeedback }
        .navigationTitle("Synthesis")
        .task { await model.open(id) }
        .onChange(of: scenePhase) { phase in
            if phase == .active { Task { await model.open(id) } }
            else { previewUndo = false; confirmDelete = false; model.clearContent() }
        }
        .sheet(isPresented: $previewUndo) { undoPreview }
        .confirmationDialog("Delete this synthesis?", isPresented: $confirmDelete, titleVisibility: .visible) {
            Button("Delete synthesis", role: .destructive) { Task { await model.deleteSummary() } }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This synthesis and its generated copies are removed and blocked from forming again. Original memories and source messages are kept.")
        }
    }

    @ViewBuilder private var actionFeedback: some View {
        if model.isSaving || model.message != nil || model.errorMessage != nil || model.canRetryWrite {
            VStack(alignment: .leading, spacing: 8) {
                if model.isSaving { ProgressView("Saving…") }
                if let message = model.message {
                    Label(message, systemImage: "checkmark.circle")
                        .accessibilityIdentifier("memory.synthesis.receipt")
                }
                if let error = model.errorMessage { Text(error).foregroundColor(.secondary) }
                if model.canRetryWrite {
                    Button("Retry same action") { Task { await model.retryWrite() } }
                        .disabled(model.isSaving).accessibilityIdentifier("memory.synthesis.retry.write")
                }
            }
            .appFont(.callout)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding().background(.regularMaterial)
        }
    }

    @ViewBuilder private func claims(_ clauses: [MemorySynthesisClause], sources: [MemorySynthesisSource]) -> some View {
        if !clauses.isEmpty {
            Section("Why these claims?") {
                ForEach(Array(clauses.enumerated()), id: \.offset) { index, clause in
                    DisclosureGroup {
                        ForEach(sources.filter { clause.sourceIDs.contains($0.id) }) { source in
                            sourceLink(source)
                        }
                    } label: { Text(clause.text) }
                    .accessibilityIdentifier("memory.synthesis.claim.\(index)")
                }
            }
        }
    }
    @ViewBuilder private func originals(_ sources: [MemorySynthesisSource]) -> some View {
        if !sources.isEmpty {
            Section("Supporting originals") {
                ForEach(sources) { source in sourceLink(source) }
            }
        }
    }
    private func sourceLink(_ source: MemorySynthesisSource) -> some View {
        NavigationLink {
            MemorySynthesisOriginalView(id: source.id)
        } label: {
            VStack(alignment: .leading, spacing: 5) {
                Text(source.statement)
                Text(source.omitted ? "Original · not included in summary text" : "Original memory")
                    .appFont(.caption).foregroundColor(.secondary)
            }
        }.accessibilityIdentifier("memory.synthesis.source.\(source.id.uuidString)")
    }
    private var undoPreview: some View {
        NavigationView {
            List {
                if let operation = model.operation, operation.canUndo {
                    Section("What will change") {
                        Text("These originals will be recalled independently again. Undo does not restore erased, expired or changed memories, and this merge will not be recreated automatically.")
                    }
                    originals(operation.sources)
                    Button("Undo merge") {
                        previewUndo = false
                        Task { await model.undo() }
                    }.accessibilityIdentifier("memory.synthesis.undo.confirm")
                } else { Text("Refresh the operation before undoing it.") }
            }
            .navigationTitle("Undo merge")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { previewUndo = false } } }
        }
        .accessibilityIdentifier("memory.synthesis.undo.sheet")
        #if os(macOS)
        .frame(minWidth: 420, minHeight: 360)
        #endif
    }
}

private struct MemorySynthesisOriginalView: View {
    let id: UUID
    @State private var original: MemoryView?
    @State private var error: String?
    @State private var requestID = UUID()
    @Environment(\.scenePhase) private var scenePhase
    var body: some View {
        Group {
            if let original { MemoryDetailView(memory: original) }
            else if let error {
                VStack { Text(error); Button("Retry") { Task { await refresh() } } }.padding()
            } else { ProgressView("Checking original memory…") }
        }
        .task { await refresh() }
        .onDisappear { clear() }
        .onReceive(NotificationCenter.default.publisher(for: .peopleConnectionChanged)) { _ in clear() }
        .onChange(of: scenePhase) { phase in
            if phase == .active { Task { await refresh() } } else { clear() }
        }
    }
    private func clear() { requestID = UUID(); original = nil; error = nil }
    private func refresh() async {
        clear()
        let token = requestID
        do {
            guard let api = await MemoryViewModel.makeDefaultAPIClient() else { throw HTTPTransportError.notConfigured }
            guard token == requestID, !Task.isCancelled else { return }
            let value = try await api.getMemory(id, ceiling: memoryBrowsingCeiling)
            guard token == requestID, !Task.isCancelled else { return }
            original = value
        } catch {
            guard token == requestID, !Task.isCancelled else { return }
            self.error = synthesisErrorMessage(error)
        }
    }
}
