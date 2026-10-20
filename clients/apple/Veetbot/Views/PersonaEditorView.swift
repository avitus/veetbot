import SwiftUI

#if os(macOS)
import AppKit
#endif

/// Edits the owner's persona document — the standing truths the agent reads
/// as trusted instruction text on every call (persona-surface.md) — and
/// reviews the nominations consolidation has raised for it. Drawn as an A2
/// page: the entries as hairline rows of fields, a server conflict and each
/// nomination in an amber-edged card, capsule buttons for the actions. One
/// full-width column: a `NavigationView` with a single child draws it on the
/// Mac as a narrow sidebar beside an empty pane.
public struct PersonaEditorView: View {
    @ObservedObject var model: PersonaViewModel
    @Environment(\.dismiss) private var dismiss

    public init(model: PersonaViewModel) {
        self.model = model
    }

    public var body: some View {
        NavigationStack {
            content
                .navigationTitle("Persona")
                #if os(iOS)
                .navigationBarTitleDisplayMode(.inline)
                #endif
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
        .tint(AppTheme.tealFill)
        .accessibilityIdentifier("persona.editor")
        .toastHost()
        .task { await model.load() }
        #if os(macOS)
        .sheetFrame(
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
            A2EmptyState(
                systemImage: "person.crop.circle.badge.questionmark",
                title: "The persona surface is not enabled on this server."
            )
            .background(AppTheme.paper)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 20) {
                    heading
                    entries
                    conflict
                    nominations
                    if let message = model.errorMessage {
                        Text(message)
                            .veetType(.ui)
                            .foregroundColor(AppTheme.red)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                .frame(maxWidth: 680, alignment: .leading)
                .padding(.horizontal, 24)
                .padding(.vertical, 20)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .background(AppTheme.paper)
        }
    }

    private var heading: some View {
        VStack(alignment: .leading, spacing: 4) {
            #if os(macOS)
            Text("Persona").veetType(.display)
            #endif
            Text("How Veetbot sounds, and what it may assume about you.")
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
        }
    }

    /// The editable standing truths: one field per entry, parted by hairlines.
    private var entries: some View {
        VStack(alignment: .leading, spacing: 8) {
            A2SectionHeading(title: "Version \(model.version)")
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array($model.drafts.enumerated()), id: \.element.id) { index, $draft in
                    if index > 0 { HairlineDivider().padding(.leading, 14) }
                    HStack(alignment: .center, spacing: 10) {
                        if draft.sourceBeliefID != nil {
                            Image(systemName: "checkmark.seal")
                                .foregroundColor(AppTheme.tealText)
                                .accessibilityLabel("Affirmed from memory")
                        }
                        TextField("A standing truth", text: $draft.text)
                            .textFieldStyle(.plain)
                            .a2Field(radius: 10)
                            .accessibilityIdentifier("persona.entry")
                        Button {
                            model.removeDraft(draft.id)
                        } label: {
                            Image(systemName: "minus.circle")
                        }
                        .buttonStyle(QuietIconButtonStyle())
                        .accessibilityLabel("Remove entry")
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                }
                if !model.drafts.isEmpty { HairlineDivider().padding(.leading, 14) }
                Button {
                    model.addDraft()
                } label: {
                    Label("Add entry", systemImage: "plus.circle")
                }
                .buttonStyle(SecondaryCapsuleButtonStyle())
                .accessibilityIdentifier("persona.add-entry")
                .padding(14)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .a2Card()
            Text(
                "Every entry is read as trusted instruction text on "
                    + "every request. Entries marked with a seal were "
                    + "affirmed from the agent's memory."
            )
            .veetType(.meta)
            .foregroundColor(AppTheme.inkSoft)
            .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// A server version that arrived while the owner was editing.
    @ViewBuilder
    private var conflict: some View {
        if model.hasPendingMerge, let conflictHead = model.conflictHead {
            VStack(alignment: .leading, spacing: 10) {
                A2SectionHeading(title: "Server version \(conflictHead.version)")
                ForEach(
                    Array(conflictHead.entries.enumerated()),
                    id: \.offset
                ) { _, entry in
                    Text(entry.text)
                        .veetType(.ui)
                        .foregroundColor(AppTheme.ink)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Text(
                    "Compare these server entries with your editable "
                        + "drafts above. Save stays disabled until you "
                        + "choose how to resolve the merge."
                )
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("I've merged the server changes") {
                        model.resolveConflictKeepingDrafts()
                    }
                    .primaryCapsule()
                    .accessibilityIdentifier("persona.conflict.resolve")
                    Button("Use the server version") {
                        model.useConflictHead()
                    }
                    .buttonStyle(SecondaryCapsuleButtonStyle())
                    .accessibilityIdentifier("persona.conflict.use-server")
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .a2Card(border: AppTheme.amberLine)
        } else if model.hasPendingMerge {
            VStack(alignment: .leading, spacing: 10) {
                A2SectionHeading(title: "Merge required")
                Text(
                    "Load the current server version before saving "
                        + "these local drafts."
                )
                .veetType(.ui)
                .foregroundColor(AppTheme.ink)
                .fixedSize(horizontal: false, vertical: true)
                Button("Load server version") {
                    Task { await model.reloadAfterConflict() }
                }
                .buttonStyle(SecondaryCapsuleButtonStyle())
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
            .a2Card(border: AppTheme.amberLine)
        }
    }

    @ViewBuilder
    private var nominations: some View {
        if !model.nominations.isEmpty {
            VStack(alignment: .leading, spacing: 10) {
                A2SectionHeading(title: "Nominated from memory")
                ForEach(model.nominations) { nomination in
                    PersonaNominationCard(nomination: nomination, model: model)
                }
            }
        }
    }
}

/// One nomination from memory, waiting on the owner: what Veetbot noticed,
/// how often, and the two answers.
private struct PersonaNominationCard: View {
    let nomination: PersonaNominationView
    @ObservedObject var model: PersonaViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Veetbot suggests", systemImage: "sparkles")
                .veetType(.meta, weight: .semibold)
                .foregroundColor(AppTheme.amberText)
            Text(nomination.statement)
                .veetType(.ui)
                .foregroundColor(AppTheme.ink)
                .fixedSize(horizontal: false, vertical: true)
            Text(
                "\(nomination.beliefType) · seen "
                    + "\(nomination.corroborationCount) times"
            )
            .veetType(.meta)
            .foregroundColor(AppTheme.inkSoft)
            HStack(spacing: 8) {
                Button("Affirm") {
                    Task { await model.affirm(nomination.id) }
                }
                .primaryCapsule()
                .accessibilityIdentifier("persona.nomination.affirm")
                Button("Decline") {
                    Task { await model.decline(nomination.id) }
                }
                .buttonStyle(QuietCapsuleButtonStyle())
                .accessibilityIdentifier("persona.nomination.decline")
            }
            .padding(.top, 2)
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .a2Card(border: AppTheme.amberLine)
    }
}
