import SwiftUI

private enum ServerContractIdentifier {
    static let sandboxRunCommand = "sandbox.run_command"
    static let workspaceReadText = "workspace.read_text"
    static let workspaceWriteText = "workspace.write_text"
    static let taskCompleted = "completed"
    static let taskInProgress = "in_progress"
    static let taskBlocked = "blocked"
}

struct ToolActivityCard: View {
    let activity: ToolActivity
    let approval: ApprovalView?
    let resolve: (ApprovalView, ApprovalDecision, String?) -> Void
    let openArtifact: (UUID) -> Void
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button {
                expanded.toggle()
            } label: {
                HStack(spacing: 10) {
                    Image(systemName: taxonomy.icon)
                        .foregroundColor(taxonomy.color)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(activity.name).font(.headline)
                        Text(activity.status.rawValue.capitalized)
                            .font(.caption)
                            .foregroundColor(taxonomy.color)
                    }
                    Spacer()
                    if let risk = activity.risk {
                        Text(risk.rawValue.uppercased())
                            .font(.caption2.bold())
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(taxonomy.color.opacity(0.14))
                            .clipShape(Capsule())
                    }
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                }
            }
            .buttonStyle(.plain)
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")

            if expanded {
                if !activity.arguments.isEmpty {
                    DetailBlock(
                        title: "Arguments", text: JSONValue.object(activity.arguments).prettyPrinted
                    )
                }
                if let result = activity.result {
                    ToolResultContent(
                        toolName: activity.name,
                        result: result,
                        openArtifact: openArtifact
                    )
                }
            }
            if let approval {
                ApprovalCard(approval: approval) { decision, reason in
                    resolve(approval, decision, reason)
                }
            }
        }
        .padding(12)
        .background(Color.secondary.opacity(0.07))
        .clipShape(RoundedRectangle(cornerRadius: 12))
    }

    private var taxonomy: TaxonomyStyle {
        TaxonomyStyle(sideEffect: activity.sideEffect, risk: activity.risk)
    }
}

struct ApprovalCard: View {
    let approval: ApprovalView
    let resolve: (ApprovalDecision, String?) -> Void
    @State private var denialReason = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label("Approval checkpoint", systemImage: "hand.raised.fill")
                .font(.headline)
                .foregroundColor(.orange)
            Text(approval.actionSummary)
            if let toolName = approval.toolName {
                KeyValueRow(key: "Tool", value: toolName)
            }
            KeyValueRow(key: "Risk", value: approval.risk.uppercased())
            KeyValueRow(key: "Expires", value: approval.expiresAt?.formatted() ?? "No expiry")
            if !approval.arguments.isEmpty {
                DetailBlock(
                    title: "Arguments",
                    text: JSONValue.object(approval.arguments).prettyPrinted
                )
            }
            if approval.status.isPending {
                TextField("Reason for denial (optional)", text: $denialReason)
                HStack {
                    Button("Approve once") { resolve(.approveOnce, nil) }
                        .buttonStyle(.borderedProminent)
                    Button("Deny", role: .destructive) {
                        let reason = denialReason.trimmingCharacters(in: .whitespacesAndNewlines)
                        resolve(.deny, reason.isEmpty ? nil : reason)
                    }
                }
            } else {
                Text("Resolved: \(approval.decision?.rawValue ?? approval.status.displayName)")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
        .padding(12)
        .background(Color.orange.opacity(0.08))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.orange.opacity(0.45))
        )
    }
}

struct ClarifyingQuestionCard: View {
    let prompt: ClarifyingQuestionPrompt
    let submit: (String) async -> Bool
    @State private var answer = ""
    @State private var isSubmitting = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Label("Clarifying question", systemImage: "questionmark.bubble.fill")
                .font(.headline)
                .foregroundColor(.blue)
            Text(prompt.question)
            TextField("Your answer", text: $answer)
            Button("Answer") {
                let value = answer
                Task {
                    isSubmitting = true
                    defer { isSubmitting = false }
                    if await submit(value) {
                        answer = ""
                    }
                }
            }
            .buttonStyle(.borderedProminent)
            .disabled(
                isSubmitting
                    || answer.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            )
        }
        .padding(12)
        .background(Color.blue.opacity(0.08))
        .overlay(
            RoundedRectangle(cornerRadius: 10)
                .stroke(Color.blue.opacity(0.4))
        )
    }
}

struct WorkingStatePanel: View {
    let state: WorkingStateView
    @State private var expanded = true

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 9) {
                if let objective = state.objective {
                    DetailBlock(title: "Objective", text: objective)
                }
                if !state.tasks.isEmpty {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Tasks").font(.caption.bold()).foregroundColor(.secondary)
                        ForEach(state.tasks) { task in
                            Label(task.description, systemImage: icon(for: task.status))
                        }
                    }
                }
                if !state.establishedFacts.isEmpty {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("Facts").font(.caption.bold()).foregroundColor(.secondary)
                        ForEach(state.establishedFacts) { fact in
                            Text("• \(fact.statement)")
                        }
                    }
                }
                if let nextAction = state.nextAction {
                    DetailBlock(title: "Next action", text: nextAction)
                }
            }
            .padding(.top, 8)
        } label: {
            Label("Working state", systemImage: "checklist")
                .font(.headline)
        }
        .padding(12)
        .background(Color.secondary.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 12))
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
                Text("Result").font(.caption.bold()).foregroundColor(.secondary)
                Spacer()
                if let trust = result.trust {
                    Text(trust.rawValue.replacingOccurrences(of: "_", with: " ").uppercased())
                        .font(.caption2)
                        .foregroundColor(trust == .externalUntrusted ? .orange : .secondary)
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
                        Text(text).textSelection(.enabled)
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
                Text(stderr).foregroundColor(.red)
            }
            if let exitCode = result.structured?["exit_code"]?.intValue {
                Text("exit_code: \(exitCode)").foregroundColor(.secondary)
            }
            if let files = result.structured?["files_changed"]?.arrayValue, !files.isEmpty {
                Text("files_changed: \(JSONValue.array(files).prettyPrinted)")
                    .foregroundColor(.secondary)
            }
        }
        .font(.system(.caption, design: .monospaced))
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.black.opacity(0.88))
        .foregroundColor(.white)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .textSelection(.enabled)
    }

    private var filePreview: some View {
        let text =
            result.structured?["content"]?.stringValue
            ?? result.content.compactMap(\.text).joined(separator: "\n")
        return Text(text)
            .font(.system(.caption, design: .monospaced))
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.secondary.opacity(0.08))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .textSelection(.enabled)
    }
}

struct DetailBlock: View {
    let title: String
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.caption.bold()).foregroundColor(.secondary)
            Text(text)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

private struct KeyValueRow: View {
    let key: String
    let value: String

    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(key).font(.caption).foregroundColor(.secondary)
            Spacer()
            Text(value).font(.caption)
        }
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
        case .critical: color = .red
        case .high: color = .orange
        case .medium: color = .yellow
        case .low: color = .green
        case nil: color = .secondary
        }
    }
}
