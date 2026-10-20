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
            minWidth: 860,
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
            groupSidebar
                .navigationSplitViewColumnWidth(min: 190, ideal: 212, max: 260)
        } content: {
            collectionColumn
                .navigationSplitViewColumnWidth(min: 260, ideal: 320, max: 440)
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
                Text(sidebarItem.title).veetType(.display)
                if sidebarItem == .memories(.review) {
                    Text("Formed without an explicit statement from you, and not yet confirmed.")
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            #else
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
            #endif
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
                #if !os(macOS)
                // The Mac's group sidebar carries the review queue and the types.
                ToolbarItem(placement: .primaryAction) {
                    if collection == "memories" { reviewQueueToggle }
                }
                #endif
                ToolbarItem(placement: .primaryAction) {
                    if collection == "memories" { statusFilterMenu }
                }
                #if !os(macOS)
                ToolbarItem(placement: .primaryAction) {
                    if collection == "memories" { typeFilterMenu }
                }
                #endif
            }
    }

    #if os(macOS)
    /// The Mac's first column (MagicPath coolly-breeze-5701): the review
    /// queue, the memories by type and the People directory, then the
    /// synthesis journal. The numbers come from the counts route (ADR-0214);
    /// an older server leaves the rows without them.
    private var groupSidebar: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 2) {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Memory").veetType(.display)
                    Text("What Veetbot has learned and may rely on.")
                        .veetType(.meta)
                        .foregroundColor(AppTheme.inkSoft)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 8)
                .padding(.bottom, 10)
                groupHeading("Review")
                groupRow(.memories(.review), systemImage: "flag")
                groupHeading("Browse")
                groupRow(.memories(.all), systemImage: "brain")
                ForEach(MemoryBrowseGroup.browseTypes) { type in
                    groupRow(.memories(type), systemImage: nil)
                }
                groupRow(.people, systemImage: "person.2")
                groupHeading("Maintenance")
                groupRow(.synthesis, systemImage: "arrow.triangle.merge")
                Text("Merges and summaries Veetbot made from overlapping memories, to review or undo.")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 8)
                    .padding(.top, 8)
            }
            .padding(10)
        }
        .background(AppTheme.paperDeep)
        .task { await model.loadCounts() }
    }

    private var sidebarItem: MemorySidebarItem {
        switch collection {
        case "people": return .people
        case "synthesis": return .synthesis
        default: return .memories(model.group)
        }
    }

    private func groupHeading(_ title: String) -> some View {
        Text(title)
            .veetType(.meta, weight: .semibold)
            .foregroundColor(AppTheme.inkSoft)
            .padding(.horizontal, 8)
            .padding(.top, 12)
            .padding(.bottom, 2)
            .accessibilityAddTraits(.isHeader)
    }

    private func sidebarCount(_ item: MemorySidebarItem) -> Int? {
        if case .memories(let group) = item { return model.count(for: group) }
        return nil
    }

    private func groupRow(_ group: MemorySidebarItem, systemImage: String?) -> some View {
        let selected = sidebarItem == group
        return Button {
            switch group {
            case .memories(let memories):
                collection = "memories"
                model.selectGroup(memories)
            case .people: collection = "people"
            case .synthesis: collection = "synthesis"
            }
        } label: {
            HStack(spacing: 9) {
                Group {
                    if let systemImage {
                        Image(systemName: systemImage)
                    } else {
                        Circle().fill(AppTheme.hairlineStrong).frame(width: 6, height: 6)
                    }
                }
                .frame(width: 16)
                .foregroundColor(group == .memories(.review) ? AppTheme.amberText : AppTheme.inkSoft)
                .accessibilityHidden(true)
                Text(group.title).foregroundColor(AppTheme.ink)
                Spacer(minLength: 4)
                if let count = sidebarCount(group) {
                    Text(count, format: .number)
                        .veetType(.meta, weight: group == .memories(.review) && count > 0 ? .semibold : .regular)
                        .monospacedDigit()
                        .foregroundColor(
                            group == .memories(.review) && count > 0 ? AppTheme.amberText : AppTheme.inkSoft
                        )
                        .accessibilityIdentifier("memory.group.\(group.id).count")
                }
            }
            .veetType(.ui)
            .padding(.horizontal, 8)
            .frame(minHeight: A2Metrics.rowHeight)
            .background(
                RoundedRectangle(cornerRadius: 7, style: .continuous)
                    .fill(selected ? AppTheme.selected : Color.clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("memory.group.\(group.id)")
    }
    #endif

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
                .veetType(.ui)
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
                        .veetType(.meta, weight: .semibold)
                        .foregroundColor(AppTheme.amberText)
                }
                Spacer(minLength: 0)
                Text(memory.updatedAt.formatted(date: .abbreviated, time: .omitted))
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkFaint)
            }
            Text(memory.statement)
                .veetType(.ui)
                .foregroundColor(AppTheme.ink)
                .lineLimit(2)
            HStack(spacing: 8) {
                Text("From \(memory.displaySubject)")
                    .veetType(.meta)
                    .foregroundColor(AppTheme.inkSoft)
                    .lineLimit(1)
                MemorySensitivityBadge(sensitivity: memory.sensitivity, kind: memory.sensitivityKind)
                if memory.statusKind != .active {
                    MemoryStatusTag(status: memory.status)
                }
                if !memory.conflictsWith.isEmpty {
                    Label("Conflicts", systemImage: "exclamationmark.triangle.fill")
                        .veetType(.meta, weight: .semibold)
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
            .veetType(.meta, weight: .semibold)
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
    // Three columns: the group sidebar, the list and the detail.
    static let minimumSize = NSSize(width: 860, height: 520)
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
            .veetType(.meta, weight: .semibold)
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

#if os(macOS)
/// One row of the Mac Memory sidebar.
enum MemorySidebarItem: Hashable, Identifiable {
    case memories(MemoryBrowseGroup)
    case people
    case synthesis

    var id: String {
        switch self {
        case .memories(let group): return group.id
        case .people: return "people"
        case .synthesis: return "synthesis"
        }
    }

    var title: String {
        switch self {
        case .memories(let group): return group.title
        case .people: return "People"
        case .synthesis: return "Synthesis"
        }
    }
}
#endif
