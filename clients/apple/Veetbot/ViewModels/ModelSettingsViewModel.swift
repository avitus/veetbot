import Combine
import Foundation

/// One row of a model picker: a model policy and the name the owner sees.
public struct ModelPickerChoice: Hashable, Identifiable, Sendable {
    public let modelPolicy: String
    public let displayName: String

    public var id: String { modelPolicy }
}

/// The owner's chat and memory model choices (`GET`/`PUT /v1/settings/models`).
/// The resource is versioned as a whole: every picker change is one PUT of
/// the full state naming the version it read. A lost version race reloads
/// and keeps the server's values; any other failure reverts the pickers to
/// the last state the server confirmed.
@MainActor
public final class ModelSettingsViewModel: ObservableObject {
    public static let unavailableMessage = "This server doesn't support model settings yet."
    public static let scopeMessage =
        "Model settings need the settings.read and settings.write scopes on the server."
    public static let conflictMessage = "Settings changed elsewhere; reloaded."

    /// The last state the server confirmed; the save precondition and the
    /// revert target.
    @Published public private(set) var settings: ModelSettingsView?
    /// What the pickers show: the confirmed state, or a pending save's values.
    @Published public private(set) var chat: ModelChoice?
    @Published public private(set) var memory: ModelChoice?
    @Published public private(set) var isLoading = false
    @Published public private(set) var isSaving = false
    /// The server predates the resource; the pickers are hidden.
    @Published public private(set) var unavailable = false
    @Published public private(set) var statusMessage: String?

    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?

    public init(
        makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = {
            await MemoryViewModel.makeDefaultAPIClient()
        }
    ) {
        self.makeAPIClient = makeAPIClient
    }

    /// Pickers accept changes only with a confirmed state and no request in flight.
    public var isEditable: Bool {
        settings != nil && !unavailable && !isSaving && !isLoading
    }

    public var chatOptions: [ChatModelOption] { settings?.chatOptions ?? [] }

    public var chatModelChoices: [ModelPickerChoice] {
        let choices = chatOptions.map {
            ModelPickerChoice(modelPolicy: $0.modelPolicy, displayName: $0.displayName)
        }
        return Self.including(chat?.modelPolicy, in: choices)
    }

    /// The efforts the selected chat model accepts; empty when it takes none.
    public var chatEffortChoices: [ReasoningEffort] {
        guard let chat else { return [] }
        return chatOptions.first { $0.modelPolicy == chat.modelPolicy }?.reasoningEfforts ?? []
    }

    public var chatEffortPickerValues: [ReasoningEffort?] {
        Self.including(chat?.reasoningEffort, in: chatEffortChoices.map { Optional($0) })
    }

    /// Distinct memory models in the order the server first lists them.
    public var memoryModelChoices: [ModelPickerChoice] {
        var seen: Set<String> = []
        var choices: [ModelPickerChoice] = []
        for option in settings?.memoryOptions ?? [] where seen.insert(option.modelPolicy).inserted {
            choices.append(
                ModelPickerChoice(modelPolicy: option.modelPolicy, displayName: option.displayName)
            )
        }
        return Self.including(memory?.modelPolicy, in: choices)
    }

    /// The evaluated efforts for the selected memory model, in server order.
    public var memoryEffortChoices: [ReasoningEffort?] {
        guard let memory else { return [] }
        return (settings?.memoryOptions ?? [])
            .filter { $0.modelPolicy == memory.modelPolicy }
            .map(\.reasoningEffort)
    }

    public var memoryEffortPickerValues: [ReasoningEffort?] {
        guard let memory else { return [] }
        return Self.including(memory.reasoningEffort, in: memoryEffortChoices)
    }

    public func load() async {
        guard !isSaving else { return }
        isLoading = true
        defer { isLoading = false }
        guard let client = await makeAPIClient() else {
            clear()
            statusMessage = HTTPTransportError.notConfigured.errorDescription
            return
        }
        do {
            apply(try await client.getModelSettings())
            statusMessage = nil
        } catch {
            handleReadFailure(error)
        }
    }

    /// A new chat model starts from its default effort when it lists that
    /// default, else its first effort, else none.
    public func selectChatModel(_ modelPolicy: String) async {
        guard isEditable, let chat, let memory, modelPolicy != chat.modelPolicy,
            let option = chatOptions.first(where: { $0.modelPolicy == modelPolicy })
        else { return }
        await commit(
            chat: ModelChoice(modelPolicy: modelPolicy, reasoningEffort: option.initialReasoningEffort),
            memory: memory
        )
    }

    public func selectChatEffort(_ effort: ReasoningEffort?) async {
        guard isEditable, let chat, let memory, effort != chat.reasoningEffort,
            let effort, chatEffortChoices.contains(effort)
        else { return }
        await commit(
            chat: ModelChoice(modelPolicy: chat.modelPolicy, reasoningEffort: effort),
            memory: memory
        )
    }

    /// A new memory model takes its first evaluated combination.
    public func selectMemoryModel(_ modelPolicy: String) async {
        guard isEditable, let chat, let memory, modelPolicy != memory.modelPolicy,
            let first = settings?.memoryOptions.first(where: { $0.modelPolicy == modelPolicy })
        else { return }
        await commit(chat: chat, memory: first.choice)
    }

    /// Only an evaluated combination for the selected memory model is sent.
    public func selectMemoryEffort(_ effort: ReasoningEffort?) async {
        guard isEditable, let chat, let memory, effort != memory.reasoningEffort,
            memoryEffortChoices.contains(effort)
        else { return }
        await commit(
            chat: chat,
            memory: ModelChoice(modelPolicy: memory.modelPolicy, reasoningEffort: effort)
        )
    }

    private func commit(chat newChat: ModelChoice, memory newMemory: ModelChoice) async {
        guard let confirmed = settings, !isSaving else { return }
        isSaving = true
        statusMessage = nil
        chat = newChat
        memory = newMemory
        defer { isSaving = false }
        guard let client = await makeAPIClient() else {
            apply(confirmed)
            statusMessage = HTTPTransportError.notConfigured.errorDescription
            return
        }
        do {
            apply(
                try await client.updateModelSettings(
                    expectedVersion: confirmed.version, chat: newChat, memory: newMemory
                )
            )
        } catch let HTTPTransportError.api(error)
            where error.code == .conflict || error.statusCode == 409
        {
            await reloadAfterConflict(client: client, fallback: confirmed)
        } catch VeetbotAPIClientError.modelSettingsUnavailable {
            markUnavailable()
        } catch HTTPTransportError.authorizationDenied {
            apply(confirmed)
            statusMessage = Self.scopeMessage
        } catch {
            apply(confirmed)
            statusMessage = Self.describe(error)
        }
    }

    /// Another client saved first: adopt its values, never re-send ours.
    private func reloadAfterConflict(client: VeetbotAPIClient, fallback: ModelSettingsView) async {
        do {
            apply(try await client.getModelSettings())
            statusMessage = Self.conflictMessage
        } catch VeetbotAPIClientError.modelSettingsUnavailable {
            markUnavailable()
        } catch HTTPTransportError.authorizationDenied {
            apply(fallback)
            statusMessage = Self.scopeMessage
        } catch {
            apply(fallback)
            statusMessage = Self.describe(error)
        }
    }

    private func handleReadFailure(_ error: Error) {
        switch error {
        case VeetbotAPIClientError.modelSettingsUnavailable:
            markUnavailable()
        case HTTPTransportError.authorizationDenied:
            clear()
            statusMessage = Self.scopeMessage
        default:
            statusMessage = Self.describe(error)
        }
    }

    private func apply(_ confirmed: ModelSettingsView) {
        settings = confirmed
        chat = confirmed.chat
        memory = confirmed.memory
        unavailable = false
    }

    private func markUnavailable() {
        clear()
        unavailable = true
        statusMessage = Self.unavailableMessage
    }

    private func clear() {
        settings = nil
        chat = nil
        memory = nil
    }

    /// Keeps a stored value that is no longer offered visible in its picker
    /// rather than leaving the picker blank.
    private static func including(
        _ current: String?, in choices: [ModelPickerChoice]
    ) -> [ModelPickerChoice] {
        guard let current, !choices.contains(where: { $0.modelPolicy == current }) else {
            return choices
        }
        return [ModelPickerChoice(modelPolicy: current, displayName: current)] + choices
    }

    private static func including(
        _ current: ReasoningEffort?, in choices: [ReasoningEffort?]
    ) -> [ReasoningEffort?] {
        choices.contains(current) ? choices : [current] + choices
    }

    private static func describe(_ error: Error) -> String {
        if case let HTTPTransportError.api(apiError) = error {
            return apiError.message
        }
        return error.localizedDescription
    }
}
