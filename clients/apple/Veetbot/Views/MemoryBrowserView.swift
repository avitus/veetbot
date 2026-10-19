import SwiftUI

#if os(macOS)
import AppKit
#endif

/// Browses the calling principal's beliefs (memory-read-api-and-browser.md).
/// A peer of the conversation list rather than a mode inside a conversation,
/// because a belief outlives the session that formed it. Drawn with the A2
/// Dock tokens: the list on paper, a kind line over each statement, and amber
/// for what still needs review.
public struct MemoryBrowserView: View {
    @ObservedObject var model: MemoryViewModel
    @StateObject private var people = PeopleViewModel()
    @State private var collection = "memories"
    /// The person shown beside the Mac directory.
    @State private var selectedPersonID: UUID?
    @State private var pendingDeletion: MemoryView?
    private let sessionID: UUID?
    @Environment(\.dismiss) private var dismiss

    public init(model: MemoryViewModel, sessionID: UUID? = nil) {
        self.model = model
        self.sessionID = sessionID
    }

    public var body: some View {
        navigationContainer
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
        .toastHost()
        .onReceive(NotificationCenter.default.publisher(for: .peopleConnectionChanged)) { _ in dismiss() }
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
        .background(MemoryBrowserWindowResizeView())
        .memoryBrowserPresentationSizing()
        #endif
    }

    /// The Mac keeps the collection beside its detail column; elsewhere the
    /// collection is the root of a stack. Either way the synthesis journal can
    /// present its detail with `navigationDestination(isPresented:)`.
    @ViewBuilder
    private var navigationContainer: some View {
        #if os(macOS)
        NavigationSplitView {
            collectionColumn
        } detail: {
            // A stack, so the synthesis detail can push the originals it cites.
            NavigationStack { detailColumn }
                .background(AppTheme.paper)
        }
        .tint(AppTheme.tealFill)
        #else
        NavigationStack {
            collectionColumn
        }
        .tint(AppTheme.tealFill)
        #endif
    }

    private var collectionColumn: some View {
        VStack(spacing: 0) {
            #if os(macOS)
            VStack(alignment: .leading, spacing: 4) {
                Text("Memory").appDisplayFont(.title2)
                Text("What Veetbot has learned and may rely on.")
                    .appFont(.caption)
                    .foregroundColor(AppTheme.inkSoft)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.top, 14)
            #endif
            Picker("Memory collection", selection: $collection) {
                Text("Memories").tag("memories")
                Text("People").tag("people")
                Text("Synthesis").tag("synthesis")
            }
            .pickerStyle(.segmented)
            // The Mac draws a picker's label beside the control; in this column
            // it wrapped letter by letter into a tall sliver.
            .labelsHidden()
            .accessibilityLabel("Memory collection")
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            if collection == "people" { PeopleBrowserView(model: people, sessionID: sessionID, selection: peopleSelection) }
            else if collection == "synthesis" { MemorySynthesisBrowserView() }
            else { content }
        }
            .background(AppTheme.paper)
            .navigationTitle("Memory")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            // No accessibility identifier is attached here: `.searchable`
            // hoists its field into the navigation bar chrome, which is
            // not a descendant of this content view, so an identifier
            // placed on this chain would land on the content underneath
            // it instead of the field itself. XCUITest reaches the field
            // the way it reaches any search bar: `app.searchFields`.
            .memoryCollectionSearch(
                text: Binding(
                    get: { collection == "people" ? people.searchText : model.searchText },
                    set: { if collection == "people" { people.setSearchText($0) } else { model.setSearchText($0) } }
                ),
                prompt: collection == "people" ? "Search people" : "Search memories",
                enabled: collection != "synthesis"
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

    /// On the Mac the directory selects the person its adjacent column shows;
    /// elsewhere each row pushes the profile.
    private var peopleSelection: Binding<UUID?>? {
        #if os(macOS)
        $selectedPersonID
        #else
        nil
        #endif
    }

    #if os(macOS)
    /// The split view's second column. A memory row's navigation link replaces
    /// it while that memory is open.
    @ViewBuilder
    private var detailColumn: some View {
        if collection == "people", let selectedPersonID {
            PeopleDetailView(personID: selectedPersonID, sessionID: sessionID)
                .environment(\.peopleSelection, PeopleSelectionAction { self.selectedPersonID = $0 })
        } else if collection == "people" {
            PeoplePlaceholder()
        } else if collection == "memories" {
            Text("Select a memory to see where it came from.")
                .appFont(.callout)
                .foregroundColor(AppTheme.inkSoft)
                .frame(minWidth: 0, maxWidth: .infinity, minHeight: 0, maxHeight: .infinity)
                .background(AppTheme.paper)
        } else {
            AppTheme.paper
        }
    }
    #endif

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
                    MemoryRow(memory: item)
                }
                .a2ListRow()
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
                .appFont(.caption)
                .foregroundColor(AppTheme.inkSoft)
            Button("Retry") {
                Task { await model.retry() }
            }
            .buttonStyle(SecondaryCapsuleButtonStyle())
        }
        .padding(.vertical, 4)
        .frame(maxWidth: .infinity, alignment: .leading)
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
        A2EmptyState(
            systemImage: "brain.head.profile",
            title: "This server does not support memory browsing yet."
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
            systemImage: model.flaggedOnly ? "checkmark.circle" : "tray",
            title: model.flaggedOnly ? "Nothing needs review." : "No memories found.",
            message: model.flaggedOnly ? "Veetbot will flag new memories here." : nil
        )
    }
}

/// One belief in the list: its kind and the day it last changed over the
/// statement, then where it came from and how far it may travel.
struct MemoryRow: View {
    let memory: MemoryView

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                A2KindLabel(text: memoryDisplayText(memory.beliefType))
                if memory.flaggedForReview {
                    Label("Needs review", systemImage: "flag.fill")
                        .appFont(.caption2, weight: .semibold)
                        .foregroundColor(AppTheme.amberText)
                }
                Spacer(minLength: 0)
                Text(memory.updatedAt.formatted(date: .abbreviated, time: .omitted))
                    .appFont(.caption2)
                    .foregroundColor(AppTheme.inkFaint)
            }
            Text(memory.statement)
                .appFont(.body)
                .foregroundColor(AppTheme.ink)
                .lineLimit(2)
            HStack(spacing: 8) {
                Text("From \(memory.displaySubject)")
                    .appFont(.caption)
                    .foregroundColor(AppTheme.inkSoft)
                    .lineLimit(1)
                MemorySensitivityBadge(sensitivity: memory.sensitivity, kind: memory.sensitivityKind)
                if memory.statusKind != .active {
                    MemoryStatusTag(status: memory.status)
                }
                if !memory.conflictsWith.isEmpty {
                    Label("Conflicts", systemImage: "exclamationmark.triangle.fill")
                        .appFont(.caption2, weight: .semibold)
                        .foregroundColor(AppTheme.red)
                }
            }
        }
        .padding(.vertical, 6)
    }
}

/// A belief's status when it is anything but active.
struct MemoryStatusTag: View {
    let status: String

    var body: some View {
        Text(memoryDisplayText(status))
            .appFont(.caption2, weight: .semibold)
            .foregroundColor(AppTheme.inkSoft)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(AppTheme.field)
            .clipShape(Capsule())
    }
}

#if os(macOS)
private extension View {
    func memoryBrowserPresentationSizing() -> some View {
        presentationSizing(.fitted)
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
            .background(color.opacity(0.1))
            .clipShape(Capsule())
    }

    private var color: Color {
        switch kind {
        case .public: return AppTheme.tealText
        case .internal: return AppTheme.inkSoft
        case .sensitive: return AppTheme.amberText
        case .restricted: return AppTheme.red
        case nil: return AppTheme.inkSoft
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

private extension View {
    @ViewBuilder func memoryCollectionSearch(text: Binding<String>, prompt: String, enabled: Bool) -> some View {
        if enabled { searchable(text: text, prompt: Text(prompt)) } else { self }
    }
}
