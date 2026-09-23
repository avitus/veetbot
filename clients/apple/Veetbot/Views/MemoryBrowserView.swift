import SwiftUI

#if os(macOS)
import AppKit
#endif

/// Browses the calling principal's beliefs (memory-read-api-and-browser.md).
/// A peer of the conversation list rather than a mode inside a conversation,
/// because a belief outlives the session that formed it.
public struct MemoryBrowserView: View {
    @ObservedObject var model: MemoryViewModel
    @StateObject private var people = PeopleViewModel()
    @State private var collection = "memories"
    @State private var pendingDeletion: MemoryView?
    private let sessionID: UUID?
    @Environment(\.dismiss) private var dismiss

    public init(model: MemoryViewModel, sessionID: UUID? = nil) {
        self.model = model
        self.sessionID = sessionID
    }

    public var body: some View {
        NavigationView {
            VStack(spacing: 0) {
                Picker("Memory collection", selection: $collection) {
                    Text("Memories").tag("memories")
                    Text("People").tag("people")
                }.pickerStyle(.segmented).padding()
                if collection == "people" { PeopleBrowserView(model: people, sessionID: sessionID) }
                else { content }
            }
                .navigationTitle("Memory")
                // No accessibility identifier is attached here: `.searchable`
                // hoists its field into the navigation bar chrome, which is
                // not a descendant of this content view, so an identifier
                // placed on this chain would land on the content underneath
                // it instead of the field itself. XCUITest reaches the field
                // the way it reaches any search bar: `app.searchFields`.
                .searchable(
                    text: Binding(
                        get: { collection == "people" ? people.searchText : model.searchText },
                        set: { if collection == "people" { people.setSearchText($0) } else { model.setSearchText($0) } }
                    ),
                    prompt: collection == "people" ? "Search people" : "Search memories"
                )
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button("Close") { dismiss() }
                    }
                    ToolbarItem(placement: .primaryAction) {
                        if collection == "memories" { reviewQueueToggle }
                    }
                    ToolbarItem(placement: .primaryAction) {
                        if collection == "memories" { statusFilterMenu }
                    }
                    ToolbarItem(placement: .primaryAction) {
                        if collection == "memories" { typeFilterMenu }
                    }
                }
        }
        .confirmationDialog(
            "Delete this memory?",
            isPresented: Binding(
                get: { pendingDeletion != nil },
                set: { if !$0 { pendingDeletion = nil } }
            ),
            titleVisibility: .visible
        ) {
            Button("Delete memory", role: .destructive) {
                if let memory = pendingDeletion {
                    Task { await model.delete(memory) }
                }
                pendingDeletion = nil
            }
            Button("Cancel", role: .cancel) { pendingDeletion = nil }
        } message: {
            Text("The memory and its generated copies are removed, and the same statement will not form again. Original messages remain at their source.")
        }
        .accessibilityIdentifier("memory.browser")
        .task { await model.reload() }
        .onReceive(NotificationCenter.default.publisher(for: .peopleConnectionChanged)) { _ in dismiss() }
        #if os(macOS)
        .frame(
            minWidth: 560,
            maxWidth: .infinity,
            minHeight: 520,
            maxHeight: .infinity
        )
        .background(MemoryBrowserWindowResizeView())
        .memoryBrowserPresentationSizing()
        #endif
    }

    @ViewBuilder
    private var content: some View {
        // Every branch below that fully replaces the screen is guarded by
        // `model.items.isEmpty`: once a page has loaded, a degradation on a
        // later page (a caught `memoryBrowsingUnavailable`, or a plain
        // failure) must not throw away what is already on screen. `list`
        // itself renders that degradation inline, with a retry affordance.
        if model.unavailable && model.items.isEmpty {
            unavailableState
        } else if model.isLoading && model.items.isEmpty {
            ProgressView("Loading memories…")
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
                    MemoryDetailView(memory: item, model: model)
                } label: {
                    row(item)
                }
                .accessibilityIdentifier("memory.row.\(item.id.uuidString)")
                .onAppear {
                    guard item.id == model.items.last?.id else { return }
                    Task { await model.loadMore() }
                }
                .swipeActions(edge: .trailing, allowsFullSwipe: false) {
                    Button(role: .destructive) {
                        pendingDeletion = item
                    } label: {
                        Label("Delete", systemImage: "trash")
                    }
                    .disabled(model.pendingActionID != nil || model.changesUnavailable)
                    .accessibilityIdentifier("memory.row.\(item.id.uuidString).delete")
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

    private func row(_ item: MemoryView) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(item.statement)
                .appFont(.body)
                .lineLimit(3)
            HStack(spacing: 6) {
                Text(item.displaySubject)
                Text("\u{00B7}")
                Text(memoryDisplayText(item.beliefType))
            }
            .appFont(.caption)
            .foregroundColor(.secondary)
            HStack(spacing: 8) {
                MemorySensitivityBadge(sensitivity: item.sensitivity, kind: item.sensitivityKind)
                if item.statusKind != .active {
                    statusTag(item)
                }
                if item.flaggedForReview {
                    Label("Flagged", systemImage: "flag.fill")
                        .appFont(.caption2, weight: .semibold)
                        .foregroundColor(AppTheme.orange)
                }
                if !item.conflictsWith.isEmpty {
                    Label("Conflicts", systemImage: "exclamationmark.triangle.fill")
                        .appFont(.caption2, weight: .semibold)
                        .foregroundColor(.red)
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func statusTag(_ item: MemoryView) -> some View {
        Text(memoryDisplayText(item.status))
            .appFont(.caption2, weight: .semibold)
            .foregroundColor(.secondary)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Color.secondary.opacity(0.12))
            .clipShape(Capsule())
    }

    /// Asks the server for the review queue only (ADR-0117).
    private var reviewQueueToggle: some View {
        Button {
            model.setFlaggedOnly(!model.flaggedOnly)
        } label: {
            Image(systemName: model.flaggedOnly ? "flag.fill" : "flag")
        }
        .accessibilityLabel(model.flaggedOnly ? "Show all memories" : "Show only memories needing review")
        .accessibilityIdentifier("memory.filter.review")
    }

    private var statusFilterMenu: some View {
        Menu {
            filterButton(title: "All Statuses", isSelected: model.statusFilter == nil) {
                model.setStatusFilter(nil)
            }
            ForEach(MemoryStatusKind.allCases, id: \.self) { status in
                filterButton(
                    title: memoryDisplayText(status.rawValue),
                    isSelected: model.statusFilter == status
                ) {
                    model.setStatusFilter(status)
                }
            }
        } label: {
            Image(systemName: "line.3.horizontal.decrease.circle")
        }
        .accessibilityLabel("Filter by status")
    }

    private var typeFilterMenu: some View {
        Menu {
            filterButton(title: "All Types", isSelected: model.typeFilter == nil) {
                model.setTypeFilter(nil)
            }
            ForEach(MemoryBeliefTypeKind.allCases, id: \.self) { type in
                filterButton(
                    title: memoryDisplayText(type.rawValue),
                    isSelected: model.typeFilter == type
                ) {
                    model.setTypeFilter(type)
                }
            }
        } label: {
            Image(systemName: "tag")
        }
        .accessibilityLabel("Filter by belief type")
    }

    @ViewBuilder
    private func filterButton(
        title: String,
        isSelected: Bool,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            if isSelected {
                Label(title, systemImage: "checkmark")
            } else {
                Text(title)
            }
        }
    }

    private var unavailableState: some View {
        VStack(spacing: 12) {
            Image(systemName: "brain.head.profile")
                .font(.largeTitle)
                .foregroundColor(.secondary)
            Text("This server does not support memory browsing yet.")
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
            Image(systemName: model.flaggedOnly ? "checkmark.circle" : "tray")
                .font(.largeTitle)
                .foregroundColor(.secondary)
            Text(model.flaggedOnly ? "Nothing needs review." : "No memories found.")
                .foregroundColor(.secondary)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

#if os(macOS)
private extension View {
    @ViewBuilder
    func memoryBrowserPresentationSizing() -> some View {
        if #available(macOS 15.0, *) {
            presentationSizing(.fitted)
        } else {
            self
        }
    }
}

enum MemoryBrowserWindowConfiguration {
    static let storageKey = "veetbot.memoryBrowser.contentSize"
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

private struct MemoryBrowserWindowResizeView: NSViewRepresentable {
    func makeNSView(context: Context) -> MemoryBrowserWindowResizeNSView {
        MemoryBrowserWindowResizeNSView()
    }

    func updateNSView(_ view: MemoryBrowserWindowResizeNSView, context: Context) {
        view.applyConfigurationIfPossible()
    }
}

private final class MemoryBrowserWindowResizeNSView: NSView {
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
            key: MemoryBrowserWindowConfiguration.storageKey
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
        MemoryBrowserWindowConfiguration.apply(to: window)
    }
}
#endif

/// Shared by the browser row and the detail view's Classification section.
func memoryDisplayText(_ raw: String) -> String {
    raw.replacingOccurrences(of: "_", with: " ").capitalized
}

/// A text-labeled sensitivity badge: color alone never carries the meaning.
struct MemorySensitivityBadge: View {
    let sensitivity: String
    let kind: MemorySensitivityKind?

    var body: some View {
        Label(memoryDisplayText(sensitivity), systemImage: symbol)
            .appFont(.caption2, weight: .semibold)
            .foregroundColor(color)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(color.opacity(0.12))
            .clipShape(Capsule())
    }

    private var color: Color {
        switch kind {
        case .public: return AppTheme.turquoise
        case .internal: return .secondary
        case .sensitive: return AppTheme.orange
        case .restricted: return .red
        case nil: return .secondary
        }
    }

    private var symbol: String {
        switch kind {
        case .public: return "globe"
        case .internal: return "lock"
        case .sensitive: return "exclamationmark.triangle"
        case .restricted: return "lock.shield"
        case nil: return "questionmark.circle"
        }
    }
}
