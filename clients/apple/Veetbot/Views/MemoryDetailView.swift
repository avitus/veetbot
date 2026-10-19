import SwiftUI

/// One belief's full exposure-list projection (memory-read-api-and-browser.md),
/// read as an A2 page: its kind, the statement in the serif face, where it
/// came from, then its classification, provenance and lifecycle as record
/// groups. Rows for a nil or empty field are omitted rather than shown blank.
/// With a browsing model attached, the view also carries the review and
/// deletion actions of ADR-0117; People's evidence inspection opens it
/// without one.
public struct MemoryDetailView: View {
    @State private var memory: MemoryView
    private let model: MemoryViewModel?
    @State private var confirmingDeletion = false
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
                    Text(message).appFont(.caption).foregroundColor(AppTheme.inkSoft)
                }
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
                        .appFont(.caption, weight: .medium)
                        .foregroundColor(AppTheme.amberText)
                }
            }
            Text(memory.statement)
                .appDisplayFont(.title2)
                .foregroundColor(AppTheme.ink)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            if let person = memory.personLink {
                NavigationLink {
                    PeopleDetailView(personID: person.id)
                } label: {
                    Label(person.name, systemImage: "person")
                        .appFont(.caption)
                        .foregroundColor(AppTheme.tealText)
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("memory.detail.person")
            } else {
                Text(memory.subject)
                    .appFont(.caption)
                    .foregroundColor(AppTheme.inkSoft)
            }
            Text(
                "Formed \(memory.createdAt.formatted(date: .abbreviated, time: .omitted)) · Last evidence \(memory.lastEvidenceAt.formatted(date: .abbreviated, time: .omitted))"
            )
            .appFont(.caption)
            .foregroundColor(AppTheme.inkSoft)
            HStack(spacing: 8) {
                MemorySensitivityBadge(sensitivity: memory.sensitivity, kind: memory.sensitivityKind)
                if memory.statusKind != .active {
                    MemoryStatusTag(status: memory.status)
                }
            }
        }
    }

    /// The amber-edged card that says why this memory waits on the owner.
    private var reviewCard: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Needs review", systemImage: "flag.fill")
                .appFont(.caption, weight: .semibold)
                .foregroundColor(AppTheme.amberText)
            if model != nil {
                Text("Formation committed this memory without an explicit statement from you. Use Review to confirm, correct, or remove it.")
                    .appFont(.callout)
                    .foregroundColor(AppTheme.ink)
                    .fixedSize(horizontal: false, vertical: true)
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

    /// The review outcomes of ADR-0117, offered only when a browsing model can
    /// carry them out. A toolbar menu rather than a card of buttons, so the
    /// belief's own lines keep their places on every layout. Each action sends
    /// a fresh idempotency key and replaces this view's belief with the
    /// server's answer.
    private func reviewMenu(_ model: MemoryViewModel) -> some View {
        let busy = model.pendingActionID != nil || model.changesUnavailable
        return Menu {
            if memory.flaggedForReview {
                Button("Mark reviewed") {
                    Task {
                        if let reviewed = await model.review(memory, outcome: .dismiss) {
                            memory = reviewed
                        }
                    }
                }
                .disabled(busy)
                .accessibilityIdentifier("memory.detail.review.dismiss")
            }
            if memory.status == "active" || memory.status == "provisional" {
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
