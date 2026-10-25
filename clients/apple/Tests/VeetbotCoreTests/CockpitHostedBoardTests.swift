#if os(macOS)
import AppKit
import CryptoKit
import Foundation
import Network
import Testing
import WebKit

@testable import VeetbotCore

/// docs/plan/cockpit.md, gates 16 and 17's hosted halves: the real exported
/// runtime in a real `WKWebView`, configured exactly as the app configures it.
/// A widget renders through the scheme handler, while one that tries every web
/// escape against a listener on this machine never reaches it, and the page
/// holds no credential. The runtime comes from `cockpit/dist/runtime`, which
/// `npm --prefix cockpit run build` writes; the native gate builds it first.
@MainActor
@Suite(.serialized) struct CockpitHostedBoardTests {
    nonisolated static let repository = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent()
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    nonisolated static let runtimeDirectory = repository.appendingPathComponent("cockpit/dist/runtime")
    nonisolated static var runtimeBuilt: Bool {
        FileManager.default.fileExists(atPath: runtimeDirectory.appendingPathComponent("manifest.json").path)
    }

    final class Listener: @unchecked Sendable {
        let listener: NWListener
        private let lock = NSLock()
        private var _connections = 0
        var connections: Int { lock.withLock { _connections } }

        init() throws {
            let parameters = NWParameters.tcp
            parameters.requiredLocalEndpoint = NWEndpoint.hostPort(host: "127.0.0.1", port: .any)
            listener = try NWListener(using: parameters)
            listener.newConnectionHandler = { [weak self] connection in
                self?.lock.withLock { self?._connections += 1 }
                connection.cancel()
            }
        }

        func start() async throws -> UInt16 {
            listener.start(queue: .global())
            for _ in 0..<500 {
                if let port = listener.port?.rawValue, port != 0 { return port }
                try await Task.sleep(nanoseconds: 2_000_000)
            }
            throw URLError(.cannotConnectToHost)
        }
    }

    struct Runtime {
        let runtime: CockpitRuntime
        let files: [String: (Data, String)]
    }

    static func loadRuntime() throws -> Runtime {
        let manifest = try #require(
            JSONSerialization.jsonObject(with: Data(contentsOf: runtimeDirectory.appendingPathComponent("manifest.json")))
                as? [String: Any]
        )
        let assets = try #require(manifest["assets"] as? [String: [String: Any]])
        var digests: [String: String] = [:]
        var files: [String: (Data, String)] = [:]
        for (logical, asset) in assets {
            let name = try #require(asset["name"] as? String)
            let digest = try #require(asset["sha256"] as? String)
            digests[logical] = digest
            let type = name.hasSuffix(".html") ? "text/html; charset=utf-8"
                : name.hasSuffix(".css") ? "text/css; charset=utf-8" : "text/javascript; charset=utf-8"
            files[digest] = (try Data(contentsOf: runtimeDirectory.appendingPathComponent(name)), type)
        }
        let entry = try #require((manifest["entry"] as? String)?.split(separator: ".").first.map(String.init))
        return Runtime(runtime: CockpitRuntime(kitAPI: 1, entry: entry, assets: digests), files: files)
    }

    static func digest(_ data: Data) -> String {
        SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }

    static func probeBundle() -> Data {
        Data("""
        (function(){var R=window.React,K=window.VeetbotKit;
        window.VeetbotRealm.register(function Probe(){return R.createElement(K.Tile,{title:"Probe"});});})();
        """.utf8)
    }

    /// Every web escape a widget can try in one render, aimed at this machine.
    static func escapeBundle(port: UInt16) -> Data {
        let target = "http://127.0.0.1:\(port)"
        return Data("""
        (function(){var R=window.React,K=window.VeetbotKit,T="\(target)";
        function tryAll(){
          try{fetch(T+"/fetch").catch(function(){});}catch(e){}
          try{var x=new XMLHttpRequest();x.open("GET",T+"/xhr");x.send();}catch(e){}
          try{new Image().src=T+"/image";}catch(e){}
          try{navigator.sendBeacon&&navigator.sendBeacon(T+"/beacon","x");}catch(e){}
          try{var s=document.createElement("script");s.src=T+"/script";document.head.appendChild(s);}catch(e){}
          try{var l=document.createElement("link");l.rel="stylesheet";l.href=T+"/style";document.head.appendChild(l);}catch(e){}
          try{var f=document.createElement("iframe");f.src=T+"/frame";document.body.appendChild(f);}catch(e){}
          try{new WebSocket("ws://127.0.0.1:\(port)/socket");}catch(e){}
          try{new EventSource(T+"/events");}catch(e){}
          try{window.open(T+"/popup");}catch(e){}
        }
        // Throwing after the attempts reports tile_error, which proves they ran.
        window.VeetbotRealm.register(function Escape(){R.useEffect(function(){tryAll();throw new Error("attempted");},[]);return R.createElement(K.Tile,{title:"Escape"});});})();
        """.utf8)
    }

    /// A widget that tries to take the realm somewhere else.
    static func navigateBundle(port: UInt16) -> Data {
        Data("""
        (function(){var R=window.React,K=window.VeetbotKit;
        window.VeetbotRealm.register(function Navigate(){R.useEffect(function(){
          setTimeout(function(){window.location.href="http://127.0.0.1:\(port)/navigated";},50);
        },[]);return R.createElement(K.Tile,{title:"Navigate"});});})();
        """.utf8)
    }

    func widget(_ slug: String, bundle: Data, revision: Int) -> CockpitWidget {
        CockpitWidget(
            slug: slug,
            revisionID: UUID(uuidString: String(format: "00000000-0000-0000-0000-%012d", revision))!,
            bundleSHA256: Self.digest(bundle),
            manifest: ["title": .string(slug.capitalized), "sizes": .array([.string("md")]), "feeds": .array([])],
            changedAt: Date(timeIntervalSince1970: 1_793_000_000 + Double(revision))
        )
    }

    @Test(.enabled(if: CockpitHostedBoardTests.runtimeBuilt, "the runtime is built by npm --prefix cockpit run build"))
    func aHostedBoardRendersItsWidgetsAndNoEscapeReachesTheNetwork() async throws {
        let listener = try Listener()
        let port = try await listener.start()
        defer { listener.listener.cancel() }
        let runtime = try Self.loadRuntime()
        let probe = Self.probeBundle()
        let escape = Self.escapeBundle(port: port)
        var files = runtime.files
        files[Self.digest(probe)] = (probe, "text/javascript; charset=utf-8")
        files[Self.digest(escape)] = (escape, "text/javascript; charset=utf-8")
        let served = files
        let snapshot = CockpitSnapshot(
            cursor: 1, runtime: runtime.runtime,
            widgets: [widget("probe", bundle: probe, revision: 1), widget("escape", bundle: escape, revision: 2)]
        )
        let controller = CockpitWebController { digest in
            guard let file = served[digest] else { throw URLError(.fileDoesNotExist) }
            return CockpitAssetData(content: file.0, contentType: file.1)
        }
        var messages: [CockpitHostMessage] = []
        var unresponsive = 0
        controller.onMessage = { messages.append($0) }
        controller.onUnresponsive = { unresponsive += 1 }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 600), styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = controller.webView
        defer { controller.tearDown(); window.close() }

        controller.show(
            runtime: runtime.runtime, allowed: CockpitBoard.allowedAssets(snapshot),
            message: CockpitBoard.hostSnapshot(
                snapshot, deviceClass: .wide, theme: "light", platform: "mac",
                offsetMilliseconds: 0, excluded: [], selected: nil
            )
        )
        func loaded(_ slug: String) -> Bool {
            messages.contains { if case .loaded(slug, _) = $0 { true } else { false } }
        }
        func attempted() -> Bool {
            messages.contains { if case .tileError("escape", _) = $0 { true } else { false } }
        }
        for _ in 0..<300 where !(loaded("probe") && loaded("escape") && attempted()) {
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        #expect(messages.first == .ready)
        #expect(loaded("probe"))
        #expect(loaded("escape"))
        #expect(attempted())
        // Give every escape time to arrive if anything let it through.
        try await Task.sleep(nanoseconds: 2_000_000_000)
        #expect(listener.connections == 0)
        #expect(unresponsive == 0)
        // The listener does accept a connection from outside the web view.
        _ = try? await URLSession.shared.data(from: URL(string: "http://127.0.0.1:\(port)/control")!)
        #expect(listener.connections >= 1)
        // The page holds no credential: no cookie, no storage, nothing beyond the host's bridge.
        let probeResult = try await controller.webView.callAsyncJavaScript(
            "return [document.cookie, typeof localStorage === 'object' ? localStorage.length : -1, typeof window.webkit.messageHandlers.cockpit.postMessage]",
            arguments: [:], contentWorld: .page
        )
        let probed = try #require(probeResult as? [Any])
        #expect(probed[0] as? String == "")
        #expect((probed[1] as? NSNumber)?.intValue ?? -1 <= 0)
        #expect(probed[2] as? String == "function")
    }

    @Test(.enabled(if: CockpitHostedBoardTests.runtimeBuilt, "the runtime is built by npm --prefix cockpit run build"))
    func aWidgetThatNavigatesIsCutOffAndReported() async throws {
        let listener = try Listener()
        let port = try await listener.start()
        defer { listener.listener.cancel() }
        let runtime = try Self.loadRuntime()
        let navigate = Self.navigateBundle(port: port)
        var files = runtime.files
        files[Self.digest(navigate)] = (navigate, "text/javascript; charset=utf-8")
        let served = files
        let snapshot = CockpitSnapshot(
            cursor: 1, runtime: runtime.runtime, widgets: [widget("navigate", bundle: navigate, revision: 3)]
        )
        let controller = CockpitWebController { digest in
            guard let file = served[digest] else { throw URLError(.fileDoesNotExist) }
            return CockpitAssetData(content: file.0, contentType: file.1)
        }
        var unresponsive = 0
        var messages: [CockpitHostMessage] = []
        controller.onUnresponsive = { unresponsive += 1 }
        controller.onMessage = { messages.append($0) }
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 600, height: 400), styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = controller.webView
        defer { controller.tearDown(); window.close() }
        controller.show(
            runtime: runtime.runtime, allowed: CockpitBoard.allowedAssets(snapshot),
            message: CockpitBoard.hostSnapshot(
                snapshot, deviceClass: .wide, theme: "light", platform: "mac",
                offsetMilliseconds: 0, excluded: [], selected: nil
            )
        )
        // Either the host sees its realm leave and quarantines the widget, or the
        // navigation policy refuses it and native code quarantines the newest.
        func cutOff() -> Bool {
            unresponsive > 0 || messages.contains { if case .quarantined("navigate", _) = $0 { true } else { false } }
        }
        for _ in 0..<200 where !cutOff() { try await Task.sleep(nanoseconds: 50_000_000) }
        #expect(cutOff(), "the navigating widget is quarantined: \(messages)")
        try await Task.sleep(nanoseconds: 500_000_000)
        #expect(listener.connections == 0)
    }
}
#endif
