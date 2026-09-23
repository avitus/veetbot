import SwiftUI

public struct PeopleDetailView: View {
    @StateObject private var model: PeopleDetailViewModel
    let sessionID: UUID?
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dismissPeople) private var dismissPeople
    @State private var name = ""
    @State private var editingFact: MemoryView?
    @State private var removingFact: MemoryView?
    @State private var showingIdentity = false
    @State private var showingAlias = false
    @State private var conversation: PeopleConversationSelection?

    public init(personID: UUID, sessionID: UUID? = nil) {
        _model = StateObject(wrappedValue: PeopleDetailViewModel(personID: personID))
        self.sessionID = sessionID
    }

    public var body: some View {
        PeopleList(label: "Person information and actions") {
            if model.isForgotten {
                Section("Forgotten") {
                    Text(model.receipt?.state == "cleanup_pending" ? "Derived memories have been removed. Generated replies and files are still being cleaned up." : "Derived memories and history have been removed. Original messages remain at their source.")
                    if model.receipt?.state == "cleanup_pending" { Button("Check cleanup") { Task { await model.refreshReceipt() } } }
                }
            } else if let profile = model.profile {
                overview(profile)
                relationships(profile)
                history
                commitments(profile)
                facts(profile)
                Section("Coverage") {
                    Text(profile.coverage).appFont(.caption).foregroundColor(.primary)
                    if profile.truncated { Text("This overview is partial. Open the history to see more recorded interactions.").appFont(.caption) }
                }
                Section("Manage person") {
                    Button("Repair identity…") { showingIdentity = true }
                    Button("Forget person…", role: .destructive) { Task { await model.previewForget(sessionID: sessionID) } }
                }.disabled(model.isSaving || model.requiresRefresh || model.canRetrySave)
            } else if model.isLoading { ProgressView("Loading person…") }
            if let error = model.errorMessage {
                Section {
                    Label(error, systemImage: "exclamationmark.triangle")
                    if model.canRetrySave { Button("Retry save") { Task { await model.retrySave() } } }
                    Button("Refresh") { Task { await model.reload(discardPending: true) } }
                }
            }
            if let error = model.evidenceError { Text(error).foregroundColor(.secondary) }
            if !model.isForgotten, let receipt = model.receipt, receipt.operation == nil {
                Section("Fact removed") {
                    Text(receipt.state == "cleanup_pending" ? "The fact has been removed. Generated replies and files are still being cleaned up." : "The fact and its generated copies have been removed. Original messages remain at their source.")
                    if receipt.state == "cleanup_pending" { Button("Check cleanup") { Task { await model.refreshReceipt() } } }
                }
            }
            if model.receipt?.operation != nil {
                Section {
                    Text("Identity change saved.")
                    Button("Preview undo") { Task { await model.undoIdentity(sessionID: sessionID) } }
                }
            }
        }
        .navigationTitle(model.profile?.person.displayName ?? "Person")
        .accessibilityIdentifier("people.detail")
        .accessibilityLabel("Person profile")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                if let dismissPeople {
                    Button(action: dismissPeople) { Image(systemName: "xmark").imageScale(.large) }
                        .accessibilityLabel("Close")
                }
            }
            ToolbarItem(placement: .primaryAction) { Button { Task { await model.reload(discardPending: true) } } label: { Label("Refresh", systemImage: "arrow.clockwise") }.disabled(model.isSaving) }
        }
        .task { await model.reload() }
        .onChange(of: scenePhase) { phase in if phase == .active && !model.isSaving && !model.canRetrySave && model.preview == nil && !model.isForgotten { Task { await model.reload() } } }
        .sheet(item: $editingFact) { fact in PeopleFactEditor(model: model, fact: fact, sessionID: sessionID) }
        .sheet(isPresented: $showingAlias) { PeopleAliasEditor(model: model, sessionID: sessionID) }
        .sheet(isPresented: $showingIdentity) { PeopleIdentityRepairView(model: model, sessionID: sessionID) }
        .sheet(item: $conversation) { selection in PeopleConversationView(selection: selection) }
        .confirmationDialog(model.preview?.operation == nil ? "Forget this person?" : "Apply this identity change?", isPresented: Binding(get: { model.preview != nil }, set: { if !$0 { model.cancelPreview() } }), titleVisibility: .visible) {
            Button(model.preview?.operation == nil ? "Forget person" : "Apply change", role: model.preview?.operation == nil ? .destructive : nil) {
                if let accepted = model.preview { Task { await model.applyPreview(accepted, sessionID: sessionID) } }
            }
            Button("Cancel", role: .cancel) { model.cancelPreview() }
        } message: {
            if let preview = model.preview {
                Text(preview.scope ?? "This moves \(preview.assignments?.count ?? 0) recorded identity assignments. Original evidence is preserved. Undo is available while those records remain unchanged.")
            }
        }
        .confirmationDialog("Remove this fact?", isPresented: Binding(get: { removingFact != nil }, set: { if !$0 { removingFact = nil } }), titleVisibility: .visible) {
            Button("Remove fact", role: .destructive) { if let fact = removingFact { Task { await model.correct(fact, operation: "remove", statement: nil, sessionID: sessionID) }; removingFact = nil } }
        } message: { Text("The fact will no longer influence memory. Its original source message remains.") }
    }

    private func overview(_ profile: PersonProfileView) -> some View {
        Section("Overview") {
            VStack(alignment: .leading, spacing: 8) {
                TextField("Name", text: $name).onAppear { name = profile.person.displayName }
                    .onChange(of: profile.person.displayName) { name = $0 }
                if name != profile.person.displayName && !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    Button("Save name") { Task { await model.saveName(name, sessionID: sessionID) } }
                        .disabled(model.isSaving || model.canRetrySave || model.requiresRefresh)
                }
            }
            if profile.person.state == "provisional" {
                // Provisional covers people the owner writes to, so this names the
                // state rather than asking for review (ADR-0118).
                Label("Not confirmed yet", systemImage: "person.crop.circle.badge.questionmark").foregroundColor(.secondary)
                Button("Confirm this identity") { Task { await model.confirmIdentity(sessionID: sessionID) } }
                    .disabled(model.isSaving || model.canRetrySave || model.requiresRefresh)
            }
            Button { Task { await model.setPinned(!profile.person.pinned, sessionID: sessionID) } } label: {
                Text(profile.person.pinned ? "Unpin" : "Pin")
                    .fixedSize(horizontal: false, vertical: true)
            }
                .accessibilityLabel(profile.person.pinned ? "Unpin person" : "Pin person")
                .disabled(model.isSaving || model.canRetrySave || model.requiresRefresh)
            ForEach(profile.aliases) { alias in
                VStack(alignment: .leading, spacing: 4) {
                    HStack { Text(alias.value); Spacer(); Text(memoryDisplayText(alias.verification)).appFont(.caption).foregroundColor(.secondary) }
                    if let end = alias.validTo { Text("Assignment ended \(end.formatted(date: .abbreviated, time: .omitted))").appFont(.caption).foregroundColor(.secondary) }
                    else { Button("End this alias assignment") { Task { await model.endAlias(alias, sessionID: sessionID) } }.appFont(.caption).disabled(model.isSaving || model.canRetrySave || model.requiresRefresh) }
                }
            }
            Button("Add name or contact alias…") { showingAlias = true }.disabled(model.isSaving || model.canRetrySave || model.requiresRefresh)
            Button("Prepare for a conversation") { conversation = PeopleConversationSelection(person: profile.person) }
        }
    }
    private func endpoint(_ endpoint: PersonEndpointView, profile: PersonProfileView) -> String {
        if endpoint.kind == "owner" { return "You" }
        if endpoint.id == profile.person.id { return profile.person.displayName }
        return endpoint.id.flatMap { profile.relatedLabels?[$0.uuidString.lowercased()] } ?? (endpoint.kind == "organization" ? "Organization" : "Person")
    }
    private func relationships(_ profile: PersonProfileView) -> some View {
        Section("Relationships") {
            if profile.relationships.isEmpty { Text("No recorded relationships.").foregroundColor(.primary) }
            ForEach(profile.relationships) { relationship in
                VStack(alignment: .leading, spacing: 6) {
                    Text("\(endpoint(relationship.subject, profile: profile)) → \(memoryDisplayText(relationship.predicate)) → \(endpoint(relationship.object, profile: profile))")
                    if !relationship.qualifier.isEmpty { Text(relationship.qualifier).appFont(.caption) }
                    if let start = relationship.sinceLabel { Text(start).appFont(.caption).foregroundColor(.secondary) }
                    if let end = relationship.untilLabel { Text(end).appFont(.caption).foregroundColor(.secondary) }
                    if let related = [relationship.subject, relationship.object].first(where: { $0.kind == "person" && ($0.id.map { $0 != profile.person.id } ?? false) }), let id = related.id {
                        NavigationLink("View \(endpoint(related, profile: profile))") { PeopleDetailView(personID: id, sessionID: sessionID) }
                    }
                    evidenceButton(relationship.supportIDs)
                }.padding(.vertical, 4)
            }
        }
    }
    private var history: some View {
        Section("History") {
            if model.history.isEmpty { Text("No recorded interactions. Earlier history may not have been imported.").foregroundColor(.primary) }
            ForEach(model.history) { interaction in
                VStack(alignment: .leading, spacing: 6) {
                    Text(interaction.summary)
                    Text("\(interaction.dateLabel) · \(memoryDisplayText(interaction.channel)) · \(memoryDisplayText(interaction.attribution))")
                        .appFont(.caption).foregroundColor(.secondary)
                    evidenceButton(interaction.supportIDs)
                }.padding(.vertical, 4)
            }
            if model.isLoadingHistory { ProgressView() }
            else if model.hasMoreHistory { Button("More history") { Task { await model.loadHistory() } } }
            if let error = model.historyError { Text(error).foregroundColor(.secondary); Button("Retry history") { Task { await model.loadHistory() } } }
        }
    }
    private func commitments(_ profile: PersonProfileView) -> some View {
        Section("Open threads") {
            let open = profile.commitments.filter { ["open", "proposed", "uncertain"].contains($0.state) }
            if open.isEmpty { Text("No recorded open commitments.").foregroundColor(.primary) }
            ForEach(open) { commitment in
                VStack(alignment: .leading, spacing: 6) {
                    Text(commitment.description)
                    Text("\(endpoint(commitment.debtor, profile: profile)) → \(endpoint(commitment.beneficiary, profile: profile)) · \(memoryDisplayText(commitment.state))").appFont(.caption).foregroundColor(.secondary)
                    if let due = commitment.dueLabel { Text(due).appFont(.caption) }
                    evidenceButton(commitment.supportIDs)
                }
            }
        }
    }
    private func facts(_ profile: PersonProfileView) -> some View {
        Section("Facts and evidence") {
            if profile.facts.isEmpty { Text("No recorded facts.").foregroundColor(.primary) }
            ForEach(profile.facts) { fact in
                VStack(alignment: .leading, spacing: 6) {
                    Text(fact.statement)
                    Text("\(memoryDisplayText(fact.derivation)) · \(memoryDisplayText(fact.status)) · Last evidence \(fact.lastEvidenceAt.formatted(date: .abbreviated, time: .omitted))").appFont(.caption).foregroundColor(.secondary)
                    HStack {
                        NavigationLink("Inspect evidence") { MemoryDetailView(memory: fact) }
                        Spacer()
                        Menu("Correct") {
                            Button("Correct or mark changed…") { editingFact = fact }
                            Button("Confirm this fact") { Task { await model.correct(fact, operation: "affirm", statement: nil, sessionID: sessionID) } }
                            Button("Reject this fact") { Task { await model.correct(fact, operation: "reject", statement: nil, sessionID: sessionID) } }
                            Button("Remove fact…", role: .destructive) { removingFact = fact }
                        }.disabled(model.isSaving || model.requiresRefresh || model.canRetrySave || profile.factRevision(fact.id) == nil)
                    }
                }.padding(.vertical, 4)
            }
        }
    }
    @ViewBuilder private func evidenceButton(_ references: [UUID]) -> some View {
        if let reference = references.first {
            Button("View source") { Task { if let source = await model.evidence(reference) { conversation = PeopleConversationSelection(source: source) } } }
                .appFont(.caption)
        }
    }
}
