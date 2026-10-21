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
    @State private var columnVisibility: NavigationSplitViewVisibility = .all
    @State private var selectedPersonID: UUID?
    @Environment(\.horizontalSizeClass) private var sizeClass
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    @Environment(\.dismiss) private var dismiss
    init(lookup: PeopleLookup, sessionID: UUID? = nil) {
        self.sessionID = sessionID
        _model = StateObject(wrappedValue: PeopleViewModel(searchText: lookup.text, asOf: lookup.asOf))
    }
    var body: some View {
        Group {
            if usesColumns {
                NavigationSplitView(columnVisibility: $columnVisibility) {
                    PeopleDirectorySidebar(model: model)
                        .navigationSplitViewColumnWidth(min: 180, ideal: 220, max: 260)
                        .accessibilityElement(children: .contain)
                        .accessibilityLabel("People collections")
                } content: {
                    navigationContent
                        .navigationSplitViewColumnWidth(min: 240, ideal: 300, max: 380)
                        .accessibilityElement(children: .contain)
                        .accessibilityLabel("People directory navigation")
                } detail: {
                    NavigationStack {
                        if let selectedPersonID, model.collection != .duplicates {
                            PeopleDetailView(personID: selectedPersonID, sessionID: sessionID)
                                .id(selectedPersonID)
                        } else {
                            VStack {
                                Spacer()
                                A2EmptyState(systemImage: "person", title: "Choose a person")
                                Spacer()
                            }
                            .frame(maxWidth: .infinity)
                            .background(AppTheme.paper)
                        }
                    }
                    .accessibilityElement(children: .contain)
                    .accessibilityLabel("Selected person")
                }
            } else {
                NavigationStack { navigationContent }
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("People navigation")
        .environment(\.dismissPeople, { dismiss() })
        .accessibilityLabel("People")
        .toastHost()
        #if os(macOS)
        .frame(minWidth: 960, minHeight: 650)
        .background(PeopleSheetAccessibilityLabel())
        #else
        .frame(minWidth: 320, minHeight: 400)
        .presentationSizing(.page)
        #endif
        .onReceive(NotificationCenter.default.publisher(for: .peopleConnectionChanged)) { _ in dismiss() }
    }
    private var usesColumns: Bool {
        #if os(macOS)
        true
        #else
        sizeClass == .regular && !dynamicTypeSize.isAccessibilitySize
        #endif
    }

    private var directory: some View {
        PeopleBrowserView(model: model, sessionID: sessionID, selection: usesColumns ? $selectedPersonID : nil, showsCollectionControls: !usesColumns)
            .navigationTitle("People")
    }

    @ViewBuilder private var searchableDirectory: some View {
        if usesColumns {
            directory
        } else {
            VStack(spacing: 0) {
                HStack(spacing: 8) {
                    if !dynamicTypeSize.isAccessibilitySize {
                        Image(systemName: "magnifyingglass").foregroundColor(AppTheme.inkSoft)
                    }
                    TextField("Search people", text: Binding(get: { model.searchText }, set: model.setSearchText))
                        .textFieldStyle(.plain)
                        .accessibilityLabel("Search people")
                        .accessibilityIdentifier("people.search")
                }
                .veetType(.ui).padding(12)
                .background(RoundedRectangle(cornerRadius: 10).fill(AppTheme.field))
                .padding(.horizontal, 16).padding(.vertical, 8)
                directory
            }
            .background(AppTheme.paper)
        }
    }

    private var navigationContent: some View {
        searchableDirectory
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
    /// The person a profile column beside the directory shows. On the Mac a
    /// navigation link there stopped responding after the first choice once the
    /// directory was longer than its column, so that layout selects instead.
    private let selection: Binding<UUID?>?
    private let showsCollectionControls: Bool
    @State private var showingAdd = false
    @State private var showingImport = false
    @Environment(\.dynamicTypeSize) private var dynamicTypeSize
    public init(model: PeopleViewModel, sessionID: UUID? = nil) { self.init(model: model, sessionID: sessionID, selection: nil) }
    init(model: PeopleViewModel, sessionID: UUID?, selection: Binding<UUID?>?, showsCollectionControls: Bool = true) {
        self.model = model
        self.sessionID = sessionID
        self.selection = selection
        self.showsCollectionControls = showsCollectionControls
    }

    public var body: some View {
        Group {
            if model.isLoading && model.items.isEmpty {
                ProgressView("Loading people…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                PeopleList(label: "People and filters") {
                    #if os(macOS)
                    VStack(alignment: .leading, spacing: 10) {
                        if showsCollectionControls {
                        PeopleFilter(title: "Show", selection: model.collection, options: PeopleCollection.allCases, label: { model.collectionLabel($0) }) { value in Task { await model.selectCollection(value) } }
                        }
                        if model.collection != .duplicates {
                            PeopleFilter(title: "Relationship with you", selection: model.relationship, options: PeopleRelationshipFilter.allCases, label: { $0.label }) { value in Task { await model.selectRelationship(value) } }
                        }
                    }
                    .padding(.bottom, 12)
                    #else
                    if showsCollectionControls {
                    Menu {
                        Picker("Show", selection: Binding(get: { model.collection }, set: { value in Task { await model.selectCollection(value) } })) {
                            ForEach(PeopleCollection.allCases) { Text(model.collectionLabel($0)).tag($0) }
                        }
                    } label: { filterLabel("Show", value: model.collectionLabel(model.collection)) }
                    }
                    if model.collection != .duplicates {
                        Menu {
                            Picker("Relationship with you", selection: Binding(get: { model.relationship }, set: { value in Task { await model.selectRelationship(value) } })) {
                                ForEach(PeopleRelationshipFilter.allCases) { Text($0.label).tag($0) }
                            }
                        } label: { filterLabel("Relationship with you", value: model.relationship.label) }
                    }
                    #endif
                    if model.collection == .duplicates && !model.suggestions.isEmpty {
                        // Its own page (MagicPath keen-earth-6102), so no asserted People row moves (ADR-0125).
                        Section {
                            ForEach(model.suggestions) { suggestion in
                                VStack(alignment: .leading, spacing: 8) {
                                    Text("\(suggestion.source.displayName) and \(suggestion.target.displayName)")
                                        .veetType(.ui, weight: .semibold)
                                        .foregroundColor(AppTheme.ink)
                                    Text(suggestion.explanation).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                                    HStack(spacing: 8) {
                                        Button("Merge") { Task { await model.resolveSuggestion(suggestion, decision: "merge") } }
                                            .primaryCapsule()
                                            .accessibilityLabel("Merge \(suggestion.source.displayName) into \(suggestion.target.displayName)")
                                        Button("Keep separate") { Task { await model.resolveSuggestion(suggestion, decision: "separate") } }
                                            .buttonStyle(SecondaryCapsuleButtonStyle())
                                            .accessibilityLabel("\(suggestion.source.displayName) and \(suggestion.target.displayName) are different people")
                                    }
                                }
                                .padding(PeopleMetrics.rowPadding)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .a2Card(border: AppTheme.amberLine)
                                .padding(.vertical, 4)
                                .accessibilityIdentifier("people.suggestion.\(suggestion.id.uuidString)")
                                .a2ListRow()
                            }
                        } header: {
                            Text("Two entries that may be one person. Merging keeps both addresses and all history under one name; Veetbot never merges people on a name alone.")
                                .veetType(.meta)
                                .foregroundColor(AppTheme.inkSoft)
                                .textCase(nil)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                    if model.items.isEmpty && model.errorMessage == nil && (model.collection != .duplicates || model.suggestions.isEmpty) {
                        if model.collection == .duplicates {
                            Section {
                                Label("No possible duplicates.", systemImage: "checkmark.circle")
                                    .foregroundColor(AppTheme.ink)
                                    .a2ListRow()
                                Text("When two entries may be one person, Veetbot asks here before anything is merged.")
                                    .foregroundColor(AppTheme.inkSoft)
                                    .a2ListRow()
                            }
                        } else if model.collection == .review {
                            Section {
                                Label("Nothing needs you.", systemImage: "checkmark.circle")
                                    .foregroundColor(AppTheme.ink)
                                    .a2ListRow()
                                Text("Someone Veetbot knows only by a name or role appears here until you confirm or remove them.")
                                    .foregroundColor(AppTheme.inkSoft)
                                    .a2ListRow()
                            }
                        } else {
                            Section {
                                Label("People you remember", systemImage: "person.2")
                                    .foregroundColor(AppTheme.ink)
                                    .a2ListRow()
                                Text("People you know and people you write to appear here. You can also add someone.")
                                    .foregroundColor(AppTheme.inkSoft)
                                    .a2ListRow()
                            }
                        }
                    }
                    ForEach(model.items) { person in
                        personRow(person)
                            .a2ListRow()
                            .accessibilityIdentifier("people.row.\(person.id.uuidString)")
                            .onAppear { if person.id == model.items.last?.id { Task { await model.loadMore() } } }
                        if model.collection == .review, person.nextThread != nil {
                            threadActions(person).a2ListRow()
                        }
                    }
                    if model.isLoadingMore { ProgressView("Loading more people…").a2ListRow() }
                    if let error = model.errorMessage {
                        Section {
                            Label(error, systemImage: model.availability == .denied ? "lock" : "exclamationmark.triangle")
                                .foregroundColor(AppTheme.inkSoft)
                                .a2ListRow()
                            Button(model.availability == .changed ? "Refresh people" : "Retry") { Task { await model.retry() } }
                                .buttonStyle(SecondaryCapsuleButtonStyle())
                                .a2ListRow()
                        }
                    }
                    if model.availability != .disabled && model.availability != .denied {
                        Group {
                            if dynamicTypeSize.isAccessibilitySize {
                                VStack(alignment: .leading, spacing: 12) { directoryActions }
                            } else {
                                HStack(spacing: 8) { directoryActions }
                            }
                        }
                        .padding(.top, 12)
                        .a2ListRow()
                    }
                }
            }
        }
        .tint(PeopleSurface.fill)
        .accessibilityIdentifier("people.browser")
        .accessibilityLabel("People directory")
        .task { await model.reload() }
        .task { await model.loadCounts() }
        .sheet(isPresented: $showingImport) { PeopleImportHistoryView(initialSessionID: sessionID) }
        .sheet(isPresented: $showingAdd) { AddPersonView(sessionID: sessionID) { _ in Task { await model.reload() } } }
    }

    @ViewBuilder private var directoryActions: some View {
        Button { showingImport = true } label: { Label("Import history", systemImage: "clock.arrow.circlepath") }
            .buttonStyle(SecondaryCapsuleButtonStyle())
        Button { showingAdd = true } label: { Label("Add person", systemImage: "person.badge.plus") }
            .buttonStyle(SecondaryCapsuleButtonStyle())
    }

    /// The owed thread's in-place correction (ADR-0227): the owner's word is
    /// the attributed report a completed or cancelled commitment needs.
    private func threadActions(_ person: PersonView) -> some View {
        HStack(spacing: 8) {
            Button("Done") { Task { await model.resolveThread(of: person, state: "completed") } }
                .accessibilityIdentifier("people.thread.done.\(person.id.uuidString)")
            Button("Not needed") { Task { await model.resolveThread(of: person, state: "cancelled") } }
                .accessibilityIdentifier("people.thread.cancel.\(person.id.uuidString)")
        }
        .buttonStyle(QuietCapsuleButtonStyle())
        .disabled(model.resolvingThreadOf != nil)
        .padding(.leading, PeopleMetrics.rowAvatar + 10)
    }

    /// Why a row is in this collection, or when the owner last had contact.
    private func note(for person: PersonView) -> String? {
        if let attention = model.attentionLine(for: person) { return attention }
        if model.collection == .recent, let at = person.lastInteractionAt {
            return PeopleViewModel.lastContactText(at)
        }
        return nil
    }

    @ViewBuilder private func personRow(_ person: PersonView) -> some View {
        if let selection {
            let selected = selection.wrappedValue == person.id
            Button { selection.wrappedValue = person.id } label: {
                PeopleDirectoryRow(person: person, note: note(for: person))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .background(
                        RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .fill(selected ? AppTheme.selected : Color.clear)
                    )
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityAddTraits(selected ? .isSelected : [])
        } else {
            NavigationLink {
                PeopleDetailView(personID: person.id, sessionID: sessionID)
            } label: {
                PeopleDirectoryRow(person: person, note: note(for: person))
                    #if os(macOS)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .contentShape(Rectangle())
                    #endif
            }
            #if os(macOS)
            .buttonStyle(.plain)
            #endif
        }
    }

    private func filterLabel(_ title: String, value: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).veetType(.meta).foregroundColor(AppTheme.inkSoft).fixedSize(horizontal: false, vertical: true)
            HStack(alignment: .firstTextBaseline) {
                Text(value).foregroundColor(AppTheme.ink).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                Image(systemName: "chevron.up.chevron.down").foregroundColor(AppTheme.inkSoft).accessibilityHidden(true)
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
                Text("Use a name or a description you recognize, such as ‘my sister in Canada’.").foregroundColor(AppTheme.inkSoft)
                if let error { Text(error).foregroundColor(AppTheme.red) }
            }
            .tint(AppTheme.tealFill)
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
                                Text("\(saved.recordsProcessed) processed · $\(saved.spentUSD)").veetType(.meta).foregroundColor(AppTheme.inkSoft)
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
                    Text("The end date is excluded.").veetType(.meta).foregroundColor(.secondary)
                    Stepper("Up to \(maximum) records", value: $maximum, in: 100...10000, step: 100)
                    TextField("Maximum cost in USD", text: $budget)
                    Button("Preview import") { Task { await model.preview(sessionIDs: selected, accountIDs: selectedAccounts, fetchMailbox: fetchMailbox, since: since, until: until, maxRecords: maximum, maxCost: budget) } }.disabled(selected.isEmpty && selectedAccounts.isEmpty)
                }.disabled(model.canRetry)
            }
            if let error = model.errorMessage { Text(error).foregroundColor(AppTheme.red) }
            if model.canRetry { Button("Retry request") { Task { await model.retry() } } }
            if model.isBusy { ProgressView() }
        }
    .tint(AppTheme.tealFill)
    .disabled(model.isBusy)
    }
    var body: some View {
        Group {
            #if os(macOS)
            VStack(alignment: .leading, spacing: 16) {
                A2SheetHeader(title: "Import People history", dismissTitle: "Close") { dismiss() }
                ScrollView { importForm }
            }
            .padding()
            .background(AppTheme.paper)
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
        .onChange(of: scenePhase) { _, phase in
            if phase == .active { Task { if model.job != nil { await model.refresh() } else { await model.loadImports() } } }
        }
        .onChange(of: model.job?.id) {
            if case .string(let saved)? = model.job?.scope?["max_cost_usd"] { budget = saved }
        }
        .accessibilityIdentifier("people.import")
    }
}

/// The directory's scroll surface, with individually accessible native controls.
struct PeopleList<Content: View>: View {
    let label: String
    @ViewBuilder let content: () -> Content
    var body: some View {
        #if os(macOS)
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 2) { content() }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
                .accessibilityElement(children: .contain)
                .accessibilityLabel(label)
        }
        .background(AppTheme.paper)
        .accessibilityElement(children: .contain)
        #else
        List { content() }
            .a2List()
        #endif
    }
}

/// One person in the directory: initials, name, and any identity note.
private struct PeopleDirectoryRow: View {
    let person: PersonView
    /// Why the person needs the owner, or their last contact (ADR-0227).
    var note: String? = nil

    var body: some View {
        HStack(spacing: 10) {
            PeopleMonogram(name: person.displayName, size: PeopleMetrics.rowAvatar)
            VStack(alignment: .leading, spacing: 2) {
                Text(person.displayName).veetType(.ui, weight: .semibold).foregroundColor(AppTheme.ink)
                if person.state == "merged" {
                    Text("Combined identity").veetType(.meta).foregroundColor(PeopleSurface.muted)
                }
                if let note {
                    Text(note)
                        .veetType(.meta)
                        .foregroundColor(note.hasPrefix("Needs you") ? AppTheme.amberText : PeopleSurface.muted)
                        .lineLimit(2)
                }
            }
            Spacer(minLength: 0)
            if person.pinned {
                Image(systemName: "pin.fill").foregroundColor(PeopleSurface.attention).accessibilityLabel("Pinned")
            }
        }
        .padding(.vertical, 3)
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
    /// Frames a People sheet with `sheetFrame` at its ideal reading width.
    func peopleModalFrame(_ width: PeopleModalWidth, minHeight: CGFloat, idealHeight: CGFloat) -> some View {
        sheetFrame(minWidth: 320, macMinWidth: width.macMinimum, idealWidth: width.ideal, maxWidth: .infinity,
                   minHeight: minHeight, idealHeight: idealHeight, maxHeight: .infinity)
    }
}

#if os(macOS)
/// SwiftUI's sheet and split-column hosting groups are separate from their
/// labeled content. Give those native navigation containers a description too.
private struct PeopleSheetAccessibilityLabel: NSViewRepresentable {
    final class LabelView: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            labelContainers()
        }
        override func layout() {
            super.layout()
            labelContainers()
        }
        private func labelContainers() {
            window?.setAccessibilityLabel("People")
            window?.contentView?.setAccessibilityLabel("People")
            guard let content = window?.contentView else { return }
            labelGroups(in: content)
        }
        private func labelGroups(in view: NSView) {
            if view.accessibilityRole() == .group,
               view.accessibilityLabel()?.isEmpty != false {
                view.setAccessibilityLabel("People navigation")
            }
            for child in view.subviews { labelGroups(in: child) }
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
                    Text(title).veetType(.ui, weight: .semibold).accessibilityAddTraits(.isHeader)
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

/// Directory navigation shared by the wide People workspace.
struct PeopleDirectorySidebar: View {
    @ObservedObject var model: PeopleViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 4) {
                Text("People").veetType(.display)
                Text("The people you know or have written to.")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                TextField("Search people", text: Binding(get: { model.searchText }, set: model.setSearchText))
                    .textFieldStyle(.plain).a2Field().padding(.vertical, 12)
                    .accessibilityIdentifier("people.sidebar.search")
                    .accessibilityLabel("Search people")
                SidebarSectionLabel(title: "Directory")
                collection(.review, symbol: "flag")
                collection(.recent, symbol: "clock")
                collection(.people, symbol: "person.2")
                collection(.pinned, symbol: "pin")
                SidebarSectionLabel(title: "Maintenance")
                collection(.duplicates, symbol: "person.2.badge.gearshape")
                collection(.all, symbol: "person.crop.rectangle.stack")
                Text("Veetbot never merges people on a name alone. Matches wait here for you.")
                    .veetType(.meta).foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true).padding(.top, 12)
            }.padding(16)
        }.background(AppTheme.paperDeep)
    }

    private func collection(_ value: PeopleCollection, symbol: String) -> some View {
        Button { Task { await model.selectCollection(value) } } label: {
            SidebarRow {
                Image(systemName: symbol)
            } title: {
                Text(value.rawValue)
            } trailing: { _ in
                if let count = model.count(for: value) { Text(count, format: .number) }
            }
            .padding(.horizontal, 8)
            .background(RoundedRectangle(cornerRadius: 7).fill(model.collection == value ? AppTheme.selected : .clear))
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(model.collection == value ? .isSelected : [])
        .accessibilityIdentifier("people.collection.\(value.rawValue)")
    }
}
