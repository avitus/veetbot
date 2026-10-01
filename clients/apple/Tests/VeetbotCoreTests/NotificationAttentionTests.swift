import Foundation
import Testing
@testable import VeetbotCore

@Suite @MainActor struct NotificationAttentionTests {
    @Test func sharesReadStateInBoundedBatchesAndIgnoresUnrequestedIDs() async throws {
        let ids = (0..<101).map { _ in UUID() }
        let unrelated = UUID()
        let api = AttentionAPI(obsolete: [], unread: ids + [unrelated])
        let coordinator = NotificationAttentionCoordinator(store: AttentionStore(values: []))
        var state: Set<UUID> = []
        await coordinator.synchronize(using: api, seenRunIDs: [], queryRunIDs: ids,
            onReadState: { queried, unread in state.subtract(queried); state.formUnion(unread) }, isCurrent: { true })
        #expect(state == Set(ids))
        #expect(await api.requests.map { $0.queryRunIDs.count } == [100, 1])
        // Another device has read the first report. The next authoritative
        // response clears just that report, without clearing newer reports.
        let afterRead = AttentionAPI(obsolete: [], unread: Array(ids.dropFirst()))
        await coordinator.synchronize(using: afterRead, seenRunIDs: [], queryRunIDs: ids,
            onReadState: { queried, unread in state.subtract(queried); state.formUnion(unread) }, isCurrent: { true })
        #expect(state == Set(ids.dropFirst()))
        let failed = AttentionAPI(obsolete: [], fails: true)
        await coordinator.synchronize(using: failed, seenRunIDs: [], queryRunIDs: ids,
            onReadState: { _, _ in state = [] }, isCurrent: { true })
        #expect(state == Set(ids.dropFirst()))
        let context = AttentionContext()
        let changed = AttentionAPI(obsolete: [], unread: [], afterResponse: { await MainActor.run { context.current = false } })
        await coordinator.synchronize(using: changed, seenRunIDs: [], queryRunIDs: ids,
            onReadState: { _, _ in state = [] }, isCurrent: { context.current })
        #expect(state == Set(ids.dropFirst()))
    }

    @Test func olderSyncServerStillReceivesAcknowledgements() async throws {
        let run = UUID(), notification = UUID()
        let store = AttentionStore(values: [.init(requestIdentifier: "old-alert", notificationID: notification)])
        let api = AttentionAPI(obsolete: [notification], rejectsQueries: true)
        var published = false
        await NotificationAttentionCoordinator(store: store).synchronize(
            using: api, seenRunIDs: [run], queryRunIDs: [run],
            onReadState: { _, _ in published = true }, isCurrent: { true })
        #expect(!published)
        #expect(store.removed == ["old-alert"])
        let requests = await api.requests
        #expect(requests.count == 2)
        #expect(requests.last?.seenRunIDs == [run])
        #expect(requests.last?.queryRunIDs == [])
        let body = try JSONSerialization.jsonObject(with: JSONEncoder().encode(try #require(requests.last))) as? [String: Any]
        #expect(body?["query_run_ids"] == nil)
    }

    @Test func suppressOnlyTheVisibleConversation() throws {
        let sessionID = UUID(), runID = UUID(), notificationID = UUID()
        let payload = try #require(NotificationPushPayload(userInfo: ["veetbot": [
            "version": 1, "kind": "run_failed", "title": "Run failed", "status": "FAILED",
            "session_id": sessionID.uuidString, "run_id": runID.uuidString,
            "notification_id": notificationID.uuidString,
        ]]))
        #expect(NotificationPresentation.suppress(payload, visibleSessionID: sessionID))
        #expect(!NotificationPresentation.suppress(payload, visibleSessionID: UUID()))
        #expect(!NotificationPresentation.suppress(payload, visibleSessionID: nil))
        #expect(!NotificationPresentation.suppress(nil, visibleSessionID: sessionID))
    }

    @Test func removesOnlyConfirmedOSIdentifiersAndAcknowledgesTheViewedRun() async throws {
        let obsolete = UUID(), pending = UUID(), unrelated = UUID(), runID = UUID()
        let store = AttentionStore(values: [
            .init(requestIdentifier: "os-one", notificationID: obsolete),
            .init(requestIdentifier: "os-replay", notificationID: obsolete),
            .init(requestIdentifier: "os-two", notificationID: pending),
        ])
        let api = AttentionAPI(obsolete: [obsolete, unrelated])
        let coordinator = NotificationAttentionCoordinator(store: store)
        await coordinator.synchronize(using: api, seenRunIDs: [runID], isCurrent: { true })
        #expect(Set(store.removed) == ["os-one", "os-replay"])
        let requests = await api.requests
        #expect(requests.count == 1)
        #expect(Set(try #require(requests.first).deliveredNotificationIDs) == [obsolete, pending])
        #expect(requests.first?.seenRunIDs == [runID])
    }

    @Test func connectionChangeDuringNetworkResponsePreventsCleanup() async {
        let id = UUID()
        let context = AttentionContext()
        let store = AttentionStore(values: [.init(requestIdentifier: "keep", notificationID: id)])
        let api = AttentionAPI(obsolete: [id], afterResponse: { await MainActor.run { context.current = false } })
        await NotificationAttentionCoordinator(store: store).synchronize(using: api, seenRunIDs: [], isCurrent: { context.current })
        #expect(store.removed.isEmpty)
    }

    @Test func reconcilesMoreThanOneBatchWithoutDroppingOldAlerts() async {
        let values = (0..<201).map { DeliveredNotification(requestIdentifier: "os-\($0)", notificationID: UUID()) }
        let store = AttentionStore(values: values)
        let api = AttentionAPI(obsolete: values.map(\.notificationID))
        await NotificationAttentionCoordinator(store: store).synchronize(using: api, seenRunIDs: [UUID()], isCurrent: { true })
        #expect(store.removed.count == 201)
        let requests = await api.requests
        #expect(requests.map { $0.deliveredNotificationIDs.count } == [200, 1])
        #expect(requests.last?.seenRunIDs.isEmpty == true)
    }

    @Test func failureOrConnectionChangeKeepsAlerts() async {
        let id = UUID()
        let store = AttentionStore(values: [.init(requestIdentifier: "keep", notificationID: id)])
        let coordinator = NotificationAttentionCoordinator(store: store)
        await coordinator.synchronize(using: AttentionAPI(obsolete: [id], fails: true), seenRunIDs: [], isCurrent: { true })
        #expect(store.removed.isEmpty)
        let api = AttentionAPI(obsolete: [id])
        await coordinator.synchronize(using: api, seenRunIDs: [UUID()], isCurrent: { false })
        #expect(await api.requests.isEmpty)
        #expect(store.removed.isEmpty)
    }
}

@MainActor final class AttentionStore: DeliveredNotificationStore {
    let values: [DeliveredNotification]
    var removed: [String] = []
    init(values: [DeliveredNotification]) { self.values = values }
    func delivered() async -> [DeliveredNotification] { values }
    func remove(identifiers: [String]) { removed.append(contentsOf: identifiers) }
}

private actor AttentionAPI: NotificationSyncAPI {
    let obsolete: [UUID]
    let fails: Bool
    let unread: [UUID]?
    let rejectsQueries: Bool
    let afterResponse: (@Sendable () async -> Void)?
    var requests: [NotificationSyncRequest] = []
    init(obsolete: [UUID], fails: Bool = false, unread: [UUID]? = nil, rejectsQueries: Bool = false, afterResponse: (@Sendable () async -> Void)? = nil) {
        self.obsolete = obsolete; self.fails = fails; self.unread = unread
        self.rejectsQueries = rejectsQueries; self.afterResponse = afterResponse
    }
    func syncNotifications(_ request: NotificationSyncRequest) async throws -> NotificationSyncResult {
        requests.append(request)
        if fails { throw URLError(.notConnectedToInternet) }
        if rejectsQueries && !request.queryRunIDs.isEmpty {
            var error = APIError(code: .unknown("malformed_request"), message: "Unknown field", requestID: "old-server")
            error.statusCode = 400
            throw HTTPTransportError.api(error)
        }
        await afterResponse?()
        return NotificationSyncResult(obsoleteNotificationIDs: obsolete, unreadRunIDs: unread)
    }
}

@MainActor private final class AttentionContext { var current = true }
