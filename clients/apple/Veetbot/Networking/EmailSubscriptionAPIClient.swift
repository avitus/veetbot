import Foundation

extension VeetbotAPIClient {
    public func emailSubscriptions(
        accountID: String? = nil, state: String? = nil, cursor: String? = nil, limit: Int = 50
    ) async throws -> Page<EmailSubscriptionView> {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let accountID { query.append(URLQueryItem(name: "account_id", value: accountID)) }
        if let state { query.append(URLQueryItem(name: "state", value: state)) }
        if let cursor { query.append(URLQueryItem(name: "cursor", value: cursor)) }
        return try await transport.send(
            TransportRequest(method: .get, path: "/v1/email/subscriptions", queryItems: query))
    }

    /// The confirmed gesture names senders by identity, revision and evidence; never a destination.
    public func unsubscribeEmailSubscriptions(
        _ targets: [EmailUnsubscribeTarget], archiveExisting: Bool, idempotencyKey: String
    ) async throws -> EmailOperationView {
        let values: [String: JSONValue] = [
            "targets": .array(targets.map {
                .object([
                    "subscription_id": .string($0.id), "evidence_digest": .string($0.evidenceDigest),
                    "expected_revision": .number(Double($0.revision))
                ])
            }),
            "archive_existing": .bool(archiveExisting),
            "idempotency_key": .string(idempotencyKey)
        ]
        return try await subscriptionCommand(path: "/unsubscribe", values: values, key: idempotencyKey)
    }

    /// Report spam, or restore exactly the threads a report moved.
    public func reportEmailSubscription(
        _ subscription: EmailSubscriptionView, spam: Bool, idempotencyKey: String
    ) async throws -> EmailOperationView {
        try await subscriptionCommand(
            path: "/\(subscription.id)/spam",
            values: ["expected_revision": .number(Double(subscription.revision)), "spam": .bool(spam),
                     "idempotency_key": .string(idempotencyKey)],
            key: idempotencyKey)
    }

    /// A durable local decision that changes no mailbox and admits no task.
    public func keepEmailSubscription(
        _ subscription: EmailSubscriptionView, kept: Bool
    ) async throws -> EmailSubscriptionView {
        try await transport.send(TransportRequest(
            method: .post, path: "/v1/email/subscriptions/\(subscription.id)/keep",
            body: try JSONEncoder.server.encode([
                "expected_revision": JSONValue.number(Double(subscription.revision)),
                "kept": JSONValue.bool(kept)
            ])
        ))
    }

    private func subscriptionCommand<Result: Decodable>(
        path: String, values: [String: JSONValue], key: String
    ) async throws -> Result {
        try await transport.send(TransportRequest(
            method: .post, path: "/v1/email/subscriptions\(path)",
            body: try JSONEncoder.server.encode(values),
            headers: ["Idempotency-Key": key], retryAttempts: 2
        ))
    }
}
