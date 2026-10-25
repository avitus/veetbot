import SwiftUI

/// A widget's details (docs/plan/cockpit.md, per platform): its purpose, feeds
/// and their freshness, a settings form generated from its settings schema,
/// revision history with commit links, import failures, and Revert, Delete and
/// Restore. The Mac shows it in the right panel; the iPad and iPhone in a sheet.
struct CockpitWidgetDetailView: View {
    @ObservedObject var model: CockpitViewModel
    let slug: String
    let close: () -> Void
    @State private var values: [String: JSONValue] = [:]
    @State private var confirmingDelete = false

    private var widget: CockpitWidget? { model.snapshot?.widgets.first { $0.slug == slug } }
    private var detail: CockpitWidgetDetail? { model.detail?.slug == slug ? model.detail : nil }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: VeetSpace.s4) {
                header
                if let message = model.errorMessage {
                    Text(message).veetType(.meta).foregroundColor(AppTheme.red)
                        .accessibilityIdentifier("cockpit.detail.error")
                } else if let message = model.actionMessage {
                    Text(message).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                        .accessibilityIdentifier("cockpit.detail.message")
                }
                if model.quarantined.contains(slug) {
                    paused
                }
                if let widget, let snapshot = model.snapshot, !widget.declaredFeeds.isEmpty {
                    feeds(widget, snapshot: snapshot)
                }
                if let schema = detail?.settingsSchema, let fields = CockpitSettingsField.fields(in: schema), !fields.isEmpty {
                    settings(fields)
                }
                if let detail {
                    history(detail)
                    if !detail.importFailures.isEmpty { failures(detail) }
                    actions(detail)
                } else {
                    ProgressView().frame(maxWidth: .infinity)
                }
            }
            .padding(VeetSpace.s4)
        }
        .background(AppTheme.paper)
        .task(id: slug) {
            if model.detail?.slug != slug { await model.openDetail(slug) }
        }
        .onChange(of: detail?.settings) { _, settings in values = settings ?? [:] }
        .onAppear { values = detail?.settings ?? [:] }
        .accessibilityIdentifier("cockpit.detail")
    }

    private var header: some View {
        HStack(alignment: .top) {
            VStack(alignment: .leading, spacing: VeetSpace.s1) {
                Text(widget?.title ?? slug).veetType(.title)
                if let purpose = widget?.purpose {
                    Text(purpose).veetType(.ui).foregroundColor(AppTheme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer()
            Button(action: close) { Image(systemName: "xmark") }
                .buttonStyle(QuietIconButtonStyle())
                .accessibilityLabel("Close details")
                .accessibilityIdentifier("cockpit.detail.close")
        }
    }

    private var paused: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            Text("Paused: this widget stopped the board, so it is held out until its next revision.")
                .veetType(.meta).foregroundColor(AppTheme.amberText)
                .fixedSize(horizontal: false, vertical: true)
            Button("Try it again") { model.retry(slug) }
                .buttonStyle(SecondaryCapsuleButtonStyle())
                .accessibilityIdentifier("cockpit.detail.retry")
        }
    }

    private func feeds(_ widget: CockpitWidget, snapshot: CockpitSnapshot) -> some View {
        section("Feeds") {
            ForEach(widget.declaredFeeds, id: \.id) { declared in
                let current = snapshot.feeds[declared.id]?.current
                HStack(spacing: VeetSpace.s2) {
                    CockpitFreshnessDot(level: current?.freshness(at: Date()) ?? .pending)
                    Text(declared.id).veetType(.meta).foregroundColor(AppTheme.ink)
                    Spacer()
                    Text(current.map { CockpitDevice.age(from: $0.observedAt, to: Date()) } ?? "Waiting")
                        .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                }
            }
        }
    }

    private func settings(_ fields: [CockpitSettingsField]) -> some View {
        section("Settings") {
            ForEach(fields) { field in
                CockpitSettingsFieldView(field: field, value: binding(field.key))
            }
            Button("Save settings") {
                Task { await model.saveSettings(slug, values: values) }
            }
            .buttonStyle(PrimaryCapsuleButtonStyle())
            .disabled(model.isSaving || values == (detail?.settings ?? [:]))
            .accessibilityIdentifier("cockpit.detail.save")
        }
    }

    private func history(_ detail: CockpitWidgetDetail) -> some View {
        section("History") {
            ForEach(detail.revisions) { revision in
                HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(revision.state.replacingOccurrences(of: "_", with: " ").capitalized)
                            .veetType(.ui).foregroundColor(AppTheme.ink)
                        Text("\(revision.source.replacingOccurrences(of: "_", with: " ")) · \(revision.createdAt.formatted(date: .abbreviated, time: .shortened))")
                            .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                        if let first = revision.diagnostics.first {
                            Text(first.message).veetType(.meta).foregroundColor(AppTheme.red).lineLimit(3)
                        }
                    }
                    Spacer()
                    if let url = revision.commitURL {
                        Link("Commit", destination: url).veetType(.meta)
                    }
                    if revision.id != detail.liveRevisionID, revision.commitSHA != nil, detail.state == "active" {
                        Button("Revert") { Task { await model.revert(slug, to: revision.id) } }
                            .buttonStyle(QuietCapsuleButtonStyle())
                            .disabled(model.isSaving)
                    }
                }
            }
        }
    }

    private func failures(_ detail: CockpitWidgetDetail) -> some View {
        section("Import failures") {
            ForEach(detail.importFailures) { failure in
                VStack(alignment: .leading, spacing: 2) {
                    Text(failure.diagnostics.first?.message ?? "This commit could not be imported.")
                        .veetType(.meta).foregroundColor(AppTheme.red)
                    if let url = failure.commitURL {
                        Link(String(failure.commitSHA.prefix(7)), destination: url).veetType(.meta)
                    }
                }
            }
        }
    }

    private func actions(_ detail: CockpitWidgetDetail) -> some View {
        HStack {
            if detail.state == "deleted" {
                Button("Restore") { Task { await model.restore(slug) } }
                    .buttonStyle(SecondaryCapsuleButtonStyle())
                    .accessibilityIdentifier("cockpit.detail.restore")
            } else {
                Button("Delete widget", role: .destructive) { confirmingDelete = true }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .accessibilityIdentifier("cockpit.detail.delete")
            }
        }
        .disabled(model.isSaving)
        .confirmationDialog("Delete \(widget?.title ?? slug)?", isPresented: $confirmingDelete) {
            Button("Delete", role: .destructive) { Task { await model.delete(slug) } }
        } message: {
            Text("Its directory is removed from the widget branch. You can restore it later.")
        }
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            Text(title).veetType(.label).foregroundColor(AppTheme.inkFaint)
            content()
        }
    }

    private func binding(_ key: String) -> Binding<JSONValue?> {
        Binding(get: { values[key] }, set: { values[key] = $0 })
    }
}

/// One editable property of a widget's settings schema.
struct CockpitSettingsField: Identifiable, Equatable {
    enum Kind: Equatable { case text, number, integer, toggle, choice([String]) }

    let key: String
    let title: String
    let kind: Kind
    var id: String { key }

    /// The schema's object properties the form can edit, in key order.
    static func fields(in schema: [String: JSONValue]) -> [CockpitSettingsField]? {
        guard case let .object(properties)? = schema["properties"] else { return nil }
        return properties.keys.sorted().compactMap { key in
            guard case let .object(property)? = properties[key] else { return nil }
            var title = key.replacingOccurrences(of: "_", with: " ").capitalized
            if case let .string(label)? = property["title"], !label.isEmpty { title = label }
            if case let .array(options)? = property["enum"] {
                let choices = options.compactMap { if case let .string(choice) = $0 { choice } else { nil } }
                return choices.isEmpty ? nil : CockpitSettingsField(key: key, title: title, kind: .choice(choices))
            }
            switch property["type"] {
            case .string("string")?: return CockpitSettingsField(key: key, title: title, kind: .text)
            case .string("number")?: return CockpitSettingsField(key: key, title: title, kind: .number)
            case .string("integer")?: return CockpitSettingsField(key: key, title: title, kind: .integer)
            case .string("boolean")?: return CockpitSettingsField(key: key, title: title, kind: .toggle)
            default: return nil
            }
        }
    }

    /// A number setting as its field shows it: whole numbers without a fraction,
    /// and a value beyond exact integers as the decimal it is.
    static func format(_ number: Double, integer: Bool) -> String {
        guard number.isFinite, abs(number) < 9.0e15, integer || number.rounded() == number else {
            return String(number)
        }
        return String(Int(number))
    }
}

private struct CockpitSettingsFieldView: View {
    let field: CockpitSettingsField
    @Binding var value: JSONValue?

    var body: some View {
        switch field.kind {
        case .text:
            TextField(field.title, text: Binding(
                get: { if case let .string(text)? = value { text } else { "" } },
                set: { value = $0.isEmpty ? nil : .string($0) }
            ))
            .textFieldStyle(.plain)
            .padding(VeetSpace.s2)
            .a2Field()
            .accessibilityIdentifier("cockpit.setting.\(field.key)")
        case .number, .integer:
            TextField(field.title, text: Binding(
                get: { if case let .number(number)? = value { CockpitSettingsField.format(number, integer: field.kind == .integer) } else { "" } },
                set: { text in
                    if let number = Double(text) { value = .number(field.kind == .integer ? number.rounded() : number) }
                    else if text.isEmpty { value = nil }
                }
            ))
            .textFieldStyle(.plain)
            .padding(VeetSpace.s2)
            .a2Field()
            .accessibilityIdentifier("cockpit.setting.\(field.key)")
        case .toggle:
            Toggle(field.title, isOn: Binding(
                get: { if case let .bool(on)? = value { on } else { false } },
                set: { value = .bool($0) }
            ))
            .accessibilityIdentifier("cockpit.setting.\(field.key)")
        case .choice(let choices):
            Picker(field.title, selection: Binding(
                get: { if case let .string(choice)? = value { choice } else { "" } },
                set: { value = $0.isEmpty ? nil : .string($0) }
            )) {
                Text("None").tag("")
                ForEach(choices, id: \.self) { Text($0).tag($0) }
            }
            .accessibilityIdentifier("cockpit.setting.\(field.key)")
        }
    }
}

/// Arrange one device class's board: order, size and which widgets it shows.
struct CockpitArrangeView: View {
    @ObservedObject var model: CockpitViewModel
    let deviceClass: CockpitDeviceClass
    @Environment(\.dismiss) private var dismiss
    @State private var rows: [Row] = []

    struct Row: Identifiable, Equatable {
        let slug: String
        let title: String
        let sizes: [String]
        var size: String
        var shown: Bool
        var id: String { slug }
    }

    var body: some View {
        NavigationStack {
            List {
                ForEach($rows) { $row in
                    HStack {
                        Toggle(isOn: $row.shown) { Text(row.title).veetType(.ui) }
                        Picker("Size", selection: $row.size) {
                            ForEach(row.sizes, id: \.self) { Text($0.uppercased()).tag($0) }
                        }
                        .labelsHidden()
                        .frame(maxWidth: 120)
                        .disabled(!row.shown)
                    }
                    .accessibilityIdentifier("cockpit.arrange.\(row.slug)")
                }
                .onMove { rows.move(fromOffsets: $0, toOffset: $1) }
            }
            .navigationTitle("Arrange board")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        let placements = rows.filter(\.shown).map { CockpitPlacement(slug: $0.slug, size: $0.size) }
                        Task {
                            await model.saveLayout(deviceClass, placements: placements)
                            dismiss()
                        }
                    }
                    .accessibilityIdentifier("cockpit.arrange.save")
                }
                #if os(iOS)
                ToolbarItem(placement: .primaryAction) { EditButton() }
                #endif
            }
        }
        #if os(macOS)
        .frame(minWidth: 420, minHeight: 360)
        #endif
        .onAppear { rows = Self.rows(model.snapshot, deviceClass: deviceClass) }
    }

    /// The board's placements first, then the live widgets it does not show.
    static func rows(_ snapshot: CockpitSnapshot?, deviceClass: CockpitDeviceClass) -> [Row] {
        guard let snapshot else { return [] }
        let placed = CockpitBoard.placements(snapshot, deviceClass: deviceClass)
        let shown = Set(placed.map(\.widget.slug))
        let first = placed.map {
            Row(slug: $0.widget.slug, title: $0.widget.title, sizes: $0.widget.sizes, size: $0.size, shown: true)
        }
        let rest = snapshot.widgets.filter { !shown.contains($0.slug) }.map {
            Row(slug: $0.slug, title: $0.title, sizes: $0.sizes, size: $0.sizes.first ?? "md", shown: false)
        }
        return first + rest
    }
}
