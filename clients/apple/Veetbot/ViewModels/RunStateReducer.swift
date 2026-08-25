import Combine
import Foundation

public struct TimelineItem: Identifiable, Sendable {
    public enum Role: Sendable { case user, assistant }

    public let id: String
    public let role: Role
    public var content: [ContentBlock]
    public var isStreaming: Bool

    public init(id: String, role: Role, content: [ContentBlock], isStreaming: Bool = false) {
        self.id = id
        self.role = role
        self.content = content
        self.isStreaming = isStreaming
    }

    public var text: String {
        content.compactMap(\.text).joined(separator: "\n")
    }
}

public enum ToolActivityStatus: String, Sendable {
    case queued
    case running
    case awaitingApproval = "awaiting approval"
    case completed
    case needsCorrection = "needs correction"
    case correctedAndRetried = "corrected and retried"
    case failed
    case denied
    case uncertain
}

public struct ToolActivity: Identifiable, Sendable {
    public let callID: String
    public var name: String
    public var status: ToolActivityStatus
    public var arguments: [String: JSONValue]
    public var result: ToolResultView?
    public var sideEffect: SideEffectClass?
    public var risk: RiskLevel?
    public var approvalID: UUID?
    fileprivate var hasKnownName: Bool

    public var id: String { callID }
    fileprivate var isBundleCandidate: Bool {
        hasKnownName && status == .completed && approvalID == nil && result?.isError != true
    }
}

public struct ToolActivityBundle: Identifiable, Sendable {
    public let activities: [ToolActivity]

    public init?(activities: [ToolActivity]) {
        guard activities.count > 1, let first = activities.first,
            first.isBundleCandidate,
            activities.dropFirst().allSatisfy({
                $0.isBundleCandidate && $0.name == first.name
            })
        else { return nil }
        self.activities = activities
    }

    public var id: String { activities[0].callID }
    public var name: String { activities[0].name }
    public var count: Int { activities.count }
    public var highestRisk: RiskLevel? {
        activities.compactMap(\.risk).max {
            Self.riskRank($0) < Self.riskRank($1)
        }
    }
    public var summary: String {
        "\(count) \(Self.pluralizedDisplayName(name)) Completed"
    }

    private static func riskRank(_ risk: RiskLevel) -> Int {
        switch risk {
        case .low: 0
        case .medium: 1
        case .high: 2
        case .critical: 3
        }
    }

    private static func pluralizedDisplayName(_ name: String) -> String {
        var segments = name.split(separator: ".", omittingEmptySubsequences: false).map(String.init)
        guard let action = segments.popLast() else { return name }
        var words = action.split(separator: "_", omittingEmptySubsequences: false).map(String.init)
        guard let finalWord = words.popLast() else { return name }
        words.append(pluralized(finalWord))
        segments.append(words.map(displaySegment).joined(separator: "_"))
        return segments.map(displaySegment).joined(separator: ".")
    }

    private static func pluralized(_ word: String) -> String {
        let lowercased = word.lowercased()
        if lowercased.hasSuffix("ch") || lowercased.hasSuffix("sh")
            || lowercased.hasSuffix("s") || lowercased.hasSuffix("x")
            || lowercased.hasSuffix("z")
        {
            return word + "es"
        }
        if lowercased.hasSuffix("y"), lowercased.count > 1 {
            let preceding = lowercased[lowercased.index(lowercased.endIndex, offsetBy: -2)]
            if !"aeiou".contains(preceding) {
                return String(word.dropLast()) + "ies"
            }
        }
        return word + "s"
    }

    private static func displaySegment(_ segment: String) -> String {
        guard let first = segment.first else { return segment }
        return first.uppercased() + segment.dropFirst()
    }
}

public enum ConversationActivity: Identifiable, Sendable {
    case message(TimelineItem)
    case tool(ToolActivity)
    case toolBundle(ToolActivityBundle)

    public var id: String {
        switch self {
        case .message(let item): "message:\(item.id)"
        case .tool(let activity): "tool:\(activity.id)"
        case .toolBundle(let bundle): "tool:\(bundle.id)"
        }
    }
}

public struct ClarifyingQuestionPrompt: Identifiable, Sendable {
    public let questionID: UUID
    public let runID: UUID
    public let question: String

    public var id: UUID { questionID }
}

@MainActor
public final class RunStateReducer: ObservableObject {
    @Published public private(set) var timeline: [TimelineItem] = []
    @Published public private(set) var tools: [ToolActivity] = []
    @Published public private(set) var approvals: [ApprovalView] = []
    @Published public private(set) var workingState: WorkingStateView?
    @Published public private(set) var clarifyingQuestion: ClarifyingQuestionPrompt?
    @Published public private(set) var runStatus: RunStatus?
    @Published public private(set) var activeRunID: UUID?
    @Published public private(set) var reasoningActive = false
    @Published public private(set) var failure: RunFailureView?

    private var persistedSequences: Set<Int> = []
    private var streamingMessageID: String?
    private var toolIndex: [String: Int] = [:]
    private var pendingApprovalIDs: Set<UUID> = []
    private var activityOrder: [ConversationActivityReference] = []

    public init() {}

    public var isRunActive: Bool { runStatus?.isActive == true }
    public var needsApprovalIDs: [UUID] { Array(pendingApprovalIDs) }
    public var activityTimeline: [ConversationActivity] {
        let activities = activityOrder.compactMap { reference in
            switch reference {
            case .message(let id):
                return timeline.first(where: { $0.id == id }).map(ConversationActivity.message)
            case .tool(let callID):
                guard let index = toolIndex[callID], tools.indices.contains(index) else {
                    return nil
                }
                return .tool(tools[index])
            }
        }
        return bundleSuccessiveCompletedTools(activities)
    }

    public func reset() {
        timeline = []
        tools = []
        approvals = []
        workingState = nil
        clarifyingQuestion = nil
        runStatus = nil
        activeRunID = nil
        reasoningActive = false
        failure = nil
        persistedSequences = []
        streamingMessageID = nil
        toolIndex = [:]
        pendingApprovalIDs = []
        activityOrder = []
    }

    public func restore(messages: [SessionMessageView]) {
        reset()
        for message in messages.sorted(by: { $0.sequence < $1.sequence }) {
            guard persistedSequences.insert(message.sequence).inserted else { continue }
            let id = "event-\(message.sequence)"
            let role: TimelineItem.Role = message.role == .user ? .user : .assistant
            activityOrder.append(.message(id))
            timeline.append(
                TimelineItem(id: id, role: role, content: message.content)
            )
        }
    }

    public func seed(run: RunView) {
        activeRunID = run.id
        runStatus = run.status
        failure = run.failure
        if run.status != .waitingForUser { clarifyingQuestion = nil }
    }

    public func begin(runID: UUID, status: RunStatus) {
        activeRunID = runID
        runStatus = status
        failure = nil
    }

    public func reduce(_ frame: SSEFrame) {
        if let id = frame.id {
            guard persistedSequences.insert(id).inserted else { return }
        }
        let runID = frame.data["run_id"]?.stringValue.flatMap(UUID.init(uuidString:))
        if let runID { activeRunID = runID }

        switch frame.event {
        case "session.created":
            break
        case "user.message.created":
            appendUserMessage(frame)
        case "run.queued":
            runStatus = .queued
        case "run.started", "run.resumed", "run.claimed":
            runStatus = .running
        case "message.delta":
            appendDelta(frame.data["text"]?.stringValue)
        case "reasoning.delta", "reasoning.summary.delta":
            // Raw reasoning text is intentionally discarded at the reducer boundary.
            reasoningActive = true
        case "assistant.message.completed":
            reasoningActive = false
            if let message = frame.data["message"] {
                reconcileAssistantMessage(message, fallbackID: frameID(frame))
            }
        case "model.response.completed":
            reasoningActive = false
            reduceModelResponse(frame)
        case "tool.call.proposed":
            updateTool(from: frame, status: .queued)
        case "tool.call.authorized", "tool.call.started":
            updateTool(from: frame, status: .running)
        case "tool.call.completed":
            updateTool(from: frame, status: .completed)
        case "tool.call.failed":
            updateTool(from: frame, status: .failed)
        case "tool.call.denied":
            updateTool(from: frame, status: .denied)
        case "tool.call.uncertain":
            updateTool(from: frame, status: .uncertain)
        case "approval.requested":
            if let id = approvalID(from: frame.data) { pendingApprovalIDs.insert(id) }
        case "approval.resolved":
            if let id = approvalID(from: frame.data) { pendingApprovalIDs.remove(id) }
        case "context.working_state.updated":
            reduceWorkingState(frame.data["working_state"])
        case "run.waiting_for_approval":
            runStatus = .waitingForApproval
            if let id = approvalID(from: frame.data) { pendingApprovalIDs.insert(id) }
        case "run.waiting_for_user":
            runStatus = .waitingForUser
            reduceQuestion(frame, runID: runID)
        case "run.completed":
            if let final = frame.data["final_message"] {
                reconcileAssistantMessage(final, fallbackID: frameID(frame))
            }
            transitionToTerminal(.completed)
        case "run.failed":
            failure = frame.data["failure"].flatMap { decode(RunFailureView.self, from: $0) }
            transitionToTerminal(.failed)
        case "run.cancelled":
            transitionToTerminal(.cancelled)
        default:
            break
        }
    }

    public func mergeApproval(_ approval: ApprovalView) {
        if let index = approvals.firstIndex(where: { $0.id == approval.id }) {
            approvals[index] = approval
        } else {
            approvals.append(approval)
            approvals.sort { $0.createdAt < $1.createdAt }
        }
        if approval.status.isPending {
            pendingApprovalIDs.insert(approval.id)
        } else {
            pendingApprovalIDs.remove(approval.id)
        }
        let associatedCallID =
            tools.last(where: { $0.approvalID == approval.id })?.callID
            ?? tools.reversed().first(where: {
                $0.name == approval.toolName
                    && $0.approvalID == nil
                    && [.queued, .running, .awaitingApproval].contains($0.status)
            })?.callID
        if let callID = associatedCallID {
            updateTool(callID: callID) { tool in
                tool.status = approval.status.isPending ? .awaitingApproval : tool.status
                tool.arguments = approval.arguments
                tool.risk = RiskLevel(rawValue: approval.risk.lowercased())
                tool.approvalID = approval.id
            }
        }
    }

    public func removeApproval(_ approvalID: UUID) {
        pendingApprovalIDs.remove(approvalID)
        approvals.removeAll { $0.id == approvalID }
    }

    private func appendUserMessage(_ frame: SSEFrame) {
        guard let content = frame.data["content"].flatMap({ decode([ContentBlock].self, from: $0) })
        else { return }
        let id = frameID(frame)
        activityOrder.append(.message(id))
        timeline.append(
            TimelineItem(id: id, role: .user, content: content)
        )
    }

    private func appendDelta(_ text: String?) {
        guard let text, !text.isEmpty else { return }
        reasoningActive = false
        if let streamingMessageID,
            let index = timeline.firstIndex(where: { $0.id == streamingMessageID })
        {
            let previous = timeline[index].text
            timeline[index].content = [.text(previous + text)]
        } else {
            let id = "transient-assistant-\(UUID().uuidString)"
            streamingMessageID = id
            activityOrder.append(.message(id))
            timeline.append(
                TimelineItem(id: id, role: .assistant, content: [.text(text)], isStreaming: true)
            )
        }
    }

    private func reconcileAssistantMessage(_ value: JSONValue, fallbackID: String) {
        guard let message = decode(AssistantMessagePayload.self, from: value) else { return }
        if let streamingMessageID,
            let index = timeline.firstIndex(where: { $0.id == streamingMessageID })
        {
            timeline[index].content = message.content
            timeline[index].isStreaming = false
            self.streamingMessageID = nil
            return
        }
        guard !timeline.contains(where: { $0.role == .assistant && $0.content == message.content })
        else { return }
        activityOrder.append(.message(fallbackID))
        timeline.append(
            TimelineItem(id: fallbackID, role: .assistant, content: message.content)
        )
    }

    private func reduceModelResponse(_ frame: SSEFrame) {
        guard let items = frame.data["conversation_items"]?.arrayValue else { return }
        for item in items {
            guard let object = item.objectValue, object["kind"]?.stringValue == "tool_call" else {
                continue
            }
            let callID = object["call_id"]?.stringValue ?? "tool-\(tools.count)"
            let suppliedName = object["name"]?.stringValue
            let name = suppliedName ?? "tool"
            let arguments = object["arguments"]?.objectValue ?? [:]
            ensureTool(
                callID: callID,
                name: name,
                hasKnownName: suppliedName != nil,
                status: .queued
            )
            updateTool(callID: callID) { tool in
                tool.arguments = arguments
                if let suppliedName {
                    tool.name = suppliedName
                    tool.hasKnownName = true
                }
            }
        }
    }

    private func updateTool(from frame: SSEFrame, status: ToolActivityStatus) {
        let suppliedName =
            frame.data["name"]?.stringValue
            ?? frame.data["tool_name"]?.stringValue
        let name = suppliedName ?? "tool"
        let runID =
            frame.data["run_id"]?.stringValue
            ?? activeRunID?.uuidString
            ?? "unknown-run"
        let callID = frame.data["call_id"]?.stringValue ?? "tool-\(runID)-\(name)"
        let result = frame.data["result_item"].flatMap({
            decode(ToolResultPayload.self, from: $0)
        })
        let presentedStatus =
            status == .failed && result?.needsArgumentCorrection == true
            ? ToolActivityStatus.needsCorrection
            : status
        ensureTool(
            callID: callID,
            name: name,
            hasKnownName: suppliedName != nil,
            status: presentedStatus
        )
        updateTool(callID: callID) { tool in
            tool.status = presentedStatus
            if let suppliedName {
                tool.name = suppliedName
                tool.hasKnownName = true
            }
            if let arguments = frame.data["arguments"]?.objectValue {
                tool.arguments = arguments
            }
            if let value = frame.data["side_effect"]?.stringValue {
                tool.sideEffect = SideEffectClass(rawValue: value)
            }
            if let value = frame.data["risk"]?.stringValue {
                tool.risk = RiskLevel(rawValue: value.lowercased())
            }
            if let result {
                tool.result = ToolResultView(
                    content: result.content,
                    trust: result.trust,
                    isError: result.isError,
                    structured: frame.data["structured_result"]?.objectValue
                )
            }
        }
        if presentedStatus == .completed {
            markImmediatelyPreviousCorrectionRecovered(
                before: callID,
                toolName: toolIndex[callID].map { tools[$0].name } ?? name
            )
        }
    }

    private func markImmediatelyPreviousCorrectionRecovered(
        before callID: String,
        toolName: String
    ) {
        guard
            let currentActivityIndex = activityOrder.lastIndex(where: {
                guard case .tool(let candidateID) = $0 else { return false }
                return candidateID == callID
            }),
            currentActivityIndex > activityOrder.startIndex
        else { return }
        let previousActivityIndex = activityOrder.index(before: currentActivityIndex)
        guard
            case .tool(let previousCallID) = activityOrder[previousActivityIndex],
            let previousToolIndex = toolIndex[previousCallID],
            tools[previousToolIndex].name == toolName,
            tools[previousToolIndex].status == .needsCorrection
        else { return }
        tools[previousToolIndex].status = .correctedAndRetried
    }

    private func ensureTool(
        callID: String,
        name: String,
        hasKnownName: Bool,
        status: ToolActivityStatus
    ) {
        guard toolIndex[callID] == nil else { return }
        toolIndex[callID] = tools.count
        activityOrder.append(.tool(callID))
        tools.append(
            ToolActivity(
                callID: callID,
                name: name,
                status: status,
                arguments: [:],
                result: nil,
                sideEffect: nil,
                risk: nil,
                approvalID: nil,
                hasKnownName: hasKnownName
            )
        )
    }

    private func updateTool(callID: String, update: (inout ToolActivity) -> Void) {
        guard let index = toolIndex[callID], tools.indices.contains(index) else { return }
        update(&tools[index])
    }

    private func bundleSuccessiveCompletedTools(
        _ activities: [ConversationActivity]
    ) -> [ConversationActivity] {
        var bundled: [ConversationActivity] = []
        var pending: [ToolActivity] = []

        func flushPending() {
            guard !pending.isEmpty else { return }
            if pending.count == 1 {
                bundled.append(.tool(pending[0]))
            } else if let bundle = ToolActivityBundle(activities: pending) {
                bundled.append(.toolBundle(bundle))
            } else {
                bundled.append(contentsOf: pending.map(ConversationActivity.tool))
            }
            pending.removeAll(keepingCapacity: true)
        }

        for activity in activities {
            guard case .tool(let tool) = activity,
                tool.isBundleCandidate
            else {
                flushPending()
                bundled.append(activity)
                continue
            }
            if let first = pending.first, first.name != tool.name {
                flushPending()
            }
            pending.append(tool)
        }
        flushPending()
        return bundled
    }

    private func reduceWorkingState(_ value: JSONValue?) {
        guard let value, let state = decode(WorkingStateView.self, from: value) else { return }
        workingState = state
        if let prompt = clarifyingQuestion,
            !state.openQuestions.contains(prompt.question)
        {
            clarifyingQuestion = nil
        }
    }

    private func reduceQuestion(_ frame: SSEFrame, runID: UUID?) {
        let nested = frame.data["suspension"]?.objectValue
        guard
            let idString = frame.data["question_id"]?.stringValue
                ?? nested?["question_id"]?.stringValue,
            let questionID = UUID(uuidString: idString),
            let runID = runID ?? activeRunID
        else { return }
        let question =
            frame.data["question"]?.stringValue
            ?? workingState?.openQuestions.last
            ?? "The agent needs more information."
        clarifyingQuestion = ClarifyingQuestionPrompt(
            questionID: questionID,
            runID: runID,
            question: question
        )
    }

    private func transitionToTerminal(_ status: RunStatus) {
        runStatus = status
        reasoningActive = false
        clarifyingQuestion = nil
        pendingApprovalIDs.removeAll()
    }

    private func approvalID(from data: [String: JSONValue]) -> UUID? {
        let direct = data["approval_id"]?.stringValue
        let nested = data["suspension"]?.objectValue?["approval_id"]?.stringValue
        return (direct ?? nested).flatMap(UUID.init(uuidString:))
    }

    private func frameID(_ frame: SSEFrame) -> String {
        frame.id.map { "event-\($0)" } ?? "transient-\(UUID().uuidString)"
    }

    private func decode<Value: Decodable>(_ type: Value.Type, from value: JSONValue) -> Value? {
        guard let data = try? JSONEncoder.server.encode(value) else { return nil }
        return try? JSONDecoder.server.decode(Value.self, from: data)
    }
}

private enum ConversationActivityReference {
    case message(String)
    case tool(String)
}

private struct AssistantMessagePayload: Decodable {
    let content: [ContentBlock]
}

private struct ToolResultPayload: Decodable {
    let content: [ContentBlock]
    let isError: Bool
    let trust: TrustLabel?

    var needsArgumentCorrection: Bool {
        guard
            isError,
            let text = content.compactMap(\.text).first,
            let data = text.data(using: .utf8),
            let outcome = try? JSONDecoder.server.decode(ToolOutcomePayload.self, from: data)
        else { return false }
        return outcome.status == "failed"
            && outcome.retryable
            && outcome.remediation == "modify_arguments"
            && (outcome.reasonCode == "tool.arguments_invalid"
                || outcome.reasonCode.hasPrefix("tool.invalid_arguments."))
    }

    enum CodingKeys: String, CodingKey {
        case content, trust
        case isError = "is_error"
    }
}

private struct ToolOutcomePayload: Decodable {
    let status: String
    let reasonCode: String
    let retryable: Bool
    let remediation: String

    enum CodingKeys: String, CodingKey {
        case status, retryable, remediation
        case reasonCode = "reason_code"
    }
}
