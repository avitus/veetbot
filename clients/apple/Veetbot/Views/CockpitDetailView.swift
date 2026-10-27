import SwiftUI

/// A widget's details (docs/plan/cockpit.md, per platform; MagicPath
/// merrily-city-4157): its purpose; Feed, with its last value, expected
/// interval and when it goes stale; Private settings; History with the live
/// revision and Revert; import failures; then Discuss in Chat, Remove from
/// this screen, and Delete or Restore. The Mac shows it in the right panel;
/// the iPad and iPhone in a sheet.
struct CockpitWidgetDetailView: View {
    @ObservedObject var model: CockpitViewModel
    let slug: String
    let close: () -> Void
    /// Discuss in Chat: a new conversation about this widget, staged, not sent.
    var discuss: (String) -> Void = { _ in }
    @State private var values: [String: JSONValue] = [:]
    @State private var confirmingDelete = false

    private var widget: CockpitWidget? { model.snapshot?.widgets.first { $0.slug == slug } }
    private var detail: CockpitWidgetDetail? { model.detail?.slug == slug ? model.detail : nil }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                header
                if let message = model.errorMessage {
                    Text(message).veetType(.meta).foregroundColor(AppTheme.red)
                        .padding(.top, VeetSpace.s3)
                        .accessibilityIdentifier("cockpit.detail.error")
                } else if let message = model.actionMessage {
                    Text(message).veetType(.meta).foregroundColor(AppTheme.inkSoft)
                        .padding(.top, VeetSpace.s3)
                        .accessibilityIdentifier("cockpit.detail.message")
                }
                if model.quarantined.contains(slug) {
                    paused.padding(.top, VeetSpace.s3)
                }
                if let widget, let snapshot = model.snapshot, !widget.declaredFeeds.isEmpty {
                    feed(widget, snapshot: snapshot)
                }
                privateSettings
                if let detail {
                    history(detail)
                    if !detail.importFailures.isEmpty { failures(detail) }
                    actions(detail)
                } else {
                    ProgressView().frame(maxWidth: .infinity).padding(.top, VeetSpace.s5)
                }
            }
            .padding(VeetSpace.s4)
        }
        .background(AppTheme.paperDeep)
        .task(id: slug) {
            if model.detail?.slug != slug { await model.openDetail(slug) }
        }
        .onChange(of: detail?.settings) { _, settings in values = settings ?? [:] }
        .onAppear { values = detail?.settings ?? [:] }
        .accessibilityIdentifier("cockpit.detail")
    }

    /// The design's head: the widget's name in the title role and a close
    /// button, then its purpose.
    private var header: some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            HStack(alignment: .firstTextBaseline, spacing: VeetSpace.s2) {
                Text(widget?.title ?? slug).veetType(.title)
                Spacer(minLength: 0)
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(QuietIconButtonStyle())
                    .accessibilityLabel("Close details")
                    .accessibilityIdentifier("cockpit.detail.close")
            }
            if let purpose = widget?.purpose {
                Text(purpose).veetType(.ui).foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
            }
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

    /// Each declared feed by name and source, then the stalest one's facts:
    /// its last value, how often it is expected, and when it goes stale.
    private func feed(_ widget: CockpitWidget, snapshot: CockpitSnapshot) -> some View {
        let now = Date()
        let entries = widget.declaredFeeds.map { (declared: $0, feed: snapshot.feeds[$0.id]) }
        let level = CockpitDevice.freshness(of: widget, in: snapshot, now: now)
        let interval = entries.compactMap { $0.feed?.current?.expectedIntervalSeconds }.max()
        return section("Feed") {
            ForEach(entries, id: \.declared.id) { entry in
                HStack(alignment: .top, spacing: VeetSpace.s2) {
                    CockpitFreshnessDot(level: entry.feed?.current?.freshness(at: now) ?? .pending)
                        .padding(.top, 5)
                    VStack(alignment: .leading, spacing: 0) {
                        Text(entry.declared.id).veetMono().foregroundColor(AppTheme.ink)
                        Text(CockpitDetailText.source(trust: entry.feed?.trust ?? "platform"))
                            .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                    }
                }
            }
            facts([
                ("Last value", CockpitDetailText.lastValue(level, age: CockpitDevice.age(of: widget, in: snapshot, now: now)),
                 level == .error ? AppTheme.red : level == .stale ? AppTheme.amberText : AppTheme.ink),
            ] + (interval.map { [
                ("Expected", CockpitDetailText.expected($0), AppTheme.ink),
                ("Stale after", CockpitDetailText.staleAfter($0), AppTheme.ink),
            ] } ?? []))
        }
    }

    /// The widget's own settings, editable where its schema allows; they stay
    /// on the owner's server.
    @ViewBuilder
    private var privateSettings: some View {
        let fields = detail?.settingsSchema.flatMap(CockpitSettingsField.fields(in:)) ?? []
        let stored = (detail?.settings ?? [:]).sorted { $0.key < $1.key }
        if !fields.isEmpty || !stored.isEmpty {
            section("Private settings", symbol: "lock") {
                if fields.isEmpty {
                    facts(stored.map { ($0.key, CockpitDetailText.display($0.value), AppTheme.ink) })
                } else {
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
                Text("Kept on your server, never in the public repo.")
                    .veetType(.meta).foregroundColor(AppTheme.inkFaint)
            }
        }
    }

    /// Each revision: what it is, who made it and when, its commit, and
    /// Live beside the one the board shows, Revert beside the others.
    private func history(_ detail: CockpitWidgetDetail) -> some View {
        section("History") {
            ForEach(detail.revisions) { revision in
                HStack(alignment: .top, spacing: VeetSpace.s2) {
                    Image(systemName: "clock.arrow.circlepath")
                        .veetIcon(.small)
                        .foregroundColor(AppTheme.inkFaint)
                        .padding(.top, 2)
                        .accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(CockpitDetailText.revisionTitle(revision.state))
                            .veetType(.ui).foregroundColor(AppTheme.ink)
                        HStack(spacing: 0) {
                            Text("\(CockpitDetailText.revisionSource(revision.source)) · \(revision.createdAt.formatted(.relative(presentation: .named)))")
                            if let sha = revision.commitSHA {
                                Text(" · ")
                                if let url = revision.commitURL {
                                    Link(destination: url) { Text(CockpitDetailText.shortSHA(sha)).veetMono() }
                                } else {
                                    Text(CockpitDetailText.shortSHA(sha)).veetMono()
                                }
                            }
                        }
                        .veetType(.meta).foregroundColor(AppTheme.inkFaint)
                        if let first = revision.diagnostics.first {
                            Text(first.message).veetType(.meta).foregroundColor(AppTheme.red).lineLimit(3)
                        }
                    }
                    Spacer(minLength: 0)
                    if revision.id == detail.liveRevisionID {
                        Text("Live").veetType(.meta).foregroundColor(AppTheme.inkFaint)
                    } else if revision.commitSHA != nil, detail.state == "active" {
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
                        Link(destination: url) { Text(CockpitDetailText.shortSHA(failure.commitSHA)).veetMono() }
                    }
                }
            }
        }
    }

    /// The design's two actions, then deleting the widget everywhere (or
    /// restoring a deleted one), which the design leaves to Chat.
    private func actions(_ detail: CockpitWidgetDetail) -> some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            Button {
                discuss(widget?.title ?? slug)
            } label: {
                Label("Discuss in Chat", systemImage: "bubble.left.and.bubble.right")
            }
            .buttonStyle(SecondaryCapsuleButtonStyle())
            .accessibilityIdentifier("cockpit.detail.discuss")
            if detail.state == "deleted" {
                Button("Restore") { Task { await model.restore(slug) } }
                    .buttonStyle(SecondaryCapsuleButtonStyle())
                    .accessibilityIdentifier("cockpit.detail.restore")
            } else {
                Button {
                    Task { await model.removeFromScreen(slug, deviceClass: CockpitDevice.deviceClass) }
                } label: {
                    // The design's quiet danger action.
                    Text("Remove from this screen").foregroundColor(AppTheme.red)
                }
                .buttonStyle(QuietCapsuleButtonStyle())
                .accessibilityIdentifier("cockpit.detail.remove")
                Button("Delete widget…", role: .destructive) { confirmingDelete = true }
                    .buttonStyle(.plain)
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkFaint)
                    .accessibilityIdentifier("cockpit.detail.delete")
            }
        }
        .padding(.top, VeetSpace.s5)
        .disabled(model.isSaving)
        .confirmationDialog("Delete \(widget?.title ?? slug)?", isPresented: $confirmingDelete) {
            Button("Delete", role: .destructive) { Task { await model.delete(slug) } }
        } message: {
            Text("It leaves every screen, and its directory is removed from the widget branch. You can restore it later.")
        }
    }

    /// The design's definition list: a faint term beside its value.
    private func facts(_ rows: [(String, String, Color)]) -> some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: VeetSpace.s3, verticalSpacing: VeetSpace.s1) {
            ForEach(rows, id: \.0) { row in
                GridRow {
                    Text(row.0).foregroundColor(AppTheme.inkFaint)
                    Text(row.1).foregroundColor(row.2).fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .veetType(.meta)
        .padding(.top, VeetSpace.s1)
    }

    /// A section of the panel: its label 20 below the section before it.
    private func section<Content: View>(
        _ title: String, symbol: String? = nil, @ViewBuilder content: () -> Content
    ) -> some View {
        VStack(alignment: .leading, spacing: VeetSpace.s2) {
            HStack(spacing: 6) {
                Text(title).veetType(.label)
                if let symbol { Image(systemName: symbol).veetIcon(.small).accessibilityHidden(true) }
            }
            .foregroundColor(AppTheme.inkFaint)
            content()
        }
        .padding(.top, 20)
    }

    private func binding(_ key: String) -> Binding<JSONValue?> {
        Binding(get: { values[key] }, set: { values[key] = $0 })
    }
}

/// The details panel's words, as the design writes them.
enum CockpitDetailText {
    /// The stalest feed's last value, in the design's long freshness.
    static func lastValue(_ level: CockpitFreshness, age: TimeInterval?) -> String {
        let text = age.map(CockpitDevice.shortAge) ?? ""
        switch level {
        case .pending: return "Waiting for the first update"
        case .error: return "Feed failed · last good \(text) ago"
        case .stale: return "Stale · last update \(text) ago"
        case .aging, .live: return text == "now" ? "Updated just now" : "Updated \(text) ago"
        }
    }

    static func interval(_ seconds: Int) -> String {
        if seconds < 60 { return "\(seconds) s" }
        if seconds >= 3600, seconds % 3600 == 0 { return "\(seconds / 3600) h" }
        return "\(seconds / 60) min"
    }

    static func expected(_ seconds: Int) -> String { "Every \(interval(seconds))" }

    /// A feed is stale after three intervals without an update.
    static func staleAfter(_ seconds: Int) -> String { "\(interval(seconds * 3)) without an update" }

    static func source(trust: String) -> String {
        trust == "platform" ? "Veetbot" : "Written by the assistant"
    }

    static func revisionTitle(_ state: String) -> String {
        let words = state.replacingOccurrences(of: "_", with: " ")
        return words.prefix(1).uppercased() + words.dropFirst()
    }

    static func revisionSource(_ source: String) -> String {
        switch source {
        case "assistant": return "Veetbot"
        case "owner_import": return "You"
        default: return source.replacingOccurrences(of: "_", with: " ")
        }
    }

    static func shortSHA(_ sha: String) -> String { String(sha.prefix(7)) }

    /// A stored setting the schema gives no field for.
    static func display(_ value: JSONValue) -> String {
        switch value {
        case .string(let text): return text
        case .number(let number): return CockpitSettingsField.format(number, integer: false)
        case .bool(let on): return on ? "On" : "Off"
        case .null: return "None"
        default: return "Set"
        }
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
            ), prompt: .a2Prompt(field.title))
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
            ), prompt: .a2Prompt(field.title))
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
