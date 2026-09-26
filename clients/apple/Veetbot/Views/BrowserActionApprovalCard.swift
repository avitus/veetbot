import SwiftUI

/// The `browser.act` approval card (ADR-0129, 0129-design §14 item 3). It
/// names the action, the element and the page in Veetbot's words, quotes the
/// website's own text as such, and offers Allow once, Deny, and, when the
/// server offers it, Allow for this task after a confirmation. Website text
/// is always verbatim, never Markdown.
struct BrowserActionApprovalCard: View {
    let approval: ApprovalView
    let presentation: BrowserActionApprovalPresentation
    let resolve: (ApprovalDecision, String?, TaskGrantEcho?) -> Void
    @State private var denialReason = ""
    @State private var confirmingTask = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Website action", systemImage: "hand.raised.fill")
                .appFont(.headline)
                .foregroundColor(AppTheme.orange)
            HStack(spacing: 8) {
                Text(presentation.verb)
                    .appFont(.body, weight: .semibold)
                if let detail = presentation.detail {
                    Text(verbatim: detail)
                        .appFont(.body)
                }
                Spacer(minLength: 4)
                if let badge = presentation.consequenceBadge {
                    Text(badge)
                        .appFont(.caption, weight: .semibold)
                        .foregroundColor(.red)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Color.red.opacity(0.12))
                        .clipShape(Capsule())
                        .accessibilityIdentifier("approval.browser.consequence")
                }
            }
            // The website names its own options (ADR-0129 decision 8).
            if let option = presentation.option {
                labelledQuote("Option from the website", option)
            }
            if presentation.elementRole != nil || presentation.elementName != nil {
                VStack(alignment: .leading, spacing: 3) {
                    Text("Text from the website")
                        .appFont(.caption)
                        .foregroundColor(.secondary)
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        if let role = presentation.elementRole {
                            Text(verbatim: role)
                                .appFont(.caption)
                                .foregroundColor(.secondary)
                        }
                        if let name = presentation.elementName {
                            WebsiteQuote(text: name)
                        }
                    }
                }
            }
            if let shown = presentation.shownOnScreen {
                labelledQuote("Shows on screen as", shown)
            }
            if let dialog = presentation.dialogName {
                labelledQuote("In a dialog titled", dialog)
            }
            if let location = presentation.pageLocation {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text("Page")
                        .appFont(.caption)
                        .foregroundColor(.secondary)
                    Text(verbatim: location)
                        .appFont(.caption)
                        .lineLimit(2)
                }
            }
            if let title = presentation.pageTitle {
                labelledQuote("Page title", title)
            }
            switch presentation.typedText {
            case .visible(let text):
                VStack(alignment: .leading, spacing: 3) {
                    Text("Types")
                        .appFont(.caption)
                        .foregroundColor(.secondary)
                    Text(verbatim: text)
                        .appCodeFont()
                        .textSelection(.enabled)
                }
            case .hidden:
                Text("Hidden: sensitive field")
                    .appFont(.caption)
                    .foregroundColor(.secondary)
            case nil:
                EmptyView()
            }
            if let refused = presentation.refusedNotice {
                Label(refused, systemImage: "nosign")
                    .appFont(.caption, weight: .semibold)
                    .foregroundColor(.red)
            }
            if let notCovered = presentation.notCoveredText {
                Text(notCovered)
                    .appFont(.caption)
                    .foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("approval.browser.not-covered")
            }
            if approval.status.isPending {
                TextField("Reason for denial (optional)", text: $denialReason)
                if presentation.offersTaskGrant, approval.taskGrantOffer != nil {
                    Button("Allow for this task") {
                        confirmingTask = true
                    }
                    .buttonStyle(.bordered)
                    .accessibilityIdentifier("approval.allow-for-task")
                }
                HStack {
                    Button("Allow once") {
                        resolve(.approveOnce, nil, nil)
                    }
                    .buttonStyle(.borderedProminent)
                    .accessibilityIdentifier("approval.allow-once")
                    Button("Deny", role: .destructive) {
                        let reason = denialReason.trimmingCharacters(in: .whitespacesAndNewlines)
                        resolve(.deny, reason.isEmpty ? nil : reason, nil)
                    }
                    .accessibilityIdentifier("approval.deny")
                }
            } else {
                Text(resolutionText)
                    .appFont(.caption)
                    .foregroundColor(.secondary)
            }
        }
        .padding(12)
        .background(AppTheme.orange.opacity(0.08))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(AppTheme.orange.opacity(0.45))
        )
        .sheet(isPresented: $confirmingTask) {
            if let offer = approval.taskGrantOffer {
                TaskGrantConfirmation(
                    offer: offer,
                    allow: {
                        confirmingTask = false
                        resolve(.approveForTask, nil, TaskGrantEcho(offer: offer))
                    },
                    cancel: { confirmingTask = false }
                )
            }
        }
    }

    private var resolutionText: String {
        switch approval.decision {
        case .approveOnce: return "Allowed once"
        case .approveForTask: return "Allowed for this task"
        case .deny: return "Denied"
        case nil: return "Resolved: \(approval.status.displayName)"
        }
    }

    private func labelledQuote(_ label: String, _ text: String) -> some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(label)
                .appFont(.caption)
                .foregroundColor(.secondary)
            WebsiteQuote(text: text)
        }
    }
}

/// Website text in quotes, verbatim, three lines until expanded. Show more
/// appears whenever three lines cut the text short at the width it has,
/// which depends on the screen and text size, not on a character count.
struct WebsiteQuote: View {
    let text: String
    @State private var expanded = false
    @State private var cappedHeight: CGFloat = 0
    @State private var fullHeight: CGFloat = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(verbatim: "“\(text)”")
                .appFont(.body)
                .lineLimit(expanded ? nil : 3)
                .fixedSize(horizontal: false, vertical: true)
                .background(truncationProbe)
            if fullHeight > cappedHeight + 0.5 {
                Button(expanded ? "Show less" : "Show more") {
                    expanded.toggle()
                }
                .buttonStyle(.borderless)
                .appFont(.caption)
            }
        }
    }

    /// The text capped at three lines and in full, both hidden at the visible
    /// width: the full one is taller exactly when the cap cuts the text short.
    private var truncationProbe: some View {
        GeometryReader { proxy in
            ZStack(alignment: .topLeading) {
                measured(lineLimit: 3).background(heightReader { cappedHeight = $0 })
                measured(lineLimit: nil).background(heightReader { fullHeight = $0 })
            }
            .frame(width: proxy.size.width, alignment: .topLeading)
            .hidden()
            .accessibilityHidden(true)
        }
    }

    private func measured(lineLimit: Int?) -> some View {
        Text(verbatim: "“\(text)”")
            .appFont(.body)
            .lineLimit(lineLimit)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func heightReader(_ update: @escaping (CGFloat) -> Void) -> some View {
        GeometryReader { proxy in
            Color.clear
                .onAppear { update(proxy.size.height) }
                .onChange(of: proxy.size.height) { update($0) }
        }
    }
}

/// Allow for this task, confirmed: the server's offer text, verbatim.
private struct TaskGrantConfirmation: View {
    let offer: TaskGrantOfferView
    let allow: () -> Void
    let cancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Allow for this task?")
                .appFont(.title3, weight: .semibold)
            Text(verbatim: offer.summary)
                .appFont(.body)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("task-grant.confirm.summary")
            HStack {
                Spacer()
                Button("Cancel") { cancel() }
                    .keyboardShortcut(.cancelAction)
                    .accessibilityIdentifier("task-grant.confirm.cancel")
                Button("Allow") { allow() }
                    .buttonStyle(.borderedProminent)
                    .tint(AppTheme.turquoise)
                    .keyboardShortcut(.defaultAction)
                    .accessibilityIdentifier("task-grant.confirm.allow")
            }
        }
        .padding(24)
        .sheetFrame(minWidth: 300, macMinWidth: 420, idealWidth: 480, minHeight: 0)
    }
}
