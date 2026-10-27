import SwiftUI

/// One belief's full exposure-list projection (memory-read-api-and-browser.md),
/// read as an A2 page: its kind, the statement in the serif face, where it
/// came from, then its classification, provenance and lifecycle as record
/// groups. Rows for a nil or empty field are omitted rather than shown blank.
/// With a browsing model attached, the view also carries the review and
/// deletion actions of ADR-0117, the wording correction and evidence quotes
/// of ADR-0224; People's evidence inspection opens it without one.
public struct MemoryDetailView: View {
    @State private var memory: MemoryView
    private let model: MemoryViewModel?
    @State private var confirmingDeletion = false
    @State private var editingWording = false
    /// The sources the belief cites; nil until read or when the server cannot say.
    @State private var evidence: [MemoryEvidenceItem]?
    @Environment(\.dismiss) private var dismiss

    public init(memory: MemoryView, model: MemoryViewModel? = nil) {
        _memory = State(initialValue: memory)
        self.model = model
    }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                heading
                if memory.flaggedForReview { reviewCard }
                if let message = model?.errorMessage {
                    Text(message).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                }
                if let evidence, !evidence.isEmpty { evidenceGroup(evidence) }
                A2RecordGroup(title: "Classification", rows: classification)
                A2RecordGroup(title: "Provenance", rows: provenance)
                A2RecordGroup(title: "Lifecycle", rows: lifecycle)
            }
            .frame(maxWidth: 680, alignment: .leading)
            .padding(.horizontal, 24)
            .padding(.vertical, 20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(AppTheme.paper)
        .navigationTitle("Memory")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .accessibilityIdentifier("memory.detail")
        // A corrected belief is a new one, so its sources are read again.
        .task(id: memory.id) {
            guard let model else { return }
            evidence = await model.evidence(for: memory)
        }
        .sheet(isPresented: $editingWording) {
            if let model {
                MemoryWordingSheet(memory: memory, model: model) { memory = $0 }
            }
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                if let model { reviewMenu(model) }
            }
        }
        .confirmationDialog(
            "Delete this memory?", isPresented: $confirmingDeletion, titleVisibility: .visible
        ) {
            Button("Delete memory", role: .destructive) {
                guard let model else { return }
                Task {
                    if await model.delete(memory) { dismiss() }
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The memory and its generated copies are removed, and the same statement will not form again. Original messages remain at their source.")
        }
    }

    // MARK: Heading

    private var heading: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 8) {
                A2KindLabel(text: memoryDisplayText(memory.beliefType))
                if memory.flaggedForReview {
                    Text("· Needs review")
                        .veetType(.meta, weight: .medium)
                        .foregroundColor(AppTheme.amberText)
                }
            }
            Text(memory.statement)
                .veetType(.display)
                .foregroundColor(AppTheme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            if let person = memory.personLink {
                NavigationLink {
                    PeopleDetailView(personID: person.id)
                } label: {
                    Label(person.name, systemImage: "person")
                        .veetType(.meta)
                        .foregroundColor(AppTheme.tealText)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("memory.detail.person")
            } else {
                Text(memory.subject)
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
            }
            Text(
                "Formed \(memory.createdAt.formatted(date: .abbreviated, time: .omitted)) · Last evidence \(memory.lastEvidenceAt.formatted(date: .abbreviated, time: .omitted))"
            )
            .veetType(.meta)
            .foregroundColor(AppTheme.inkSoft)
            HStack(spacing: 8) {
                MemorySensitivityBadge(sensitivity: memory.sensitivity, kind: memory.sensitivityKind)
                if memory.statusKind != .active {
                    MemoryStatusTag(status: memory.status)
                }
            }
        }
    }

    /// The amber-edged card that asks whether a flagged memory is right
    /// (MagicPath coolly-breeze-5701): Looks right confirms it, Fix the
    /// wording supersedes it with the owner's words (ADR-0224), and the quiet
    /// answers reject it.
    private var reviewCard: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Is this right?", systemImage: "flag")
                .veetType(.meta, weight: .semibold)
                .foregroundColor(AppTheme.amberText)
            Text("Veetbot formed this without an explicit statement from you. Confirm it, correct it, or remove it.")
                .veetType(.ui)
                .foregroundColor(AppTheme.ink)
                .fixedSize(horizontal: false, vertical: true)
            if let model {
                reviewButtons(model)
            }
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .a2Card(border: AppTheme.amberLine)
    }

    // MARK: Records

    private var classification: [A2Record] {
        [
            A2Record("Belief type", memoryDisplayText(memory.beliefType)),
            A2Record("Status", memoryDisplayText(memory.status)),
            A2Record("Polarity", memoryDisplayText(memory.polarity)),
            A2Record("Portability", memoryDisplayText(memory.portability)),
            A2Record("Authority", memoryDisplayText(memory.authority)),
            A2Record("Confidence", String(format: "%.0f%%", memory.confidence * 100)),
            A2Record("Sensitivity", memoryDisplayText(memory.sensitivity)),
        ]
    }

    private var provenance: [A2Record] {
        var rows = [A2Record("Source session", memory.sourceSessionID.uuidString)]
        if !memory.sourceEventIDs.isEmpty {
            rows.append(A2Record("Source events", memory.sourceEventIDs.map(String.init).joined(separator: ", ")))
        }
        rows.append(A2Record("Formation run", memory.formationRunID.uuidString))
        if !memory.consolidationPolicyVersion.isEmpty {
            rows.append(A2Record("Consolidation policy version", memory.consolidationPolicyVersion))
        }
        if !memory.originScopes.isEmpty {
            rows.append(A2Record("Origin scopes", memory.originScopes.joined(separator: ", ")))
        }
        if !memory.conflictsWith.isEmpty {
            rows.append(A2Record("Conflicts with", memory.conflictsWith.map(\.uuidString).joined(separator: ", ")))
        }
        if let supersededBy = memory.supersededBy {
            rows.append(A2Record("Superseded by", supersededBy.uuidString))
        }
        return rows
    }

    private var lifecycle: [A2Record] {
        var rows = [
            A2Record("Claim kind", memory.claimKind),
            A2Record("Derivation", memory.derivation),
            A2Record("Longevity", memory.longevity),
            A2Record("Valid from", memory.validFrom.formatted()),
        ]
        if let validTo = memory.validTo {
            rows.append(A2Record("Valid to", validTo.formatted()))
        }
        if let expiresAt = memory.expiresAt {
            rows.append(A2Record("Expires", expiresAt.formatted()))
        }
        rows.append(A2Record("Last evidence", memory.lastEvidenceAt.formatted()))
        if let lastUsedAt = memory.lastUsedAt {
            rows.append(A2Record("Last used", lastUsedAt.formatted()))
        }
        rows.append(A2Record("Legacy reinforcement", memory.lastReinforcedAt.formatted()))
        rows.append(A2Record("Created", memory.createdAt.formatted()))
        rows.append(A2Record("Updated", memory.updatedAt.formatted()))
        return rows
    }

    // MARK: Evidence (ADR-0224 part B)

    /// Each cited source, newest first: the owner's own words as a quote,
    /// anything else as where it came from. The server decides what may show.
    private func evidenceGroup(_ items: [MemoryEvidenceItem]) -> some View {
        A2Group(title: "Where this came from") {
            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                if index > 0 { HairlineDivider().padding(.leading, VeetSpace.s4) }
                evidenceRow(item)
            }
        }
        .accessibilityIdentifier("memory.detail.evidence")
    }

    private func evidenceRow(_ item: MemoryEvidenceItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            if let excerpt = item.excerpt {
                A2Quote(cite: citation(for: item)) {
                    Text("\u{201C}\(excerpt)\u{201D}")
                        .veetType(.body)
                        .foregroundColor(AppTheme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            } else {
                Text(citation(for: item))
                    .veetType(.ui)
                    .foregroundColor(AppTheme.ink)
                if let note = withheldNote(for: item) {
                    Text(note).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                }
            }
        }
        .padding(.horizontal, VeetSpace.s4)
        .padding(.vertical, VeetSpace.s2)
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("memory.evidence.\(item.eventSequence)")
    }

    private func citation(for item: MemoryEvidenceItem) -> String {
        guard let at = item.evidenceAt else { return item.sourceLine }
        return "\(item.sourceLine) · \(at.formatted(date: .abbreviated, time: .omitted))"
    }

    private func withheldNote(for item: MemoryEvidenceItem) -> String? {
        switch item.withheld {
        case "erased": return "The message was deleted or is no longer kept."
        case "hazard": return "Not shown: it looks like it holds a password or instructions."
        case "ceiling", "scope": return "Not shown here. Open the conversation to read it."
        default: return nil
        }
    }

    private func reviewButtons(_ model: MemoryViewModel) -> some View {
        let busy = model.pendingActionID != nil || model.changesUnavailable
        return ViewThatFits(in: .horizontal) {
            HStack(spacing: 8) { reviewButtonSet(model) }
            VStack(alignment: .leading, spacing: 8) { reviewButtonSet(model) }
        }
        .disabled(busy)
        .padding(.top, 4)
    }

    @ViewBuilder
    private func reviewButtonSet(_ model: MemoryViewModel) -> some View {
        Button("Looks right") { apply(.dismiss, with: model) }
            .primaryCapsule()
            .accessibilityIdentifier("memory.review.looks-right")
        Button("Fix the wording") { editingWording = true }
            .buttonStyle(QuietCapsuleButtonStyle())
            .accessibilityIdentifier("memory.review.fix-wording")
        Button("Not true") { apply(.untrue, with: model) }
            .buttonStyle(QuietCapsuleButtonStyle())
            .accessibilityIdentifier("memory.review.not-true")
        if memory.portability != "local" {
            Button("Not relevant here") { apply(.notHere, with: model) }
                .buttonStyle(QuietCapsuleButtonStyle())
                .accessibilityIdentifier("memory.review.not-here")
        }
    }

    private func apply(_ outcome: MemoryReviewOutcome, with model: MemoryViewModel) {
        Task {
            if let reviewed = await model.review(memory, outcome: outcome) {
                memory = reviewed
            }
        }
    }

    /// The review outcomes of ADR-0117, offered only when a browsing model can
    /// carry them out. A toolbar menu rather than a card of buttons, so the
    /// belief's own lines keep their places on every layout. Each action sends
    /// a fresh idempotency key and replaces this view's belief with the
    /// server's answer.
    private func reviewMenu(_ model: MemoryViewModel) -> some View {
        let busy = model.pendingActionID != nil || model.changesUnavailable
        return Menu {
            if memory.flaggedForReview {
                Button("Looks right") {
                    Task {
                        if let reviewed = await model.review(memory, outcome: .dismiss) {
                            memory = reviewed
                        }
                    }
                }
                .disabled(busy)
                .accessibilityIdentifier("memory.detail.review.dismiss")
            }
            if memory.status == "active" || memory.status == "provisional"
                || memory.status == "retired" || memory.status == "expired"
            {
                // ADR-0247: the governed restore path. The server supersedes the
                // belief with the same words at user authority; the row is
                // replaced by the replacement, and the original stays as history.
                Button("Still true") {
                    Task {
                        if let reviewed = await model.review(memory, outcome: .confirm) {
                            memory = reviewed
                        }
                    }
                }
                .disabled(busy)
                .accessibilityIdentifier("memory.detail.review.confirm")
            }
            if memory.status == "active" || memory.status == "provisional" {
                Button("Edit wording…") { editingWording = true }
                    .disabled(busy)
                    .accessibilityIdentifier("memory.detail.edit")
                Button("Not true") {
                    Task {
                        if let reviewed = await model.review(memory, outcome: .untrue) {
                            memory = reviewed
                        }
                    }
                }
                .disabled(busy)
                .accessibilityIdentifier("memory.detail.review.untrue")
                if memory.portability != "local" {
                    Button("Not relevant here") {
                        Task {
                            if let reviewed = await model.review(memory, outcome: .notHere) {
                                memory = reviewed
                            }
                        }
                    }
                    .disabled(busy)
                    .accessibilityIdentifier("memory.detail.review.not_here")
                }
            }
            Button("Delete memory…", role: .destructive) { confirmingDeletion = true }
                .disabled(busy)
                .accessibilityIdentifier("memory.detail.delete")
        } label: {
            Label("Review", systemImage: memory.flaggedForReview ? "flag.fill" : "ellipsis.circle")
        }
        .accessibilityLabel("Review this memory")
        .accessibilityIdentifier("memory.detail.review")
    }
}

/// Fix the wording / Edit (ADR-0224 part A). Saving never edits the memory in
/// place: the server keeps the earlier wording as history, will not form it
/// again, and answers with the new memory, which this view then shows.
struct MemoryWordingSheet: View {
    let memory: MemoryView
    @ObservedObject var model: MemoryViewModel
    let onSaved: (MemoryView) -> Void
    @Environment(\.dismiss) private var dismiss
    @State private var text: String
    @FocusState private var focused: Bool

    static let limit = 500

    init(memory: MemoryView, model: MemoryViewModel, onSaved: @escaping (MemoryView) -> Void) {
        self.memory = memory
        self.model = model
        self.onSaved = onSaved
        _text = State(initialValue: memory.statement)
    }

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 12) {
                Text("Now")
                    .veetType(.label)
                    .foregroundColor(AppTheme.inkSoft)
                Text(memory.statement)
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                TextField(
                    "What should Veetbot remember?", text: $text,
                    prompt: .a2Prompt("What should Veetbot remember?"), axis: .vertical
                )
                .lineLimit(3...8)
                .textFieldStyle(.plain)
                .a2Field(radius: 12)
                .focused($focused)
                .accessibilityIdentifier("memory.wording.text")
                HStack(alignment: .firstTextBaseline) {
                    Text("Veetbot keeps the earlier wording as history and won\u{2019}t form it again.")
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 8)
                    Text("\(cleaned.count)/\(Self.limit)")
                        .foregroundColor(cleaned.count > Self.limit ? AppTheme.amberText : AppTheme.inkSoft)
                }
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
                if let message = model.errorMessage {
                    Text(message)
                        .veetType(.meta)
                        .foregroundColor(AppTheme.amberText)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("memory.wording.error")
                }
                Spacer(minLength: 0)
            }
            .padding(20)
            .background(AppTheme.paper)
            .navigationTitle(memory.flaggedForReview ? "Fix the wording" : "Edit wording")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        Task {
                            if let replacement = await model.correct(memory, wording: text) {
                                onSaved(replacement)
                                dismiss()
                            }
                        }
                    }
                    .disabled(!canSave)
                    .accessibilityIdentifier("memory.wording.save")
                }
            }
        }
        .tint(AppTheme.tealFill)
        .toastHost()
        .onAppear { focused = true }
        #if os(macOS)
        .sheetFrame(minWidth: 480, idealWidth: 560, minHeight: 300, idealHeight: 340)
        #endif
    }

    private var cleaned: String {
        text.split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }

    /// The server checks again; this only keeps an obviously unchanged or
    /// empty wording from costing a request.
    private var canSave: Bool {
        let original = memory.statement.split(whereSeparator: \.isWhitespace).joined(separator: " ")
        return !cleaned.isEmpty
            && cleaned.count <= Self.limit
            && cleaned.lowercased() != original.lowercased()
            && model.pendingActionID == nil
    }
}
