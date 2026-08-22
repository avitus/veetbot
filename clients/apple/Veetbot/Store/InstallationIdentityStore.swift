import Foundation
import Security

public protocol InstallationIdentityStore: Sendable {
    func readOrCreateInstallationID() async throws -> String
}

public actor KeychainInstallationIdentityStore: InstallationIdentityStore {
    private let service: String
    private let account: String

    public init(
        service: String = "com.veetbot.client.device-identity",
        account: String = "installation-id"
    ) {
        self.service = service
        self.account = account
    }

    public func readOrCreateInstallationID() throws -> String {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let lookup = SecItemCopyMatching(query as CFDictionary, &result)
        if lookup == errSecSuccess {
            guard let data = result as? Data,
                let value = String(data: data, encoding: .utf8),
                UUID(uuidString: value) != nil
            else { throw KeychainTokenStoreError.unexpectedData }
            return value
        }
        guard lookup == errSecItemNotFound else {
            throw KeychainTokenStoreError.operationFailed(lookup)
        }

        let value = UUID().uuidString.lowercased()
        var insert = baseQuery
        insert[kSecValueData as String] = Data(value.utf8)
        insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let status = SecItemAdd(insert as CFDictionary, nil)
        if status == errSecDuplicateItem {
            return try readOrCreateInstallationID()
        }
        guard status == errSecSuccess else {
            throw KeychainTokenStoreError.operationFailed(status)
        }
        return value
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
}

public actor InMemoryInstallationIdentityStore: InstallationIdentityStore {
    private var installationID: String?
    private let seededID: String?
    private var creates = 0

    public init(installationID: String? = nil) {
        seededID = installationID
    }

    public func readOrCreateInstallationID() -> String {
        if let installationID { return installationID }
        let value = seededID ?? UUID().uuidString.lowercased()
        installationID = value
        creates += 1
        return value
    }

    public func creationCount() -> Int { creates }
}
