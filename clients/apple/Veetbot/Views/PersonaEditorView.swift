import SwiftUI

#if os(macOS)
import AppKit
#endif

/// Edits the owner's persona document — the standing truths the agent reads
/// as trusted instruction text on every call (persona-surface.md) — and
/// reviews the nominations consolidation has raised for it.
public struct PersonaEditorView: View {
    @ObservedObject var model: PersonaViewModel
    @Environment(\.dismiss) private var dismiss

    public init(model: PersonaViewModel) {
        self.model = model
    }

    public var body: some View {
        NavigationView {
            content
                .navigationTitle("Persona")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { dismiss() }
                    }
                    ToolbarItem(placement: .primaryAction) {
                        Button("Save") {
                            Task { await model.save() }
                        }
                        .disabled(
                            model.isSaving || model.unavailable || model.hasPendingMerge
                        )
                        .accessibilityIdentifier("persona.save")
                    }
                }
        }
        .accessibilityIdentifier("persona.editor")
        .task { await model.load() }
        #if os(macOS)
        .frame(
            minWidth: 520,
            idealWidth: 640,
            maxWidth: .infinity,
            minHeight: 480,
            idealHeight: 640,
            maxHeight: .infinity
        )
        #endif
        .alert(
            "The persona changed while you were editing",
            isPresented: Binding(
                get: { model.conflictDetected },
                set: { if !$0 { model.dismissConflictAlert() } }
            )
        ) {
            Button("Compare with server") {
                Task { await model.reloadAfterConflict() }
            }
            Button("Cancel", role: .cancel) {
                model.dismissConflictAlert()
            }
        } message: {
            Text(
                "Your entries are still local. Load the server version to "
                    + "compare both sides before saving again."
            )
        }
    }

    @ViewBuilder
    private var content: some View {
        if model.unavailable {
            VStack(spacing: 8) {
                Image(systemName: "person.crop.circle.badge.questionmark")
                    .font(.largeTitle)
                Text("The persona surface is not enabled on this server.")
                    .multilineTextAlignment(.center)
            }
            .padding()
        } else {
            List {
                Section {
                    ForEach($model.drafts) { $draft in
                        HStack(alignment: .top) {
                            if draft.sourceBeliefID != nil {
                                Image(systemName: "checkmark.seal")
                                    .foregroundColor(AppTheme.turquoise)
                                    .accessibilityLabel("Affirmed from memory")
                            }
                            TextField("A standing truth", text: $draft.text)
                                .accessibilityIdentifier("persona.entry")
                            Button {
                                model.removeDraft(draft.id)
                            } label: {
                                Image(systemName: "minus.circle")
                            }
                            .buttonStyle(.borderless)
                            .accessibilityLabel("Remove entry")
                        }
                    }
                    Button {
                        model.addDraft()
                    } label: {
                        Label("Add entry", systemImage: "plus.circle")
                    }
                    .accessibilityIdentifier("persona.add-entry")
                } header: {
                    Text("Version \(model.version)")
                } footer: {
                    Text(
                        "Every entry is read as trusted instruction text on "
                            + "every request. Entries marked with a seal were "
                            + "affirmed from the agent's memory."
                    )
                }

                if model.hasPendingMerge, let conflictHead = model.conflictHead {
                    Section {
                        ForEach(
                            Array(conflictHead.entries.enumerated()),
                            id: \.offset
                        ) { _, entry in
                            Text(entry.text)
                        }
                        Text(
                            "Compare these server entries with your editable "
                                + "drafts above. Save stays disabled until you "
                                + "choose how to resolve the merge."
                        )
                        .font(.caption)
                        .foregroundColor(.secondary)
                        Button("I've merged the server changes") {
                            model.resolveConflictKeepingDrafts()
                        }
                        .accessibilityIdentifier("persona.conflict.resolve")
                        Button("Use the server version") {
                            model.useConflictHead()
                        }
                        .accessibilityIdentifier("persona.conflict.use-server")
                    } header: {
                        Text("Server version \(conflictHead.version)")
                    }
                } else if model.hasPendingMerge {
                    Section("Merge required") {
                        Text(
                            "Load the current server version before saving "
                                + "these local drafts."
                        )
                        Button("Load server version") {
                            Task { await model.reloadAfterConflict() }
                        }
                    }
                }

                if !model.nominations.isEmpty {
                    Section("Nominated from memory") {
                        ForEach(model.nominations) { nomination in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(nomination.statement)
                                Text(
                                    "\(nomination.beliefType) · seen "
                                        + "\(nomination.corroborationCount) times"
                                )
                                .font(.caption)
                                .foregroundColor(.secondary)
                                HStack {
                                    Button("Affirm") {
                                        Task { await model.affirm(nomination.id) }
                                    }
                                    .buttonStyle(.borderedProminent)
                                    .accessibilityIdentifier("persona.nomination.affirm")
                                    Button("Decline") {
                                        Task { await model.decline(nomination.id) }
                                    }
                                    .buttonStyle(.bordered)
                                    .accessibilityIdentifier("persona.nomination.decline")
                                }
                            }
                            .padding(.vertical, 2)
                        }
                    }
                }

                if let message = model.errorMessage {
                    Section {
                        Text(message).foregroundColor(.red)
                    }
                }
            }
        }
    }
}
