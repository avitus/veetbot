import Foundation
import Testing

@testable import VeetbotCore

/// The A2 toast: one short sentence at a time, gone after 2.2 s; an Undo
/// toast stays 6 s, holds while the pointer or focus is on it, and an undo
/// that succeeds replaces it with what was restored. Failures never toast.
@MainActor
@Suite struct ToastCenterTests {
    @Test
    func testAConfirmationShowsAndLeavesAfterItsDuration() async throws {
        let clock = ManualToastClock()
        let toasts = ToastCenter(sleep: clock.sleep)

        toasts.show("Marked as reviewed.")

        #expect(toasts.current?.text == "Marked as reviewed.")
        #expect(toasts.current?.undo == nil)
        let requested = await clock.nextRequest()
        #expect(requested == ToastCenter.confirmationDuration)
        clock.fire()
        await settle()
        #expect(toasts.current == nil)
    }

    @Test
    func testANewToastReplacesTheLastAndKeepsItsOwnTimer() async throws {
        let clock = ManualToastClock()
        let toasts = ToastCenter(sleep: clock.sleep)

        toasts.show("Saved.")
        _ = await clock.nextRequest()
        toasts.show("Deleted.")
        _ = await clock.nextRequest()
        clock.fire(first: true)
        await settle()

        #expect(toasts.current?.text == "Deleted.")
    }

    @Test
    func testUndoStaysLongerAndASuccessfulUndoSaysWhatWasRestored() async throws {
        let clock = ManualToastClock()
        let toasts = ToastCenter(sleep: clock.sleep)
        var undone = 0

        toasts.show("Marked handled.", restored: "Back in Needs you.") {
            undone += 1
            return true
        }

        #expect(toasts.current?.undo?.label == "Undo")
        #expect(await clock.nextRequest() == ToastCenter.undoDuration)
        await toasts.performUndo()

        #expect(undone == 1)
        #expect(toasts.current?.text == "Back in Needs you.")
        #expect(toasts.current?.undo == nil)
    }

    @Test
    func testAFailedUndoLeavesNoToastBehind() async throws {
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })

        toasts.show("Paused.", restored: "Resumed.") { false }
        await toasts.performUndo()

        // The view model that owned the change reports the failure inline.
        #expect(toasts.current == nil)
    }

    @Test
    func testHoldingAToastStopsItsTimerUntilReleased() async throws {
        let clock = ManualToastClock()
        let toasts = ToastCenter(sleep: clock.sleep)

        toasts.show("Merged.", restored: "Split.") { true }
        _ = await clock.nextRequest()
        toasts.setHeld(true)
        clock.fire(first: true)
        await settle()
        #expect(toasts.current?.text == "Merged.")

        toasts.setHeld(false)
        #expect(await clock.nextRequest() == ToastCenter.undoDuration)
        clock.fire()
        await settle()
        #expect(toasts.current == nil)
    }

    @Test
    func testOnlyTheTopmostHostShowsTheToast() {
        let toasts = ToastCenter(sleep: { _ in try await Task.sleep(nanoseconds: .max) })
        let window = UUID()
        let sheet = UUID()

        toasts.registerHost(window)
        toasts.registerHost(sheet)
        #expect(toasts.isActiveHost(sheet))
        #expect(!toasts.isActiveHost(window))

        toasts.unregisterHost(sheet)
        #expect(toasts.isActiveHost(window))
    }

    @Test
    func testTheHostIsMountedOnTheWindowAndEverySheet() throws {
        let root = try source(at: "Veetbot/Views/RootView.swift")
        let settings = try source(at: "Veetbot/Views/ConnectionSettingsView.swift")
        let memory = try source(at: "Veetbot/Views/MemoryBrowserView.swift")
        let people = try source(at: "Veetbot/Views/PeopleBrowserView.swift")
        let schedules = try source(at: "Veetbot/Views/ScheduleBrowserView.swift")
        let persona = try source(at: "Veetbot/Views/PersonaEditorView.swift")

        #expect(root.contains(".toastHost("))
        for sheet in [settings, memory, people, schedules, persona] {
            #expect(sheet.contains(".toastHost()"))
        }
    }

    private func settle() async {
        for _ in 0..<5 { await Task.yield() }
    }

    private func source(at relativePath: String) throws -> String {
        let packageRoot = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: packageRoot.appendingPathComponent(relativePath), encoding: .utf8)
    }
}

/// Hands each requested sleep back to the test, which decides when it ends.
@MainActor
final class ManualToastClock {
    private var waiters: [(Duration, CheckedContinuation<Void, Error>)] = []
    private var requestWaiters: [CheckedContinuation<Duration, Never>] = []
    private var unclaimed: [Duration] = []

    var sleep: @MainActor @Sendable (Duration) async throws -> Void {
        { [self] duration in
            try await withCheckedThrowingContinuation { continuation in
                waiters.append((duration, continuation))
                if requestWaiters.isEmpty {
                    unclaimed.append(duration)
                } else {
                    requestWaiters.removeFirst().resume(returning: duration)
                }
            }
        }
    }

    func nextRequest() async -> Duration {
        if !unclaimed.isEmpty { return unclaimed.removeFirst() }
        return await withCheckedContinuation { requestWaiters.append($0) }
    }

    /// Ends the newest sleep, or the oldest when `first` is set.
    func fire(first: Bool = false) {
        guard !waiters.isEmpty else { return }
        let waiter = first ? waiters.removeFirst() : waiters.removeLast()
        waiter.1.resume()
    }
}
