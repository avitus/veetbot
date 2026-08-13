import Foundation
import Security
#if os(macOS)
import LocalAuthentication
#endif

public protocol TokenStore: Sendable {
    func readToken() async throws -> String?
    func saveToken(_ token: String) async throws
    func deleteToken() async throws
}

public enum KeychainTokenStoreError: Error, LocalizedError {
    case unexpectedData
    case operationFailed(OSStatus)

    public var errorDescription: String? {
        switch self {
        case .unexpectedData:
            return "The saved Veetbot token is not valid UTF-8."
        case let .operationFailed(status):
            if status == errSecMissingEntitlement {
                return "Veetbot cannot access Keychain because this build is not signed for an Apple development team. Select the Veetbot target in Xcode, open Signing & Capabilities, and choose your team."
            }
            if status == errSecInteractionNotAllowed {
                return "Keychain is locked or unavailable for interaction. Unlock the device or login Keychain and try again."
            }
            if status == errSecNotAvailable {
                return "Keychain is not available. Unlock the device or login Keychain and try again."
            }
            let message = SecCopyErrorMessageString(status, nil) as String? ?? "OSStatus \(status)"
            return "Keychain operation failed: \(message)"
        }
    }
}

public actor KeychainTokenStore: TokenStore {
    private let service: String
    private let account: String

    public init(
        service: String = "com.veetbot.client.bearer-token",
        account: String = "static-bearer-token"
    ) {
        self.service = service
        self.account = account
    }

    public func readToken() throws -> String? {
        let (status, token) = try copyToken(matching: baseQuery)
        if status == errSecSuccess { return token }
        if status != errSecItemNotFound {
            throw KeychainTokenStoreError.operationFailed(status)
        }
#if os(macOS)
        if let legacyToken = try readLegacyTokenWithoutPrompt() {
            try saveToken(legacyToken)
            deleteLegacyTokenWithoutPrompt()
            return legacyToken
        }
#endif
        return nil
    }

    private func copyToken(matching base: [String: Any]) throws -> (OSStatus, String?) {
        var query = base
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return (status, nil) }
        guard status == errSecSuccess else {
            return (status, nil)
        }
        guard
            let data = result as? Data,
            let token = String(data: data, encoding: .utf8)
        else {
            throw KeychainTokenStoreError.unexpectedData
        }
        return (status, token)
    }

    public func saveToken(_ token: String) throws {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            try deleteToken()
            return
        }
        let data = Data(trimmed.utf8)
        let attributes = [kSecValueData as String: data]
        let update = SecItemUpdate(baseQuery as CFDictionary, attributes as CFDictionary)
        if update == errSecSuccess { return }
        guard update == errSecItemNotFound else {
            throw KeychainTokenStoreError.operationFailed(update)
        }
        var insert = baseQuery
        insert[kSecValueData as String] = data
        insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(insert as CFDictionary, nil)
        guard status == errSecSuccess else {
            throw KeychainTokenStoreError.operationFailed(status)
        }
    }

    public func deleteToken() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw KeychainTokenStoreError.operationFailed(status)
        }
    }

    private var baseQuery: [String: Any] {
        Self.makeBaseQuery(service: service, account: account)
    }

    static func makeBaseQuery(service: String, account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrSynchronizable as String: false,
            kSecUseDataProtectionKeychain as String: true,
        ]
    }

#if os(macOS)
    private var legacyBaseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecAttrSynchronizable as String: false,
        ]
    }

    private func readLegacyTokenWithoutPrompt() throws -> String? {
        let query = legacyQueryWithoutInteraction()
        let (status, token) = try copyToken(matching: query)
        if status == errSecSuccess { return token }
        if status == errSecItemNotFound
            || status == errSecInteractionNotAllowed
            || status == errSecAuthFailed
        {
            return nil
        }
        throw KeychainTokenStoreError.operationFailed(status)
    }

    private func deleteLegacyTokenWithoutPrompt() {
        let query = legacyQueryWithoutInteraction()
        _ = SecItemDelete(query as CFDictionary)
    }

    private func legacyQueryWithoutInteraction() -> [String: Any] {
        let context = LAContext()
        context.interactionNotAllowed = true
        var query = legacyBaseQuery
        query[kSecUseAuthenticationContext as String] = context
        return query
    }
#endif
}

public actor InMemoryTokenStore: TokenStore {
    private var token: String?

    public init(token: String? = nil) { self.token = token }
    public func readToken() -> String? { token }
    public func saveToken(_ token: String) {
        let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
        self.token = trimmed.isEmpty ? nil : trimmed
    }
    public func deleteToken() { token = nil }
}
