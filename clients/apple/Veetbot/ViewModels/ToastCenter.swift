import Foundation

/// The A2 toast: one short sentence that confirms what just happened. One is
/// shown at a time and a new one replaces the last. A confirmation leaves
/// after 2.2 s; a toast with Undo, offered only where the server can reverse
/// the change, stays 6 s and holds while the pointer or focus rests on it.
/// Failures never toast: the view model that owns the change keeps its error
/// beside the control that failed.
///
/// Every window and sheet mounts a host (`.toastHost()`), and only the
/// topmost one draws, so a toast raised under a sheet appears in the sheet.
@MainActor
public final class ToastCenter: ObservableObject {
    public struct Undo {
        public let label: String
        let restored: String
        let action: @MainActor () async -> Bool
    }

    public struct Toast: Identifiable {
        public let id = UUID()
        public let text: String
        public let undo: Undo?
    }

    public static let confirmationDuration: Duration = .milliseconds(2200)
    public static let undoDuration: Duration = .seconds(6)
    /// The app's one center; tests build their own.
    public static let shared = ToastCenter()

    @Published public private(set) var current: Toast?
    @Published private var hosts: [UUID] = []
    private var held = false
    private var undoing = false
    private var timer: Task<Void, Never>?
    private let sleep: @MainActor @Sendable (Duration) async throws -> Void

    public init(sleep: @escaping @MainActor @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }) {
        self.sleep = sleep
    }

    /// Confirms a change that has nothing to undo.
    public func show(_ text: String) {
        present(Toast(text: text, undo: nil))
    }

    /// Confirms a change the server can reverse. `undo` returns whether the
    /// reversal succeeded; on success the toast says what was restored.
    public func show(_ text: String, undoLabel: String = "Undo", restored: String,
                     undo: @escaping @MainActor () async -> Bool) {
        present(Toast(text: text, undo: Undo(label: undoLabel, restored: restored, action: undo)))
    }

    public func performUndo() async {
        guard let toast = current, let undo = toast.undo, !undoing else { return }
        undoing = true
        timer?.cancel()
        let succeeded = await undo.action()
        undoing = false
        // A newer toast raised while the undo ran wins.
        guard current?.id == toast.id else { return }
        if succeeded {
            held = false
            present(Toast(text: undo.restored, undo: nil))
        } else {
            current = nil
        }
    }

    /// Holds the toast while the pointer or keyboard focus rests on it.
    public func setHeld(_ held: Bool) {
        guard self.held != held else { return }
        self.held = held
        if held { timer?.cancel() } else if let current { schedule(current) }
    }

    public func dismiss() {
        timer?.cancel()
        current = nil
    }

    func registerHost(_ id: UUID) {
        hosts.removeAll { $0 == id }
        hosts.append(id)
    }

    func unregisterHost(_ id: UUID) {
        hosts.removeAll { $0 == id }
    }

    func isActiveHost(_ id: UUID) -> Bool { hosts.last == id }

    private func present(_ toast: Toast) {
        held = false
        current = toast
        schedule(toast)
    }

    private func schedule(_ toast: Toast) {
        timer?.cancel()
        let duration = toast.undo == nil ? Self.confirmationDuration : Self.undoDuration
        timer = Task { [weak self, sleep] in
            do { try await sleep(duration) } catch { return }
            guard let self, !Task.isCancelled, !self.held, !self.undoing,
                  self.current?.id == toast.id else { return }
            self.current = nil
        }
    }
}
