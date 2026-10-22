import SwiftUI

#if os(macOS)
import AppKit
#endif

/// Edits the owner's persona document — the standing truths the agent reads
/// as trusted instruction text on every call (persona-surface.md) — and
/// reviews the nominations consolidation has raised for it. Drawn as an A2
/// page: the entries as hairline rows of fields, a server conflict and each
/// nomination in an amber-edged card, capsule buttons for the actions. One
/// section sidebar on wide screens and a section menu on compact screens.
public struct PersonaEditorView: View {
    @ObservedObject var model: PersonaViewModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.horizontalSizeClass) private var sizeClass
    private var usesColumns: Bool {
        #if os(macOS)
        true
        #else
        sizeClass == .regular
        #endif
    }

    public init(model: PersonaViewModel) {
        self.model = model
    }

    public var body: some View {
        NavigationStack {
            HStack(spacing: 0) {
                if usesColumns { sectionSidebar.frame(width: 232) }
                VStack(spacing: 0) {
                    if !usesColumns {
                        Picker("Persona section", selection: $model.selectedSection) {
                            ForEach(PersonaViewModel.Section.allCases) { Text($0.rawValue).tag($0) }
                        }.pickerStyle(.menu).padding(12).accessibilityIdentifier("persona.section-picker")
                    }
                    content
                }
            }
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
            minWidth: 820,
            idealWidth: 1000,
            maxWidth: .infinity,
            minHeight: 480,
            idealHeight: 640,
            maxHeight: .infinity
        )
        #else
        .presentationSizing(.page)
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
                    Text(model.selectedSection.rawValue).veetType(.display)
                    switch model.selectedSection {
                    case .voice:
                        if model.toneSupported {
                            voiceSection
                            traitSection
                            sampleSection
                        } else {
                            Text("Voice settings are not available on this server.").veetType(.ui)
                        }
                    case .assumptions: entries
                    case .nominations: nominations
                    case .boundaries: boundarySection
                    }
                    conflict
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

    private var sectionSidebar: some View {
        VStack(alignment: .leading, spacing: 4) {
            heading.padding(.bottom, 12)
            SidebarSectionLabel(title: "Sections")
            ForEach(PersonaViewModel.Section.allCases) { page in
                Button { model.selectedSection = page } label: {
                    SidebarRow { Image(systemName: page.symbol) } title: { Text(page.rawValue) }
                        .padding(.horizontal, 8)
                        .background(RoundedRectangle(cornerRadius: 7).fill(model.selectedSection == page ? AppTheme.selected : .clear))
                }
                .buttonStyle(.plain)
                .accessibilityAddTraits(model.selectedSection == page ? .isSelected : [])
                .accessibilityIdentifier("persona.section.\(page.rawValue)")
            }
            Text("Veetbot nominates new assumptions from your chats. Nothing joins the persona until you accept it.")
                .veetType(.meta).foregroundColor(AppTheme.inkSoft).padding(.top, 16)
            Spacer(minLength: 0)
        }.padding(16).background(AppTheme.paperDeep)
    }

    private var heading: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text("Persona").veetType(.display)
            Text("How Veetbot sounds, and what it may assume about you.")
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
        }
    }

    // MARK: Tone (ADR-0226)

    private var voiceSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            A2SectionHeading(title: "Voice")
            TextField(
                "Describe how you want Veetbot to talk to you.",
                text: $model.voice,
                axis: .vertical
            )
            .lineLimit(2...6)
            .textFieldStyle(.plain)
            .a2Field(radius: 12)
            .accessibilityIdentifier("persona.voice")
            Text("Veetbot reads this before every reply.")
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
        }
    }

    private var traitSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            A2SectionHeading(title: "Lean towards")
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 90, maximum: 120), alignment: .leading)], alignment: .leading, spacing: 8) {
                ForEach(PersonaViewModel.traitChoices, id: \.self) { trait in
                    let chosen = model.traits.contains(trait)
                    Button {
                        model.toggleTrait(trait)
                    } label: {
                        Text(trait.capitalized)
                            .veetType(.ui, weight: chosen ? .semibold : .regular)
                            .foregroundColor(chosen ? AppTheme.tealText : AppTheme.ink)
                            .padding(.horizontal, 12)
                            .padding(.vertical, 6)
                            .background(
                                Capsule().fill(chosen ? AppTheme.selected : AppTheme.surface)
                            )
                            .overlay(
                                Capsule().stroke(chosen ? AppTheme.tealText : AppTheme.hairlineStrong)
                            )
                    }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(chosen ? .isSelected : [])
                    .accessibilityIdentifier("persona.trait.\(trait)")
                }
            }
            Text("Up to three. Formal and Playful replace each other.")
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
        }
    }

    private var sampleSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            A2SectionHeading(title: "How that sounds")
            if let sample = model.sample {
                A2Quote(cite: sample.label) {
                    VStack(alignment: .leading, spacing: 6) {
                        Text("You: \(sample.prompt)")
                            .veetType(.meta)
                            .foregroundColor(AppTheme.inkSoft)
                        Text(sample.reply)
                            .veetType(.body)
                            .foregroundColor(AppTheme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityIdentifier("persona.sample.reply")
                    }
                }
            }
            Button(model.sample == nil ? "Hear it" : "Try again") {
                Task { await model.loadSample() }
            }
            .buttonStyle(SecondaryCapsuleButtonStyle())
            .disabled(model.isLoadingSample)
            .accessibilityIdentifier("persona.sample")
        }
    }

    /// The fixed rules from the server, each enforced by something real.
    @ViewBuilder
    private var boundarySection: some View {
        if !model.boundaries.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                A2SectionHeading(title: "Boundaries")
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(model.boundaries.enumerated()), id: \.element.id) { index, boundary in
                        if index > 0 { HairlineDivider().padding(.leading, 14) }
                        Label(boundary.text, systemImage: "lock")
                            .veetType(.ui)
                            .foregroundColor(AppTheme.ink)
                            .fixedSize(horizontal: false, vertical: true)
                            .padding(.horizontal, 14)
                            .padding(.vertical, 10)
                            .accessibilityIdentifier("persona.boundary.\(boundary.id)")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .a2Card()
                Text("Your voice and entries cannot change these.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
            }
        } else {
            Text("This server does not list any boundaries.")
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    /// The editable standing truths: one field per entry, parted by hairlines.
    private var entries: some View {
        VStack(alignment: .leading, spacing: 8) {
            A2SectionHeading(title: model.toneSupported ? "What Veetbot assumes · version \(model.version)" : "Version \(model.version)")
            VStack(alignment: .leading, spacing: 0) {
                ForEach(Array($model.drafts.enumerated()), id: \.element.id) { index, $draft in
                    if index > 0 { HairlineDivider().padding(.leading, 14) }
                    HStack(alignment: .center, spacing: 10) {
                        if draft.sourceBeliefID != nil {
                            Image(systemName: "checkmark.seal")
                                .foregroundColor(AppTheme.tealText)
                                .accessibilityLabel("Affirmed from memory")
                        }
                        VStack(alignment: .leading, spacing: 4) {
                            TextField("A standing truth", text: $draft.text)
                                .textFieldStyle(.plain)
                                .a2Field(radius: 10)
                                .opacity(draft.enabled ? 1 : 0.55)
                                .accessibilityIdentifier("persona.entry")
                            if draft.stale {
                                Text("The memory this came from has changed. Update or remove it.")
                                    .veetType(.meta)
                                    .foregroundColor(AppTheme.amberText)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        if model.toneSupported {
                            Toggle("Use", isOn: $draft.enabled)
                                .labelsHidden()
                                .toggleStyle(.switch)
                                .accessibilityLabel("Use this entry")
                                .accessibilityIdentifier("persona.entry.enabled")
                        }
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
                model.toneSupported
                    ? "Turn one off to stop Veetbot treating it as a standing instruction. "
                        + "Memory keeps what it learned; review it in Memory to change that. "
                        + "Entries marked with a seal were affirmed from memory."
                    : "Every entry is read as trusted instruction text on "
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
        } else {
            Text("Veetbot has no new assumptions to suggest right now.")
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
                .fixedSize(horizontal: false, vertical: true)
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
