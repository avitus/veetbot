import SwiftUI

/// An evidence trail, from the derived claim to its retained originals, drawn
/// as an A2 list: a kind line over each claim, soft ink for the counts, teal
/// text for the actions.
struct MemorySynthesisBrowserView: View {
    @StateObject private var model = MemorySynthesisViewModel()
    @State private var selectedID: UUID?
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        List {
            Section {
                VStack(alignment: .leading, spacing: 6) {
                    Label("Ideas, with their evidence", systemImage: "point.3.connected.trianglepath.dotted")
                        .veetType(.ui, weight: .semibold)
                        .foregroundColor(AppTheme.ink)
                    Text("Explore summaries, tentative connections, conflicts and duplicate merges. Open a claim to see why it exists; your original memories are kept.")
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                }
                .a2ListRow()
                Picker("Synthesis kind", selection: Binding(get: { model.kind ?? "all" }, set: { value in
                    Task { await model.select(kind: value == "all" ? nil : value, state: model.state) }
                })) {
                    Text("All").tag("all")
                    Text("Summaries").tag("summary")
                    Text("Connections").tag("hypothesis")
                    Text("Conflicts").tag("conflict")
                    Text("Merges").tag("merge")
                }.pickerStyle(.menu).accessibilityIdentifier("memory.synthesis.kind")
                    .a2ListRow()
                Picker("History", selection: Binding(get: { model.state ?? "all" }, set: { value in
                    Task { await model.select(kind: model.kind, state: value == "all" ? nil : value) }
                })) {
                    Text("All history").tag("all")
                    Text("Committed").tag("committed")
                    Text("Invalidated").tag("invalidated")
                    Text("Undone").tag("undone")
                }.accessibilityIdentifier("memory.synthesis.state")
                    .a2ListRow()
                Button("Refresh journal") { Task { await model.reload() } }
                    .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                    .a2ListRow()
            }
            ForEach(model.items) { operation in
                Button { selectedID = operation.id } label: {
                    MemorySynthesisRow(operation: operation)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("memory.synthesis.row.\(operation.id.uuidString)")
                .a2ListRow()
            }
            if model.isLoading { ProgressView("Loading synthesis…").a2ListRow() }
            else if let error = model.errorMessage {
                Text(error).foregroundColor(AppTheme.inkSoft).a2ListRow()
                Button("Retry") { Task { await retryRead() } }
                    .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                    .a2ListRow()
            } else if model.items.isEmpty {
                Text("No synthesis in this view yet.").foregroundColor(AppTheme.inkSoft).a2ListRow()
                Text("Original memories remain in Memories. Browsing this journal does not start dreaming.")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    .a2ListRow()
            }
            if model.hasMore && !model.isLoading {
                Button("Load more history") { Task { await model.loadMore() } }
                    .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                    .accessibilityIdentifier("memory.synthesis.more")
                    .a2ListRow()
            }
        }
        .a2List()
        .accessibilityIdentifier("memory.synthesis")
        .navigationDestination(isPresented: Binding(get: { selectedID != nil }, set: { if !$0 { selectedID = nil } })) {
            if let selectedID { MemorySynthesisDetailView(id: selectedID) }
        }
        .task { await model.reload() }
        .onChange(of: selectedID) { _, selection in
            if selection == nil { Task { await model.reload() } }
        }
        .onChange(of: scenePhase) { _, phase in
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

/// One journal entry: its kind and state over the derived statement, then
/// how many originals and conversations stand behind it.
struct MemorySynthesisRow: View {
    let operation: MemorySynthesisOperation

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Label(operation.title, systemImage: operation.kind == "merge" ? "square.on.square" : "sparkles")
                    .veetType(.meta, weight: .semibold)
                    .foregroundColor(AppTheme.inkSoft)
                Spacer(minLength: 0)
                Text(memoryDisplayText(operation.state))
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
            }
            if let content = operation.content {
                Text(content.statement)
                    .veetType(.ui)
                    .foregroundColor(AppTheme.ink)
                    .lineLimit(4)
                Text("\(operation.sources.count) original memories · \(Set(operation.sources.map(\.sessionID)).count) conversations")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
            } else {
                Text("Source details unavailable").foregroundColor(AppTheme.inkSoft)
            }
            Text(operation.createdAt, style: .date).veetType(.meta).foregroundColor(AppTheme.inkFaint)
        }
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
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
            if model.isLoading { ProgressView("Checking current evidence…").a2ListRow() }
            if let summary = model.summary {
                Section {
                    if summary.flaggedForReview {
                        Label("Needs review", systemImage: "flag.fill")
                            .veetType(.meta, weight: .semibold)
                            .foregroundColor(AppTheme.amberText)
                            .a2ListRow()
                    }
                    if let content = summary.content {
                        Text(content.statement)
                            .veetType(.title)
                            .foregroundColor(AppTheme.ink)
                            .a2ListRow()
                        MemorySensitivityBadge(sensitivity: content.sensitivity, kind: MemorySensitivityKind(rawValue: content.sensitivity))
                            .a2ListRow()
                        Text("Evidence last observed \(content.lastEvidenceAt.formatted()). Combining it does not make it new evidence.")
                            .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                            .a2ListRow()
                        Text("\(memoryDisplayText(content.authority)) authority · \(memoryDisplayText(content.portability))")
                            .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                            .a2ListRow()
                    } else {
                        Text("Synthesis content is no longer available.").a2ListRow()
                    }
                } header: {
                    sectionHeading(summary.recordKind == "hypothesis" ? "Tentative connection · derived memory" : "Related summary · derived memory")
                }
                if let content = summary.content {
                    claims(content.clauses, sources: summary.sources)
                    originals(summary.sources)
                }
                Section {
                    if summary.status == "active", summary.content != nil {
                        if summary.flaggedForReview {
                            Button("Mark reviewed") { Task { await model.review(.dismiss) } }
                                .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                                .accessibilityIdentifier("memory.synthesis.review")
                                .a2ListRow()
                        }
                        Button("Not true") { Task { await model.review(.untrue) } }
                            .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                            .accessibilityIdentifier("memory.synthesis.untrue")
                            .a2ListRow()
                        if summary.content?.portability != "local" {
                            Button("Not relevant here") { Task { await model.review(.notHere) } }
                                .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                                .a2ListRow()
                        }
                    }
                    Button("Delete synthesis…", role: .destructive) { confirmDelete = true }
                        .buttonStyle(.plain).foregroundColor(AppTheme.red)
                        .accessibilityIdentifier("memory.synthesis.delete")
                        .a2ListRow()
                    Text("These actions affect this synthesis. Its original memories are kept.")
                        .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                        .a2ListRow()
                } header: {
                    sectionHeading("Your control")
                }.disabled(model.isSaving || model.canRetryWrite)
            } else if let operation = model.operation {
                Section {
                    if let content = operation.content {
                        Text(content.statement)
                            .veetType(.title)
                            .foregroundColor(AppTheme.ink)
                            .a2ListRow()
                    } else {
                        Text("Source details unavailable").a2ListRow()
                        Text("This history entry keeps the outcome, without retaining unavailable memory content.")
                            .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                            .a2ListRow()
                    }
                    Text(memoryDisplayText(operation.state))
                        .veetType(.meta, weight: .semibold)
                        .foregroundColor(AppTheme.inkSoft)
                        .a2ListRow()
                } header: {
                    sectionHeading(operation.title)
                }
                if operation.content != nil {
                    claims(operation.content?.clauses ?? [], sources: operation.sources)
                    originals(operation.sources)
                }
                if operation.canUndo {
                    Section {
                        Button("Preview undo merge") { previewUndo = true }
                            .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                            .accessibilityIdentifier("memory.synthesis.undo.preview")
                            .disabled(model.isSaving || model.canRetryWrite)
                            .a2ListRow()
                    }
                }
            }
            if let operation = model.operation {
                Section {
                    Text("Created \(operation.createdAt.formatted())").a2ListRow()
                    Text("Outcome: \(memoryDisplayText(operation.reason))").a2ListRow()
                    if let time = operation.undoneAt { Text("Undone \(time.formatted())").a2ListRow() }
                    if let time = operation.invalidatedAt { Text("Invalidated \(time.formatted())").a2ListRow() }
                    DisclosureGroup("Technical details") {
                        Text("Policy: \(operation.policy)")
                        Text("Model: \(operation.modelIdentity)")
                        Text("Revision: \(operation.revision)")
                    }
                    .a2ListRow()
                } header: {
                    sectionHeading("History")
                }
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
            }
            if !model.canRetryWrite && !model.isLoading && !model.isSaving {
                Button("Refresh evidence") { Task { await model.open(id) } }
                    .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                    .a2ListRow()
            }
        }
        .a2List()
        .accessibilityIdentifier("memory.synthesis.detail")
        .safeAreaInset(edge: .bottom) { actionFeedback }
        .navigationTitle("Synthesis")
        .task { await model.open(id) }
        .onChange(of: scenePhase) { _, phase in
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

    /// A section heading in the A2 voice; the kind line keeps its exact words.
    private func sectionHeading(_ title: String) -> some View {
        Text(title)
            .veetType(.meta, weight: .semibold)
            .foregroundColor(AppTheme.inkSoft)
            .textCase(nil)
    }

    @ViewBuilder private var actionFeedback: some View {
        if model.isSaving || model.message != nil || model.errorMessage != nil || model.canRetryWrite {
            VStack(alignment: .leading, spacing: 0) {
                HairlineDivider()
                VStack(alignment: .leading, spacing: 8) {
                    if model.isSaving { ProgressView("Saving…") }
                    if let message = model.message {
                        Label(message, systemImage: "checkmark.circle")
                            .foregroundColor(AppTheme.tealText)
                            .accessibilityIdentifier("memory.synthesis.receipt")
                    }
                    if let error = model.errorMessage { Text(error).foregroundColor(AppTheme.inkSoft) }
                    if model.canRetryWrite {
                        Button("Retry same action") { Task { await model.retryWrite() } }
                            .buttonStyle(SecondaryCapsuleButtonStyle())
                            .disabled(model.isSaving).accessibilityIdentifier("memory.synthesis.retry.write")
                    }
                }
                .veetType(.ui)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding()
            }
            .background(AppTheme.surface)
        }
    }

    @ViewBuilder private func claims(_ clauses: [MemorySynthesisClause], sources: [MemorySynthesisSource]) -> some View {
        if !clauses.isEmpty {
            Section {
                ForEach(Array(clauses.enumerated()), id: \.offset) { index, clause in
                    DisclosureGroup {
                        ForEach(sources.filter { clause.sourceIDs.contains($0.id) }) { source in
                            sourceLink(source)
                        }
                    } label: { Text(clause.text).foregroundColor(AppTheme.ink) }
                    .accessibilityIdentifier("memory.synthesis.claim.\(index)")
                    .a2ListRow()
                }
            } header: {
                sectionHeading("Why these claims?")
            }
        }
    }
    @ViewBuilder private func originals(_ sources: [MemorySynthesisSource]) -> some View {
        if !sources.isEmpty {
            Section {
                ForEach(sources) { source in sourceLink(source).a2ListRow() }
            } header: {
                sectionHeading("Supporting originals")
            }
        }
    }
    private func sourceLink(_ source: MemorySynthesisSource) -> some View {
        NavigationLink {
            MemorySynthesisOriginalView(id: source.id)
        } label: {
            VStack(alignment: .leading, spacing: 5) {
                Text(source.statement).foregroundColor(AppTheme.ink)
                Text(source.omitted ? "Original · not included in summary text" : "Original memory")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
            }
        }
        .accessibilityLabel("\(source.statement) \(source.omitted ? "Original · not included in summary text" : "Original memory")")
        .accessibilityIdentifier("memory.synthesis.source.\(source.id.uuidString)")
    }
    private var undoPreview: some View {
        NavigationStack { undoContent }
        .tint(AppTheme.tealFill)
        #if os(macOS)
        .sheetFrame(minWidth: 520, idealWidth: 520, minHeight: 440, idealHeight: 440)
        #endif
    }
    private var undoContent: some View {
        List {
            if let operation = model.operation, operation.canUndo {
                Section {
                    Text("These originals will be recalled independently again. Undo does not restore erased, expired or changed memories, and this merge will not be recreated automatically.")
                        .foregroundColor(AppTheme.ink)
                        .a2ListRow()
                } header: {
                    sectionHeading("What will change")
                }
                originals(operation.sources)
                Button("Undo merge") {
                    previewUndo = false
                    Task { await model.undo() }
                }
                .buttonStyle(.plain).foregroundColor(AppTheme.tealText)
                .accessibilityIdentifier("memory.synthesis.undo.confirm")
                .a2ListRow()
            } else { Text("Refresh the operation before undoing it.").a2ListRow() }
        }
        .a2List()
        .accessibilityIdentifier("memory.synthesis.undo.sheet")
        .navigationTitle("Undo merge")
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Cancel") { previewUndo = false } } }
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
                A2EmptyState(systemImage: "exclamationmark.triangle", title: error) {
                    Button("Retry") { Task { await refresh() } }
                        .buttonStyle(SecondaryCapsuleButtonStyle())
                }
            } else { ProgressView("Checking original memory…") }
        }
        .background(AppTheme.paper)
        .task { await refresh() }
        .onDisappear { clear() }
        .onReceive(NotificationCenter.default.publisher(for: .peopleConnectionChanged)) { _ in clear() }
        .onChange(of: scenePhase) { _, phase in
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
