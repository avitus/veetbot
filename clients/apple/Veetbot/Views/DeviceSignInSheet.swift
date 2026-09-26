import SwiftUI
import WebKit

#if os(iOS)
import UIKit
#endif

/// The private sign-in window of a device sign-in (ADR-0128, 0128-design §5.1).
/// It owns a non-persistent website data store, the visible web view and the
/// navigation rules, and it reads the session only when the owner taps I'm
/// signed in. Nothing it holds is persisted, logged or kept in view state, and
/// `destroy()` clears the store and releases the web views.
@MainActor
final class DeviceSignInWebSession: NSObject, ObservableObject {
    /// Reads `localStorage` as `[name, value]` pairs (§2.5 item 4).
    static let storageScript =
        "const o=[];for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);o.push([k,localStorage.getItem(k)]);}return JSON.stringify(o);"

    @Published private(set) var currentURL: URL?
    @Published private(set) var isLoading = false
    @Published private(set) var blockedHost: String?
    @Published private(set) var allowedOrigins: [String]
    /// The origin a new profile adopted from its first load (D11), if any.
    @Published private(set) var adoptedOrigin: String?

    private(set) var webView: WKWebView?
    private var dataStore: WKWebsiteDataStore?
    private var adoptableOrigin: String?
    private var visitedOrigins: Set<String> = []
    private var observations: [NSKeyValueObservation] = []

    init(request: DeviceSignInRequest) {
        allowedOrigins = request.allowedOrigins
        adoptableOrigin = request.profileID == nil ? request.adoptableOrigin : nil
        super.init()
        let store = Self.makeDataStore()
        let webView = WKWebView(frame: .zero, configuration: Self.makeConfiguration(dataStore: store))
        webView.navigationDelegate = self
        webView.uiDelegate = self
        observations = [
            webView.observe(\.url, options: [.initial, .new]) { [weak self] view, _ in
                let url = view.url
                Task { @MainActor in self?.currentURL = url }
            },
            webView.observe(\.isLoading, options: [.initial, .new]) { [weak self] view, _ in
                let loading = view.isLoading
                Task { @MainActor in self?.isLoading = loading }
            },
        ]
        dataStore = store
        self.webView = webView
        webView.load(URLRequest(url: request.startURL))
    }

    /// A configuration that keeps nothing and runs nothing of Veetbot's in the
    /// page: a non-persistent store, no user script or message handler, the
    /// platform's own user agent, and no window the page opens by itself
    /// (§2.5 item 1).
    static func makeConfiguration(dataStore: WKWebsiteDataStore) -> WKWebViewConfiguration {
        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = dataStore
        configuration.userContentController = WKUserContentController()
        configuration.preferences.javaScriptCanOpenWindowsAutomatically = false
        #if os(iOS)
        configuration.dataDetectorTypes = []
        #endif
        return configuration
    }

    static func makeDataStore() -> WKWebsiteDataStore {
        WKWebsiteDataStore.nonPersistent()
    }

    var canConfirm: Bool {
        DeviceSignInNavigationPolicy.canConfirm(
            currentURL: currentURL, isLoading: isLoading, allowedOrigins: Set(allowedOrigins)
        )
    }

    /// The in-scope cookies and the allowed origins' storage of the page the
    /// owner confirmed on (§2.5 item 4). Other visited allowed origins are read
    /// through a hidden web view on the same store; if that fails, only the
    /// current origin's storage is sent (D15).
    func collect() async throws -> DeviceSessionHandoff {
        guard let webView, let dataStore, let current = webView.url else {
            throw WebsiteSessionScopeError.invalid("page")
        }
        let scope = WebsiteSessionScope(allowedOrigins: allowedOrigins)
        let cookies = await dataStore.httpCookieStore.allCookies()
        var storage: [String: [HandoffStorageItem]] = [:]
        if let origin = WebsiteSessionScope.origin(of: current), scope.allowedOrigins.contains(origin) {
            storage[origin] = try await Self.readStorage(in: webView, scope: scope)
        }
        for origin in visitedOrigins.sorted()
        where storage[origin] == nil && scope.allowedOrigins.contains(origin) {
            storage[origin] = try? await readStorage(ofOrigin: origin, scope: scope, dataStore: dataStore)
        }
        guard
            let handoff = scope.handoff(
                confirmedURL: current, cookies: cookies, localStorage: storage, now: Date()
            )
        else { throw WebsiteSessionScopeError.invalid("page") }
        return handoff
    }

    /// Stops the page, clears every website data type from the store, and
    /// releases the web views (§2.5 item 7). Safe to call more than once.
    func destroy() async {
        observations.forEach { $0.invalidate() }
        observations = []
        webView?.stopLoading()
        webView?.navigationDelegate = nil
        webView?.uiDelegate = nil
        webView = nil
        let store = dataStore
        dataStore = nil
        await store?.removeData(
            ofTypes: WKWebsiteDataStore.allWebsiteDataTypes(), modifiedSince: .distantPast
        )
    }

    private static func readStorage(
        in webView: WKWebView, scope: WebsiteSessionScope
    ) async throws -> [HandoffStorageItem] {
        let result = try await webView.callAsyncJavaScript(
            storageScript, arguments: [:], in: nil, contentWorld: .defaultClient
        )
        guard let json = result as? String else { throw WebsiteSessionScopeError.notAPair }
        return try scope.storageItems(fromJSON: json)
    }

    private func readStorage(
        ofOrigin origin: String, scope: WebsiteSessionScope, dataStore: WKWebsiteDataStore
    ) async throws -> [HandoffStorageItem] {
        guard let base = URL(string: origin + "/") else { throw WebsiteSessionScopeError.invalid("origin") }
        let hidden = WKWebView(frame: .zero, configuration: Self.makeConfiguration(dataStore: dataStore))
        let loader = BlankPageLoader()
        hidden.navigationDelegate = loader
        defer {
            hidden.stopLoading()
            hidden.navigationDelegate = nil
        }
        try await loader.load(in: hidden, baseURL: base)
        return try await Self.readStorage(in: hidden, scope: scope)
    }

    private func adopt(_ origin: String) {
        allowedOrigins = [origin]
        adoptedOrigin = origin
        adoptableOrigin = nil
    }
}

extension DeviceSignInWebSession: WKNavigationDelegate, WKUIDelegate {
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        let decision = DeviceSignInNavigationPolicy.decide(
            url: navigationAction.request.url,
            isMainFrame: navigationAction.targetFrame?.isMainFrame ?? true,
            allowedOrigins: Set(allowedOrigins),
            adoptableOrigin: adoptableOrigin
        )
        switch decision {
        case .allow:
            decisionHandler(.allow)
        case .adopt(let origin):
            adopt(origin)
            decisionHandler(.allow)
        case .cancel(let host):
            blockedHost = host
            decisionHandler(.cancel)
        }
    }

    /// No downloads: a response the view cannot show is cancelled.
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void
    ) {
        decisionHandler(navigationResponse.canShowMIMEType ? .allow : .cancel)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // Only the first load may adopt the bare or www origin (D11).
        adoptableOrigin = nil
        if let url = webView.url, let origin = WebsiteSessionScope.origin(of: url),
            allowedOrigins.contains(origin)
        {
            visitedOrigins.insert(origin)
            blockedHost = nil
        }
    }

    /// No popups.
    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        nil
    }
}

/// Loads an empty page on an origin so its `localStorage` can be read.
@MainActor
private final class BlankPageLoader: NSObject, WKNavigationDelegate {
    private var continuation: CheckedContinuation<Void, Error>?

    func load(in webView: WKWebView, baseURL: URL) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, Error>) in
            self.continuation = continuation
            webView.loadHTMLString("", baseURL: baseURL)
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                self?.finish(WebsiteSessionScopeError.invalid("storage"))
            }
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        finish(nil)
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        finish(WebsiteSessionScopeError.invalid("storage"))
    }

    func webView(
        _ webView: WKWebView,
        didFailProvisionalNavigation navigation: WKNavigation!,
        withError error: Error
    ) {
        finish(WebsiteSessionScopeError.invalid("storage"))
    }

    private func finish(_ error: Error?) {
        guard let continuation else { return }
        self.continuation = nil
        if let error {
            continuation.resume(throwing: error)
        } else {
            continuation.resume()
        }
    }
}

#if os(macOS)
struct DeviceSignInWebView: NSViewRepresentable {
    let webView: WKWebView?

    func makeNSView(context: Context) -> NSView {
        webView ?? NSView()
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}
#else
struct DeviceSignInWebView: UIViewRepresentable {
    let webView: WKWebView?

    func makeUIView(context: Context) -> UIView {
        webView ?? UIView()
    }

    func updateUIView(_ uiView: UIView, context: Context) {}
}
#endif

/// Sign in on this device: the website in a private window, then I'm signed
/// in hands only that website's session to Veetbot's isolated browser, once.
struct DeviceSignInSheet: View {
    @ObservedObject var model: ChatViewModel
    let request: DeviceSignInRequest
    @StateObject private var session: DeviceSignInWebSession
    @State private var isConfirming = false
    @State private var failure: DeviceSignInFailure?

    init(model: ChatViewModel, request: DeviceSignInRequest) {
        self.model = model
        self.request = request
        _session = StateObject(wrappedValue: DeviceSignInWebSession(request: request))
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if let blockedHost = session.blockedHost {
                Label(
                    blockedHost.isEmpty
                        ? "That page is outside this website, so it was not opened."
                        : "\(blockedHost) is outside this website, so it was not opened.",
                    systemImage: "hand.raised.fill"
                )
                .appFont(.caption)
                .foregroundColor(AppTheme.orange)
                .padding(.horizontal, 16)
                .padding(.vertical, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(AppTheme.orange.opacity(0.08))
                .accessibilityIdentifier("device-sign-in.blocked")
            }
            DeviceSignInWebView(webView: session.webView)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            footer
        }
        // Like Cancel, a swipe on the iPad sheet cannot close the window
        // while the sign-in is being checked.
        .interactiveDismissDisabled(isConfirming)
        .onDisappear {
            Task { await session.destroy() }
        }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(session.currentURL?.host ?? request.startURL.host ?? "Website")
                    .appFont(.headline)
                    .lineLimit(1)
                Text(
                    "Sign in to the website here. Veetbot does not see what you type. When you are signed in, tap I'm signed in: only this website's session goes to Veetbot's isolated browser, and this window is cleared."
                )
                .appFont(.caption)
                .foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Button("Cancel") {
                Task {
                    await session.destroy()
                    await model.abandonDeviceSignIn()
                }
            }
            .disabled(isConfirming)
            .accessibilityIdentifier("device-sign-in.cancel")
        }
        .padding(16)
    }

    private var footer: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let failure {
                Label(failure.message, systemImage: "exclamationmark.triangle.fill")
                    .appFont(.caption)
                    .foregroundColor(.red)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityIdentifier("device-sign-in.error")
                if failure.offersRemoteBrowser {
                    Button("Use Veetbot's remote browser") {
                        Task {
                            await session.destroy()
                            await model.switchDeviceSignInToRemoteBrowser(request)
                        }
                    }
                    .accessibilityIdentifier("device-sign-in.remote-browser")
                }
            }
            HStack(spacing: 12) {
                if isConfirming {
                    ProgressView().controlSize(.small)
                    Text("Checking the sign-in…")
                        .appFont(.caption)
                        .foregroundColor(.secondary)
                }
                Spacer()
                Button("I'm signed in") {
                    confirm()
                }
                .buttonStyle(.borderedProminent)
                .tint(AppTheme.turquoise)
                .disabled(isConfirming || !DeviceSignInNavigationPolicy.canConfirm(
                    currentURL: session.currentURL,
                    isLoading: session.isLoading,
                    allowedOrigins: Set(session.allowedOrigins)
                ) || (failure.map { !$0.canRetry } ?? false))
                .accessibilityIdentifier("device-sign-in.confirm")
            }
        }
        .padding(16)
    }

    /// The session exists only in this task's locals until the view model
    /// hands it over.
    private func confirm() {
        isConfirming = true
        failure = nil
        Task {
            defer { isConfirming = false }
            let result: DeviceSignInResult
            do {
                let handoff = try await session.collect()
                result = await model.completeDeviceSignIn(
                    request, handoff: handoff, adoptedOrigin: session.adoptedOrigin
                )
            } catch {
                result = .failed(
                    DeviceSignInFailure(
                        message: DeviceSignInMessage.couldNotStart,
                        canRetry: true,
                        offersRemoteBrowser: request.profileID == nil
                    )
                )
            }
            switch result {
            case .signedIn:
                await session.destroy()
                model.finishDeviceSignIn(request)
            case .failed(let reason):
                failure = reason
            }
        }
    }
}

extension View {
    /// Presents the device sign-in window: full screen on iPhone, where the
    /// website needs the height; a large sheet on iPad; a sheet of at least
    /// 820 by 680 points on the Mac. Dismissing it abandons the sign-in.
    func deviceSignInPresentation(model: ChatViewModel) -> some View {
        modifier(DeviceSignInPresentation(model: model))
    }
}

private struct DeviceSignInPresentation: ViewModifier {
    @ObservedObject var model: ChatViewModel

    private var request: Binding<DeviceSignInRequest?> {
        Binding(
            get: { model.deviceSignInRequest },
            set: { value in
                if value == nil, model.deviceSignInRequest != nil {
                    Task { await model.abandonDeviceSignIn() }
                }
            }
        )
    }

    func body(content: Content) -> some View {
        #if os(iOS)
        if UIDevice.current.userInterfaceIdiom == .phone {
            content.fullScreenCover(item: request) { request in
                DeviceSignInSheet(model: model, request: request)
            }
        } else {
            content.sheet(item: request) { request in
                DeviceSignInSheet(model: model, request: request)
            }
        }
        #else
        content.sheet(item: request) { request in
            DeviceSignInSheet(model: model, request: request)
                .frame(minWidth: 820, minHeight: 680)
        }
        #endif
    }
}
