import Foundation
import Testing

@testable import VeetbotCore

struct CallNotificationTests {
    @Test
    func acceptsOnlyContentFreeCallIdentity() throws {
        var payload: [String: Any] = [
            "version": 1, "kind": "call_finished", "title": "New call result",
            "call_id": UUID().uuidString, "notification_id": UUID().uuidString,
        ]
        let decoded = NotificationPushPayload(userInfo: ["veetbot": payload])
        #expect(decoded != nil)
        payload["transcript"] = "Untrusted caller content"
        #expect(NotificationPushPayload(userInfo: ["veetbot": payload]) == nil)
        payload.removeValue(forKey: "transcript")
        payload["session_id"] = UUID().uuidString
        #expect(NotificationPushPayload(userInfo: ["veetbot": payload]) == nil)
    }
}
