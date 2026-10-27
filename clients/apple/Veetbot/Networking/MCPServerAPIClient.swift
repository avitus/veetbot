import Foundation

/// Why an MCP server request was not answered as a server view.
public enum MCPServerClientError: Error, LocalizedError, Equatable, Sendable {
    /// The server does not mount the routes: the feature is off (ADR-0255
    /// decision 1) or the server predates it.
    case unavailable
    /// The name is not one the contract allows in a path segment, so the
    /// request was never built.
    case invalidServerID

    public var errorDescription: String? {
        switch self {
        case .unavailable: return "MCP servers aren't enabled on this Veetbot server."
        case .invalidServerID: return MCPServerInputError.invalidName.message
        }
    }
}

/// `/v1/mcp-servers` (ADR-0255 decision 4), under `settings.read` and
/// `settings.write`.
extension VeetbotAPIClient {
    public func listMCPServers() async throws -> MCPServerList {
        do {
            return try await transport.send(
                TransportRequest(method: .get, path: "/v1/mcp-servers", retryAttempts: 2)
            )
        } catch {
            throw mcpCollectionCompatibilityError(from: error) ?? error
        }
    }

    /// Probes the URL and stores the server. Not retried: each attempt is a
    /// fresh probe, and a lost answer is recovered by the identical replay
    /// the owner's next tap sends.
    public func addMCPServer(
        serverID: String, url: String, access: MCPServerAccess
    ) async throws -> MCPServerAddResult {
        do {
            let (data, response) = try await transport.sendData(
                TransportRequest(
                    method: .post,
                    path: "/v1/mcp-servers",
                    body: try JSONEncoder.server.encode(
                        AddMCPServerBody(serverID: serverID, url: url, access: access)
                    )
                )
            )
            return MCPServerAddResult(
                server: try JSONDecoder.server.decode(MCPServerView.self, from: data),
                created: response.statusCode == 201
            )
        } catch {
            throw mcpCollectionCompatibilityError(from: error) ?? error
        }
    }

    public func updateMCPServerAccess(
        _ serverID: String, access: MCPServerAccess
    ) async throws -> MCPServerView {
        do {
            return try await transport.send(
                TransportRequest(
                    method: .patch,
                    path: try mcpServerPath(serverID),
                    body: try JSONEncoder.server.encode(UpdateMCPServerAccessBody(access: access)),
                    retryAttempts: 2
                )
            )
        } catch {
            throw mcpItemCompatibilityError(from: error) ?? error
        }
    }

    /// A server that is already gone is also 204, so a retry is safe.
    public func removeMCPServer(_ serverID: String) async throws {
        do {
            _ = try await transport.sendData(
                TransportRequest(method: .delete, path: try mcpServerPath(serverID), retryAttempts: 2)
            )
        } catch {
            throw mcpItemCompatibilityError(from: error) ?? error
        }
    }

    /// Starts the authorization-code flow; each call stores a fresh state, so
    /// it is not retried.
    public func beginMCPServerSignIn(_ serverID: String) async throws -> MCPSignInStart {
        do {
            return try await transport.send(
                TransportRequest(method: .post, path: try mcpServerPath(serverID) + "/sign-in")
            )
        } catch {
            throw mcpItemCompatibilityError(from: error) ?? error
        }
    }

    /// Forgets the tokens and keeps the registration.
    public func signOutOfMCPServer(_ serverID: String) async throws {
        do {
            _ = try await transport.sendData(
                TransportRequest(
                    method: .delete, path: try mcpServerPath(serverID) + "/sign-in", retryAttempts: 2
                )
            )
        } catch {
            throw mcpItemCompatibilityError(from: error) ?? error
        }
    }
}

/// The path of one server. The transport's URL builder percent-encodes every
/// character a path segment cannot hold except `/`, and would encode a `%`
/// written here a second time, so a name is checked against the contract's
/// grammar instead: one that passes is a single segment as written.
func mcpServerPath(_ serverID: String) throws -> String {
    guard MCPServerInput.isWellFormedName(serverID) else {
        throw MCPServerClientError.invalidServerID
    }
    return "/v1/mcp-servers/\(serverID)"
}

/// The list and add routes have no per-item 404, so a 404 or 405 there means
/// the routes are not mounted.
private func mcpCollectionCompatibilityError(from error: Error) -> MCPServerClientError? {
    guard case HTTPTransportError.api(let apiError) = error else { return nil }
    if apiError.statusCode == 404 || apiError.statusCode == 405 { return .unavailable }
    return nil
}

/// A per-server 404 may be an unknown server, so it stays an API error and
/// the caller reloads the list to tell the two apart; a 405 is a server
/// without the routes.
private func mcpItemCompatibilityError(from error: Error) -> MCPServerClientError? {
    guard case HTTPTransportError.api(let apiError) = error else { return nil }
    if apiError.statusCode == 405 { return .unavailable }
    return nil
}
