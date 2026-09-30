import Foundation
import Testing
@testable import VeetbotCore

@Suite @MainActor struct NotificationAttentionTests {
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
    let afterResponse: (@Sendable () async -> Void)?
    var requests: [NotificationSyncRequest] = []
    init(obsolete: [UUID], fails: Bool = false, afterResponse: (@Sendable () async -> Void)? = nil) { self.obsolete = obsolete; self.fails = fails; self.afterResponse = afterResponse }
    func syncNotifications(_ request: NotificationSyncRequest) async throws -> NotificationSyncResult {
        requests.append(request)
        if fails { throw URLError(.notConnectedToInternet) }
        await afterResponse?()
        return NotificationSyncResult(obsoleteNotificationIDs: obsolete)
    }
}

@MainActor private final class AttentionContext { var current = true }
