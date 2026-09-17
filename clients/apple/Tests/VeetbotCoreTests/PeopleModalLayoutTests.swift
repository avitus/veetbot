#if os(macOS)
import Foundation
import SwiftUI
import Testing

@testable import VeetbotCore

/// macOS sizes a presented sheet from its content's frame, not from the
/// content's unconstrained fitting size, so a minimum-only frame opened People
/// modals too narrow to read conversation titles, transcripts, and evidence.
/// These present each modal as a real sheet and measure the sheet window.
@Suite(.serialized) @MainActor struct PeopleModalLayoutTests {
    private static let formWidth: CGFloat = 560
    private static let conversationWidth: CGFloat = 680

    @Test func importHistoryOpensWideEnoughToReadConversations() async throws {
        let width = try await presentedSheetWidth { PeopleImportHistoryView(initialSessionID: nil) }
        #expect(width >= Self.conversationWidth)
    }

    @Test func sourceConversationOpensWideEnoughToReadTheTranscript() async throws {
        let width = try await presentedSheetWidth { PeopleConversationView(selection: PeopleConversationSelection()) }
        #expect(width >= Self.conversationWidth)
    }

    @Test func identityRepairOpensWideEnoughToReadEvidence() async throws {
        let model = detailModel()
        let width = try await presentedSheetWidth { PeopleIdentityRepairView(model: model, sessionID: nil) }
        #expect(width >= Self.conversationWidth)
    }

    @Test func addPersonOpensAtAReadableFormWidth() async throws {
        let width = try await presentedSheetWidth { AddPersonView(sessionID: nil) { _ in } }
        #expect(width >= Self.formWidth)
    }

    @Test func aliasEditorOpensAtAReadableFormWidth() async throws {
        let model = detailModel()
        let width = try await presentedSheetWidth { PeopleAliasEditor(model: model, sessionID: nil) }
        #expect(width >= Self.formWidth)
    }

    @Test func factEditorOpensAtAReadableFormWidth() async throws {
        let model = detailModel()
        let fact = try JSONDecoder.server.decode(MemoryView.self, from: Data(Self.factJSON.utf8))
        let width = try await presentedSheetWidth { PeopleFactEditor(model: model, fact: fact, sessionID: nil) }
        #expect(width >= Self.formWidth)
    }

    private func detailModel() -> PeopleDetailViewModel {
        PeopleDetailViewModel(personID: UUID(), notifications: NotificationCenter(), makeAPIClient: { nil })
    }

    private static let factJSON = """
    {"id":"00000000-0000-0000-0000-000000000101","subject":"Ada","statement":"Ada works at the observatory.","belief_type":"fact","claim_kind":"fact","derivation":"direct","longevity":"durable","status":"active","polarity":"assert","scope":"session","portability":"portable","authority":"user","sensitivity":"restricted","confidence":0.9,"corroboration_count":1,"flagged_for_review":false,"conflicts_with":[],"superseded_by":null,"source_session_id":"00000000-0000-0000-0000-000000000103","source_event_ids":[10],"formation_run_id":"00000000-0000-0000-0000-000000000900","consolidation_policy_version":"formation@1","origin_scopes":["session"],"valid_from":"2026-08-01T00:00:00Z","valid_to":null,"expires_at":null,"last_evidence_at":"2026-08-15T00:00:00Z","last_used_at":null,"last_reinforced_at":"2026-08-15T00:00:00Z","created_at":"2026-07-01T00:00:00Z","updated_at":"2026-08-20T00:00:00Z"}
    """
}
#endif
