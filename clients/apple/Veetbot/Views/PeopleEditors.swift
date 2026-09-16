import SwiftUI

struct PeopleFactEditor: View {
    @ObservedObject var model: PeopleDetailViewModel
    let fact: MemoryView
    let sessionID: UUID?
    @Environment(\.dismiss) private var dismiss
    @State private var statement = ""
    @State private var operation = "correct"
    @State private var effectiveAt = Date()
    @State private var relationshipPredicate: String = ""
    @State private var commitmentState: String = ""
    private let predicates = ["parent", "child", "sibling", "relative", "partner", "spouse", "friend", "colleague", "collaborator", "introduced_by", "reports_to", "employment", "founder", "board_member", "investor", "other"]
    var body: some View {
        NavigationView {
            Form {
                Picker("What changed?", selection: $operation) {
                    Text("The recorded fact was wrong").tag("correct")
                    Text("It was true, but changed").tag("changed")
                }
                if model.profile?.relationships.contains(where: { $0.beliefID == fact.id }) == true {
                    Picker("Relationship", selection: $relationshipPredicate) {
                        Text("Text only").tag("")
                        ForEach(predicates, id: \.self) { Text(memoryDisplayText($0)).tag($0) }
                    }
                }
                if model.profile?.commitments.contains(where: { $0.beliefID == fact.id }) == true {
                    Picker("Commitment status", selection: $commitmentState) {
                        Text("Text only").tag("")
                        ForEach(["proposed", "open", "completed", "cancelled", "uncertain"], id: \.self) { Text(memoryDisplayText($0)).tag($0) }
                    }
                }
                TextEditor(text: $statement).frame(minHeight: 130).accessibilityLabel("Corrected fact")
                if operation == "changed" {
                    DatePicker("When did it change?", selection: $effectiveAt, in: fact.validFrom...Date(), displayedComponents: [.date, .hourAndMinute])
                }
                Text(operation == "changed" ? "The earlier fact stays in dated history." : "The previous fact is marked incorrect and replaced.").foregroundColor(.secondary)
                if let error = model.errorMessage { Text(error).foregroundColor(.red) }
                if model.canRetrySave { Button("Retry save") { Task { await model.retrySave(); if model.errorMessage == nil { dismiss() } } } }
            }
            .navigationTitle("Correct fact")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(model.isSaving) }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") { Task {
                        await model.correct(fact, operation: operation, statement: statement, sessionID: sessionID, effectiveAt: operation == "changed" ? effectiveAt : nil, relationshipPredicate: relationshipPredicate.isEmpty ? nil : relationshipPredicate, commitmentState: commitmentState.isEmpty ? nil : commitmentState)
                        if model.errorMessage == nil { dismiss() }
                    } }.disabled(model.isSaving || model.requiresRefresh || model.canRetrySave || statement.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }.onAppear {
            statement = fact.statement
            relationshipPredicate = model.profile?.relationships.first(where: { $0.beliefID == fact.id })?.predicate ?? ""
            commitmentState = model.profile?.commitments.first(where: { $0.beliefID == fact.id })?.state ?? ""
        }.frame(minWidth: 340, minHeight: 300)
    }
}

struct PeopleIdentityRepairView: View {
    @ObservedObject var model: PeopleDetailViewModel
    @StateObject private var people = PeopleViewModel()
    let sessionID: UUID?
    @Environment(\.dismiss) private var dismiss
    @State private var operation = "merge"
    @State private var target: PersonView?
    @State private var selected: Set<UUID> = []
    @State private var showingAdd = false
    @State private var sourceSelection: PeopleConversationSelection?
    var body: some View {
        NavigationView {
            List {
                Section {
                    Picker("Repair", selection: $operation) {
                        Text("Combine duplicate people").tag("merge")
                        Text("Move evidence to another person").tag("split")
                    }
                    Text(operation == "merge" ? "Choose the person these records should belong to. You will review the change before applying it." : "Select only the evidence that belongs to the other person. Facts with mixed evidence remain unresolved.").foregroundColor(.secondary)
                }
                Section("Destination person") {
                    if let target { Label("Selected: \(target.displayName)", systemImage: "checkmark.circle") }
                    if operation == "split" { Button("Create a separate person") { showingAdd = true } }
                    TextField("Search people", text: Binding(get: { people.searchText }, set: { people.setSearchText($0) }))
                    ForEach(people.items.filter { $0.id != model.personID }) { person in
                        Button { target = person } label: { HStack { Text(person.displayName); Spacer(); if target?.id == person.id { Image(systemName: "checkmark") } } }
                            .accessibilityAddTraits(target?.id == person.id ? .isSelected : [])
                            .accessibilityIdentifier("people.identity.destination.\(person.id.uuidString)")
                    }
                    if let error = people.errorMessage { Text(error).foregroundColor(.secondary) }
                    Button("More people") { Task { await people.loadMore() } }
                }
                if operation == "split" {
                    Section("Evidence to move") {
                        ForEach(model.identityEvidence) { row in
                            VStack(alignment: .leading) {
                                evidenceToggle(row.id, label: row.label)
                                Text(memoryDisplayText(row.kind) + (row.unresolved ? " · Unresolved" : "")).appFont(.caption).foregroundColor(.secondary)
                                ForEach(row.supportIDs, id: \.self) { reference in
                                    Button("View source") { Task { if let source = await model.evidence(reference) { sourceSelection = PeopleConversationSelection(source: source) } } }
                                }
                            }
                        }
                        if model.isLoadingIdentityEvidence { ProgressView("Loading evidence…") }
                        if let error = model.identityEvidenceError { Text(error).foregroundColor(.secondary) }
                        if model.hasMoreIdentityEvidence && !model.requiresRefresh {
                            Button(model.identityEvidenceError == nil ? "More evidence" : "Retry evidence") { Task { await model.loadIdentityEvidence() } }.disabled(model.isLoadingIdentityEvidence)
                        }
                        if let error = model.evidenceError { Text(error).foregroundColor(.secondary) }
                    }
                }
                if let error = model.errorMessage { Text(error).foregroundColor(.red) }
                if model.canRetrySave { Button("Retry preview") { Task { await model.retrySave(); if model.preview != nil { dismiss() } } } }
            }
            .navigationTitle("Repair identity")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(model.isSaving) }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Preview") { guard let target else { return }; Task {
                        await model.previewIdentity(operation: operation, target: target, selectedIDs: operation == "split" ? selected : [], sessionID: sessionID)
                        if model.preview != nil { dismiss() }
                    } }.disabled(target == nil || model.isSaving || model.requiresRefresh || model.canRetrySave || (operation == "split" && selected.isEmpty))
                }
            }
        }.task { await people.reload(); await model.loadIdentityEvidence(restart: true) }
        .sheet(isPresented: $showingAdd) { AddPersonView(sessionID: sessionID) { person in target = person; Task { await people.reload() } } }
        .sheet(item: $sourceSelection) { PeopleConversationView(selection: $0) }
        .frame(minWidth: 340, minHeight: 400)
    }
    private func evidenceToggle(_ id: UUID, label: String) -> some View {
        Toggle(label, isOn: Binding(get: { selected.contains(id) }, set: { if $0 { selected.insert(id) } else { selected.remove(id) } }))
    }
}

struct PeopleConversationSelection: Identifiable {
    let id = UUID()
    var person: PersonView?
    var source: PeopleEvidenceView?
}

/// A separate presentation preserves the existing Chat and Email drafts and streams.
struct PeopleConversationView: View {
    let selection: PeopleConversationSelection
    @StateObject private var chat = ChatViewModel()
    @Environment(\.dismiss) private var dismiss
    @State private var prepared = false
    @State private var error: String?
    var body: some View {
        NavigationView {
            VStack(spacing: 0) {
                if let source = selection.source {
                    Text("\(memoryDisplayText(source.sourceKind)) evidence · \(source.evidenceAt.formatted(date: .abbreviated, time: .shortened))").appFont(.caption).foregroundColor(.secondary).padding()
                }
                if let source = selection.source { PeopleSourceReader(source: source) }
                else {
                    if let error { Text(error).foregroundColor(.red).padding(); Button("Retry") { Task { await prepare() } } }
                    ChatView(model: chat)
                }
            }
            .navigationTitle(selection.person.map { "Prepare: \($0.displayName)" } ?? "Source conversation")
            .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() }.accessibilityIdentifier("people.conversation.close") } }
        }
        .frame(minWidth: 340, minHeight: 450)
        .task { if chat.isConfigured && selection.source == nil { await prepare() } }
        .onChange(of: chat.isConfigured) { configured in if configured && selection.source == nil { Task { await prepare() } } }
    }
    private func prepare() async {
        guard !prepared, let api = chat.currentAPIClient else { return }
        prepared = true
        do {
            if let source = selection.source { await chat.openSharedSession(source.sessionID) }
            else if let person = selection.person {
                let session = try await api.createSession(metadata: ["people_person_ids": .array([.string(person.id.uuidString)])])
                await chat.openSharedSession(session.id)
                chat.composerText = "Help me prepare for my next conversation with \(person.displayName)."
            }
            error = nil
        } catch { self.error = error.localizedDescription; prepared = false }
    }
}


struct PeopleAliasEditor: View {
    @ObservedObject var model: PeopleDetailViewModel
    let sessionID: UUID?
    @Environment(\.dismiss) private var dismiss
    @State private var value = ""
    @State private var kind = "name"
    @State private var context = "owner"
    var body: some View {
        NavigationView {
            Form {
                Picker("Alias type", selection: $kind) {
                    Text("Name").tag("name")
                    Text("Email address").tag("email")
                    Text("Phone number").tag("phone")
                    Text("Channel handle").tag("handle")
                    Text("Role").tag("role")
                }
                TextField("Name or contact", text: $value)
                TextField("Context", text: $context)
                Text("This confirms that the alias belongs to this person in the stated context. Phone numbers need an international prefix. End an assignment when an address or number changes hands.").appFont(.caption).foregroundColor(.secondary)
                if let error = model.errorMessage { Text(error).foregroundColor(.red) }
                if model.canRetrySave { Button("Retry save") { Task { await model.retrySave(); if model.errorMessage == nil { dismiss() } } } }
            }
            .navigationTitle("Add alias")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Confirm alias") { Task {
                        await model.addAlias(value: value, kind: kind, context: context, sessionID: sessionID)
                        if model.errorMessage == nil { dismiss() }
                    } }.disabled(value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.isSaving || model.canRetrySave || model.requiresRefresh)
                }
            }
        }
    }
}


private struct PeopleSourceReader: View {
    @StateObject private var model: PeopleEvidenceViewModel
    init(source: PeopleEvidenceView) { _model = StateObject(wrappedValue: PeopleEvidenceViewModel(source: source)) }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if model.isLoading { ProgressView("Loading source…") }
                if let heading = model.heading { Text(heading).font(.headline) }
                if model.partial { Text("Only part of the original message is retained.").foregroundColor(.secondary) }
                if let text = model.text { Text(text).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                if model.hasMoreHistory {
                    Text("The source is further into this conversation’s history.").foregroundColor(.secondary)
                    Button("Continue searching history") { Task { await model.loadMoreHistory() } }.disabled(model.isLoading)
                }
                if let error = model.errorMessage {
                    Text(error).foregroundColor(.secondary)
                    Button("Retry source") { Task { if model.hasMoreHistory { await model.loadMoreHistory() } else { await model.load() } } }
                }
            }.padding()
        }.task { await model.load() }
        .accessibilityIdentifier("people.source")
    }
}
