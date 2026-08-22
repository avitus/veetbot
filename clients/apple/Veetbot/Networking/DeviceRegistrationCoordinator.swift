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

    public init(
        name: String,
        kind: DeviceKind,
        platform: String,
        bundleID: String,
        environment: PushEnvironment
    ) {
        self.name = name
        self.kind = kind
        self.platform = platform
        self.bundleID = bundleID
        self.environment = environment
    }

    @MainActor
    public static var current: AppleDeviceDescriptor {
        #if os(iOS)
        let name = UIDevice.current.name
        let kind = DeviceKind.mobile
        let platform = "ios"
        #elseif os(macOS)
        let name = Host.current().localizedName ?? "Mac"
        let kind = DeviceKind.desktop
        let platform = "macos"
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
            environment: environment
        )
    }
}

public enum DeviceRegistrationOutcome: Equatable, Sendable {
    case registered(UUID)
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
        let body = AppleDeviceRegistration(
            clientDeviceID: installationID,
            name: descriptor.name,
            kind: descriptor.kind,
            platform: descriptor.platform,
            appBundleID: descriptor.bundleID,
            pushToken: deviceToken.map { String(format: "%02x", $0) }.joined(),
            pushEnvironment: descriptor.environment
        )
        do {
            let device = try await api.registerDevice(body, idempotencyKey: installationID)
            registeredDeviceIDs[api.notificationServerID] = device.id
            return .registered(device.id)
        } catch {
            if Self.isUnsupported(error) { return .unsupported }
            throw error
        }
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
