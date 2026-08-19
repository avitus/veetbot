# ADR-0049: Native Apple client as a secure transport-only surface

- Status: Proposed
- Date: 2026-08-12
- Related: Section 16 (HTTP API), Section 29 (multi-device shared core),
  ADR-0006 (no private reasoning storage), ADR-0009 (run and turn identity),
  ADR-0010 (live event transport), ADR-0011 (multi-device shared core), ADR-0028
  (HTTP API and SSE semantics), ADR-0034 (the deferred Device seam), ADR-0047
  (downloadable terminal client)
- User authorization: implement a SwiftUI multiplatform client with the typed
  wire contract, secure networking, streaming reducer, local history, and
  agentic interaction surfaces

ADR-0050 later supersedes this ADR's local-history constraint by adding an
authoritative session index and deletion. The transport-only boundary and the
local store's status as a nonauthoritative cache remain unchanged.
ADR-0053 further requires selection to restore the durable session transcript
before the client attaches to the latest run.

## Context

ADR-0047 provided the first downloadable terminal surface and deliberately
deferred a native GUI. A later explicit assignment now requires a native Apple
surface while retaining the same shared-core boundary. The existing HTTP API
has no session-list route, so navigation history must be a device-local index,
not an authoritative projection.

The requested iOS 15 and macOS 12 deployment floors support incremental
`URLSession` byte streaming but predate SwiftData, which begins at iOS 17 and
macOS 14. The client must preserve the lower deployment targets without
pretending SwiftData is available there.

## Proposed decision

1. **Add a native SwiftUI client under `clients/apple/`.** One Xcode application
   target supports iOS 15+ and macOS 12+ with no third-party dependency.
2. **Keep the client transport-only.** It consumes the existing `/v1` routes and
   does not import server implementation, execute a runtime loop, or add Device,
   pairing, presence, notification, or device-local tool concepts.
3. **Store the bearer credential only in Keychain.** The non-secret HTTPS base
   URL may use preferences. Plain HTTP and redirects are rejected, and normal
   platform TLS validation remains enabled.
4. **Mirror the public wire contract with typed DTOs.** Unknown error codes and
   error details remain representable so compatible server additions do not
   erase diagnostics, while the published vocabulary has named Swift cases.
5. **Make SSE replay state explicit.** Persisted session sequence values alone
   advance `Last-Event-ID`; transient frames are not replayed; suspension is not
   terminal; overflow reconnects from its durable watermark; final assistant
   messages reconcile best-effort deltas.
6. **Discard raw reasoning at the reducer boundary.** The UI receives only an
   activity boolean and never persists or renders reasoning text.
7. **Treat local history as a cache.** SwiftData implements the store on iOS
   17+/macOS 14+. An atomic Application Support file implements the same
   nonauthoritative fields on iOS 15–16/macOS 12–13, where SwiftData cannot be
   linked. Selecting history always refreshes the server session. ADR-0050 later
   adds whole-index reconciliation and server-authoritative deletion without
   changing this cache boundary.
8. **Do not expand the server surface for richer tool cards.** The client renders
   content and trust from public events, consumes classification and structured
   result fields when present, and degrades to generic cards when the public
   stream does not provide stored invocation details. As a presentation-only
   compaction, adjacent successful completions of the same tool render as one
   count-labelled activity bundle. Every invocation, argument, and result stays
   available inside the expanded bundle; messages, different tools, approvals,
   and non-success outcomes break the bundle.
9. **Execute native tests under full Xcode in hosted CI.** Command Line Tools may
   compile a Swift Testing bundle without running its tests. The repository's
   `make test-apple` target therefore refuses that environment, selects full
   Xcode when it is installed at the standard path, and CircleCI runs the target
   on a pinned Xcode macOS executor. Server release packaging depends on that
   job, so native-client regressions block delivery.

## Consequences

- Apple users gain a native conversation, approval, question, working-state,
  artifact, and interrupt surface without changing shared-core authority.
- The token is device-local and not recoverable from preferences or local
  history.
- History survives relaunch on every supported OS, but only newer OS releases
  use SwiftData because the framework cannot satisfy the declared minimums.
- A full Xcode installation is required to build the application and execute
  Apple test bundles. The Swift package can compile shared source independently.
- Tool cards cannot invent effect classifications or structured fields omitted
  by the public event contract. A future API expansion would require its own
  authorized contract and security review.
- Repetitive successful activity stays concise without changing or discarding
  the durable per-invocation event history.
- Project milestone status does not change; this is a separately authorized
  client surface over completed API capabilities.
- Native-client tests are an explicit hosted verification partition because
  they require Apple tooling and cannot be folded into the portable Python
  `make check` contract.

## Alternatives considered

- **Raise the deployment floor to iOS 17/macOS 14:** rejected because it violates
  the explicitly requested compatibility range.
- **Pretend SwiftData can deploy to older systems:** rejected because availability
  annotations do not make a missing operating-system framework exist.
- **Store the bearer token in `UserDefaults`:** rejected because preferences are
  not a credential store.
- **Add a server session-list or tool-invocation-detail route:** deferred by this
  assignment. ADR-0050 later authorizes the session list and delete routes; the
  tool-invocation-detail route remains deferred.
- **Embed the Python downloadable client:** rejected because a native client can
  use the public protocol directly and should not inherit the server toolchain.
