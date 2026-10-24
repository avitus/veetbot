import Foundation

/// The client's three modes, in the order the switch reads them: Cockpit ·
/// Chat · Email (docs/plan/cockpit.md, "The mode shell"; the owner set the
/// order on 2026-10-22). Every surface that depends on the mode switches over
/// all three, so none silently drops one.
public enum ClientMode: String, CaseIterable, Identifiable, Sendable {
    case cockpit, chat, email

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .cockpit: "Cockpit"
        case .chat: "Chat"
        case .email: "Email"
        }
    }

    public var symbol: String {
        switch self {
        case .cockpit: "gauge.with.needle"
        case .chat: "bubble.left.and.bubble.right"
        case .email: "envelope"
        }
    }
}
