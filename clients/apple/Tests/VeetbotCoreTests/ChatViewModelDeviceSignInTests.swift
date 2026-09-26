import Combine
import Foundation
import Testing

@testable import VeetbotCore

/// ADR-0128, 0128-design §2.5 and §5.2: the view model's device sign-in, begin
/// recovery, and the remote-ceremony refresh (D16). One stub serves both the
/// Veetbot API (`veetbot.test`) and the isolated service (`browser.example`).
@Suite(.serialized) @MainActor struct ChatViewModelDeviceSignInTests {
    nonisolated private static let capability = String(repeating: "A", count: 43)
    nonisolated private static let future = "2099-09-25T12:05:00Z"
    nonisolated private static let emptyPage = #"{"items":[],"next_cursor":null}"#

    // MARK: - Device sign-in

    @Test
    func aNewWebsiteIsCreatedBegunHandedOffCheckedAndSelectedInThatOrder() async throws {
        let profileID = UUID()
        let ceremonyID = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles":
                return (201, Self.profile(profileID, origins: ["https://example.org"], status: "authentication_required"))
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(ceremonyID, profileID: profileID, launch: true))
            case "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff":
                return (200, #"{"status":"ready"}"#)
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)":
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: "ready"))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.page([Self.profile(profileID, origins: ["https://example.org"], status: "ready")]))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)

        let request = try #require(model.beginDeviceSignIn(websiteURL: "example.org"))
        #expect(request.startURL.absoluteString == "https://example.org")
        #expect(request.allowedOrigins == ["https://example.org"])
        #expect(request.profileID == nil)
        #expect(request.adoptableOrigin == "https://www.example.org")
        #expect(model.deviceSignInRequest == request)

        let handoff = Self.handoff(confirmed: "https://example.org/learn?session=private#top")
        let result = await model.completeDeviceSignIn(request, handoff: handoff, adoptedOrigin: nil)

        #expect(result == .signedIn(profileID: profileID))
        #expect(server.routes == [
            "POST veetbot.test /v1/browser-profiles",
            "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies",
            "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff",
            "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)",
            "GET veetbot.test /v1/browser-profiles",
        ])
        let create = try #require(server.json(of: "POST veetbot.test /v1/browser-profiles"))
        #expect(create["allowed_origins"] as? [String] == ["https://example.org"])
        let begin = try #require(
            server.json(of: "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies")
        )
        #expect(begin["mode"] as? String == "device")
        #expect(begin["login_url"] as? String == "https://example.org/")
        let sent = try #require(server.entry(of: "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff"))
        #expect(sent.capability == Self.capability)
        #expect(sent.authorization == nil)
        #expect(sent.body == (try WebsiteSessionScope(allowedOrigins: ["https://example.org"]).encode(handoff)))
        #expect(model.selectedBrowserProfileID == profileID)
        #expect(model.browserProfiles.map(\.id) == [profileID])
    }

    @Test
    func signingInAgainToAnExistingProfileCreatesNoProfile() async throws {
        let profileID = UUID()
        let ceremonyID = UUID()
        let existing = Self.profile(profileID, origins: ["https://www.example.org"], status: "needs_user")
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(ceremonyID, profileID: profileID, launch: true))
            case "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff":
                return (200, #"{"status":"ready"}"#)
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)":
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: "ready"))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.page([Self.profile(profileID, origins: ["https://www.example.org"], status: "ready")]))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)
        let view = try JSONDecoder.server.decode(BrowserProfileView.self, from: Data(existing.utf8))

        let request = try #require(model.beginDeviceSignIn(profile: view))
        #expect(request.startURL.absoluteString == "https://www.example.org/")
        #expect(request.profileID == profileID)
        #expect(request.adoptableOrigin == nil)
        let result = await model.completeDeviceSignIn(
            request,
            handoff: Self.handoff(confirmed: "https://www.example.org/learn", domain: "www.example.org"),
            adoptedOrigin: nil
        )

        #expect(result == .signedIn(profileID: profileID))
        #expect(!server.routes.contains("POST veetbot.test /v1/browser-profiles"))
        #expect(model.selectedBrowserProfileID == profileID)

        let revoked = try JSONDecoder.server.decode(
            BrowserProfileView.self,
            from: Data(Self.profile(profileID, origins: ["https://www.example.org"], status: "revoked").utf8)
        )
        #expect(model.beginDeviceSignIn(profile: revoked) == nil)
    }

    @Test
    func aNewProfileTakesTheAdoptedWWWOriginAndBeginsAtItsRoot() async throws {
        let profileID = UUID()
        let ceremonyID = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles":
                return (201, Self.profile(profileID, origins: ["https://www.example.org"], status: "authentication_required"))
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(ceremonyID, profileID: profileID, launch: true))
            case "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff":
                return (200, #"{"status":"ready"}"#)
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)":
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: "ready"))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.page([Self.profile(profileID, origins: ["https://www.example.org"], status: "ready")]))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)

        let request = try #require(model.beginDeviceSignIn(websiteURL: "https://example.org"))
        let result = await model.completeDeviceSignIn(
            request,
            handoff: Self.handoff(confirmed: "https://www.example.org/learn", domain: ".example.org"),
            adoptedOrigin: "https://www.example.org"
        )

        #expect(result == .signedIn(profileID: profileID))
        let create = try #require(server.json(of: "POST veetbot.test /v1/browser-profiles"))
        #expect(create["allowed_origins"] as? [String] == ["https://www.example.org"])
        let begin = try #require(
            server.json(of: "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies")
        )
        #expect(begin["login_url"] as? String == "https://www.example.org/")
    }

    @Test
    func anUnconfirmedSessionCancelsAndARetryBeginsAgainOnTheSameProfile() async throws {
        let profileID = UUID()
        let first = UUID()
        let second = UUID()
        let begins = Counter()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles":
                return (201, Self.profile(profileID, origins: ["https://example.org"], status: "authentication_required"))
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                let id = begins.next() == 1 ? first : second
                return (201, Self.ceremony(id, profileID: profileID, launch: true))
            case "POST browser.example /authentication/\(first.uuidString)/handoff":
                return (422, #"{"error":{"code":"session_unconfirmed","message":"Unconfirmed."}}"#)
            case "POST veetbot.test /v1/browser-authentication-ceremonies/\(first.uuidString)/cancel":
                return (200, Self.ceremony(first, profileID: profileID, status: "cancelled"))
            case "POST browser.example /authentication/\(second.uuidString)/handoff":
                return (200, #"{"status":"ready"}"#)
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(second.uuidString)":
                return (200, Self.ceremony(second, profileID: profileID, status: "ready"))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.page([Self.profile(profileID, origins: ["https://example.org"], status: "ready")]))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)
        let request = try #require(model.beginDeviceSignIn(websiteURL: "example.org"))
        let handoff = Self.handoff(confirmed: "https://example.org/")

        let refused = await model.completeDeviceSignIn(request, handoff: handoff, adoptedOrigin: nil)
        #expect(
            refused == .failed(
                DeviceSignInFailure(message: DeviceSignInMessage.sessionUnconfirmed, canRetry: true)
            )
        )
        #expect(server.routes.contains("POST veetbot.test /v1/browser-authentication-ceremonies/\(first.uuidString)/cancel"))
        #expect(model.deviceSignInRequest == request)

        let retried = await model.completeDeviceSignIn(request, handoff: handoff, adoptedOrigin: nil)
        #expect(retried == .signedIn(profileID: profileID))
        #expect(server.routes.filter { $0 == "POST veetbot.test /v1/browser-profiles" }.count == 1)
        #expect(begins.value == 2)
    }

    @Test
    func aLostHandoffAnswerIsResolvedByPollingTheStatus() async throws {
        let profileID = UUID()
        let ceremonyID = UUID()
        let reads = Counter()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(ceremonyID, profileID: profileID, launch: true))
            case "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff":
                throw URLError(.networkConnectionLost)
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)":
                let status = reads.next() < 3 ? "authentication_required" : "ready"
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: status))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.page([Self.profile(profileID, origins: ["https://example.org"], status: "ready")]))
            default:
                return nil
            }
        }
        let model = try await configuredModel(
            server, timing: DeviceSignInTiming(pollInterval: 0.01, pollLimit: 30)
        )
        let request = DeviceSignInRequest(
            startURL: URL(string: "https://example.org/")!, allowedOrigins: ["https://example.org"],
            profileID: profileID, adoptableOrigin: nil
        )

        let result = await model.completeDeviceSignIn(
            request, handoff: Self.handoff(confirmed: "https://example.org/learn"), adoptedOrigin: nil
        )

        #expect(result == .signedIn(profileID: profileID))
        #expect(reads.value == 3)
        #expect(server.routes.filter { $0.contains("/handoff") }.count == 1)
        #expect(!server.routes.contains { $0.hasSuffix("/cancel") })
    }

    @Test
    func aHandoffAnswerThatNeverArrivesEndsWithCancelAfterTheLimit() async throws {
        let profileID = UUID()
        let ceremonyID = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(ceremonyID, profileID: profileID, launch: true))
            case "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff":
                throw URLError(.timedOut)
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)":
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: "authentication_required"))
            case "POST veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)/cancel":
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: "cancelled"))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)
        let request = DeviceSignInRequest(
            startURL: URL(string: "https://example.org/")!, allowedOrigins: ["https://example.org"],
            profileID: profileID, adoptableOrigin: nil
        )

        let result = await model.completeDeviceSignIn(
            request, handoff: Self.handoff(confirmed: "https://example.org/learn"), adoptedOrigin: nil
        )

        #expect(result == .failed(DeviceSignInFailure(message: DeviceSignInMessage.couldNotConfirm, canRetry: true)))
        #expect(server.routes.last == "POST veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)/cancel")
        #expect(model.selectedBrowserProfileID == nil)
    }

    @Test
    func aLostBeginIsRecoveredByListingCancellingTheNewestOpenCeremonyAndBeginningAgain() async throws {
        try await assertBeginRecovery(firstBegin: { throw URLError(.networkConnectionLost) })
    }

    @Test
    func aConflictWithAnOpenCeremonyIsRecoveredTheSameWay() async throws {
        try await assertBeginRecovery(firstBegin: {
            (409, #"{"error":{"code":"conflict","message":"An authentication ceremony is already open.","details":{},"request_id":"r"}}"#)
        })
    }

    @Test
    func aConflictWithNoOpenCeremonyIsTheLeaseMessageAndCancelsNothing() async throws {
        let profileID = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (409, #"{"error":{"code":"conflict","message":"busy","details":{},"request_id":"r"}}"#)
            case "GET veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (200, "[\(Self.ceremony(UUID(), profileID: profileID, status: "cancelled"))]")
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)

        let result = await model.completeDeviceSignIn(
            Self.existingRequest(profileID), handoff: Self.handoff(confirmed: "https://example.org/learn"),
            adoptedOrigin: nil
        )

        #expect(result == .failed(DeviceSignInFailure(message: DeviceSignInMessage.websiteInUse, canRetry: true)))
        #expect(!server.routes.contains { $0.hasSuffix("/cancel") })
        #expect(server.routes.filter { $0.hasSuffix("/authentication-ceremonies") && $0.hasPrefix("POST") }.count == 1)
    }

    @Test
    func aSecondBeginFailureStopsWithTheGenericMessage() async throws {
        let profileID = UUID()
        let open = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (409, #"{"error":{"code":"conflict","message":"busy","details":{},"request_id":"r"}}"#)
            case "GET veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (200, "[\(Self.ceremony(open, profileID: profileID))]")
            case "POST veetbot.test /v1/browser-authentication-ceremonies/\(open.uuidString)/cancel":
                return (200, Self.ceremony(open, profileID: profileID, status: "cancelled"))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)

        let result = await model.completeDeviceSignIn(
            Self.existingRequest(profileID), handoff: Self.handoff(confirmed: "https://example.org/learn"),
            adoptedOrigin: nil
        )

        #expect(result == .failed(DeviceSignInFailure(message: DeviceSignInMessage.couldNotStart, canRetry: true)))
        #expect(server.routes.filter { $0.hasSuffix("/authentication-ceremonies") && $0.hasPrefix("POST") }.count == 2)
    }

    @Test
    func aRejectedBeginOffersTheRemoteBrowserForANewWebsite() async throws {
        let profileID = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles":
                return (201, Self.profile(profileID, origins: ["https://example.org"], status: "authentication_required"))
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (400, #"{"error":{"code":"malformed_request","message":"Extra inputs are not permitted.","details":{},"request_id":"r"}}"#)
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)
        let request = try #require(model.beginDeviceSignIn(websiteURL: "example.org"))

        let result = await model.completeDeviceSignIn(
            request, handoff: Self.handoff(confirmed: "https://example.org/learn"), adoptedOrigin: nil
        )

        #expect(
            result == .failed(
                DeviceSignInFailure(
                    message: DeviceSignInMessage.couldNotStart, canRetry: true, offersRemoteBrowser: true
                )
            )
        )
        #expect(!server.routes.contains { $0.hasPrefix("GET") && $0.hasSuffix("/authentication-ceremonies") })
    }

    @Test
    func choosingTheRemoteBrowserRemovesTheDeviceAttemptAndBeginsARemoteCeremony() async throws {
        let deviceProfile = UUID()
        let remoteProfile = UUID()
        let remoteCeremony = UUID()
        let creates = Counter()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles":
                let id = creates.next() == 1 ? deviceProfile : remoteProfile
                return (201, Self.profile(id, origins: ["https://example.org"], status: "authentication_required"))
            case "POST veetbot.test /v1/browser-profiles/\(deviceProfile.uuidString)/authentication-ceremonies":
                return (400, #"{"error":{"code":"malformed_request","message":"Extra inputs are not permitted.","details":{},"request_id":"r"}}"#)
            case "POST veetbot.test /v1/browser-profiles/\(deviceProfile.uuidString)/revoke":
                return (200, Self.profile(deviceProfile, origins: ["https://example.org"], status: "revoked"))
            case "DELETE veetbot.test /v1/browser-profiles/\(deviceProfile.uuidString)":
                return (204, "")
            case "POST veetbot.test /v1/browser-profiles/\(remoteProfile.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(remoteCeremony, profileID: remoteProfile, launch: true, remote: true))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.emptyPage)
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)
        let request = try #require(model.beginDeviceSignIn(websiteURL: "example.org/login"))
        _ = await model.completeDeviceSignIn(
            request, handoff: Self.handoff(confirmed: "https://example.org/learn"), adoptedOrigin: nil
        )

        let launch = await model.switchDeviceSignInToRemoteBrowser(request)

        #expect(launch?.fragment == "capability=\(Self.capability)")
        #expect(model.deviceSignInRequest == nil)
        #expect(server.routes.contains("DELETE veetbot.test /v1/browser-profiles/\(deviceProfile.uuidString)"))
        let remoteBegin = try #require(
            server.json(of: "POST veetbot.test /v1/browser-profiles/\(remoteProfile.uuidString)/authentication-ceremonies")
        )
        #expect(remoteBegin["mode"] == nil)
        #expect(remoteBegin["login_url"] as? String == "https://example.org/login")
        #expect(model.browserAuthentication?.id == remoteCeremony)
    }

    @Test
    func tooMuchSiteDataIsRefusedBeforeAnythingIsSent() async throws {
        let server = DeviceFlowServer { _ in nil }
        let model = try await configuredModel(server)
        let request = try #require(model.beginDeviceSignIn(websiteURL: "example.org"))
        let big = String(repeating: "x", count: WebsiteSessionScope.maximumBodyBytes)
        let handoff = DeviceSessionHandoff(
            confirmedURL: "https://example.org/learn",
            cookies: [],
            origins: [
                HandoffOrigin(origin: "https://example.org", localStorage: [HandoffStorageItem(name: "blob", value: big)])
            ]
        )

        let result = await model.completeDeviceSignIn(request, handoff: handoff, adoptedOrigin: nil)

        #expect(
            result == .failed(
                DeviceSignInFailure(message: DeviceSignInMessage.tooMuchData, canRetry: false, offersRemoteBrowser: true)
            )
        )
        #expect(server.routes.isEmpty)
    }

    @Test
    func theLaunchURLIsNeverPublishedDuringTheDeviceFlow() async throws {
        let profileID = UUID()
        let ceremonyID = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles":
                return (201, Self.profile(profileID, origins: ["https://example.org"], status: "authentication_required"))
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(ceremonyID, profileID: profileID, launch: true))
            case "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff":
                return (200, #"{"status":"ready"}"#)
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)":
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: "ready"))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.page([Self.profile(profileID, origins: ["https://example.org"], status: "ready")]))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)
        var launchURLs: [URL?] = []
        var ceremonies: [BrowserAuthenticationView?] = []
        let launchWatch = model.$websiteAuthenticationLaunchURL.sink { launchURLs.append($0) }
        let ceremonyWatch = model.$browserAuthentication.sink { ceremonies.append($0) }
        defer {
            launchWatch.cancel()
            ceremonyWatch.cancel()
        }

        let request = try #require(model.beginDeviceSignIn(websiteURL: "example.org"))
        let result = await model.completeDeviceSignIn(
            request, handoff: Self.handoff(confirmed: "https://example.org/learn"), adoptedOrigin: nil
        )

        #expect(result == .signedIn(profileID: profileID))
        #expect(!launchURLs.isEmpty)
        #expect(launchURLs.allSatisfy { $0 == nil })
        #expect(ceremonies.allSatisfy { $0 == nil })
    }

    @Test
    func closingTheWindowRemovesAProfileItCreatedThatNeverBecameReady() async throws {
        let profileID = UUID()
        let ceremonyID = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles":
                return (201, Self.profile(profileID, origins: ["https://example.org"], status: "authentication_required"))
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(ceremonyID, profileID: profileID, launch: true))
            case "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff":
                return (422, #"{"error":{"code":"session_empty","message":"Empty."}}"#)
            case "POST veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)/cancel":
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: "cancelled"))
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/revoke":
                return (200, Self.profile(profileID, origins: ["https://example.org"], status: "revoked"))
            case "DELETE veetbot.test /v1/browser-profiles/\(profileID.uuidString)":
                return (204, "")
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.emptyPage)
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)
        let request = try #require(model.beginDeviceSignIn(websiteURL: "example.org"))
        let result = await model.completeDeviceSignIn(
            request, handoff: Self.handoff(confirmed: "https://example.org/"), adoptedOrigin: nil
        )
        #expect(result == .failed(DeviceSignInFailure(message: DeviceSignInMessage.sessionEmpty, canRetry: true)))

        await model.abandonDeviceSignIn()

        #expect(model.deviceSignInRequest == nil)
        #expect(server.routes.contains("POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/revoke"))
        #expect(server.routes.contains("DELETE veetbot.test /v1/browser-profiles/\(profileID.uuidString)"))
    }

    @Test
    func closingTheWindowAfterASignInRemovesNothing() async throws {
        let profileID = UUID()
        let ceremonyID = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles":
                return (201, Self.profile(profileID, origins: ["https://example.org"], status: "authentication_required"))
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(ceremonyID, profileID: profileID, launch: true))
            case "POST browser.example /authentication/\(ceremonyID.uuidString)/handoff":
                return (200, #"{"status":"ready"}"#)
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)":
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: "ready"))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.page([Self.profile(profileID, origins: ["https://example.org"], status: "ready")]))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)
        let request = try #require(model.beginDeviceSignIn(websiteURL: "example.org"))
        _ = await model.completeDeviceSignIn(
            request, handoff: Self.handoff(confirmed: "https://example.org/learn"), adoptedOrigin: nil
        )

        model.finishDeviceSignIn()
        await model.abandonDeviceSignIn()

        #expect(model.deviceSignInRequest == nil)
        #expect(!server.routes.contains { $0.hasSuffix("/revoke") || $0.hasPrefix("DELETE") })
    }

    @Test
    func aConnectionChangeClosesTheWindow() async throws {
        let server = DeviceFlowServer { _ in nil }
        let model = try await configuredModel(server)
        #expect(model.beginDeviceSignIn(websiteURL: "example.org") != nil)

        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "another-token"))

        #expect(model.deviceSignInRequest == nil)
    }

    @Test(arguments: ["", "http://example.org", "https://example.org:8443", "https://user@example.org"])
    func anInvalidWebsiteOpensNoWindow(input: String) async throws {
        let server = DeviceFlowServer { _ in nil }
        let model = try await configuredModel(server)

        #expect(model.beginDeviceSignIn(websiteURL: input) == nil)
        #expect(model.deviceSignInRequest == nil)
        #expect(model.errorMessage != nil)
    }

    // MARK: - Remote ceremonies (D16)

    @Test
    func becomingActiveRefreshesAnOpenRemoteCeremony() async throws {
        let profileID = UUID()
        let ceremonyID = UUID()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles":
                return (201, Self.profile(profileID, origins: ["https://example.org"], status: "authentication_required"))
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (201, Self.ceremony(ceremonyID, profileID: profileID, launch: true, remote: true))
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)":
                return (200, Self.ceremony(ceremonyID, profileID: profileID, status: "ready"))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.page([Self.profile(profileID, origins: ["https://example.org"], status: "ready")]))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)
        #expect(await model.createWebsiteAccess(websiteURL: "example.org") != nil)
        server.clear()

        await model.refreshOpenRemoteAuthentication()

        #expect(server.routes.first == "GET veetbot.test /v1/browser-authentication-ceremonies/\(ceremonyID.uuidString)")
        #expect(model.browserAuthentication?.status == .ready)
        #expect(model.selectedBrowserProfileID == profileID)

        server.clear()
        await model.refreshOpenRemoteAuthentication()
        #expect(server.routes.isEmpty)
    }

    @Test
    func reconcileRefreshesTheNewestOpenCeremonyOfAProfileThatIsNotReady() async throws {
        let waiting = UUID()
        let ready = UUID()
        let older = UUID()
        let newest = UUID()
        let closed = UUID()
        let profileReads = Counter()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "GET veetbot.test /v1/browser-profiles":
                let status = profileReads.next() == 1 ? "authentication_required" : "ready"
                return (200, Self.page([
                    Self.profile(waiting, origins: ["https://example.org"], status: status),
                    Self.profile(ready, origins: ["https://news.example.org"], status: "ready"),
                ]))
            case "GET veetbot.test /v1/browser-profiles/\(waiting.uuidString)/authentication-ceremonies":
                return (200, "[\(Self.ceremony(older, profileID: waiting, expires: "2099-09-25T12:01:00Z")),\(Self.ceremony(newest, profileID: waiting, expires: "2099-09-25T12:04:00Z")),\(Self.ceremony(closed, profileID: waiting, status: "cancelled", expires: "2099-09-25T12:09:00Z"))]")
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(newest.uuidString)":
                return (200, Self.ceremony(newest, profileID: waiting, status: "ready"))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)

        await model.refreshBrowserProfiles()

        #expect(server.routes == [
            "GET veetbot.test /v1/browser-profiles",
            "GET veetbot.test /v1/browser-profiles/\(waiting.uuidString)/authentication-ceremonies",
            "GET veetbot.test /v1/browser-authentication-ceremonies/\(newest.uuidString)",
            "GET veetbot.test /v1/browser-profiles",
        ])
        #expect(model.browserProfiles.allSatisfy { $0.status == .ready })
        #expect(model.errorMessage == nil)
    }

    // MARK: - Helpers

    /// A short poll limit keeps the give-up case fast; a case that must
    /// succeed by polling passes a generous one, so a loaded machine cannot
    /// time it out.
    private func configuredModel(
        _ server: DeviceFlowServer,
        timing: DeviceSignInTiming = DeviceSignInTiming(pollInterval: 0.01, pollLimit: 0.2)
    ) async throws -> ChatViewModel {
        let suiteName = "com.veetbot.tests.\(UUID().uuidString)"
        let defaults = try #require(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        let model = ChatViewModel(
            tokenStore: InMemoryTokenStore(),
            configurationStore: ConnectionConfigurationStore(defaults: defaults),
            historyStore: VolatileSessionHistoryStore(),
            urlSession: URLSession(configuration: server.configuration(base: .ephemeral)),
            deviceHandoffClient: DeviceSignInHandoffClient(
                session: URLSession(
                    configuration: server.configuration(
                        base: DeviceSignInHandoffClient.makeSessionConfiguration()
                    ),
                    delegate: RejectRedirectsDelegate(),
                    delegateQueue: nil
                )
            ),
            deviceSignInTiming: timing
        )
        #expect(await model.configure(baseURLString: "https://veetbot.test", token: "test-token"))
        server.clear()
        return model
    }

    private func assertBeginRecovery(
        firstBegin: @escaping @Sendable () throws -> (Int, String)
    ) async throws {
        let profileID = UUID()
        let stale = UUID()
        let open = UUID()
        let fresh = UUID()
        let begins = Counter()
        let server = DeviceFlowServer { request in
            switch Self.route(request) {
            case "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                if begins.next() == 1 { return try firstBegin() }
                return (201, Self.ceremony(fresh, profileID: profileID, launch: true))
            case "GET veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies":
                return (200, "[\(Self.ceremony(stale, profileID: profileID, expires: "2099-09-25T12:00:00Z")),\(Self.ceremony(open, profileID: profileID, status: "needs_user", expires: "2099-09-25T12:04:00Z")),\(Self.ceremony(UUID(), profileID: profileID, expires: "2020-01-01T00:00:00Z"))]")
            case "POST veetbot.test /v1/browser-authentication-ceremonies/\(open.uuidString)/cancel":
                return (200, Self.ceremony(open, profileID: profileID, status: "cancelled"))
            case "POST browser.example /authentication/\(fresh.uuidString)/handoff":
                return (200, #"{"status":"ready"}"#)
            case "GET veetbot.test /v1/browser-authentication-ceremonies/\(fresh.uuidString)":
                return (200, Self.ceremony(fresh, profileID: profileID, status: "ready"))
            case "GET veetbot.test /v1/browser-profiles":
                return (200, Self.page([Self.profile(profileID, origins: ["https://example.org"], status: "ready")]))
            default:
                return nil
            }
        }
        let model = try await configuredModel(server)

        let result = await model.completeDeviceSignIn(
            Self.existingRequest(profileID), handoff: Self.handoff(confirmed: "https://example.org/learn"),
            adoptedOrigin: nil
        )

        #expect(result == .signedIn(profileID: profileID))
        #expect(Array(server.routes.prefix(4)) == [
            "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies",
            "GET veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies",
            "POST veetbot.test /v1/browser-authentication-ceremonies/\(open.uuidString)/cancel",
            "POST veetbot.test /v1/browser-profiles/\(profileID.uuidString)/authentication-ceremonies",
        ])
        #expect(!server.routes.contains { $0.contains(stale.uuidString) })
    }

    nonisolated private static func route(_ request: URLRequest) -> String {
        "\(request.httpMethod ?? "") \(request.url?.host ?? "") \(request.url?.path ?? "")"
    }

    nonisolated private static func existingRequest(_ profileID: UUID) -> DeviceSignInRequest {
        DeviceSignInRequest(
            startURL: URL(string: "https://example.org/")!, allowedOrigins: ["https://example.org"],
            profileID: profileID, adoptableOrigin: nil
        )
    }

    nonisolated private static func handoff(confirmed: String, domain: String = "example.org") -> DeviceSessionHandoff {
        let origin = WebsiteSessionScope.origin(of: URL(string: confirmed)!)!
        return DeviceSessionHandoff(
            confirmedURL: confirmed.components(separatedBy: "#")[0],
            cookies: [
                HandoffCookie(
                    name: "session", value: "opaque-session-value", domain: domain, path: "/",
                    expires: 1_900_000_000, httpOnly: true, secure: true, sameSite: .lax
                )
            ],
            origins: [HandoffOrigin(origin: origin, localStorage: [HandoffStorageItem(name: "k", value: "v")])]
        )
    }

    nonisolated private static func profile(_ id: UUID, origins: [String], status: String) -> String {
        let list = origins.map { "\"\($0)\"" }.joined(separator: ",")
        return #"{"id":"\#(id.uuidString)","allowed_origins":[\#(list)],"status":"\#(status)","generation":3,"created_at":"2026-09-25T12:00:00Z","updated_at":"2026-09-25T12:00:00Z","last_used_at":null}"#
    }

    nonisolated private static func page(_ items: [String]) -> String {
        "{\"items\":[\(items.joined(separator: ","))],\"next_cursor\":null}"
    }

    nonisolated private static func ceremony(
        _ id: UUID, profileID: UUID, status: String = "authentication_required",
        expires: String = future, launch: Bool = false, remote: Bool = false
    ) -> String {
        let suffix = remote ? "" : "/handoff"
        let launchURL = launch
            ? "\"https://browser.example/authentication/\(id.uuidString)\(suffix)#capability=\(capability)\""
            : "null"
        return #"{"id":"\#(id.uuidString)","profile_id":"\#(profileID.uuidString)","status":"\#(status)","expires_at":"\#(expires)","launch_url":\#(launchURL)}"#
    }
}

/// A counter the stub's handler can advance from any thread.
final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0

    var value: Int { lock.withLock { count } }

    func next() -> Int {
        lock.withLock {
            count += 1
            return count
        }
    }
}

/// Routes every request of one test, recording each after `configure`.
/// `GET /v1/sessions` answers an empty history; a nil answer is unexpected.
final class DeviceFlowServer: @unchecked Sendable {
    struct Entry {
        let route: String
        let body: Data
        let capability: String?
        let authorization: String?
        let url: String
    }

    typealias Handler = @Sendable (URLRequest) throws -> (Int, String)?

    let id = UUID().uuidString
    private let handler: Handler
    private let lock = NSLock()
    private var entries: [Entry] = []

    init(handler: @escaping Handler) {
        self.handler = handler
        DeviceFlowURLProtocol.register(self)
    }

    var routes: [String] { lock.withLock { entries.map(\.route) } }

    func clear() { lock.withLock { entries.removeAll() } }

    func entry(of route: String) -> Entry? { lock.withLock { entries.first { $0.route == route } } }

    func entryURL(of route: String) -> String? { entry(of: route)?.url }

    func json(of route: String) -> [String: Any]? {
        entry(of: route).flatMap { try? JSONSerialization.jsonObject(with: $0.body) as? [String: Any] }
    }

    func configuration(base: URLSessionConfiguration) -> URLSessionConfiguration {
        base.protocolClasses = [DeviceFlowURLProtocol.self]
        base.httpAdditionalHeaders = [DeviceFlowURLProtocol.serverHeader: id]
        return base
    }

    func answer(_ request: URLRequest, body: Data) throws -> (Int, String) {
        let route = "\(request.httpMethod ?? "") \(request.url?.host ?? "") \(request.url?.path ?? "")"
        if route == "GET veetbot.test /v1/sessions" {
            return (200, #"{"items":[],"next_cursor":null}"#)
        }
        lock.withLock {
            entries.append(
                Entry(
                    route: route, body: body,
                    capability: request.value(forHTTPHeaderField: DeviceSignInHandoffClient.capabilityHeader),
                    authorization: request.value(forHTTPHeaderField: "Authorization"),
                    url: request.url?.absoluteString ?? ""
                )
            )
        }
        guard let answer = try handler(request) else {
            Issue.record("unexpected request: \(route)")
            return (500, #"{"error":{"code":"internal_error","message":"unexpected","details":{},"request_id":"t"}}"#)
        }
        return answer
    }
}

final class DeviceFlowURLProtocol: URLProtocol {
    static let serverHeader = "X-Veetbot-Test-Device-Server"
    private static let lock = NSLock()
    nonisolated(unsafe) private static var servers: [String: DeviceFlowServer] = [:]

    static func register(_ server: DeviceFlowServer) {
        lock.withLock { servers[server.id] = server }
    }

    override static func canInit(with request: URLRequest) -> Bool { true }
    override static func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        guard
            let id = request.value(forHTTPHeaderField: Self.serverHeader),
            let server = Self.lock.withLock({ Self.servers[id] }),
            let url = request.url
        else {
            client?.urlProtocol(self, didFailWithError: URLError(.unknown))
            return
        }
        do {
            let (status, body) = try server.answer(request, body: readBody())
            let response = HTTPURLResponse(
                url: url, statusCode: status, httpVersion: nil,
                headerFields: ["Content-Type": "application/json"]
            )!
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data(body.utf8))
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}

    private func readBody() -> Data {
        if let body = request.httpBody { return body }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open()
        defer { stream.close() }
        var collected = Data()
        var buffer = [UInt8](repeating: 0, count: 4_096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            collected.append(buffer, count: count)
        }
        return collected
    }
}
