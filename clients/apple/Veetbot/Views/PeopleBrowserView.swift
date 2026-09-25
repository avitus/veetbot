import SwiftUI

private struct DismissPeopleKey: EnvironmentKey {
    static let defaultValue: (() -> Void)? = nil
}

extension EnvironmentValues {
    var dismissPeople: (() -> Void)? {
        get { self[DismissPeopleKey.self] }
        set { self[DismissPeopleKey.self] = newValue }
    }
}

struct PeopleLookupSheet: View {
    @StateObject private var model: PeopleViewModel
    let sessionID: UUID?
    @Environment(\.dismiss) private var dismiss
    init(lookup: PeopleLookup, sessionID: UUID? = nil) {
        self.sessionID = sessionID
        _model = StateObject(wrappedValue: PeopleViewModel(searchText: lookup.text, asOf: lookup.asOf))
    }
    var body: some View {
        Group {
            if #available(macOS 13, iOS 16, *) {
                NavigationStack { navigationContent }
                    .accessibilityElement(children: .contain)
                    .accessibilityLabel("People navigation")
            } else {
                NavigationView { navigationContent }
            }
        }
        .environment(\.dismissPeople, { dismiss() })
        .accessibilityLabel("People")
        #if os(macOS)
        .frame(minWidth: 820, minHeight: 650)
        .background(PeopleSheetAccessibilityLabel())
        #else
        .frame(minWidth: 320, minHeight: 400)
        #endif
        .onReceive(NotificationCenter.default.publisher(for: .peopleConnectionChanged)) { _ in dismiss() }
    }
    private var navigationContent: some View {
            PeopleBrowserView(model: model, sessionID: sessionID)
                .navigationTitle("People")
                #if os(iOS)
                .searchable(text: Binding(get: { model.searchText }, set: model.setSearchText), placement: .navigationBarDrawer(displayMode: .always), prompt: "Search")
                #else
                .searchable(text: Binding(get: { model.searchText }, set: model.setSearchText), prompt: "Search people")
                #endif
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button { dismiss() } label: { Image(systemName: "xmark").imageScale(.large) }
                            .accessibilityLabel("Close")
                    }
                }
    }

}

public struct PeopleBrowserView: View {
    @ObservedObject var model: PeopleViewModel
    let sessionID: UUID?
    @State private var showingAdd = false
    @State private var showingImport = false
    public init(model: PeopleViewModel, sessionID: UUID? = nil) { self.model = model; self.sessionID = sessionID }

    public var body: some View {
        Group {
            if model.isLoading && model.items.isEmpty {
                ProgressView("Loading people…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                PeopleList(label: "People and filters") {
                    #if os(macOS)
                    PeopleFilter(title: "Show", selection: model.collection, options: PeopleCollection.allCases, label: { $0.rawValue }) { value in Task { await model.selectCollection(value) } }
                    PeopleFilter(title: "Relationship with you", selection: model.relationship, options: PeopleRelationshipFilter.allCases, label: { $0.label }) { value in Task { await model.selectRelationship(value) } }
                    #else
                    Menu {
                        Picker("Show", selection: Binding(get: { model.collection }, set: { value in Task { await model.selectCollection(value) } })) {
                            ForEach(PeopleCollection.allCases) { Text($0.rawValue).tag($0) }
                        }
                    } label: { filterLabel("Show", value: model.collection.rawValue) }
                    Menu {
                        Picker("Relationship with you", selection: Binding(get: { model.relationship }, set: { value in Task { await model.selectRelationship(value) } })) {
                            ForEach(PeopleRelationshipFilter.allCases) { Text($0.label).tag($0) }
                        }
                    } label: { filterLabel("Relationship with you", value: model.relationship.label) }
                    #endif
                    Toggle("Recent interactions first", isOn: Binding(get: { model.recentFirst }, set: { value in Task { await model.setRecentFirst(value) } }))
                    if model.collection == .review && !model.suggestions.isEmpty {
                        // Only Needs review shows this, so no asserted People row moves (ADR-0125).
                        Section("Possible duplicates") {
                            ForEach(model.suggestions) { suggestion in
                                VStack(alignment: .leading, spacing: 6) {
                                    Text("\(suggestion.source.displayName) and \(suggestion.target.displayName)").appFont(.headline)
                                    Text(suggestion.explanation).appFont(.caption).foregroundColor(.secondary)
                                    HStack {
                                        Button("Merge") { Task { await model.resolveSuggestion(suggestion, decision: "merge") } }
                                            .accessibilityLabel("Merge \(suggestion.source.displayName) into \(suggestion.target.displayName)")
                                        Button("Not the same") { Task { await model.resolveSuggestion(suggestion, decision: "separate") } }
                                            .accessibilityLabel("\(suggestion.source.displayName) and \(suggestion.target.displayName) are different people")
                                    }
                                    .buttonStyle(.borderless)
                                }
                                .padding(.vertical, 4)
                                .accessibilityIdentifier("people.suggestion.\(suggestion.id.uuidString)")
                            }
                        }
                    }
                    if model.items.isEmpty && model.errorMessage == nil && (model.collection != .review || model.suggestions.isEmpty) {
                        if model.collection == .review {
                            Section {
                                Label("Nothing needs review.", systemImage: "checkmark.circle")
                                Text("Someone Veetbot knows only by a name or role appears here until you confirm or remove them.")
                                    .foregroundColor(.secondary)
                            }
                        } else {
                            Section {
                                Label("People you remember", systemImage: "person.2")
                                Text("People you know and people you write to appear here. You can also add someone.")
                                    .foregroundColor(.secondary)
                            }
                        }
                    }
                    ForEach(model.items) { person in
                        NavigationLink {
                            PeopleDetailView(personID: person.id, sessionID: sessionID)
                        } label: {
                            HStack {
                                Image(systemName: person.pinned ? "pin.fill" : "person.crop.circle")
                                    .foregroundColor(person.pinned ? AppTheme.orange : .secondary)
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(person.displayName).appFont(.headline)
                                    if person.state == "merged" { Text("Combined identity").appFont(.caption).foregroundColor(.secondary) }
                                }
                            }.padding(.vertical, 4)
                        }
                        .accessibilityIdentifier("people.row.\(person.id.uuidString)")
                        .onAppear { if person.id == model.items.last?.id { Task { await model.loadMore() } } }
                    }
                    if model.isLoadingMore { ProgressView("Loading more people…") }
                    if let error = model.errorMessage {
                        Section {
                            Label(error, systemImage: model.availability == .denied ? "lock" : "exclamationmark.triangle")
                            Button(model.availability == .changed ? "Refresh people" : "Retry") { Task { await model.retry() } }
                        }
                    }
                    if model.availability != .disabled && model.availability != .denied {
                        Button { showingImport = true } label: { Label("Import history", systemImage: "clock.arrow.circlepath") }
                        Button { showingAdd = true } label: { Label("Add person", systemImage: "person.badge.plus") }
                    }
                }
            }
        }
        .accessibilityIdentifier("people.browser")
        .accessibilityLabel("People directory")
        .task { await model.reload() }
        .sheet(isPresented: $showingImport) { PeopleImportHistoryView(initialSessionID: sessionID) }
        .sheet(isPresented: $showingAdd) { AddPersonView(sessionID: sessionID) { _ in Task { await model.reload() } } }
    }

    private func filterLabel(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).foregroundColor(.primary).fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .firstTextBaseline) {
                Text(value).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Image(systemName: "chevron.up.chevron.down").accessibilityHidden(true)
            }
        }
        .accessibilityElement(children: .combine)
    }

}

struct AddPersonView: View {
    let sessionID: UUID?
    let saved: (PersonView) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var name = ""
    @State private var saving = false
    @State private var error: String?
    @State private var key = UUID().uuidString
    @State private var auditID: UUID?
    @State private var pendingName: String?
    var body: some View {
        NavigationView {
            Form {
                TextField("Name", text: $name).disabled(pendingName != nil)
                Text("Use a name or a description you recognize, such as ‘my sister in Canada’.").foregroundColor(.secondary)
                if let error { Text(error).foregroundColor(.red) }
            }
            .navigationTitle("Add person")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(saving) }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Saving…" : pendingName == nil ? "Add" : "Retry") { Task { await save() } }
                        .disabled(saving || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }.peopleModalFrame(.form, minHeight: 220, idealHeight: 280)
    }
    private func save() async {
        saving = true
        defer { saving = false }
        do {
            guard let api = await MemoryViewModel.makeDefaultAPIClient() else { throw HTTPTransportError.notConfigured }
            if auditID == nil { auditID = sessionID }
            if auditID == nil { auditID = try await api.createSession(metadata: ["purpose": .string("people-management")]).id }
            guard let auditID else { return }
            if pendingName == nil { pendingName = name.trimmingCharacters(in: .whitespacesAndNewlines) }
            let person = try await api.writePerson(nil, body: ["session_id": .string(auditID.uuidString), "display_name": .string(pendingName ?? name)], key: key)
            saved(person)
            dismiss()
        } catch { self.error = error.localizedDescription }
    }
}

struct PeopleImportHistoryView: View {
    @StateObject private var model = PeopleImportViewModel()
    let initialSessionID: UUID?
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var selected: Set<UUID> = []
    @State private var selectedAccounts: Set<String> = []
    @State private var fetchMailbox = false
    @State private var since = Calendar.current.date(byAdding: .day, value: -90, to: Date()) ?? Date()
    @State private var until = Date()
    @State private var maximum = 100
    @State private var budget = "1.00"
    private func progressRow(_ title: String, value: String) -> some View {
        HStack { Text(title); Spacer(); Text(value).foregroundColor(.secondary) }
            .accessibilityElement(children: .combine)
    }

    private var importForm: some View {
        Form {
            if let job = model.job {
                Section("Import progress") {
                    progressRow("Status", value: memoryDisplayText(job.state))
                    Button("Choose another import") { model.showImportSelection(); Task { await model.loadImports() } }
                        .disabled(model.canRetry)
                    if case .string("mailbox")? = job.scope?["email_source"] {
                        progressRow("Mailbox messages read", value: String(job.mailboxRecordsRead ?? 0))
                        progressRow("Mailbox coverage", value: job.mailboxReadComplete == true ? "Complete" : "Partial or in progress")
                    }
                    progressRow("Sources analyzed", value: String(job.recordsRead))
                    progressRow("Processed", value: String(job.recordsProcessed))
                    progressRow("Excluded", value: String(job.recordsExcluded))
                    progressRow("Cost", value: "$\(job.spentUSD)")
                    progressRow("Reserved", value: "$\(job.reservedUSD)")
                    Text(job.knownRecords.map { "\($0) known records" } ?? "Total source count is unknown.")
                    Text(job.coverage).foregroundColor(.secondary)
                    if let code = job.errorCode { Text(memoryDisplayText(code)).foregroundColor(.secondary) }
                    if job.state == "preview" { Button("Start import") { Task { await model.start() } } }
                    if ["budget_paused", "failed", "cancelled"].contains(job.state) {
                        if Decimal(string: job.reservedUSD) == 0 {
                            TextField("Total budget in USD", text: $budget)
                            Button("Resume import") { Task { await model.start(maxCost: budget) } }
                        } else {
                            Text("An earlier model call has unresolved charges. Its reservation must be settled before this import can resume.").foregroundColor(.secondary)
                        }
                    }
                    if job.failures > 0 { Text("\(job.failures) source records need another analysis attempt.").foregroundColor(.secondary) }
                    if job.isActive {
                        Button("Refresh progress") { Task { await model.refresh() } }
                        Button("Cancel import", role: .destructive) { Task { await model.cancel() } }
                    }
                }
            } else {
                Section("Existing imports") {
                    ForEach(model.existingImports) { saved in
                        Button { Task { await model.restore(saved.id) } } label: {
                            VStack(alignment: .leading) {
                                Text(memoryDisplayText(saved.state))
                                Text("\(saved.recordsProcessed) processed · $\(saved.spentUSD)").font(.caption).foregroundColor(.secondary)
                            }
                        }.disabled(model.canRetry)
                    }
                    Button("Refresh imports") { Task { await model.loadImports() } }
                    if model.hasMoreImports { Button("More imports") { Task { await model.loadImports(more: true) } } }
                }
                Section("Conversations") {
                    Text("Choose the conversations whose People history you want to import.").foregroundColor(.secondary)
                    ForEach(model.sessions) { session in
                        Toggle(session.title ?? "Conversation from \(session.createdAt.formatted(date: .abbreviated, time: .omitted))", isOn: Binding(get: { selected.contains(session.id) }, set: { if $0 { selected.insert(session.id) } else { selected.remove(session.id) } }))
                    }
                    if model.hasMoreSessions { Button("Load conversations") { Task { await model.loadSessions() } } }
                }
                Section("Email accounts") {
                    Toggle("Read older mail from selected accounts", isOn: $fetchMailbox)
                        .accessibilityIdentifier("people.import.mailbox")
                    Text(fetchMailbox ? "Fetch messages in the selected date range, then analyze them in date order. The record limit bounds mailbox reads; partial coverage is shown in progress." : "Analyze email passages Veetbot has already retained. Unread mailbox history is outside this import’s coverage.").foregroundColor(.secondary)
                    ForEach(model.accounts) { account in
                        Toggle(account.label, isOn: Binding(get: { selectedAccounts.contains(account.id) }, set: { if $0 { selectedAccounts.insert(account.id) } else { selectedAccounts.remove(account.id) } }))
                            .accessibilityIdentifier("people.import.account.\(account.id)")
                    }
                    if let error = model.emailError {
                        Text(error).foregroundColor(.secondary)
                        Button("Reload email accounts") { Task { await model.loadAccounts() } }
                    }
                }
                Section("Limits") {
                    DatePicker("From", selection: $since, displayedComponents: .date)
                    DatePicker("Until", selection: $until, displayedComponents: .date)
                    Text("The end date is excluded.").font(.caption).foregroundColor(.secondary)
                    Stepper("Up to \(maximum) records", value: $maximum, in: 100...10000, step: 100)
                    TextField("Maximum cost in USD", text: $budget)
                    Button("Preview import") { Task { await model.preview(sessionIDs: selected, accountIDs: selectedAccounts, fetchMailbox: fetchMailbox, since: since, until: until, maxRecords: maximum, maxCost: budget) } }.disabled(selected.isEmpty && selectedAccounts.isEmpty)
                }.disabled(model.canRetry)
            }
            if let error = model.errorMessage { Text(error).foregroundColor(.red) }
            if model.canRetry { Button("Retry request") { Task { await model.retry() } } }
            if model.isBusy { ProgressView() }
        }
    .disabled(model.isBusy)
    }
    var body: some View {
        Group {
            #if os(macOS)
            VStack(alignment: .leading, spacing: 16) {
                HStack {
                    Text("Import People history").font(.headline)
                    Spacer()
                    Button("Close") { dismiss() }
                }
                ScrollView { importForm }
            }
            .padding()
            #else
            NavigationView {
                importForm
                    .navigationTitle("Import People history")
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } } }
            }
            .navigationViewStyle(.stack)
            #endif
        }
        .peopleModalFrame(.reading, minHeight: 450, idealHeight: 640)
        .task { if let initialSessionID { selected.insert(initialSessionID) }; await model.loadImports(); await model.loadSessions(); await model.loadAccounts() }
        .task(id: "\(model.job?.id.uuidString ?? ""):\(model.job?.isActive == true):\(scenePhase == .active)") {
            guard scenePhase == .active else { return }
            while model.job?.isActive == true && !Task.isCancelled {
                do { try await Task.sleep(nanoseconds: 3_000_000_000) } catch { return }
                await model.refresh()
                if model.errorMessage != nil { return }
            }
        }
        .onChange(of: scenePhase) { phase in
            if phase == .active { Task { if model.job != nil { await model.refresh() } else { await model.loadImports() } } }
        }
        .onChange(of: model.job?.id) { _ in
            if case .string(let saved)? = model.job?.scope?["max_cost_usd"] { budget = saved }
        }
        .accessibilityIdentifier("people.import")
    }
}

/// A directory/profile scroll surface with individually accessible native controls.
struct PeopleList<Content: View>: View {
    let label: String
    @ViewBuilder let content: () -> Content
    var body: some View {
        #if os(macOS)
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 16) { content() }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(20)
                .accessibilityElement(children: .contain)
                .accessibilityLabel(label)
        }
        .accessibilityElement(children: .contain)
        #else
        List { content() }
        #endif
    }
}

/// Opening widths for People modals.
enum PeopleModalWidth {
    /// Short editors: adding a person, an alias, or a fact correction.
    case form
    /// Conversation lists, transcripts, and identity evidence.
    case reading

    var macMinimum: CGFloat { self == .form ? 560 : 680 }
    var ideal: CGFloat { self == .form ? 600 : 760 }
}

extension View {
    /// Frames a People sheet with `sheetFrame`, so both its macOS 15 ideal
    /// width and its earlier-release minimum are readable.
    func peopleModalFrame(_ width: PeopleModalWidth, minHeight: CGFloat, idealHeight: CGFloat) -> some View {
        sheetFrame(minWidth: 320, macMinWidth: width.macMinimum, idealWidth: width.ideal, maxWidth: .infinity,
                   minHeight: minHeight, idealHeight: idealHeight, maxHeight: .infinity)
    }
}

#if os(macOS)
/// SwiftUI's sheet hosting container is separate from its labeled content.
private struct PeopleSheetAccessibilityLabel: NSViewRepresentable {
    final class LabelView: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            window?.contentView?.setAccessibilityLabel("People")
        }
    }
    func makeNSView(context: Context) -> LabelView { LabelView() }
    func updateNSView(_ view: LabelView, context: Context) {}
}

/// A keyboard-operable choice list, with the current selection announced on its button.
private struct PeopleFilter<Option: Identifiable & Equatable>: View {
    let title: String
    let selection: Option
    let options: [Option]
    let label: (Option) -> String
    let select: (Option) -> Void
    @State private var isPresented = false

    var body: some View {
        HStack {
            Text(title)
            Button { isPresented = true } label: {
                Label(label(selection), systemImage: "chevron.up.chevron.down")
            }
            .accessibilityLabel(title)
            .accessibilityValue(label(selection))
            .popover(isPresented: $isPresented) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(title).font(.headline).accessibilityAddTraits(.isHeader)
                    ForEach(options) { option in
                        Button {
                            select(option)
                            isPresented = false
                        } label: {
                            HStack {
                                Text(label(option))
                                Spacer()
                                if option == selection { Image(systemName: "checkmark").accessibilityHidden(true) }
                            }
                        }
                        .accessibilityValue(option == selection ? "Selected" : "")
                    }
                }.padding().frame(width: 260)
            }
        }
    }
}
#endif
