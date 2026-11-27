#if os(macOS)
import SwiftUI
import WebKit

/// An immutable attachment and the last matching reply already present when it opened.
struct GeneratedPageRequest: Codable, Hashable {
    let sessionID: UUID
    let connection: UUID
    let artifactID: UUID
    let name: String
    private let lastAtOpen: UUID?

    init(sessionID: UUID, connection: UUID, artifactID: UUID, name: String, timeline: [TimelineItem]) {
        self.sessionID = sessionID
        self.connection = connection
        self.artifactID = artifactID
        self.name = name
        lastAtOpen = Self.revisions(named: name, in: timeline).last
    }

    @MainActor
    init?(model: ChatViewModel, artifactID: UUID, name: String, mediaType: String) {
        guard let sessionID = model.selectedSessionID, Self.isHTML(mediaType, name: name) else { return nil }
        self.init(sessionID: sessionID, connection: model.connectionGeneration, artifactID: artifactID,
                  name: name, timeline: model.runState.timeline)
    }

    static func isHTML(_ mediaType: String, name: String) -> Bool {
        let mime = mediaType.components(separatedBy: ";")[0].trimmingCharacters(in: .whitespaces).lowercased()
        if mime == "text/html" { return true }
        guard ["application/octet-stream", "text/plain", ""].contains(mime) else { return false }
        return ["html", "htm"].contains((name as NSString).pathExtension.lowercased())
    }

    private static func revisions(named name: String, in timeline: [TimelineItem]) -> [UUID] {
        timeline.filter { $0.role == .assistant }.flatMap { message in
            message.content.compactMap { block -> UUID? in
                guard case .file(let id, let mime, let filename) = block, filename == name,
                      isHTML(mime, name: name) else { return nil }
                return id
            }
        }
    }

    func displayedID(in timeline: [TimelineItem]) -> UUID {
        let revisions = Self.revisions(named: name, in: timeline)
        guard let latest = revisions.last else { return artifactID }
        if let lastAtOpen {
            guard let index = revisions.lastIndex(of: lastAtOpen), index < revisions.count - 1 else { return artifactID }
        }
        return latest
    }
}

struct GeneratedPageWindow: View {
    @ObservedObject var model: ChatViewModel
    @ObservedObject var state: RunStateReducer
    let request: GeneratedPageRequest

    var body: some View {
        Group {
            if model.connectionGeneration == request.connection, model.selectedSessionID == request.sessionID {
                let id = request.displayedID(in: state.timeline)
                GeneratedPageDocument(model: model, request: request, artifactID: id)
                    .id(id)
            } else {
                ContentUnavailableView("Page unavailable", systemImage: "doc",
                                       description: Text("Open the attachment in its conversation."))
            }
        }
        .navigationTitle(Text(verbatim: request.name))
        .frame(minWidth: 320, minHeight: 240)
    }
}

/// Only the in-memory document and its fragment links may navigate this view.
private struct PreviewNavigation: WebPage.NavigationDeciding {
    func decidePolicy(for action: WebPage.NavigationAction,
                      preferences: inout WebPage.NavigationPreferences) async -> WKNavigationActionPolicy {
        guard action.target?.isMainFrame == true, !action.shouldPerformDownload,
              let url = action.request.url,
              url.absoluteString == "about:blank" || url.absoluteString.hasPrefix("about:blank#")
        else { return .cancel }
        return .allow
    }
}

struct GeneratedPageDocument: View {
    let model: ChatViewModel
    let request: GeneratedPageRequest
    let artifactID: UUID
    @State private var page: WebPage?
    @State private var errorMessage: String?
    @State private var attempt = 0

    static func makePage() -> WebPage {
        var config = WebPage.Configuration()
        config.websiteDataStore = .nonPersistent()
        config.defaultNavigationPreferences.allowsContentJavaScript = false
        config.loadsSubresources = false
        return WebPage(configuration: config, navigationDecider: PreviewNavigation())
    }

    var body: some View {
        Group {
            if let errorMessage {
                ContentUnavailableView {
                    Label("Couldn’t open page", systemImage: "doc")
                } description: {
                    Text(errorMessage)
                } actions: {
                    Button("Try Again") { attempt += 1 }
                }
            } else if let page {
                WebView(page)
                    .webViewLinkPreviews(.disabled)
            } else {
                ProgressView("Opening page…")
            }
        }
        .task(id: attempt) {
            errorMessage = nil
            do {
                let artifact = try await model.loadArtifact(artifactID)
                try Task.checkCancellation()
                guard artifact.metadata.id == artifactID, artifact.metadata.sessionID == request.sessionID,
                      model.connectionGeneration == request.connection, model.selectedSessionID == request.sessionID,
                      GeneratedPageRequest.isHTML(artifact.metadata.mediaType, name: artifact.metadata.name)
                else { throw HTTPTransportError.invalidResponse }

                let document = Self.makePage()
                page = document
                for try await _ in document.load(artifact.data, mimeType: "text/html", characterEncoding: .utf8,
                                                 baseURL: URL(string: "about:blank")!) {}
            } catch {
                guard !Task.isCancelled else { return }
                page = nil
                errorMessage = error.localizedDescription
            }
        }
    }
}
#endif
