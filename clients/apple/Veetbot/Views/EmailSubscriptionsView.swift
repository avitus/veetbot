import SwiftUI

/// The Subscriptions census. A list-to-detail flow on iPhone, and a list beside
/// its detail on Mac and regular-width iPad, as the inbox itself is laid out.
struct EmailSubscriptionsScreen: View {
    @ObservedObject var model: EmailSubscriptionsViewModel
    let accounts: [EmailAccountView]
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationView {
            census
            EmailEmptyState(
                symbol: "tray.full", title: "Choose a sender",
                message: "Its conversations, what it offers, and what you can still do appear here."
            )
            .frame(maxWidth: 400).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(EmailSurface.canvas)
        }
        .tint(EmailSurface.accent)
        .accessibilityIdentifier("email.subscriptions")
        .sheet(isPresented: Binding(
            get: { model.confirmation?.source == .list },
            set: { if !$0 { model.cancelConfirmation() } })
        ) {
            EmailUnsubscribeConfirmationSheet(model: model)
        }
        .sheetFrame(minWidth: 320, macMinWidth: 700, idealWidth: 820, minHeight: 420, idealHeight: 620)
    }

    private var census: some View {
        VStack(spacing: 0) {
            filters.fixedSize(horizontal: false, vertical: true)
            Divider()
            List {
                if model.unavailable {
                    EmailEmptyState(
                        symbol: "tray", title: "Subscriptions aren’t available",
                        message: "This server does not offer unsubscribe assistance yet."
                    )
                } else if let error = model.errorMessage, model.items.isEmpty {
                    VStack(alignment: .leading, spacing: 12) {
                        Label("Senders couldn’t be loaded", systemImage: "exclamationmark.circle")
                            .appFont(.headline)
                        Text(error).appFont(.callout).foregroundColor(.secondary)
                        Button("Try again") { Task { await model.reload() } }.buttonStyle(.bordered)
                    }.padding(.vertical, 16)
                } else if model.isLoading && model.items.isEmpty {
                    ProgressView("Finding bulk senders…").padding(.vertical, 32).frame(maxWidth: .infinity)
                } else if model.items.isEmpty {
                    EmailEmptyState(
                        symbol: "tray", title: "No bulk senders yet",
                        message: "Senders appear here as Veetbot reads your recent mail."
                    )
                }
                ForEach(model.items) { row in
                    HStack(spacing: 10) {
                        if model.isSelecting {
                            Button {
                                model.toggleSelection(row)
                            } label: {
                                Image(systemName: model.isSelected(row.id) ? "checkmark.circle.fill" : "circle")
                                    .frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
                            }
                            .buttonStyle(.plain).foregroundColor(EmailSurface.accent)
                            .disabled(!model.canSelect(row))
                            .accessibilityLabel("Select \(row.displayName)")
                            .accessibilityValue(model.isSelected(row.id) ? "Selected" : "Not selected")
                            .accessibilityIdentifier("email.subscription.select.\(row.id)")
                        }
                        NavigationLink {
                            EmailSubscriptionDetailView(model: model, row: row)
                        } label: {
                            EmailSubscriptionRowLabel(model: model, row: row)
                        }
                        .accessibilityIdentifier("email.subscription.row.\(row.id)")
                    }
                    .listRowInsets(EdgeInsets(top: 6, leading: 12, bottom: 6, trailing: 12))
                }
                if model.hasMore {
                    Button("More senders") { Task { await model.loadMore() } }
                        .disabled(model.isLoading).padding(.vertical, 12)
                }
            }
            .listStyle(.plain)
        }
        .background(EmailSurface.canvas)
        .navigationTitle("Subscriptions")
        .task(id: model.filterKey) { await model.reload() }
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
            ToolbarItemGroup(placement: .primaryAction) {
                if model.isSelecting {
                    Button("Select all") { model.selectAll() }
                        .accessibilityIdentifier("email.subscriptions.select-all")
                    Button("Unsubscribe \(model.selection.count)") { model.beginUnsubscribe() }
                        .disabled(model.selection.isEmpty)
                        .accessibilityIdentifier("email.subscriptions.unsubscribe-selected")
                }
                Button(model.isSelecting ? "Done" : "Select") { model.setSelecting(!model.isSelecting) }
                    .accessibilityIdentifier("email.subscriptions.select")
            }
        }
    }

    /// Account and state are the two filters the route accepts; nothing is filtered locally.
    private var filters: some View {
        HStack(spacing: 8) {
            Picker("Accounts", selection: Binding(
                get: { model.accountFilter }, set: { model.setAccountFilter($0) })
            ) {
                Text("All accounts").tag(String?.none)
                ForEach(accounts.filter { $0.unsubscribeSupported == true }) { account in
                    Text(account.label).tag(Optional(account.id))
                }
            }
            .pickerStyle(.menu).labelsHidden().accessibilityLabel("Accounts")
            .accessibilityIdentifier("email.subscriptions.accounts")
            Picker("Sender state", selection: Binding(
                get: { model.stateFilter }, set: { model.setStateFilter($0) })
            ) {
                Text("All senders").tag(String?.none)
                Text("Active").tag(Optional("active"))
                Text("Kept").tag(Optional("kept"))
                Text("Unsubscribed").tag(Optional("unsubscribed"))
                Text("Unsubscribe failed").tag(Optional("failed"))
                Text("Still sending").tag(Optional("still_sending"))
                Text("Reported as spam").tag(Optional("reported_spam"))
            }
            .pickerStyle(.menu).labelsHidden().accessibilityLabel("Sender state")
            .accessibilityIdentifier("email.subscriptions.state")
            Spacer(minLength: 4)
            Text("\(model.items.count) \(model.items.count == 1 ? "sender" : "senders")")
                .appFont(.caption).foregroundColor(.secondary).fixedSize()
        }.padding(12)
    }
}

/// One census row: who, how much, what it offers, and where it stands.
private struct EmailSubscriptionRowLabel: View {
    @ObservedObject var model: EmailSubscriptionsViewModel
    let row: EmailSubscriptionView

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(row.displayName.isEmpty ? row.address : row.displayName)
                    .appFont(.callout, weight: .semibold).lineLimit(1)
                Spacer(minLength: 4)
                Text(row.lastReceivedAt, format: .dateTime.month(.abbreviated).day())
                    .appFont(.caption).foregroundColor(.secondary).fixedSize()
            }
            Text(row.address).appFont(.caption).foregroundColor(.secondary).lineLimit(1)
            HStack(spacing: 6) {
                Text(row.volumeDescription)
                Text("·")
                Text(row.mechanismSentence).lineLimit(1)
            }.appFont(.caption).foregroundColor(.secondary)
            HStack(spacing: 6) {
                Text(row.stateDescription).appFont(.caption).foregroundColor(EmailSurface.accent)
                if row.protected {
                    Label("Protected", systemImage: "hand.raised").appFont(.caption)
                        .foregroundColor(.secondary)
                }
            }
            if let status = model.status(for: row) {
                Text(status).appFont(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("email.subscription.status.\(row.id)")
            }
        }
        .padding(.vertical, 4).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
    }
}

/// The detail: the same facts, and exactly the actions this sender still allows.
struct EmailSubscriptionDetailView: View {
    @ObservedObject var model: EmailSubscriptionsViewModel
    let row: EmailSubscriptionView

    /// The list is authoritative, so a settled row updates this pane in place.
    private var current: EmailSubscriptionView { model.items.first { $0.id == row.id } ?? row }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(current.displayName.isEmpty ? current.address : current.displayName)
                        .appFont(.title, weight: .bold).fixedSize(horizontal: false, vertical: true)
                    Text(current.address).appFont(.callout).foregroundColor(.secondary)
                        .textSelection(.enabled)
                    if !current.listID.isEmpty {
                        Text(current.listID).appFont(.caption).foregroundColor(.secondary)
                    }
                }
                VStack(alignment: .leading, spacing: 8) {
                    Label(current.volumeDescription, systemImage: "tray.full")
                    Label("Last received \(current.lastReceivedAt.formatted(date: .abbreviated, time: .shortened))",
                          systemImage: "clock")
                    Label(current.mechanismSentence, systemImage: "arrow.up.right")
                    Label(current.stateDescription, systemImage: "checkmark.seal")
                    if current.protected {
                        Label(
                            current.protectedReason == "important_feedback"
                                ? "Protected: you marked this sender important"
                                : "Protected: you write to this sender",
                            systemImage: "hand.raised")
                    }
                }.appFont(.callout).padding(18).emailCard()
                if let status = model.status(for: current) {
                    Text(status).appFont(.callout).foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("email.subscription.detail.status")
                }
                ForEach(model.actions(for: current)) { action in
                    Button {
                        perform(action)
                    } label: {
                        Label(action.title, systemImage: action.symbol).frame(minHeight: 44)
                    }
                    .buttonStyle(.bordered)
                    .accessibilityIdentifier("email.subscription.action.\(action.rawValue)")
                }
            }
            .padding(24).frame(maxWidth: 640, alignment: .leading).frame(maxWidth: .infinity)
        }
        .background(EmailSurface.canvas)
        .navigationTitle(current.displayName.isEmpty ? current.address : current.displayName)
    }

    private func perform(_ action: EmailSubscriptionAction) {
        switch action {
        case .unsubscribe, .tryAgain: model.beginUnsubscribe(current)
        case .reportSpam: Task { await model.report(current, spam: true) }
        case .notSpam: Task { await model.report(current, spam: false) }
        case .keep: Task { await model.setKept(current, kept: true) }
        case .unkeep: Task { await model.setKept(current, kept: false) }
        }
    }
}

/// The consent: every sender, its mechanism in plain words, and what cannot be undone.
struct EmailUnsubscribeConfirmationSheet: View {
    @ObservedObject var model: EmailSubscriptionsViewModel
    @State private var isSending = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(title).appFont(.title2, weight: .bold)
            Text(EmailUnsubscribeConfirmation.warning).appFont(.callout).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(model.confirmation?.targets ?? []) { target in
                        VStack(alignment: .leading, spacing: 3) {
                            Text(target.displayName.isEmpty ? target.address : target.displayName)
                                .appFont(.callout, weight: .semibold)
                            if !target.address.isEmpty, target.address != target.displayName {
                                Text(target.address).appFont(.caption).foregroundColor(.secondary)
                            }
                            Text(target.sentence).appFont(.caption).foregroundColor(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }.frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }.frame(maxHeight: 280)
            Toggle("Also archive existing mail from these senders", isOn: $model.archiveExisting)
                .accessibilityIdentifier("email.unsubscribe.archive")
            HStack {
                Button("Cancel") { model.cancelConfirmation() }
                Spacer()
                Button("Unsubscribe") {
                    isSending = true
                    Task {
                        await model.confirmUnsubscribe()
                        isSending = false
                    }
                }
                .buttonStyle(.borderedProminent).tint(AppTheme.turquoise).disabled(isSending)
                .accessibilityIdentifier("email.unsubscribe.confirm")
            }
        }
        .padding(24)
        .sheetFrame(minWidth: 300, macMinWidth: 480, idealWidth: 540, minHeight: 360, idealHeight: 520)
    }

    private var title: String {
        let count = model.confirmation?.targets.count ?? 0
        return count == 1 ? "Unsubscribe from this sender?" : "Unsubscribe from \(count) senders?"
    }
}
