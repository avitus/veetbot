import Combine
import Foundation

/// Edits the owner's persona document over the guarded write surface
/// (persona-surface.md). The document is versioned as a whole: every save
/// names the version it read, and a concurrent edit surfaces as a
/// reload-and-merge state rather than silent data loss.
@MainActor
public final class PersonaViewModel: ObservableObject {
    public struct DraftEntry: Identifiable, Equatable, Sendable {
        public let id: UUID
        public var text: String
        public var sensitivity: String
        /// Set exactly for a kept affirmation entry; an owner edit may keep,
        /// reword, or drop promoted provenance, never mint it.
        public let sourceBeliefID: UUID?
        /// ADR-0226: off keeps the entry but stops Veetbot treating it as a
        /// standing instruction.
        public var enabled: Bool
        /// The memory it was affirmed from has since changed.
        public let stale: Bool

        public init(
            id: UUID = UUID(),
            text: String,
            sensitivity: String = "internal",
            sourceBeliefID: UUID? = nil,
            enabled: Bool = true,
            stale: Bool = false
        ) {
            self.id = id
            self.text = text
            self.sensitivity = sensitivity
            self.sourceBeliefID = sourceBeliefID
            self.enabled = enabled
            self.stale = stale
        }
    }

    /// The A2 "Lean towards" chips, in the order they are shown.
    public static let traitChoices = ["brief", "warm", "direct", "formal", "playful"]

    /// A sample reply and what it is: a generated example, or, when the server
    /// cannot generate one, an illustration of the chips alone (ADR-0226).
    public struct Sample: Equatable, Sendable {
        public let prompt: String
        public let reply: String
        public let label: String
    }

    @Published public private(set) var version = 0
    @Published public var drafts: [DraftEntry] = []
    /// ADR-0226: how Veetbot should talk, and up to three tones.
    @Published public var voice = ""
    @Published public private(set) var traits: [String] = []
    /// The server reported voice and traits; an older one is never sent them.
    @Published public private(set) var toneSupported = false
    @Published public private(set) var sample: Sample?
    @Published public private(set) var isLoadingSample = false
    /// The active rules from the server; empty on a server without the route.
    @Published public private(set) var boundaries: [PersonaBoundaryView] = []
    @Published public private(set) var nominations: [PersonaNominationView] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var isSaving = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var unavailable = false
    /// A save lost the version race: the server document moved underneath
    /// the drafts. The drafts are retained so the owner can merge by hand
    /// after a reload; nothing is overwritten and nothing is thrown away.
    @Published public private(set) var conflictDetected = false
    /// The server document that won a version race. It remains separate from
    /// the owner's local drafts so the two versions can be compared and
    /// merged without silently replacing either one.
    @Published public private(set) var conflictHead: PersonaView?
    @Published private var mergeRequired = false

    public var hasPendingMerge: Bool { mergeRequired }

    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?
    private let toasts: ToastCenter

    public init(
        makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = {
            await MemoryViewModel.makeDefaultAPIClient()
        },
        toasts: ToastCenter? = nil
    ) {
        self.makeAPIClient = makeAPIClient
        self.toasts = toasts ?? .shared
    }

    public func load() async {
        guard let client = await makeAPIClient() else {
            unavailable = true
            return
        }
        isLoading = true
        errorMessage = nil
        defer { isLoading = false }
        do {
            let document = try await client.getPersona()
            apply(document)
            conflictHead = nil
            mergeRequired = false
            conflictDetected = false
            nominations = try await client.listPersonaNominations(state: "nominated").items
            // Rules are listed only as the server states them; none otherwise.
            boundaries = ((try? await client.personaBoundaries()) ?? []).filter(\.active)
            unavailable = false
        } catch let HTTPTransportError.api(error) where error.code == .notFound {
            // An older server, or the surface's default-off flag: the sheet
            // degrades to an explanation rather than an error banner.
            unavailable = true
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    public func save() async {
        guard !hasPendingMerge else {
            errorMessage = "Resolve the pending persona merge before saving."
            return
        }
        guard let client = await makeAPIClient() else {
            unavailable = true
            return
        }
        isSaving = true
        errorMessage = nil
        defer { isSaving = false }
        do {
            let document = try await client.updatePersona(
                expectedVersion: version,
                entries: drafts.map {
                    UpdatePersonaEntryBody(
                        text: $0.text,
                        sensitivity: $0.sensitivity,
                        sourceBeliefID: $0.sourceBeliefID,
                        enabled: $0.enabled
                    )
                },
                tone: toneSupported ? UpdatePersonaBody.Tone(voice: trimmedVoice, traits: traits) : nil
            )
            apply(document)
            conflictHead = nil
            mergeRequired = false
            conflictDetected = false
            toasts.show("Persona saved.")
        } catch let HTTPTransportError.api(error) where error.code == .conflict {
            mergeRequired = true
            conflictDetected = true
            errorMessage = error.message
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    /// Re-read the server head after a conflict. The winning document is kept
    /// separately for comparison, while the local drafts and their original
    /// version remain untouched until the owner resolves the merge.
    public func reloadAfterConflict() async {
        guard let client = await makeAPIClient() else { return }
        do {
            let document = try await client.getPersona()
            conflictHead = document
            conflictDetected = false
            errorMessage = nil
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    /// Confirm that the visible local drafts include the owner's intended
    /// merge. Only this explicit action advances their write precondition to
    /// the server head that was shown alongside them.
    public func resolveConflictKeepingDrafts() {
        guard let conflictHead else { return }
        version = conflictHead.version
        self.conflictHead = nil
        mergeRequired = false
        conflictDetected = false
        errorMessage = nil
    }

    /// Discard the local side of a conflict and adopt the displayed server
    /// document exactly.
    public func useConflictHead() {
        guard let conflictHead else { return }
        apply(conflictHead)
        self.conflictHead = nil
        mergeRequired = false
        conflictDetected = false
        errorMessage = nil
    }

    public func dismissConflictAlert() {
        conflictDetected = false
    }

    public func affirm(_ nominationID: UUID) async {
        guard let client = await makeAPIClient() else { return }
        errorMessage = nil
        do {
            let document = try await client.affirmPersonaNomination(nominationID)
            apply(document)
            toasts.show("Added to what Veetbot assumes about you.")
            nominations = try await client.listPersonaNominations(state: "nominated").items
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    public func decline(_ nominationID: UUID) async {
        guard let client = await makeAPIClient() else { return }
        errorMessage = nil
        do {
            _ = try await client.declinePersonaNomination(nominationID)
            // A decline is durable (ADR-0079): the same belief is not nominated again.
            toasts.show("Declined. Veetbot will not suggest this again.")
            nominations = try await client.listPersonaNominations(state: "nominated").items
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    /// Chooses or clears a tone: at most three, and formal and playful
    /// replace each other because they pull opposite ways.
    public func toggleTrait(_ trait: String) {
        if let index = traits.firstIndex(of: trait) {
            traits.remove(at: index)
            return
        }
        let opposite = ["formal": "playful", "playful": "formal"][trait]
        if let opposite, let index = traits.firstIndex(of: opposite) {
            traits[index] = trait
            return
        }
        guard traits.count < 3 else { return }
        traits.append(trait)
    }

    /// Asks the server how the unsaved voice sounds. A server that cannot
    /// generate one gets a labelled illustration of the chips instead, which
    /// never pretends to reflect the voice text.
    public func loadSample() async {
        guard !isLoadingSample else { return }
        isLoadingSample = true
        defer { isLoadingSample = false }
        if let client = await makeAPIClient(),
           let preview = try? await client.previewPersona(voice: trimmedVoice, traits: traits)
        {
            sample = Sample(prompt: preview.prompt, reply: preview.reply, label: "Example — generated, not saved")
            return
        }
        sample = Sample(prompt: Self.samplePrompt, reply: Self.illustration(for: traits), label: "Illustration")
    }

    static let samplePrompt = "Can you move my 3 pm to tomorrow?"

    /// A fixed reply per tone; it reflects the chips only.
    static func illustration(for traits: [String]) -> String {
        if traits.contains("formal") { return "Certainly. I can move your 3 pm meeting to tomorrow at the same time. Shall I proceed?" }
        if traits.contains("playful") { return "Sure thing — 3 pm, meet tomorrow! Want me to make it official?" }
        if traits.contains("brief") { return "Yes. Tomorrow at 3?" }
        if traits.contains("direct") { return "Yes — moving it to tomorrow at 3. Confirm and I'll send the update." }
        if traits.contains("warm") { return "Of course! Tomorrow at 3 works. I'll let everyone know, if you'd like." }
        return "I can move it to tomorrow at 3 pm. Would you like me to?"
    }

    private var trimmedVoice: String? {
        let text = voice.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }

    public func addDraft() {
        drafts.append(DraftEntry(text: ""))
    }

    public func removeDraft(_ id: UUID) {
        drafts.removeAll { $0.id == id }
    }

    private func apply(_ document: PersonaView) {
        version = document.version
        drafts = document.entries.map {
            DraftEntry(
                text: $0.text,
                sensitivity: $0.sensitivity,
                sourceBeliefID: $0.sourceBeliefID,
                enabled: $0.enabled,
                stale: $0.stale
            )
        }
        toneSupported = document.traits != nil
        voice = document.voice ?? ""
        traits = document.traits ?? []
    }

    private static func describe(_ error: Error) -> String {
        if case let HTTPTransportError.api(apiError) = error {
            return apiError.message
        }
        return error.localizedDescription
    }
}
