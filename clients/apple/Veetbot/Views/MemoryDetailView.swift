import SwiftUI

/// One belief's full exposure-list projection, sectioned to mirror the
/// server's `MemoryView` fields (memory-read-api-and-browser.md). Rows for a
/// nil or empty field are omitted rather than shown blank. With a browsing
/// model attached, the view also carries the review and deletion actions of
/// ADR-0117; People's evidence inspection opens it without one.
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
        List {
            Section("Statement") {
                Text(memory.statement)
                if let person = memory.personLink {
                    NavigationLink {
                        PeopleDetailView(personID: person.id)
                    } label: {
                        Label(person.name, systemImage: "person")
                            .appFont(.caption)
                    }
                    .accessibilityIdentifier("memory.detail.person")
                } else {
                    Text(memory.subject)
                        .appFont(.caption)
                        .foregroundColor(.secondary)
                }
            }
            if let model {
                reviewSection(model)
            }

            Section("Classification") {
                KeyValueRow(key: "Belief type", value: memoryDisplayText(memory.beliefType))
                KeyValueRow(key: "Status", value: memoryDisplayText(memory.status))
                KeyValueRow(key: "Polarity", value: memoryDisplayText(memory.polarity))
                KeyValueRow(key: "Portability", value: memoryDisplayText(memory.portability))
                KeyValueRow(key: "Authority", value: memoryDisplayText(memory.authority))
                KeyValueRow(
                    key: "Confidence",
                    value: String(format: "%.0f%%", memory.confidence * 100)
                )
                HStack(alignment: .firstTextBaseline) {
                    Text("Sensitivity").appFont(.caption).foregroundColor(.secondary)
                    Spacer()
                    MemorySensitivityBadge(
                        sensitivity: memory.sensitivity,
                        kind: memory.sensitivityKind
                    )
                }
            }

            Section("Provenance") {
                KeyValueRow(key: "Source session", value: memory.sourceSessionID.uuidString)
                if !memory.sourceEventIDs.isEmpty {
                    KeyValueRow(
                        key: "Source events",
                        value: memory.sourceEventIDs.map(String.init).joined(separator: ", ")
                    )
                }
                KeyValueRow(key: "Formation run", value: memory.formationRunID.uuidString)
                if !memory.consolidationPolicyVersion.isEmpty {
                    KeyValueRow(
                        key: "Consolidation policy version",
                        value: memory.consolidationPolicyVersion
                    )
                }
                if !memory.originScopes.isEmpty {
                    KeyValueRow(
                        key: "Origin scopes",
                        value: memory.originScopes.joined(separator: ", ")
                    )
                }
                if !memory.conflictsWith.isEmpty {
                    KeyValueRow(
                        key: "Conflicts with",
                        value: memory.conflictsWith.map(\.uuidString).joined(separator: ", ")
                    )
                }
                if let supersededBy = memory.supersededBy {
                    KeyValueRow(key: "Superseded by", value: supersededBy.uuidString)
                }
                if memory.flaggedForReview {
                    Label("Flagged for review", systemImage: "flag.fill")
                        .foregroundColor(AppTheme.orange)
                }
            }

            Section("Lifecycle") {
                KeyValueRow(key: "Claim kind", value: memory.claimKind)
                KeyValueRow(key: "Derivation", value: memory.derivation)
                KeyValueRow(key: "Longevity", value: memory.longevity)
                KeyValueRow(key: "Valid from", value: memory.validFrom.formatted())
                if let validTo = memory.validTo {
                    KeyValueRow(key: "Valid to", value: validTo.formatted())
                }
                if let expiresAt = memory.expiresAt {
                    KeyValueRow(key: "Expires", value: expiresAt.formatted())
                }
                KeyValueRow(key: "Last evidence", value: memory.lastEvidenceAt.formatted())
                if let lastUsedAt = memory.lastUsedAt {
                    KeyValueRow(key: "Last used", value: lastUsedAt.formatted())
                }
                KeyValueRow(
                    key: "Legacy reinforcement",
                    value: memory.lastReinforcedAt.formatted()
                )
                KeyValueRow(key: "Created", value: memory.createdAt.formatted())
                KeyValueRow(key: "Updated", value: memory.updatedAt.formatted())
            }
        }
        .navigationTitle("Memory")
        .accessibilityIdentifier("memory.detail")
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

    /// The review outcomes of ADR-0117, shown only when a browsing model can
    /// carry them out. Each button sends a fresh idempotency key and replaces
    /// this view's belief with the server's answer.
    @ViewBuilder
    private func reviewSection(_ model: MemoryViewModel) -> some View {
        Section("Review") {
            if memory.flaggedForReview {
                Text("Formation committed this memory without an explicit statement from you. Confirm it, correct it, or remove it.")
                    .appFont(.caption)
                    .foregroundColor(.secondary)
            }
            if let message = model.errorMessage {
                Text(message).appFont(.caption).foregroundColor(.secondary)
            }
            let busy = model.pendingActionID != nil || model.changesUnavailable
            if memory.flaggedForReview {
                Button("Mark reviewed") {
                    Task { if let reviewed = await model.review(memory, outcome: .dismiss) { memory = reviewed } }
                }
                .disabled(busy)
                .accessibilityIdentifier("memory.detail.review.dismiss")
            }
            if memory.status == "active" || memory.status == "provisional" {
                Button("Not true") {
                    Task { if let reviewed = await model.review(memory, outcome: .untrue) { memory = reviewed } }
                }
                .disabled(busy)
                .accessibilityIdentifier("memory.detail.review.untrue")
                if memory.portability != "local" {
                    Button("Not relevant here") {
                        Task { if let reviewed = await model.review(memory, outcome: .notHere) { memory = reviewed } }
                    }
                    .disabled(busy)
                    .accessibilityIdentifier("memory.detail.review.not_here")
                }
            }
            Button("Delete memory…", role: .destructive) { confirmingDeletion = true }
                .disabled(busy)
                .accessibilityIdentifier("memory.detail.delete")
        }
    }
}

private struct KeyValueRow: View {
    let key: String
    let value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(key).appFont(.caption).foregroundColor(.secondary)
            Spacer()
            Text(value)
                .appFont(.caption)
                .multilineTextAlignment(.trailing)
        }
    }
}
