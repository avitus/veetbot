import CryptoKit
import Foundation

#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

public protocol DeviceRegistrationAPI: Sendable {
    var notificationServerID: String { get }

    func registerDevice(
        _ body: AppleDeviceRegistration,
        idempotencyKey: String
    ) async throws -> DeviceView
    func listDevices(limit: Int, cursor: String?) async throws -> Page<DeviceView>
    func revokeDevice(_ deviceID: UUID) async throws -> DeviceView
}

public struct AppleDeviceDescriptor: Equatable, Sendable {
    public let name: String
    public let kind: DeviceKind
    public let platform: String
    public let bundleID: String
    public let environment: PushEnvironment
    public let capabilities: [String]
    let registrationRevision: String?

    public init(
        name: String,
        kind: DeviceKind,
        platform: String,
        bundleID: String,
        environment: PushEnvironment,
        capabilities: [String] = [],
        registrationRevision: String? = nil
    ) {
        self.name = name
        self.kind = kind
        self.platform = platform
        self.bundleID = bundleID
        self.environment = environment
        self.capabilities = capabilities
        self.registrationRevision = registrationRevision
    }

    /// This device as the server should see it, declaring whichever
    /// device-scoped capabilities the owner has switched on. The capability
    /// set rides into the registration digest, so a change re-registers rather
    /// than replaying the previous body.
    @MainActor
    public static func current(capabilities: [String] = [], defaults: UserDefaults = .standard) -> AppleDeviceDescriptor {
        #if os(iOS)
        let name = UIDevice.current.name
        let kind = DeviceKind.mobile
        let platform = "ios"
        let revision: String? = nil
        #elseif os(macOS)
        let name = Host.current().localizedName ?? "Mac"
        let kind = DeviceKind.desktop
        let platform = "macos"
        // A fresh negotiation after a push-only fallback must reach the server even if
        // this same grant succeeded earlier. Retries of this descriptor keep its key.
        let revision = capabilities.contains(where: { $0.hasPrefix("device.folder.write_") })
            ? UUID().uuidString : WritableFolder.registrationRevision(defaults: defaults)
        #endif
        #if DEBUG
        let environment = PushEnvironment.sandbox
        #else
        let environment = PushEnvironment.production
        #endif
        return AppleDeviceDescriptor(
            name: name,
            kind: kind,
            platform: platform,
            bundleID: Bundle.main.bundleIdentifier ?? "com.veetbot.apple",
            environment: environment,
            capabilities: capabilities,
            registrationRevision: revision
        )
    }
}

public enum DeviceRegistrationOutcome: Equatable, Sendable {
    case registered(UUID, folderCapability: String? = nil)
    case unsupported
}

public enum DeviceRevocationOutcome: Equatable, Sendable {
    case revoked(UUID)
    case notRegistered
    case unsupported
}

public actor DeviceRegistrationCoordinator {
    private let identityStore: any InstallationIdentityStore
    private var registeredDeviceIDs: [String: UUID] = [:]

    public init(
        identityStore: any InstallationIdentityStore = KeychainInstallationIdentityStore()
    ) {
        self.identityStore = identityStore
    }

    public func register(
        deviceToken: Data,
        descriptor: AppleDeviceDescriptor,
        using api: any DeviceRegistrationAPI
    ) async throws -> DeviceRegistrationOutcome {
        let installationID = try await identityStore.readOrCreateInstallationID()
        func upload(capabilities: [String]) async throws -> DeviceView {
            let body = AppleDeviceRegistration(
                clientDeviceID: installationID,
                name: descriptor.name,
                kind: descriptor.kind,
                platform: descriptor.platform,
                appBundleID: descriptor.bundleID,
                pushToken: deviceToken.map { String(format: "%02x", $0) }.joined(),
                pushEnvironment: descriptor.environment,
                capabilities: capabilities
            )
            return try await api.registerDevice(
                body,
                idempotencyKey: try Self.registrationIdempotencyKey(for: body, revision: descriptor.registrationRevision)
            )
        }
        do {
            let device: DeviceView
            do {
                device = try await upload(capabilities: descriptor.capabilities)
            } catch HTTPTransportError.api(let error) where descriptor.platform == "macos"
                && descriptor.capabilities.count == 1
                && descriptor.capabilities[0].hasPrefix("device.folder.write_")
                && error.statusCode == 422 && error.code.rawValue == "device_validation_error"
                && (error.details.reason == "device.folder_disabled" || error.details.reason == "device.capability_unknown") {
                device = try await upload(capabilities: [])
            }
            registeredDeviceIDs[api.notificationServerID] = device.id
            return .registered(device.id, folderCapability: device.capabilities.first { $0.hasPrefix("device.folder.write_") })
        } catch {
            if Self.isUnsupported(error) { return .unsupported }
            throw error
        }
    }

    private static func registrationIdempotencyKey(
        for body: AppleDeviceRegistration, revision: String?
    ) throws -> String {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        let digest = SHA256.hash(data: try encoder.encode(body))
            .map { String(format: "%02x", $0) }
            .joined()
        // Removing a grant can recreate a previous registration body. Give each local
        // selection change a distinct key so a historical replay cannot restore the old state.
        if let revision {
            return "\(body.clientDeviceID):\(digest):\(revision)"
        }
        return "\(body.clientDeviceID):\(digest)"
    }

    public func revoke(
        using api: any DeviceRegistrationAPI
    ) async throws -> DeviceRevocationOutcome {
        do {
            let deviceID: UUID?
            if let registered = registeredDeviceIDs[api.notificationServerID] {
                deviceID = registered
            } else {
                let installationID = try await identityStore.readOrCreateInstallationID()
                deviceID = try await findDeviceID(
                    installationID: installationID,
                    using: api
                )
            }
            guard let deviceID else { return .notRegistered }
            _ = try await api.revokeDevice(deviceID)
            registeredDeviceIDs.removeValue(forKey: api.notificationServerID)
            return .revoked(deviceID)
        } catch {
            if Self.isUnsupported(error) { return .unsupported }
            throw error
        }
    }

    private func findDeviceID(
        installationID: String,
        using api: any DeviceRegistrationAPI
    ) async throws -> UUID? {
        var cursor: String?
        var seen: Set<String> = []
        repeat {
            let page = try await api.listDevices(limit: 200, cursor: cursor)
            if let matching = page.items.first(where: { $0.clientDeviceID == installationID }) {
                return matching.id
            }
            cursor = page.nextCursor
            if let cursor, !seen.insert(cursor).inserted {
                throw HTTPTransportError.invalidResponse
            }
        } while cursor != nil
        return nil
    }

    private static func isUnsupported(_ error: Error) -> Bool {
        guard case HTTPTransportError.api(let apiError) = error else { return false }
        return apiError.statusCode == 404
    }
}

extension VeetbotAPIClient: DeviceRegistrationAPI {
    public var notificationServerID: String {
        transport.configuration.baseURL.absoluteString
    }
}
