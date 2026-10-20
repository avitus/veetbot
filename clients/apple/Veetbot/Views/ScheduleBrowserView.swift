import Foundation
import SwiftUI

#if os(macOS)
import AppKit
#endif

/// Read-only schedule index over the existing principal-scoped control plane,
/// drawn as an A2 list: a calendar glyph, the name, the preview in soft ink,
/// and the cadence with the next run on one line.
public struct ScheduleBrowserView: View {
    @ObservedObject var model: ScheduleViewModel
    @State private var creating = false
    @Environment(\.dismiss) private var dismiss
    @State private var section = ScheduleBrowserSection.current

    public init(model: ScheduleViewModel) {
        self.model = model
    }

    public var body: some View {
        navigationContainer
        .tint(AppTheme.tealFill)
        .accessibilityIdentifier("schedule.browser")
        .toastHost()
        .task(id: section) { await model.reload(section) }
        #if os(macOS)
        // The ideal height sizes the fitted sheet before a saved size exists;
        // without it a column whose content wants all the height it is offered
        // (an empty or unavailable state) lays out at the screen's height and
        // is cut off above the sheet.
        .frame(
            minWidth: 560,
            maxWidth: .infinity,
            minHeight: 520,
            idealHeight: 600,
            maxHeight: .infinity
        )
        .background(ScheduleBrowserWindowResizeView())
        .scheduleBrowserPresentationSizing()
        #endif
    }

    /// The Mac keeps the index beside the detail column, as the Memory browser
    /// does; elsewhere the index is the root of a stack. A NavigationView in a
    /// Mac sheet laid its first column out at the presenting window's height
    /// and cut the title and the section picker off above the sheet.
    @ViewBuilder
    private var navigationContainer: some View {
        #if os(macOS)
        NavigationSplitView {
            column
        } detail: {
            Text("Select a schedule to read its instruction.")
                .veetType(.ui)
                .foregroundColor(AppTheme.inkSoft)
                .frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
                .background(AppTheme.paper)
        }
        #else
        NavigationStack {
            column
        }
        #endif
    }

    private var column: some View {
        VStack(spacing: 0) {
            #if os(macOS)
            VStack(alignment: .leading, spacing: 4) {
                Text("Schedules").veetType(.display)
                Text("Work Veetbot does on its own, on a rhythm you set.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.top, 14)
            #endif
            Picker("Schedule view", selection: $section) {
                Text("Current").tag(ScheduleBrowserSection.current)
                Text("Recent History").tag(ScheduleBrowserSection.recentHistory)
            }
            .pickerStyle(.segmented)
            // The Mac draws a picker's label beside the control; in this column
            // it wrapped letter by letter into a tall sliver.
            .labelsHidden()
            .accessibilityLabel("Schedule view")
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .accessibilityIdentifier("schedule.section")
            content
        }
        .background(AppTheme.paper)
        .navigationTitle("Schedules")
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        #endif
        .toolbar {
            ToolbarItem(placement: .cancellationAction) {
                Button("Close") { dismiss() }
            }
            ToolbarItem(placement: .primaryAction) {
                Button { creating = true } label: { Label("New schedule", systemImage: "plus") }
                    .accessibilityIdentifier("schedule.new")
            }
        }
        .sheet(isPresented: $creating, onDismiss: { model.clearDraft() }) {
            ScheduleCreateSheet(model: model)
        }
    }

    @ViewBuilder
    private var content: some View {
        if model.unavailable && model.items.isEmpty {
            unavailableState
        } else if model.isLoading && model.items.isEmpty {
            ProgressView("Loading schedules…")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let errorMessage = model.errorMessage, model.items.isEmpty {
            errorState(errorMessage)
        } else if model.items.isEmpty {
            emptyState
        } else {
            list
        }
    }

    private var list: some View {
        List {
            ForEach(model.items) { item in
                NavigationLink {
                    ScheduleDetailView(model: model, summary: item)
                } label: {
                    ScheduleRow(item: item)
                }
                .a2ListRow()
                .accessibilityIdentifier("schedule.row.\(item.id.uuidString)")
                .onAppear {
                    guard item.id == model.items.last?.id else { return }
                    Task { await model.loadMore() }
                }
            }
            if model.isLoadingMore {
                HStack {
                    Spacer()
                    ProgressView()
                    Spacer()
                }
                .a2ListRow()
            } else if let errorMessage = model.errorMessage {
                loadMoreFailureFooter(errorMessage)
                    .a2ListRow()
            }
        }
        .a2List()
    }

    private func loadMoreFailureFooter(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(message, systemImage: "exclamationmark.triangle")
                .veetType(.meta)
                .foregroundColor(AppTheme.inkSoft)
            Button("Retry") {
                Task { await model.retry() }
            }
            .buttonStyle(SecondaryCapsuleButtonStyle())
        }
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var unavailableState: some View {
        A2EmptyState(
            systemImage: "calendar.badge.exclamationmark",
            title: "This server does not support schedule browsing yet."
        )
    }

    private func errorState(_ message: String) -> some View {
        A2EmptyState(systemImage: "exclamationmark.triangle", title: message) {
            Button("Retry") {
                Task { await model.retry() }
            }
            .buttonStyle(SecondaryCapsuleButtonStyle())
        }
    }

    private var emptyState: some View {
        A2EmptyState(
            systemImage: "calendar",
            title: section == .current
                ? "No current schedules."
                : "No completed or cancelled schedules in the last 30 days.",
            message: section == .current ? "Add one with New schedule, in plain words, or ask in Chat." : nil
        )
    }
}

/// One schedule in the list: the name beside its state, the instruction's
/// preview, and when it runs.
struct ScheduleRow: View {
    let item: ScheduleListItemView

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: "calendar")
                .foregroundColor(AppTheme.tealText)
                .frame(width: 20)
                .padding(.top, 2)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) {
                HStack(alignment: .firstTextBaseline) {
                    Text(item.title)
                        .veetType(.ui, weight: .semibold)
                        .foregroundColor(AppTheme.ink)
                    Spacer()
                    ScheduleStateBadge(state: item.state, kind: item.stateKind)
                }
                Text(item.instructionPreview)
                    .veetType(.ui)
                    .foregroundColor(AppTheme.inkSoft)
                    .lineLimit(3)
                Text(scheduleTiming(item.cadence, nextFireAt: item.nextFireAt))
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
            }
        }
        .padding(.vertical, 6)
    }
}

/// "Daily · 09:00 · Next Oct 20, 2026 at 9:00 AM", or the cadence alone.
func scheduleTiming(_ cadence: ScheduleCadenceView, nextFireAt: Date?) -> String {
    let summary = scheduleCadenceSummary(cadence)
    guard let nextFireAt else { return summary }
    return "\(summary) · Next \(nextFireAt.formatted())"
}

struct ScheduleStateBadge: View {
    let state: String
    let kind: ScheduleStateKind?

    var body: some View {
        Text(scheduleDisplayText(state))
            .veetType(.meta, weight: .semibold)
            .foregroundColor(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.1))
            .clipShape(Capsule())
    }

    private var color: Color {
        switch kind {
        case .active: return AppTheme.tealText
        case .paused: return AppTheme.amberText
        case .completed: return AppTheme.inkSoft
        case .cancelled: return AppTheme.red
        case nil: return AppTheme.inkSoft
        }
    }
}
func scheduleDisplayText(_ raw: String) -> String {
    raw.replacingOccurrences(of: "_", with: " ").capitalized
}

func scheduleCadenceSummary(
    _ cadence: ScheduleCadenceView,
    deviceTimeZoneIdentifier: String = TimeZone.current.identifier
) -> String {
    let exceptionalTimeZone = scheduleExceptionalTimeZone(
        cadence.timezone,
        deviceTimeZoneIdentifier: deviceTimeZoneIdentifier
    )
    switch cadence.kindKind {
    case .once:
        return cadence.at.map { "Once · \($0.formatted())" } ?? "Once"
    case .daily:
        return joinedScheduleParts(["Daily", cadence.localTime, exceptionalTimeZone])
    case .weekly:
        let days = cadence.weekdays?.map(scheduleWeekdayName).joined(separator: ", ")
        return joinedScheduleParts([
            days.map { "Weekly · \($0)" } ?? "Weekly",
            cadence.localTime,
            exceptionalTimeZone,
        ])
    case .monthly:
        var selectors = (cadence.daysOfMonth ?? []).map(String.init)
        if cadence.lastDay == true { selectors.append("last day") }
        let rule = selectors.isEmpty ? "Monthly" : "Monthly · \(selectors.joined(separator: ", "))"
        return joinedScheduleParts([rule, cadence.localTime, exceptionalTimeZone])
    case .yearly:
        let dates = cadence.dates?.map { "\($0.month)/\($0.day)" }.joined(separator: ", ")
        return joinedScheduleParts([
            dates.map { "Yearly · \($0)" } ?? "Yearly",
            cadence.localTime,
            exceptionalTimeZone,
        ])
    case nil:
        return scheduleDisplayText(cadence.kind)
    }
}

private func scheduleExceptionalTimeZone(
    _ identifier: String?,
    deviceTimeZoneIdentifier: String
) -> String? {
    guard let identifier else { return nil }
    if identifier == deviceTimeZoneIdentifier { return nil }
    if let scheduleZone = TimeZone(identifier: identifier),
        let deviceZone = TimeZone(identifier: deviceTimeZoneIdentifier),
        scheduleZone == deviceZone
    {
        return nil
    }
    return identifier
}

private func joinedScheduleParts(_ parts: [String?]) -> String {
    parts.compactMap { value in
        guard let value, !value.isEmpty else { return nil }
        return value
    }.joined(separator: " · ")
}

private func scheduleWeekdayName(_ isoDay: Int) -> String {
    switch isoDay {
    case 1: return "Mon"
    case 2: return "Tue"
    case 3: return "Wed"
    case 4: return "Thu"
    case 5: return "Fri"
    case 6: return "Sat"
    case 7: return "Sun"
    default: return String(isoDay)
    }
}

#if os(macOS)
private extension View {
    func scheduleBrowserPresentationSizing() -> some View {
        presentationSizing(.fitted)
    }
}

enum ScheduleBrowserWindowConfiguration {
    static let storageKey = "veetbot.scheduleBrowser.contentSize"
    static let minimumSize = NSSize(width: 560, height: 520)
    static let maximumSize = NSSize(width: 10_000, height: 10_000)

    @MainActor
    static func apply(to window: NSWindow) {
        window.styleMask.insert(.resizable)
        window.contentMinSize = minimumSize
        window.contentMaxSize = maximumSize
        PopupWindowContentSizeStore.restore(
            window: window,
            key: storageKey,
            minimumSize: minimumSize,
            maximumSize: maximumSize
        )
    }
}

private struct ScheduleBrowserWindowResizeView: NSViewRepresentable {
    func makeNSView(context: Context) -> ScheduleBrowserWindowResizeNSView {
        ScheduleBrowserWindowResizeNSView()
    }

    func updateNSView(_ view: ScheduleBrowserWindowResizeNSView, context: Context) {
        view.applyConfigurationIfPossible()
    }
}

private final class ScheduleBrowserWindowResizeNSView: NSView {
    private var keyWindowObserver: NSObjectProtocol?
    private var resizePersistence: PopupWindowResizePersistence?

    deinit {
        if let keyWindowObserver {
            NotificationCenter.default.removeObserver(keyWindowObserver)
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if let keyWindowObserver {
            NotificationCenter.default.removeObserver(keyWindowObserver)
        }
        keyWindowObserver = nil
        resizePersistence = nil
        guard let window else { return }
        applyConfigurationAfterPresentation()
        resizePersistence = PopupWindowResizePersistence(
            window: window,
            key: ScheduleBrowserWindowConfiguration.storageKey
        )
        keyWindowObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didBecomeKeyNotification,
            object: window,
            queue: .main
        ) { [weak self] _ in
            self?.applyConfigurationAfterPresentation()
        }
    }

    private func applyConfigurationAfterPresentation() {
        applyConfigurationIfPossible()
        DispatchQueue.main.async { [weak self] in
            self?.applyConfigurationIfPossible()
        }
    }

    func applyConfigurationIfPossible() {
        guard let window else { return }
        ScheduleBrowserWindowConfiguration.apply(to: window)
    }
}
#endif

/// New schedule (MagicPath quiet-home-5746, ADR-0215 part B): the owner says
/// what should happen and when; the server reads it, and the sheet shows how
/// before anything is saved. Create posts the server's own definition.
struct ScheduleCreateSheet: View {
    @ObservedObject var model: ScheduleViewModel
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @FocusState private var focused: Bool

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 14) {
                Text("Say what should happen and when, the way you would tell a person.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                TextField(
                    "Every weekday at 7 am, tell me what needs me today.",
                    text: $text,
                    axis: .vertical
                )
                .lineLimit(3...6)
                .textFieldStyle(.plain)
                .a2Field(radius: 12)
                .focused($focused)
                .accessibilityIdentifier("schedule.new.text")
                understood
                Spacer(minLength: 0)
            }
            .padding(20)
            .background(AppTheme.paper)
            .navigationTitle("New schedule")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Create schedule") {
                        Task { if await model.createFromDraft() { dismiss() } }
                    }
                    .disabled(!isReady)
                    .accessibilityIdentifier("schedule.new.create")
                }
            }
        }
        .tint(AppTheme.tealFill)
        // The confirmation shows here first; as the sheet closes, the
        // browser's host below takes the same toast over.
        .toastHost()
        .onAppear { focused = true }
        // Read the sentence once typing pauses, so each draft costs one call.
        .task(id: text) {
            do { try await Task.sleep(nanoseconds: 1_200_000_000) } catch { return }
            await model.readDraft(text)
        }
        #if os(macOS)
        .frame(minWidth: 520, idealWidth: 600, minHeight: 320, idealHeight: 360)
        #endif
    }

    private var isReady: Bool {
        if case .ready = model.draftState { return true }
        return false
    }

    @ViewBuilder
    private var understood: some View {
        VStack(alignment: .leading, spacing: 4) {
            switch model.draftState {
            case .idle:
                Text("Veetbot will show how it understood the timing before anything is saved.")
                    .foregroundColor(AppTheme.inkSoft)
            case .reading:
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Reading…").foregroundColor(AppTheme.inkSoft)
                }
            case .ready(let draft):
                Text("Veetbot understood")
                    .veetType(.label)
                    .foregroundColor(AppTheme.inkSoft)
                Text([
                    draft.cadence.map { scheduleCadenceSummary($0) },
                    draft.nextFireAt.first.map {
                        "first run " + $0.formatted(.dateTime.weekday(.wide).month().day().hour().minute())
                    },
                ].compactMap { $0 }.joined(separator: " · "))
                .foregroundColor(AppTheme.ink)
                .accessibilityIdentifier("schedule.new.understood")
                if let title = draft.title {
                    Text("Saved as \u{201C}\(title)\u{201D}. Reports arrive in Chat.")
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkSoft)
                }
            case .unclear(let message):
                Text(message).foregroundColor(AppTheme.amberText)
            case .unavailable:
                Text("This server can't read schedules from plain words yet. Ask in Chat instead.")
                    .foregroundColor(AppTheme.inkSoft)
            case .failed(let message):
                Text(message).foregroundColor(AppTheme.red)
            }
        }
        .veetType(.ui)
        .fixedSize(horizontal: false, vertical: true)
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 12, style: .continuous).fill(AppTheme.paperDeep))
    }
}
