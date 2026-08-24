import SwiftUI

/// One belief's full exposure-list projection, sectioned to mirror the
/// server's `MemoryView` fields (memory-read-api-and-browser.md). Rows for a
/// nil or empty field are omitted rather than shown blank.
public struct MemoryDetailView: View {
    let memory: MemoryView

    public init(memory: MemoryView) {
        self.memory = memory
    }

    public var body: some View {
        List {
            Section("Statement") {
                Text(memory.statement)
                Text(memory.subject)
                    .appFont(.caption)
                    .foregroundColor(.secondary)
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
                KeyValueRow(key: "Valid from", value: memory.validFrom.formatted())
                if let validTo = memory.validTo {
                    KeyValueRow(key: "Valid to", value: validTo.formatted())
                }
                if let expiresAt = memory.expiresAt {
                    KeyValueRow(key: "Expires", value: expiresAt.formatted())
                }
                KeyValueRow(key: "Last reinforced", value: memory.lastReinforcedAt.formatted())
                KeyValueRow(key: "Created", value: memory.createdAt.formatted())
                KeyValueRow(key: "Updated", value: memory.updatedAt.formatted())
            }
        }
        .navigationTitle("Memory")
        .accessibilityIdentifier("memory.detail")
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
