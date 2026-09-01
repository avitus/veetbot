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

        public init(
            id: UUID = UUID(),
            text: String,
            sensitivity: String = "internal",
            sourceBeliefID: UUID? = nil
        ) {
            self.id = id
            self.text = text
            self.sensitivity = sensitivity
            self.sourceBeliefID = sourceBeliefID
        }
    }

    @Published public private(set) var version = 0
    @Published public var drafts: [DraftEntry] = []
    @Published public private(set) var nominations: [PersonaNominationView] = []
    @Published public private(set) var isLoading = false
    @Published public private(set) var isSaving = false
    @Published public private(set) var errorMessage: String?
    @Published public private(set) var unavailable = false
    /// A save lost the version race: the server document moved underneath
    /// the drafts. The drafts are retained so the owner can merge by hand
    /// after a reload; nothing is overwritten and nothing is thrown away.
    @Published public private(set) var conflictDetected = false

    private let makeAPIClient: @Sendable () async -> VeetbotAPIClient?

    public init(
        makeAPIClient: @escaping @Sendable () async -> VeetbotAPIClient? = {
            await MemoryViewModel.makeDefaultAPIClient()
        }
    ) {
        self.makeAPIClient = makeAPIClient
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
            nominations = try await client.listPersonaNominations(state: "nominated").items
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
                        sourceBeliefID: $0.sourceBeliefID
                    )
                }
            )
            apply(document)
            conflictDetected = false
        } catch let HTTPTransportError.api(error) where error.code == .conflict {
            conflictDetected = true
            errorMessage = error.message
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    /// Re-read the server head after a conflict; the drafts stay as typed.
    public func reloadAfterConflict() async {
        guard let client = await makeAPIClient() else { return }
        do {
            let document = try await client.getPersona()
            version = document.version
            conflictDetected = false
        } catch {
            errorMessage = Self.describe(error)
        }
    }

    public func affirm(_ nominationID: UUID) async {
        guard let client = await makeAPIClient() else { return }
        errorMessage = nil
        do {
            let document = try await client.affirmPersonaNomination(nominationID)
            apply(document)
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
            nominations = try await client.listPersonaNominations(state: "nominated").items
        } catch {
            errorMessage = Self.describe(error)
        }
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
                sourceBeliefID: $0.sourceBeliefID
            )
        }
    }

    private static func describe(_ error: Error) -> String {
        if case let HTTPTransportError.api(apiError) = error {
            return apiError.message
        }
        return error.localizedDescription
    }
}
