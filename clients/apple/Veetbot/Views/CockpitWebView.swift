import Foundation
import SwiftUI
import WebKit

/// Serves `veetbot-cockpit://board/<sha256>.<ext>` (docs/plan/cockpit.md,
/// `CockpitWebView`): only content hashes the current snapshot names, fetched
/// natively with the token and cached by hash, and 404 for everything else.
/// Web content never sees the token or any header that would carry it.
@MainActor
public final class CockpitSchemeHandler: NSObject, WKURLSchemeHandler {
    /// The hashes the current snapshot names; nothing else resolves.
    public var allowed: Set<String> = []
    private let fetch: @Sendable (String) async throws -> CockpitAssetData
    private var cache: [String: CockpitAssetData] = [:]
    private var loads: [ObjectIdentifier: Task<Void, Never>] = [:]
    private static let cacheLimit = 64

    public init(fetch: @escaping @Sendable (String) async throws -> CockpitAssetData) {
        self.fetch = fetch
    }

    public func webView(_ webView: WKWebView, start urlSchemeTask: WKURLSchemeTask) { start(urlSchemeTask) }
    public func webView(_ webView: WKWebView, stop urlSchemeTask: WKURLSchemeTask) { stop(urlSchemeTask) }

    public func start(_ task: WKURLSchemeTask) {
        guard let url = task.request.url, let digest = CockpitAssetRoute.digest(of: url),
              allowed.contains(digest)
        else {
            Self.notFound(task)
            return
        }
        if let asset = cache[digest] {
            Self.serve(asset, to: task, url: url)
            return
        }
        let key = ObjectIdentifier(task)
        let fetch = self.fetch
        loads[key] = Task { [weak self] in
            let result: Result<CockpitAssetData, Error>
            do { result = .success(try await fetch(digest)) } catch { result = .failure(error) }
            guard let self, self.loads.removeValue(forKey: key) != nil, !Task.isCancelled else { return }
            switch result {
            case .success(let asset):
                if self.cache.count >= Self.cacheLimit { self.cache.removeAll() }
                self.cache[digest] = asset
                Self.serve(asset, to: task, url: url)
            case .failure:
                Self.notFound(task)
            }
        }
    }

    public func stop(_ task: WKURLSchemeTask) {
        // A stopped load must hear nothing more; WebKit raises on a late answer.
        loads.removeValue(forKey: ObjectIdentifier(task))?.cancel()
    }

    private static func serve(_ asset: CockpitAssetData, to task: WKURLSchemeTask, url: URL) {
        let response = HTTPURLResponse(
            url: url, statusCode: 200, httpVersion: "HTTP/1.1",
            headerFields: [
                "Content-Type": asset.contentType,
                "Content-Length": String(asset.content.count),
                "X-Content-Type-Options": "nosniff",
            ]
        )
        guard let response else { return }
        task.didReceive(response)
        task.didReceive(asset.content)
        task.didFinish()
    }

    private static func notFound(_ task: WKURLSchemeTask) {
        guard let url = task.request.url ?? URL(string: "\(CockpitAssetRoute.scheme)://board/"),
              let response = HTTPURLResponse(
                  url: url, statusCode: 404, httpVersion: "HTTP/1.1",
                  headerFields: ["Content-Type": "text/plain; charset=utf-8", "Content-Length": "0"]
              )
        else { return }
        task.didReceive(response)
        task.didFinish()
    }
}

/// The one web view that hosts the board: a non-persistent store, the scheme
/// handler, a rule list that blocks every web load, the network primitives
/// removed in every frame, the navigation policy and one main-frame-only
/// message handler. Native code pushes data with `callAsyncJavaScript`
/// arguments, never string interpolation.
@MainActor
final class CockpitWebController: NSObject, WKNavigationDelegate, WKUIDelegate, WKScriptMessageHandler {
    let webView: WKWebView
    let schemes: CockpitSchemeHandler
    var onMessage: (CockpitHostMessage) -> Void = { _ in }
    /// The host stopped answering, or the realm tried to leave its document.
    var onUnresponsive: () -> Void = {}
    private var runtime: CockpitRuntime?
    private var pendingMessage: JSONValue?
    private var hostReady = false
    /// Whether Cockpit is the visible mode; the window's own state is read at each beat.
    var isBoardVisible = true
    private var watchdog: Timer?
    private var heartbeat = CockpitWatchdog(deadline: deadlineSeconds, now: Date())
    private var rulesInstalled = false
    static let heartbeatSeconds: TimeInterval = 2
    static let deadlineSeconds: TimeInterval = 6

    init(fetch: @escaping @Sendable (String) async throws -> CockpitAssetData) {
        schemes = CockpitSchemeHandler(fetch: fetch)
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .nonPersistent()
        configuration.setURLSchemeHandler(schemes, forURLScheme: CockpitAssetRoute.scheme)
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        configuration.userContentController.addUserScript(WKUserScript(
            source: CockpitWebContent.removedPrimitivesScript, injectionTime: .atDocumentStart,
            forMainFrameOnly: false
        ))
        webView = WKWebView(frame: .zero, configuration: configuration)
        super.init()
        configuration.userContentController.add(self, name: CockpitWebContent.messageHandlerName)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        #if os(macOS)
        webView.setValue(false, forKey: "drawsBackground")
        webView.setAccessibilityIdentifier("cockpit.board")
        #else
        webView.isOpaque = false
        webView.backgroundColor = .clear
        webView.scrollView.backgroundColor = .clear
        webView.accessibilityIdentifier = "cockpit.board"
        #endif
    }

    /// Load the host document once its rule list is installed, then keep it current.
    func show(runtime: CockpitRuntime, allowed: Set<String>, message: JSONValue) {
        schemes.allowed = allowed
        pendingMessage = message
        let changedRuntime = self.runtime != runtime
        self.runtime = runtime
        if changedRuntime {
            hostReady = false
            Task { await self.installRulesThenLoad(runtime) }
        } else if hostReady {
            push(message)
        }
    }

    func tearDown() {
        watchdog?.invalidate()
        watchdog = nil
        webView.configuration.userContentController.removeScriptMessageHandler(
            forName: CockpitWebContent.messageHandlerName
        )
    }

    private func installRulesThenLoad(_ runtime: CockpitRuntime) async {
        if !rulesInstalled {
            // Without its rule list the board does not load at all.
            guard let rules = try? await WKContentRuleListStore.default().compileContentRuleList(
                forIdentifier: "veetbot-cockpit-block-web", encodedContentRuleList: CockpitWebContent.blockingRules
            ) else {
                return
            }
            webView.configuration.userContentController.add(rules)
            rulesInstalled = true
        }
        guard let url = CockpitAssetRoute.url(for: runtime.entry, extension: "html") else { return }
        webView.load(URLRequest(url: url))
        startWatchdog()
    }

    private func push(_ message: JSONValue) {
        webView.callAsyncJavaScript(
            "window.cockpitHost && window.cockpitHost.apply(message)",
            arguments: ["message": message.foundationValue], in: nil, in: .page
        ) { _ in }
    }

    private func startWatchdog() {
        watchdog?.invalidate()
        heartbeat = CockpitWatchdog(deadline: Self.deadlineSeconds, now: Date())
        watchdog = Timer.scheduledTimer(withTimeInterval: Self.heartbeatSeconds, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.beat() }
        }
    }

    /// Ping the host from outside the web view; a widget that never yields stops the host too.
    private func beat() {
        guard hostReady else { return }
        if heartbeat.beat(now: Date(), displayed: isOnScreen) == .unresponsive {
            hostReady = false
            onUnresponsive()
            webView.reload()
            return
        }
        webView.callAsyncJavaScript("return 1", arguments: [:], in: nil, in: .page) { [weak self] result in
            if case .success = result { self?.heartbeat.answered(now: Date()) }
        }
    }

    /// The board is in a window the owner can see, in the visible mode of an active scene.
    private var isOnScreen: Bool {
        guard isBoardVisible, let window = webView.window else { return false }
        #if os(macOS)
        return window.occlusionState.contains(.visible)
        #else
        return window.windowScene?.activationState == .foregroundActive
        #endif
    }

    // MARK: WKScriptMessageHandler

    func userContentController(_ controller: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let parsed = CockpitHostMessage(body: message.body, isMainFrame: message.frameInfo.isMainFrame)
        else { return }
        if parsed == .ready {
            hostReady = true
            heartbeat.answered(now: Date())
            if let pendingMessage { push(pendingMessage) }
        }
        onMessage(parsed)
    }

    // MARK: WKNavigationDelegate

    func webView(
        _ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction
    ) async -> WKNavigationActionPolicy {
        // A navigation without a target frame would open a window: refused.
        guard let frame = navigationAction.targetFrame else { return .cancel }
        switch CockpitNavigationPolicy.decide(
            url: navigationAction.request.url, isMainFrame: frame.isMainFrame, runtime: runtime
        ) {
        case .allow:
            return .allow
        case .cancel:
            return .cancel
        case .cancelSubframe:
            onUnresponsive()
            return .cancel
        }
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        hostReady = false
        // The system ends a hidden board's web process on its own; that is no widget's doing.
        if isOnScreen { onUnresponsive() }
        webView.reload()
    }

    // MARK: WKUIDelegate

    func webView(
        _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        nil
    }
}

extension JSONValue {
    /// The property-list form `callAsyncJavaScript` arguments take.
    var foundationValue: Any {
        switch self {
        case .string(let value): return value
        case .number(let value): return NSNumber(value: value)
        case .bool(let value): return NSNumber(value: value)
        case .object(let value): return value.mapValues(\.foundationValue)
        case .array(let value): return value.map(\.foundationValue)
        case .null: return NSNull()
        }
    }
}

/// The board's web view in SwiftUI.
struct CockpitWebView {
    let runtime: CockpitRuntime
    let allowed: Set<String>
    let message: JSONValue
    let fetch: @Sendable (String) async throws -> CockpitAssetData
    let onMessage: (CockpitHostMessage) -> Void
    let onUnresponsive: () -> Void
    let isVisible: Bool

    @MainActor
    func makeCoordinator() -> CockpitWebController { CockpitWebController(fetch: fetch) }

    @MainActor
    private func update(_ controller: CockpitWebController) {
        controller.onMessage = onMessage
        controller.onUnresponsive = onUnresponsive
        controller.isBoardVisible = isVisible
        controller.show(runtime: runtime, allowed: allowed, message: message)
    }
}

#if os(macOS)
extension CockpitWebView: NSViewRepresentable {
    func makeNSView(context: Context) -> WKWebView {
        update(context.coordinator)
        return context.coordinator.webView
    }

    func updateNSView(_ view: WKWebView, context: Context) { update(context.coordinator) }

    static func dismantleNSView(_ view: WKWebView, coordinator: CockpitWebController) { coordinator.tearDown() }
}
#else
extension CockpitWebView: UIViewRepresentable {
    func makeUIView(context: Context) -> WKWebView {
        update(context.coordinator)
        return context.coordinator.webView
    }

    func updateUIView(_ view: WKWebView, context: Context) { update(context.coordinator) }

    static func dismantleUIView(_ view: WKWebView, coordinator: CockpitWebController) { coordinator.tearDown() }
}
#endif
