import SwiftUI

private enum ServerContractIdentifier {
    static let sandboxRunCommand = "sandbox.run_command"
    static let workspaceReadText = "workspace.read_text"
    static let workspaceWriteText = "workspace.write_text"
    static let taskCompleted = "completed"
    static let taskInProgress = "in_progress"
    static let taskBlocked = "blocked"
}

/// What a run did, in one sentence the owner can open: a single tool call or
/// a bundle of adjacent settled ones. Open, it is a monospace ledger, one row
/// per call, each opening again onto its arguments and result. A pending
/// approval is never here; the transcript says it is waiting and the card
/// sits above the composer. A settled approval stays with its call, folded.
struct WorkStrip: View {
    enum Content {
        case single(ToolActivity)
        case bundle(ToolActivityBundle)
    }

    let content: Content
    /// The settled approvals the calls carry.
    var approvals: [ApprovalView] = []
    let openArtifact: (UUID) -> Void
    /// On the Mac the calls open in the right panel instead of here (ADR-0237).
    @Environment(\.inspectorReveal) private var inspectorReveal
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                if let reveal = inspectorReveal {
                    reveal.activity(revealID)
                } else {
                    withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() }
                }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: "chevron.right")
                        .veetIcon(.small, weight: .semibold)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                        .frame(width: 12)
                    Text(sentence)
                        .veetType(.ui)
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    if inspectorReveal != nil {
                        Image(systemName: "sidebar.trailing")
                            .veetIcon(.small)
                            .accessibilityHidden(true)
                    }
                }
                .foregroundColor(shownInPanel ? AppTheme.tealText : AppTheme.inkSoft)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(inspectorReveal == nil ? "" : "Show these calls in the panel")
            .accessibilityValue(summaryValue)
            .accessibilityIdentifier(summaryIdentifier)

            if expanded {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(Array(activities.enumerated()), id: \.element.id) { index, activity in
                        // A single call's settled approval is the row under the
                        // strip, so its ledger row does not repeat it.
                        ToolLedgerRow(
                            index: index + 1,
                            activity: activity,
                            approval: isBundle
                                ? approvals.first { $0.id == activity.approvalID && !$0.status.isPending } : nil,
                            openArtifact: openArtifact
                        )
                    }
                }
                .padding(.leading, 14)
                .overlay(alignment: .leading) {
                    Rectangle().fill(AppTheme.hairlineStrong).frame(width: 1).padding(.leading, 5)
                }
                .padding(.leading, 4)
            }
            if case .single(let activity) = content,
                let approval = approvals.first(where: { $0.id == activity.approvalID }),
                !approval.status.isPending
            {
                ApprovalCardView(approval: approval) { _, _, _ in }
            }
        }
    }

    private var isBundle: Bool {
        if case .bundle = content { return true }
        return false
    }

    private var activities: [ToolActivity] {
        switch content {
        case .single(let activity): return [activity]
        case .bundle(let bundle): return bundle.activities
        }
    }

    /// The conversation activity id the panel's ledger groups by.
    private var revealID: String {
        switch content {
        case .single(let activity): return "tool:\(activity.id)"
        case .bundle(let bundle): return "tool:\(bundle.id)"
        }
    }

    private var shownInPanel: Bool {
        inspectorReveal?.activeActivityID == revealID
    }

    private var summaryValue: String {
        if inspectorReveal != nil { return shownInPanel ? "Shown in panel" : "In panel" }
        return expanded ? "Expanded" : "Collapsed"
    }

    private var summaryIdentifier: String {
        switch content {
        case .single(let activity): return "tool.run.\(activity.id)"
        case .bundle(let bundle): return "tool.bundle.\(bundle.id)"
        }
    }

    /// One sentence: the call and its outcome, or the bundle's count and outcomes.
    private var sentence: String {
        switch content {
        case .bundle(let bundle):
            return bundle.summary
        case .single(let activity):
            var parts = [activity.name]
            if let detail = ToolLedgerRow.detail(for: activity) { parts.append(detail) }
            if activity.allowedByTaskGrant { parts.append("Allowed by task permission") }
            parts.append(activity.presentationStatus.rawValue.capitalized)
            return parts.joined(separator: " · ")
        }
    }
}

/// One call in the ledger: its name, what it was asked, its outcome and risk,
/// in the monospace face; opening it shows arguments and the result.
struct ToolLedgerRow: View {
    let index: Int
    let activity: ToolActivity
    let approval: ApprovalView?
    let openArtifact: (UUID) -> Void
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() }
            } label: {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("\(index). \(activity.name)")
                            .veetMono()
                            .foregroundColor(AppTheme.ink)
                        Spacer(minLength: 8)
                        if let risk = activity.risk, risk == .high || risk == .critical {
                            Text(risk.rawValue.uppercased())
                                .veetType(.meta, weight: .semibold)
                                .foregroundColor(taxonomy.color)
                        }
                        Text(statusText)
                            .veetType(.meta)
                            .foregroundColor(
                                activity.presentationStatus == .failed
                                    || activity.presentationStatus == .denied
                                    ? AppTheme.red : AppTheme.inkSoft
                            )
                    }
                    if let detail = Self.detail(for: activity) {
                        Text(detail)
                            .veetMono()
                            .foregroundColor(AppTheme.inkSoft)
                            .lineLimit(1)
                    }
                    if activity.allowedByTaskGrant {
                        Text("Allowed by task permission")
                            .veetType(.meta)
                            .foregroundColor(AppTheme.inkFaint)
                    }
                    if let approval {
                        Text(approval.settledTitle)
                            .veetType(.meta)
                            .foregroundColor(AppTheme.inkFaint)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            .accessibilityIdentifier("tool.detail.\(activity.id)")

            if expanded {
                if let summary = activity.taskGrantSummary {
                    DetailBlock(title: "What it did", text: summary)
                }
                if let diagnostics = activity.browserDiagnostics {
                    DetailBlock(title: "Browser activity", text: diagnostics.summary + "\n" + diagnostics.detail)
                }
                if !activity.arguments.isEmpty {
                    DetailBlock(
                        title: "Arguments",
                        text: JSONValue.object(activity.arguments).prettyPrinted
                    )
                }
                if let result = activity.result {
                    ToolResultContent(
                        toolName: activity.name,
                        result: result,
                        openArtifact: openArtifact
                    )
                }
                if let approval {
                    // A ledger holds only settled approvals, which offer no controls.
                    ApprovalCardView(approval: approval) { _, _, _ in }
                }
            }
        }
        .padding(.vertical, 2)
    }

    private var statusText: String {
        activity.presentationStatus.rawValue.capitalized
    }

    private var taxonomy: TaxonomyStyle {
        TaxonomyStyle(sideEffect: activity.sideEffect, risk: activity.risk)
    }

    /// Identify a call by its query, URL or path when it carries one.
    static func detail(for activity: ToolActivity) -> String? {
        for key in ["query", "url", "path"] {
            if let value = activity.arguments[key]?.stringValue, !value.isEmpty { return value }
        }
        return nil
    }
}

/// A `browser.act` approval with a described view gets its own card
/// (ADR-0129); every other approval keeps the generic one. Pending, the card
/// is the whole thing, docked above the composer; settled, it is one quiet
/// row that opens onto what was decided.
struct ApprovalCardView: View {
    let approval: ApprovalView
    var activeTaskGrant: BrowserTaskGrantView? = nil
    /// The decision on its way, which disables every control until the server answers.
    var inFlight: ApprovalDecision? = nil
    let resolve: (ApprovalDecision, String?, TaskGrantEcho?) -> Void
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Only a pending approval needs its details and controls in view.
            if !approval.status.isPending {
                Button {
                    expanded.toggle()
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: approval.settledSymbol)
                            .foregroundColor(approval.settledColor)
                        Text(approval.settledTitle)
                            .veetType(.ui, weight: .medium)
                            .foregroundColor(AppTheme.inkSoft)
                        Text(verbatim: "· " + approval.actionSummary)
                            .veetType(.ui)
                            .foregroundColor(AppTheme.inkFaint)
                            .lineLimit(1)
                        Spacer(minLength: 0)
                        Image(systemName: "chevron.right")
                            .veetIcon(.small, weight: .semibold)
                            .foregroundColor(AppTheme.inkFaint)
                            .rotationEffect(.degrees(expanded ? 90 : 0))
                    }
                    .padding(.vertical, 4)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("approval.details")
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
                if expanded { details }
            } else {
                details
            }
        }
        .onChange(of: approval.status) { expanded = false }
        .onChange(of: approval.id) { expanded = false }
    }

    @ViewBuilder
    private var details: some View {
        if let presentation = BrowserActionApprovalPresentation(approval: approval, activeGrant: activeTaskGrant) {
            BrowserActionApprovalCard(
                approval: approval, presentation: presentation, inFlight: inFlight, resolve: resolve
            )
        } else {
            ApprovalCard(approval: approval, inFlight: inFlight) { decision, reason in
                resolve(decision, reason, nil)
            }
        }
    }
}

private extension ApprovalView {
    /// How a settled approval reads in its collapsed row.
    var settledTitle: String {
        switch status {
        case .approved: decision == .approveForTask ? "Allowed for this task" : "Approved"
        case .denied: "Denied"
        case .expired: "Expired"
        case .cancelled: "Cancelled"
        case .pending: "Approval checkpoint"
        }
    }

    var settledSymbol: String {
        switch status {
        case .approved: "checkmark"
        case .denied: "xmark"
        case .expired: "clock"
        case .cancelled: "slash.circle"
        case .pending: "flag"
        }
    }

    var settledColor: Color {
        switch status {
        case .approved: AppTheme.teal
        case .denied: AppTheme.red
        case .expired, .cancelled: AppTheme.inkFaint
        case .pending: AppTheme.amberText
        }
    }
}

/// The surface, border and padding every card that needs the owner shares:
/// white on paper, an amber hairline, and room to breathe.
struct AttentionCardSurface: ViewModifier {
    func body(content: Content) -> some View {
        content
            .padding(.horizontal, 18)
            .padding(.top, 14)
            .padding(.bottom, 14)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: A2Metrics.cardRadius, style: .continuous)
                    .fill(AppTheme.surface)
            )
            .overlay(
                RoundedRectangle(cornerRadius: A2Metrics.cardRadius, style: .continuous)
                    .stroke(AppTheme.amberLine)
            )
    }
}

extension View {
    func attentionCard() -> some View { modifier(AttentionCardSurface()) }
}

/// The amber eyebrow of a card that needs the owner.
struct AttentionCardLabel: View {
    let title: String

    var body: some View {
        Label {
            Text(title)
        } icon: {
            Image(systemName: "flag")
        }
        .veetType(.meta, weight: .semibold)
        .foregroundColor(AppTheme.amberText)
    }
}

/// What will happen and why Veetbot is asking come first, in the server's
/// words; the tool, risk, expiry and raw arguments wait behind Details while
/// the card is pending (ADR-0260).
struct ApprovalCard: View {
    let approval: ApprovalView
    var inFlight: ApprovalDecision? = nil
    let resolve: (ApprovalDecision, String?) -> Void
    @State private var denialReason = ""
    @State private var showsDetails = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            AttentionCardLabel(title: "Approval checkpoint")
            Text(approval.actionSummary)
                .veetType(.ui, weight: .semibold)
                .fixedSize(horizontal: false, vertical: true)
            if let reason = approval.ownerReason {
                Text(reason)
                    .veetType(.ui)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("approval.reason")
            }
            if !approval.status.isPending { technicalDetails }
            if approval.status.isPending {
                DenialReasonField(text: $denialReason)
                    .disabled(inFlight != nil)
                ApprovalDecisionButtons {
                    Button { resolve(.approveOnce, nil) } label: {
                        ApprovalDecisionLabel(title: "Approve once", decision: .approveOnce, inFlight: inFlight)
                    }
                } secondary: {
                    EmptyView()
                } deny: {
                    Button(role: .destructive) {
                        let reason = denialReason.trimmingCharacters(in: .whitespacesAndNewlines)
                        resolve(.deny, reason.isEmpty ? nil : reason)
                    } label: {
                        ApprovalDecisionLabel(title: "Deny", decision: .deny, inFlight: inFlight)
                    }
                }
                .padding(.top, 4)
                .disabled(inFlight != nil)
                Button {
                    showsDetails.toggle()
                } label: {
                    HStack(spacing: 4) {
                        Text("Details")
                        Image(systemName: "chevron.right")
                            .veetIcon(.small, weight: .semibold)
                            .rotationEffect(.degrees(showsDetails ? 90 : 0))
                    }
                    .veetType(.meta, weight: .medium)
                    .foregroundColor(AppTheme.inkSoft)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityIdentifier("approval.more")
                .accessibilityValue(showsDetails ? "Expanded" : "Collapsed")
                if showsDetails { technicalDetails }
            } else {
                Text("Resolved: \(approval.decision?.rawValue ?? approval.status.displayName)")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
            }
        }
        .attentionCard()
    }

    /// The registry name, risk, expiry and the redacted arguments themselves.
    @ViewBuilder
    private var technicalDetails: some View {
        HStack(spacing: 6) {
            if let toolName = approval.toolName {
                Text(toolName).veetMono()
                Text("·")
            }
            Text("\(approval.risk.capitalized) risk")
            Text("·")
            Text(approval.expiresAt.map { "Expires \($0.formatted())" } ?? "No expiry")
        }
        .veetType(.meta)
        .foregroundColor(AppTheme.inkSoft)
        if !approval.arguments.isEmpty {
            DetailBlock(
                title: "Arguments",
                text: JSONValue.object(approval.arguments).prettyPrinted
            )
        }
    }
}

/// The optional reason behind a denial, as one quiet line.
struct DenialReasonField: View {
    @Binding var text: String

    var body: some View {
        TextField("Reason for denial (optional)", text: $text, prompt: .a2Prompt("Reason for denial (optional)"))
            .textFieldStyle(.plain)
            .veetType(.ui)
            .padding(.vertical, 6)
            .overlay(alignment: .bottom) {
                Rectangle().fill(AppTheme.hairline).frame(height: 1)
            }
    }
}

/// A decision control's own title, or progress once that decision is on its way.
struct ApprovalDecisionLabel: View {
    let title: String
    let decision: ApprovalDecision
    let inFlight: ApprovalDecision?
    var websiteCard = false

    var body: some View {
        if inFlight == decision {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(decision.progressLabel(websiteCard: websiteCard))
            }
        } else {
            Text(title)
        }
    }
}

struct ClarifyingQuestionCard: View {
    let prompt: ClarifyingQuestionPrompt
    let submit: (String) async -> Bool
    @State private var answer = ""
    @State private var isSubmitting = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            AttentionCardLabel(title: "Clarifying question")
            Text(prompt.question)
                .veetType(.ui, weight: .semibold)
                .fixedSize(horizontal: false, vertical: true)
            TextField("Your answer", text: $answer, prompt: .a2Prompt("Your answer"))
                .textFieldStyle(.plain)
                .veetType(.ui)
                .padding(.vertical, 6)
                .overlay(alignment: .bottom) {
                    Rectangle().fill(AppTheme.hairline).frame(height: 1)
                }
                .onSubmit(send)
            Button("Answer", action: send)
                .buttonStyle(PrimaryCapsuleButtonStyle())
                .padding(.top, 4)
                .disabled(
                    isSubmitting
                        || answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                )
        }
        .attentionCard()
    }

    private func send() {
        let value = answer
        guard !isSubmitting, !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        // Set before the Task so a Return and a tap in the same turn cannot both pass the guard.
        isSubmitting = true
        Task {
            defer { isSubmitting = false }
            if await submit(value) {
                answer = ""
            }
        }
    }
}

/// ADR-0280: the runtime paused because a website signed the owner out. With
/// a waiting website the run resumes once sign-in is verified, so there is
/// nothing to type; otherwise the owner signs in and says so.
struct SignInRequiredCard: View {
    let prompt: ClarifyingQuestionPrompt
    /// A verified sign-in continues the run; false once the wait has expired.
    let resumesByItself: Bool
    /// The run's own deadline has passed, so only stopping it is left.
    let ranOutOfTime: Bool
    let signIn: () async -> Void
    let openWebsiteAccess: () -> Void
    let confirmSignedIn: () async -> Bool
    let stop: () async -> Void
    @State private var isWorking = false

    private var waitsForWebsite: Bool { prompt.signIn?.profileID != nil }

    /// Names the website that signed the owner out, when the pause knows it.
    static func title(for prompt: ClarifyingQuestionPrompt) -> String {
        guard let host = prompt.signIn?.host else { return "A website needs you to sign in" }
        return "\(host) signed you out"
    }

    private var explanation: String {
        if ranOutOfTime {
            return "This task ran out of time. Stop it, then start it again."
        }
        if resumesByItself {
            return "Veetbot paused this task. It continues on its own once you are signed in."
        }
        return waitsForWebsite
            ? "Sign in, then let Veetbot know to continue."
            : "Sign in from Website Access, then let Veetbot know to continue."
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            AttentionCardLabel(title: "Sign-in needed")
            Text(Self.title(for: prompt))
                .veetType(.ui, weight: .semibold)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityIdentifier("sign-in-card.title")
            Text(explanation)
            .veetType(.meta)
            .foregroundColor(AppTheme.inkSoft)
            .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: VeetSpace.s2) {
                if ranOutOfTime {
                    EmptyView()
                } else if waitsForWebsite {
                    Button(prompt.signIn?.host.map { "Sign in to \($0)" } ?? "Sign in") {
                        run { await signIn() }
                    }
                    .primaryCapsule()
                    .accessibilityIdentifier("sign-in-card.sign-in")
                } else {
                    Button("Open Website Access", action: openWebsiteAccess)
                        .primaryCapsule()
                        .accessibilityIdentifier("sign-in-card.website-access")
                }
                if !resumesByItself && !ranOutOfTime {
                    Button("I've signed in") { run { _ = await confirmSignedIn() } }
                        .buttonStyle(QuietCapsuleButtonStyle())
                        .accessibilityIdentifier("sign-in-card.signed-in")
                }
                Button("Stop task") { run { await stop() } }
                    .buttonStyle(QuietCapsuleButtonStyle())
                    .accessibilityIdentifier("sign-in-card.stop")
                Spacer(minLength: 0)
            }
            .disabled(isWorking)
            .padding(.top, 4)
        }
        .attentionCard()
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("sign-in-card")
    }

    /// Runs one button's action at a time; the buttons stay disabled meanwhile.
    private func run(_ action: @escaping () async -> Void) {
        guard !isWorking else { return }
        isWorking = true
        Task {
            defer { isWorking = false }
            await action()
        }
    }
}

struct WorkingStatePanel: View {
    let state: WorkingStateView
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() }
            } label: {
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Image(systemName: "chevron.right")
                        .veetIcon(.small, weight: .semibold)
                        .rotationEffect(.degrees(expanded ? 90 : 0))
                        .frame(width: 12)
                    Text(summary)
                        .veetType(.ui)
                        .lineLimit(expanded ? nil : 1)
                }
                .foregroundColor(AppTheme.inkSoft)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            .accessibilityIdentifier("chat.working-state")

            if expanded {
                WorkingStateDetails(state: state)
                    .padding(.leading, 18)
            }
        }
    }

    private var summary: String {
        if let objective = state.objective, !objective.isEmpty { return "Working state · \(objective)" }
        return "Working state"
    }
}

/// The run's working notes: objective, tasks, facts and the next action.
/// Behind a disclosure above the transcript on touch; always open in the
/// Mac right panel.
struct WorkingStateDetails: View {
    let state: WorkingStateView

    var body: some View {
        VStack(alignment: .leading, spacing: 9) {
            if let objective = state.objective {
                DetailBlock(title: "Objective", text: objective)
            }
            if !state.tasks.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Tasks")
                        .veetType(.meta, weight: .semibold)
                        .foregroundColor(AppTheme.inkSoft)
                    ForEach(state.tasks) { task in
                        Label(task.description, systemImage: icon(for: task.status))
                            .veetType(.ui)
                    }
                }
            }
            if !state.establishedFacts.isEmpty {
                VStack(alignment: .leading, spacing: 5) {
                    Text("Facts")
                        .veetType(.meta, weight: .semibold)
                        .foregroundColor(AppTheme.inkSoft)
                    ForEach(state.establishedFacts) { fact in
                        Text("• \(fact.statement)").veetType(.ui)
                    }
                }
            }
            if let nextAction = state.nextAction {
                DetailBlock(title: "Next action", text: nextAction)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func icon(for status: String) -> String {
        switch status {
        case ServerContractIdentifier.taskCompleted: return "checkmark.circle.fill"
        case ServerContractIdentifier.taskInProgress: return "arrow.triangle.2.circlepath"
        case ServerContractIdentifier.taskBlocked: return "exclamationmark.octagon"
        default: return "circle"
        }
    }
}

private struct ToolResultContent: View {
    let toolName: String
    let result: ToolResultView
    let openArtifact: (UUID) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Result")
                    .veetType(.meta, weight: .semibold)
                    .foregroundColor(AppTheme.inkSoft)
                Spacer()
                if let trust = result.trust {
                    Text(trust.rawValue.replacingOccurrences(of: "_", with: " ").uppercased())
                        .veetType(.meta, weight: .semibold)
                        .foregroundColor(trust == .externalUntrusted ? AppTheme.amberText : AppTheme.inkFaint)
                }
            }
            if toolName == ServerContractIdentifier.sandboxRunCommand {
                terminalTranscript
            } else if toolName == ServerContractIdentifier.workspaceReadText
                || toolName == ServerContractIdentifier.workspaceWriteText
            {
                filePreview
            } else {
                ForEach(Array(result.content.enumerated()), id: \.offset) { _, block in
                    switch block {
                    case .text(let text):
                        Text(text).veetType(.ui).textSelection(.enabled)
                    case .image(let artifactID, let mediaType, _):
                        artifactButton("Image · \(mediaType)", id: artifactID)
                    case .file(let artifactID, _, let filename):
                        artifactButton(filename ?? "File", id: artifactID)
                    }
                }
            }
        }
    }

    private func artifactButton(_ label: String, id: UUID) -> some View {
        Button {
            openArtifact(id)
        } label: {
            Label(label, systemImage: "doc")
        }
        .buttonStyle(SecondaryCapsuleButtonStyle())
    }

    private var terminalTranscript: some View {
        VStack(alignment: .leading, spacing: 5) {
            let stdout =
                result.structured?["stdout"]?.stringValue
                ?? result.content.first?.text
                ?? ""
            let stderr = result.structured?["stderr"]?.stringValue ?? ""
            Text(stdout.isEmpty ? "(no stdout)" : stdout)
            if !stderr.isEmpty {
                Divider()
                Text(stderr).foregroundColor(AppTheme.red)
            }
            if let exitCode = result.structured?["exit_code"]?.intValue {
                Text("exit_code: \(exitCode)").foregroundColor(AppTheme.inkSoft)
            }
            if let files = result.structured?["files_changed"]?.arrayValue, !files.isEmpty {
                Text("files_changed: \(JSONValue.array(files).prettyPrinted)")
                    .foregroundColor(AppTheme.inkSoft)
            }
        }
        .veetMono()
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(AppTheme.field)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .textSelection(.enabled)
    }

    private var filePreview: some View {
        let text =
            result.structured?["content"]?.stringValue
            ?? result.content.compactMap(\.text).joined(separator: "\n")
        return Text(text)
            .veetMono()
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(AppTheme.field)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .textSelection(.enabled)
    }
}

struct DetailBlock: View {
    let title: String
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title)
                .veetType(.meta, weight: .semibold)
                .foregroundColor(AppTheme.inkSoft)
            Text(text)
                .veetMono()
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct TaxonomyStyle {
    let icon: String
    let color: Color

    init(sideEffect: SideEffectClass?, risk: RiskLevel?) {
        switch sideEffect {
        case .some(.none): icon = "sparkles"
        case .workspaceRead: icon = "doc.text.magnifyingglass"
        case .workspaceWrite: icon = "square.and.pencil"
        case .networkRead, .sandboxNetwork: icon = "network"
        case .codeExecution, .packageInstall: icon = "terminal"
        case .externalMessage: icon = "paperplane"
        case .externalWrite, .externalDelete, .publication: icon = "arrow.up.right.square"
        case .financial: icon = "creditcard"
        case .credentialAccess, .hostAccess, .privileged: icon = "lock.shield"
        case nil: icon = "wrench.and.screwdriver"
        }
        switch risk {
        case .critical: color = AppTheme.red
        case .high: color = AppTheme.amberText
        case .medium: color = AppTheme.amberText
        case .low: color = AppTheme.inkSoft
        case nil: color = AppTheme.teal
        }
    }
}
