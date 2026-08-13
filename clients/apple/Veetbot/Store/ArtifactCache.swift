import Foundation

public struct CachedArtifactContent: Sendable {
    public let data: Data
    public let etag: String?
}

public actor ArtifactCache {
    private var values: [UUID: CachedArtifactContent] = [:]
    private var recency: [UUID] = []
    private var totalBytes = 0
    private let maximumBytes: Int

    public init(maximumBytes: Int = 32 * 1_024 * 1_024) {
        self.maximumBytes = max(0, maximumBytes)
    }

    public func value(for artifactID: UUID) -> CachedArtifactContent? {
        guard let value = values[artifactID] else { return nil }
        markMostRecent(artifactID)
        return value
    }

    public func insert(_ value: CachedArtifactContent, for artifactID: UUID) {
        remove(artifactID)
        values[artifactID] = value
        recency.append(artifactID)
        totalBytes += value.data.count
        while totalBytes > maximumBytes, let leastRecent = recency.first {
            remove(leastRecent)
        }
    }

    public func remove(_ artifactID: UUID) {
        if let value = values.removeValue(forKey: artifactID) {
            totalBytes -= value.data.count
        }
        recency.removeAll { $0 == artifactID }
    }

    public func removeAll() {
        values.removeAll(keepingCapacity: false)
        recency.removeAll(keepingCapacity: false)
        totalBytes = 0
    }

    private func markMostRecent(_ artifactID: UUID) {
        recency.removeAll { $0 == artifactID }
        recency.append(artifactID)
    }
}
