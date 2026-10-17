import SwiftUI

/// The `browser.act` approval card (ADR-0129, 0129-design §14 item 3). It
/// names the action, the element and the page in Veetbot's words, quotes the
/// website's own text as such, and offers Allow once, Deny, and, when the
/// server offers it, Allow all actions for this task after a confirmation. Website text
/// is always verbatim, never Markdown.
struct BrowserActionApprovalCard: View {
    let approval: ApprovalView
    let presentation: BrowserActionApprovalPresentation
    /// The decision on its way, which disables every control until the server answers.
    var inFlight: ApprovalDecision? = nil
    let resolve: (ApprovalDecision, String?, TaskGrantEcho?) -> Void
    @State private var denialReason = ""
    @State private var confirmingTask = false

    /// Show the action and server-provided permission scope before an owner decides.
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            AttentionCardLabel(title: "Needs your approval")
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text("""
                    \(Text(presentation.verb).fontWeight(.semibold))\
                    \(Text(verbatim: presentation.detail.map { " " + $0 } ?? "").foregroundColor(AppTheme.inkSoft))
                    """)
                    .appFont(.body)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                if let badge = presentation.consequenceBadge {
                    Text(badge)
                        .appFont(.caption, weight: .semibold)
                        .foregroundColor(AppTheme.red)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .overlay(Capsule().stroke(AppTheme.red.opacity(0.5)))
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
                        .foregroundColor(AppTheme.inkSoft)
                    HStack(alignment: .firstTextBaseline, spacing: 6) {
                        if let role = presentation.elementRole {
                            Text(verbatim: role)
                                .appFont(.caption)
                                .foregroundColor(AppTheme.inkSoft)
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
                        .foregroundColor(AppTheme.inkSoft)
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
                        .foregroundColor(AppTheme.inkSoft)
                    Text(verbatim: text)
                        .appCodeFont()
                        .textSelection(.enabled)
                }
            case .hidden:
                Text("Hidden: sensitive field")
                    .appFont(.caption)
                    .foregroundColor(AppTheme.inkSoft)
            case nil:
                EmptyView()
            }
            if let refused = presentation.refusedNotice {
                Label(refused, systemImage: "nosign")
                    .appFont(.caption, weight: .semibold)
                    .foregroundColor(AppTheme.red)
            }
            if let notCovered = presentation.notCoveredText {
                Text(notCovered)
                    .appFont(.caption)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("approval.browser.not-covered")
            }
            if approval.status.isPending {
                DenialReasonField(text: $denialReason)
                    .disabled(inFlight != nil)
                ApprovalDecisionButtons {
                    Button {
                        resolve(.approveOnce, nil, nil)
                    } label: {
                        ApprovalDecisionLabel(
                            title: "Allow once", decision: .approveOnce, inFlight: inFlight, websiteCard: true
                        )
                    }
                    .accessibilityIdentifier("approval.allow-once")
                } secondary: {
                    if presentation.offersTaskGrant, approval.taskGrantOffer != nil {
                        Button {
                            confirmingTask = true
                        } label: {
                            ApprovalDecisionLabel(
                                title: "Allow all actions for this task", decision: .approveForTask,
                                inFlight: inFlight, websiteCard: true
                            )
                        }
                        .accessibilityIdentifier("approval.allow-for-task")
                    }
                } deny: {
                    Button(role: .destructive) {
                        let reason = denialReason.trimmingCharacters(in: .whitespacesAndNewlines)
                        resolve(.deny, reason.isEmpty ? nil : reason, nil)
                    } label: {
                        ApprovalDecisionLabel(title: "Deny", decision: .deny, inFlight: inFlight, websiteCard: true)
                    }
                    .accessibilityIdentifier("approval.deny")
                }
                .padding(.top, 4)
                .disabled(inFlight != nil)
                if presentation.offersTaskGrant, let offer = approval.taskGrantOffer {
                    // The offer's own words, beneath the decisions, as the reference places its limits.
                    Text(verbatim: offer.summary)
                        .appFont(.caption)
                        .foregroundColor(AppTheme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("approval.task-scope")
                }
            } else {
                Text(resolutionText)
                    .appFont(.caption)
                    .foregroundColor(AppTheme.inkSoft)
            }
        }
        .attentionCard()
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
                .foregroundColor(AppTheme.inkSoft)
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
                .buttonStyle(.plain)
                .appFont(.caption, weight: .medium)
                .foregroundColor(AppTheme.tealText)
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

    /// Heights land after the layout pass that measured them: written during
    /// it, from a card docked outside the transcript, they left the whole
    /// window undrawn on macOS 26.
    private func heightReader(_ update: @escaping (CGFloat) -> Void) -> some View {
        GeometryReader { proxy in
            Color.clear
                .onAppear { let height = proxy.size.height; DispatchQueue.main.async { update(height) } }
                .onChange(of: proxy.size.height) { _, height in DispatchQueue.main.async { update(height) } }
        }
    }
}

/// Allow all actions for this task, confirmed: the server's offer text, verbatim.
private struct TaskGrantConfirmation: View {
    let offer: TaskGrantOfferView
    let allow: () -> Void
    let cancel: () -> Void

    /// Confirm the exact server offer before creating a task permission.
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("Allow all actions for this task?")
                .appFont(.title3, weight: .semibold)
            Text(verbatim: offer.summary)
                .appFont(.body)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("task-grant.confirm.summary")
            HStack {
                Spacer()
                Button("Cancel") { cancel() }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .keyboardShortcut(.cancelAction)
                    .accessibilityIdentifier("task-grant.confirm.cancel")
                Button("Allow") { allow() }
                    .buttonStyle(PrimaryCapsuleButtonStyle())
                    .keyboardShortcut(.defaultAction)
                    .accessibilityIdentifier("task-grant.confirm.allow")
            }
        }
        .padding(24)
        .sheetFrame(minWidth: 300, macMinWidth: 420, idealWidth: 480, minHeight: 0)
    }
}

/// The decisions of a card that needs the owner, in the reference order: the
/// filled primary, the bordered alternative when there is one, and a quiet
/// Deny. One row on the Mac; on iOS the primary spans the card and the other
/// two share the row beneath it.
struct ApprovalDecisionButtons<Primary: View, Secondary: View, Deny: View>: View {
    @ViewBuilder let primary: () -> Primary
    @ViewBuilder let secondary: () -> Secondary
    @ViewBuilder let deny: () -> Deny

    var body: some View {
        #if os(iOS)
        VStack(spacing: 8) {
            primary().buttonStyle(PrimaryCapsuleButtonStyle(expands: true))
            HStack(spacing: 8) {
                secondary().buttonStyle(SecondaryCapsuleButtonStyle(expands: true))
                deny().buttonStyle(QuietCapsuleButtonStyle(expands: true))
            }
        }
        #else
        HStack(spacing: 8) {
            primary().buttonStyle(PrimaryCapsuleButtonStyle())
            secondary().buttonStyle(SecondaryCapsuleButtonStyle())
            deny().buttonStyle(QuietCapsuleButtonStyle())
        }
        #endif
    }
}
