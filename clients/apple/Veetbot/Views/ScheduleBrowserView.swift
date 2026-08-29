import SwiftUI

#if os(macOS)
import AppKit
#endif

/// Read-only schedule index over the existing principal-scoped control plane.
public struct ScheduleBrowserView: View {
    @ObservedObject var model: ScheduleViewModel
    @Environment(\.dismiss) private var dismiss

    public init(model: ScheduleViewModel) {
        self.model = model
    }

    public var body: some View {
        NavigationView {
            content
                .navigationTitle("Schedules")
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { dismiss() }
                    }
                }
        }
        .accessibilityIdentifier("schedule.browser")
        .task { await model.reload() }
        #if os(macOS)
        .frame(
            minWidth: 560,
            maxWidth: .infinity,
            minHeight: 520,
            maxHeight: .infinity
        )
        .background(ScheduleBrowserWindowResizeView())
        .scheduleBrowserPresentationSizing()
        #endif
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
                    row(item)
                }
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
            } else if let errorMessage = model.errorMessage {
                loadMoreFailureFooter(errorMessage)
            }
        }
    }

    private func row(_ item: ScheduleListItemView) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(item.title)
                    .appFont(.body, weight: .semibold)
                Spacer()
                ScheduleStateBadge(state: item.state, kind: item.stateKind)
            }
            Text(item.instructionPreview)
                .appFont(.body)
                .foregroundColor(.secondary)
                .lineLimit(3)
            Label(scheduleCadenceSummary(item.cadence), systemImage: "repeat")
                .appFont(.caption)
                .foregroundColor(.secondary)
            if let nextFireAt = item.nextFireAt {
                Label("Next \(nextFireAt.formatted())", systemImage: "clock")
                    .appFont(.caption)
                    .foregroundColor(.secondary)
            }
        }
        .padding(.vertical, 4)
    }

    private func loadMoreFailureFooter(_ message: String) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Label(message, systemImage: "exclamationmark.triangle")
                .appFont(.caption)
                .foregroundColor(.secondary)
            Button("Retry") {
                Task { await model.retry() }
            }
            .appFont(.caption)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var unavailableState: some View {
        VStack(spacing: 12) {
            Image(systemName: "calendar.badge.exclamationmark")
                .font(.largeTitle)
                .foregroundColor(.secondary)
            Text("This server does not support schedule browsing yet.")
                .multilineTextAlignment(.center)
                .foregroundColor(.secondary)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ message: String) -> some View {
        VStack(spacing: 12) {
            Image(systemName: "exclamationmark.triangle")
                .font(.largeTitle)
                .foregroundColor(.secondary)
            Text(message)
                .multilineTextAlignment(.center)
                .foregroundColor(.secondary)
            Button("Retry") {
                Task { await model.retry() }
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Image(systemName: "calendar")
                .font(.largeTitle)
                .foregroundColor(.secondary)
            Text("No schedules found.")
                .foregroundColor(.secondary)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct ScheduleStateBadge: View {
    let state: String
    let kind: ScheduleStateKind?

    var body: some View {
        Text(scheduleDisplayText(state))
            .appFont(.caption2, weight: .semibold)
            .foregroundColor(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.12))
            .clipShape(Capsule())
    }

    private var color: Color {
        switch kind {
        case .active: return AppTheme.turquoise
        case .paused: return AppTheme.orange
        case .completed: return .secondary
        case .cancelled: return .red
        case nil: return .secondary
        }
    }
}

func scheduleDisplayText(_ raw: String) -> String {
    raw.replacingOccurrences(of: "_", with: " ").capitalized
}

func scheduleCadenceSummary(_ cadence: ScheduleCadenceView) -> String {
    switch cadence.kindKind {
    case .once:
        return cadence.at.map { "Once · \($0.formatted())" } ?? "Once"
    case .daily:
        return joinedScheduleParts(["Daily", cadence.localTime, cadence.timezone])
    case .weekly:
        let days = cadence.weekdays?.map(scheduleWeekdayName).joined(separator: ", ")
        return joinedScheduleParts([days.map { "Weekly · \($0)" } ?? "Weekly", cadence.localTime, cadence.timezone])
    case .monthly:
        var selectors = (cadence.daysOfMonth ?? []).map(String.init)
        if cadence.lastDay == true { selectors.append("last day") }
        let rule = selectors.isEmpty ? "Monthly" : "Monthly · \(selectors.joined(separator: ", "))"
        return joinedScheduleParts([rule, cadence.localTime, cadence.timezone])
    case .yearly:
        let dates = cadence.dates?.map { "\($0.month)/\($0.day)" }.joined(separator: ", ")
        return joinedScheduleParts([dates.map { "Yearly · \($0)" } ?? "Yearly", cadence.localTime, cadence.timezone])
    case nil:
        return scheduleDisplayText(cadence.kind)
    }
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
    @ViewBuilder
    func scheduleBrowserPresentationSizing() -> some View {
        if #available(macOS 15.0, *) {
            presentationSizing(.fitted)
        } else {
            self
        }
    }
}

enum ScheduleBrowserWindowConfiguration {
    static let minimumSize = NSSize(width: 560, height: 520)
    static let maximumSize = NSSize(width: 10_000, height: 10_000)

    @MainActor
    static func apply(to window: NSWindow) {
        window.styleMask.insert(.resizable)
        window.contentMinSize = minimumSize
        window.contentMaxSize = maximumSize
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
        guard let window else { return }
        applyConfigurationIfPossible()
        keyWindowObserver = NotificationCenter.default.addObserver(
            forName: NSWindow.didBecomeKeyNotification,
            object: window,
            queue: .main
        ) { [weak self] _ in
            self?.applyConfigurationIfPossible()
        }
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
