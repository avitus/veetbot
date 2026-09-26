import SwiftUI

/// A person's profile (people-and-relationships.md §9): who they are to the
/// owner, their relationships, history, open threads, and the facts Veetbot
/// holds about them, each with its evidence and corrections.
public struct PeopleDetailView: View {
    private let personID: UUID
    private let sessionID: UUID?
    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?

    public init(personID: UUID, sessionID: UUID? = nil,
                makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = { await MemoryViewModel.makeDefaultAPIClient() }) {
        self.personID = personID
        self.sessionID = sessionID
        self.makeAPIClient = makeAPIClient
    }

    public var body: some View {
        // SwiftUI keeps a view's state when a container shows it again for
        // another person, as the Mac directory's profile column does. The
        // identity gives every person their own model, edits and sheets.
        PeopleProfile(personID: personID, sessionID: sessionID, makeAPIClient: makeAPIClient)
            .id(personID)
    }
}

private struct PeopleProfile: View {
    @StateObject private var model: PeopleDetailViewModel
    let sessionID: UUID?
    @Environment(\.scenePhase) private var scenePhase
    @Environment(\.dismissPeople) private var dismissPeople
    @Environment(\.peopleSelection) private var selection
    @Environment(\.dynamicTypeSize) private var typeSize
    @State private var renaming = false
    @State private var name = ""
    @FocusState private var nameFocused: Bool
    @State private var editingFact: MemoryView?
    @State private var removingFact: MemoryView?
    @State private var inspectedFact: MemoryView?
    @State private var showingIdentity = false
    @State private var showingAlias = false
    @State private var conversation: PeopleConversationSelection?

    init(personID: UUID, sessionID: UUID?, makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient?) {
        _model = StateObject(wrappedValue: PeopleDetailViewModel(personID: personID, makeAPIClient: makeAPIClient))
        self.sessionID = sessionID
    }

    /// Writes wait while one is saving, one can be retried, or the person changed.
    private var locked: Bool { model.isSaving || model.canRetrySave || model.requiresRefresh }

    /// A removed fact's receipt; identity receipts carry their operation.
    private var factReceipt: PeopleOperationView? {
        guard !model.isForgotten, let receipt = model.receipt, receipt.operation == nil else { return nil }
        return receipt
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: PeopleMetrics.sectionSpacing) { content }
                .frame(maxWidth: PeopleMetrics.readableWidth, alignment: .leading)
                .padding(PeopleMetrics.pagePadding)
                .frame(maxWidth: .infinity)
        }
        .background(PeopleSurface.canvas)
        .tint(PeopleSurface.accent)
        // A capsule clips a label that wraps at accessibility sizes.
        .buttonBorderShape(typeSize.isAccessibilitySize ? .roundedRectangle : .automatic)
        .accessibilityElement(children: .contain)
        .navigationTitle(model.profile?.person.displayName ?? "Person")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .accessibilityIdentifier("people.detail")
        .accessibilityLabel("Person profile")
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                if let dismissPeople {
                    Button(action: dismissPeople) { Image(systemName: "xmark").imageScale(.large) }
                        .accessibilityLabel("Close")
                }
            }
            ToolbarItem(placement: .primaryAction) {
                Button { Task { await model.reload(discardPending: true) } } label: { Label("Refresh", systemImage: "arrow.clockwise") }
                    .disabled(model.isSaving)
            }
        }
        .task { await model.reload() }
        .onChange(of: scenePhase) { phase in if phase == .active && !model.isSaving && !model.canRetrySave && model.preview == nil && !model.isForgotten { Task { await model.reload() } } }
        .sheet(item: $editingFact) { fact in PeopleFactEditor(model: model, fact: fact, sessionID: sessionID) }
        .sheet(isPresented: $showingAlias) { PeopleAliasEditor(model: model, sessionID: sessionID) }
        .sheet(isPresented: $showingIdentity) { PeopleIdentityRepairView(model: model, sessionID: sessionID) }
        .sheet(item: $conversation) { selection in PeopleConversationView(selection: selection) }
        .sheet(item: $inspectedFact) { fact in PeopleFactEvidenceSheet(fact: fact) }
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

    @ViewBuilder private var content: some View {
        if model.isForgotten {
            forgotten
            if let error = model.errorMessage { errorNotice(error) }
        } else if let profile = model.profile {
            let open = profile.commitments.filter { ["open", "proposed", "uncertain"].contains($0.state) }
            header(profile)
            notices(profile)
            names(profile)
            if !profile.relationships.isEmpty { relationships(profile) }
            history
            if !open.isEmpty { commitments(open, in: profile) }
            if !profile.facts.isEmpty || factReceipt != nil { facts(profile) }
            notRecorded(profile, open: open)
            coverage(profile)
            manage(profile)
        } else if let error = model.errorMessage {
            errorNotice(error)
        } else {
            ProgressView("Loading person…")
                .frame(maxWidth: .infinity)
                .padding(.vertical, 48)
        }
    }

    // MARK: Overview

    private func header(_ profile: PersonProfileView) -> some View {
        let person = profile.person
        return VStack(alignment: .leading, spacing: 16) {
            HStack(alignment: .center, spacing: 16) { PeopleMonogram(name: person.displayName); identity(profile) }
            headerActions(person)
        }
    }

    /// One row where it fits; otherwise the conversation on its own line above
    /// the smaller actions, and at accessibility sizes one action per line.
    @ViewBuilder private func headerActions(_ person: PersonView) -> some View {
        let prepare = Button { conversation = PeopleConversationSelection(person: person) } label: {
            Label("Prepare for a conversation", systemImage: "text.bubble")
        }
        .buttonStyle(.borderedProminent)
        let pin = Button { Task { await model.setPinned(!person.pinned, sessionID: sessionID) } } label: {
            Label(person.pinned ? "Unpin" : "Pin", systemImage: person.pinned ? "pin.slash" : "pin")
        }
        .buttonStyle(.bordered)
        .accessibilityLabel(person.pinned ? "Unpin person" : "Pin person")
        .disabled(locked)
        let rename = Button { startRenaming(person) } label: { Label("Rename", systemImage: "pencil") }
            .buttonStyle(.bordered)
            .disabled(locked || renaming)
        if typeSize.isAccessibilitySize {
            VStack(alignment: .leading, spacing: 8) { prepare; pin; rename }
        } else if #available(macOS 13, iOS 16, *) {
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { prepare; pin; rename }
                VStack(alignment: .leading, spacing: 8) { prepare; HStack(spacing: 8) { pin; rename } }
            }
        } else {
            VStack(alignment: .leading, spacing: 8) { prepare; HStack(spacing: 8) { pin; rename } }
        }
    }

    /// A row's leading symbol. Accessibility sizes drop it so the text keeps the width.
    @ViewBuilder private func rowSymbol(_ name: String, tint: Color = PeopleSurface.accent) -> some View {
        if !typeSize.isAccessibilitySize {
            Image(systemName: name)
                .foregroundColor(tint)
                .frame(width: 20)
                .accessibilityHidden(true)
        }
    }

    private func identity(_ profile: PersonProfileView) -> some View {
        let person = profile.person
        return VStack(alignment: .leading, spacing: 5) {
            if renaming {
                renameField(person)
            } else {
                Text(person.displayName)
                    .appFont(.title, weight: .bold)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
                    .accessibilityAddTraits(.isHeader)
            }
            if let summary = profile.relationshipSummary {
                Text(summary).appFont(.body).foregroundColor(PeopleSurface.muted)
            }
            if let contact = profile.primaryContact {
                Label(contact.value, systemImage: contact.symbolName)
                    .appFont(PeopleMetrics.meta)
                    .foregroundColor(PeopleSurface.muted)
                    .textSelection(.enabled)
            }
            if person.pinned || person.state != "active" {
                PeopleActionRow {
                    if person.pinned { PeopleBadge(text: "Pinned", systemImage: "pin.fill", tint: PeopleSurface.accent) }
                    if person.state == "merged" { PeopleBadge(text: "Combined identity", systemImage: "arrow.triangle.merge") }
                    if person.state == "provisional" {
                        // Provisional covers people the owner writes to, so this names the
                        // state rather than asking for review (ADR-0121).
                        PeopleBadge(text: "Not confirmed yet", systemImage: "questionmark.circle", tint: PeopleSurface.attention)
                        Button("Confirm this identity") { Task { await model.confirmIdentity(sessionID: sessionID) } }
                            .buttonStyle(PeopleInlineButtonStyle())
                            .disabled(locked)
                    }
                }
                .padding(.top, 4)
            }
        }
    }

    private func renameField(_ person: PersonView) -> some View {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let unchanged = trimmed.isEmpty || trimmed == person.displayName
        return VStack(alignment: .leading, spacing: 8) {
            TextField("Name", text: $name)
                .textFieldStyle(.roundedBorder)
                .appFont(.title3)
                .focused($nameFocused)
                .onSubmit { if !unchanged && !locked { Task { await saveName(trimmed) } } }
            HStack(spacing: 8) {
                Button("Save name") { Task { await saveName(trimmed) } }
                    .buttonStyle(.borderedProminent)
                    .tint(PeopleSurface.accent)
                    .disabled(unchanged || locked)
                Button("Cancel") { renaming = false }
                    .buttonStyle(.bordered)
            }
        }
    }

    private func startRenaming(_ person: PersonView) {
        name = person.displayName
        renaming = true
        nameFocused = true
    }

    private func saveName(_ trimmed: String) async {
        await model.saveName(trimmed, sessionID: sessionID)
        if model.errorMessage == nil && !model.canRetrySave { renaming = false }
    }

    /// Questions and outcomes that belong above the sections.
    @ViewBuilder private func notices(_ profile: PersonProfileView) -> some View {
        if let error = model.errorMessage { errorNotice(error) }
        if let error = model.evidenceError {
            PeopleCallout(systemImage: "doc.text.magnifyingglass", title: error, tint: PeopleSurface.muted) { EmptyView() }
        }
        ForEach(profile.mergeSuggestions) { suggestion in
            let other = suggestion.other(than: profile.person.id)
            PeopleCallout(systemImage: "person.2", title: "May be the same person as \(other.displayName)", message: suggestion.explanation) {
                Button("Merge") { Task { await model.resolveSuggestion(suggestion, decision: "merge", sessionID: sessionID) } }
                    .buttonStyle(.borderedProminent)
                    .tint(PeopleSurface.accent)
                Button("Not the same") { Task { await model.resolveSuggestion(suggestion, decision: "separate", sessionID: sessionID) } }
                    .buttonStyle(.bordered)
            }
            .disabled(locked)
            .accessibilityIdentifier("people.detail.suggestion.\(suggestion.id.uuidString)")
        }
    }

    private func errorNotice(_ message: String) -> some View {
        PeopleCallout(systemImage: "exclamationmark.triangle", title: message) {
            if model.canRetrySave {
                Button("Retry save") { Task { await model.retrySave() } }
                    .buttonStyle(.borderedProminent)
                    .tint(PeopleSurface.accent)
            }
            Button("Refresh") { Task { await model.reload(discardPending: true) } }
                .buttonStyle(.bordered)
        }
    }

    private func names(_ profile: PersonProfileView) -> some View {
        PeopleSection("Names and contact details") {
            ForEach(Array(profile.aliases.enumerated()), id: \.element.id) { index, alias in
                PeopleRow(divided: index > 0) { aliasRow(alias) }
            }
            PeopleRow(divided: !profile.aliases.isEmpty) {
                Button { showingAlias = true } label: { Label("Add name or contact alias…", systemImage: "plus.circle") }
                    .buttonStyle(PeopleInlineButtonStyle())
                    .disabled(locked)
            }
        }
    }

    private func aliasRow(_ alias: PersonAliasView) -> some View {
        let ended = alias.validTo != nil
        return HStack(alignment: .firstTextBaseline, spacing: 12) {
            rowSymbol(alias.symbolName, tint: ended ? PeopleSurface.muted : PeopleSurface.accent)
            VStack(alignment: .leading, spacing: 3) {
                Text(alias.value)
                    .appFont(.body)
                    .foregroundColor(ended ? PeopleSurface.muted : .primary)
                    .textSelection(.enabled)
                PeopleMeta(alias.validTo.map { "Assignment ended \($0.formatted(date: .abbreviated, time: .omitted))" } ?? alias.provenance)
            }
            Spacer(minLength: 8)
            if !ended {
                Menu {
                    Button("End this alias assignment") { Task { await model.endAlias(alias, sessionID: sessionID) } }
                } label: {
                    Image(systemName: "ellipsis.circle").imageScale(.large)
                }
                .peopleInlineMenu()
                .accessibilityLabel("Options for \(alias.value)")
                .disabled(locked)
            }
        }
    }

    // MARK: Sections

    private func relationships(_ profile: PersonProfileView) -> some View {
        PeopleSection("Relationships") {
            ForEach(Array(profile.relationships.enumerated()), id: \.element.id) { index, relationship in
                PeopleRow(divided: index > 0) {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        rowSymbol(relationship.symbolName)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(relationship.phrase(in: profile))
                                .appFont(.body)
                                .foregroundColor(relationship.validTo == nil ? .primary : PeopleSurface.muted)
                                .fixedSize(horizontal: false, vertical: true)
                            if !relationship.qualifier.isEmpty { PeopleMeta(relationship.qualifier) }
                            if let since = relationship.sinceLabel { PeopleMeta(since) }
                            if let until = relationship.untilLabel { PeopleMeta(until) }
                            let counterpart = relationship.counterpart(in: profile)
                            if counterpart != nil || !relationship.supportIDs.isEmpty {
                                HStack(spacing: 16) {
                                    if let counterpart, let id = counterpart.id { personLink(id, name: profile.name(of: counterpart)) }
                                    evidenceButton(relationship.supportIDs)
                                }
                            }
                        }
                        Spacer(minLength: 0)
                    }
                }
            }
        }
    }

    private var history: some View {
        PeopleSection("History") {
            PeopleRow {
                if model.history.isEmpty {
                    Text("No recorded interactions. Earlier history may not have been imported.")
                        .appFont(.body)
                        .fixedSize(horizontal: false, vertical: true)
                } else {
                    VStack(alignment: .leading, spacing: 0) {
                        ForEach(Array(model.history.enumerated()), id: \.element.id) { index, interaction in
                            PeopleTimelineRow(systemImage: interaction.symbolName, symbolLabel: interaction.channelDescription,
                                              isLast: index == model.history.count - 1) {
                                Text(interaction.summary).appFont(.body).fixedSize(horizontal: false, vertical: true)
                                PeopleMeta(interaction.dateLabel)
                                // Observed mail and messages already say how they arrived.
                                if interaction.direction == "reported" || interaction.attribution != "observed" {
                                    PeopleMeta(interaction.channelDescription)
                                }
                                evidenceButton(interaction.supportIDs)
                            }
                        }
                    }
                }
            }
            if model.isLoadingHistory || model.hasMoreHistory || model.historyError != nil {
                PeopleRow(divided: true) {
                    VStack(alignment: .leading, spacing: 6) {
                        if let error = model.historyError { PeopleMeta(error) }
                        if model.isLoadingHistory {
                            ProgressView().controlSize(.small)
                        } else if model.historyError != nil {
                            Button("Retry history") { Task { await model.loadHistory() } }.buttonStyle(PeopleInlineButtonStyle())
                        } else {
                            Button("More history") { Task { await model.loadHistory() } }.buttonStyle(PeopleInlineButtonStyle())
                        }
                    }
                }
            }
        }
    }

    private func commitments(_ open: [PersonCommitmentView], in profile: PersonProfileView) -> some View {
        PeopleSection("Open threads") {
            ForEach(Array(open.enumerated()), id: \.element.id) { index, commitment in
                PeopleRow(divided: index > 0) {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        rowSymbol(commitment.state == "uncertain" ? "questionmark.circle" : "circle",
                                  tint: commitment.state == "uncertain" ? PeopleSurface.attention : PeopleSurface.accent)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(commitment.description).appFont(.body).fixedSize(horizontal: false, vertical: true)
                            PeopleMeta(commitment.parties(in: profile))
                            if let due = commitment.dueLabel { PeopleMeta(due) }
                            evidenceButton(commitment.supportIDs)
                        }
                        Spacer(minLength: 8)
                        PeopleBadge(text: memoryDisplayText(commitment.state),
                                    tint: commitment.state == "open" ? PeopleSurface.accent : commitment.state == "uncertain" ? PeopleSurface.attention : PeopleSurface.muted)
                    }
                }
            }
        }
    }

    private func facts(_ profile: PersonProfileView) -> some View {
        PeopleSection("Facts and evidence") {
            if let receipt = factReceipt {
                PeopleRow {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("Fact removed").appFont(.headline)
                        Text(receipt.state == "cleanup_pending" ? "The fact has been removed. Generated replies and files are still being cleaned up." : "The fact and its generated copies have been removed. Original messages remain at their source.")
                            .appFont(.body)
                            .fixedSize(horizontal: false, vertical: true)
                        if receipt.state == "cleanup_pending" {
                            Button("Check cleanup") { Task { await model.refreshReceipt() } }.buttonStyle(PeopleInlineButtonStyle())
                        }
                    }
                }
            }
            ForEach(Array(profile.facts.enumerated()), id: \.element.id) { index, fact in
                PeopleRow(divided: index > 0 || factReceipt != nil) {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Text(fact.statement)
                                .appFont(.body)
                                .fixedSize(horizontal: false, vertical: true)
                                .textSelection(.enabled)
                            Spacer(minLength: 8)
                            if fact.statusKind != .active { PeopleBadge(text: memoryDisplayText(fact.status)) }
                        }
                        PeopleMeta("\(memoryDisplayText(fact.derivation)), last evidence \(fact.lastEvidenceAt.formatted(date: .abbreviated, time: .omitted))")
                        HStack(spacing: 16) {
                            inspectLink(fact)
                            Menu {
                                Button("Correct or mark changed…") { editingFact = fact }
                                Button("Confirm this fact") { Task { await model.correct(fact, operation: "affirm", statement: nil, sessionID: sessionID) } }
                                Button("Reject this fact") { Task { await model.correct(fact, operation: "reject", statement: nil, sessionID: sessionID) } }
                                Button("Remove fact…", role: .destructive) { removingFact = fact }
                            } label: {
                                Text("Correct")
                            }
                            .peopleInlineMenu()
                            .disabled(locked || profile.factRevision(fact.id) == nil)
                        }
                    }
                }
            }
        }
    }

    /// The sections with nothing to show, in one quiet line. An empty section
    /// does not claim the relationship, thread or fact does not exist.
    @ViewBuilder private func notRecorded(_ profile: PersonProfileView, open: [PersonCommitmentView]) -> some View {
        let missing = [
            profile.relationships.isEmpty ? "relationships" : nil,
            open.isEmpty ? "open threads" : nil,
            profile.facts.isEmpty && factReceipt == nil ? "facts" : nil,
        ].compactMap { $0 }
        if !missing.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                Text("No \(peopleList(missing, conjunction: "or")) recorded yet.")
                    .appFont(.body)
                    .fixedSize(horizontal: false, vertical: true)
                // Empty states keep primary text: the Mac audit at Large text
                // fails muted supporting text here, as it failed secondary text.
                Text("They appear here as Veetbot learns more about \(profile.person.displayName).")
                    .appFont(PeopleMetrics.meta)
                    .fixedSize(horizontal: false, vertical: true)
            }
            // The element covers the text, not the decorative edge.
            .accessibilityElement(children: .combine)
            .padding(PeopleMetrics.rowPadding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .overlay(
                RoundedRectangle(cornerRadius: PeopleMetrics.cardRadius, style: .continuous)
                    .stroke(PeopleSurface.hairline, style: StrokeStyle(lineWidth: 1, dash: [5, 4]))
            )
        }
    }

    private func coverage(_ profile: PersonProfileView) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: "info.circle").foregroundColor(PeopleSurface.muted).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                Text(profile.coverage)
                if profile.truncated { Text("This overview is partial. Open the history to see more recorded interactions.") }
            }
            .appFont(PeopleMetrics.meta)
            .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.horizontal, 4)
    }

    private func manage(_ profile: PersonProfileView) -> some View {
        let identityReceipt = model.receipt?.operation != nil
        return PeopleSection("Manage person") {
            ForEach(Array(profile.automaticMerges.enumerated()), id: \.element.id) { index, merge in
                PeopleRow(divided: index > 0) {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        VStack(alignment: .leading, spacing: 3) {
                            Text("Merged with \(merge.merged.displayName)").appFont(.body)
                            PeopleMeta("Combined automatically \(merge.mergedAt.formatted(date: .abbreviated, time: .omitted))")
                        }
                        Spacer(minLength: 8)
                        Button("Preview undo") { Task { await model.undoAutomaticMerge(merge, sessionID: sessionID) } }
                            .buttonStyle(PeopleInlineButtonStyle())
                            .accessibilityLabel("Preview undoing the merge with \(merge.merged.displayName)")
                    }
                }
            }
            if identityReceipt {
                PeopleRow(divided: !profile.automaticMerges.isEmpty) {
                    HStack(alignment: .firstTextBaseline, spacing: 12) {
                        Image(systemName: "checkmark.circle").foregroundColor(PeopleSurface.accent).accessibilityHidden(true)
                        Text("Identity change saved.").appFont(.body)
                        Spacer(minLength: 8)
                        Button("Preview undo") { Task { await model.undoIdentity(sessionID: sessionID) } }
                            .buttonStyle(PeopleInlineButtonStyle())
                    }
                }
            }
            PeopleRow(divided: !profile.automaticMerges.isEmpty || identityReceipt) {
                PeopleActionRow {
                    Button("Repair identity…") { showingIdentity = true }
                    Button("Forget person…", role: .destructive) { Task { await model.previewForget(sessionID: sessionID) } }
                        .tint(PeopleSurface.destructive)
                }
                .buttonStyle(.bordered)
            }
        }
        .disabled(locked)
    }

    private var forgotten: some View {
        let pending = model.receipt?.state == "cleanup_pending"
        return VStack(spacing: 14) {
            Image(systemName: "person.crop.circle.badge.xmark")
                .font(.system(size: 30, weight: .light))
                .foregroundColor(PeopleSurface.accent)
                .frame(width: 72, height: 72)
                .background(PeopleSurface.accent.opacity(0.08))
                .clipShape(Circle())
                .accessibilityHidden(true)
            Text("Forgotten").appFont(.title3, weight: .semibold).accessibilityAddTraits(.isHeader)
            Text(pending ? "Derived memories have been removed. Generated replies and files are still being cleaned up." : "Derived memories and history have been removed. Original messages remain at their source.")
                .appFont(.body)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if pending { Button("Check cleanup") { Task { await model.refreshReceipt() } }.buttonStyle(.bordered) }
        }
        .padding(.vertical, 40)
        .padding(.horizontal, 20)
        .frame(maxWidth: .infinity)
    }

    // MARK: Links

    @ViewBuilder private func evidenceButton(_ references: [UUID]) -> some View {
        if let reference = references.first {
            Button("View source") { Task { if let source = await model.evidence(reference) { conversation = PeopleConversationSelection(source: source) } } }
                .buttonStyle(PeopleInlineButtonStyle())
                .accessibilityHint("Opens the message this came from")
        }
    }

    /// Opens a related person: in place beside a directory, or pushed onto the stack.
    @ViewBuilder private func personLink(_ id: UUID, name: String) -> some View {
        if let selection {
            Button("View \(name)") { selection(id) }.buttonStyle(PeopleInlineButtonStyle())
        } else {
            NavigationLink("View \(name)") { PeopleDetailView(personID: id, sessionID: sessionID) }
                .buttonStyle(PeopleInlineButtonStyle())
        }
    }

    @ViewBuilder private func inspectLink(_ fact: MemoryView) -> some View {
        if selection != nil {
            Button("Inspect evidence") { inspectedFact = fact }.buttonStyle(PeopleInlineButtonStyle())
        } else {
            NavigationLink("Inspect evidence") { MemoryDetailView(memory: fact) }
                .buttonStyle(PeopleInlineButtonStyle())
        }
    }
}

/// A fact's full memory record, opened over a profile that sits beside the
/// directory, where pushing it would leave no way back.
private struct PeopleFactEvidenceSheet: View {
    let fact: MemoryView
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        Group {
            if #available(macOS 13, iOS 16, *) {
                NavigationStack { record }
            } else {
                NavigationView { record }
            }
        }
        .environment(\.peopleSelection, nil)
        .peopleModalFrame(.reading, minHeight: 450, idealHeight: 620)
    }

    private var record: some View {
        MemoryDetailView(memory: fact)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
    }
}
