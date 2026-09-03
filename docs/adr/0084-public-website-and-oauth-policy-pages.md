# ADR-0084: Atomic public website publication on DigitalOcean

- **Status:** Accepted
- **Date:** 2026-09-02
- **Related:** ADR-0046, ADR-0048, ADR-0066, ADR-0071
- **Detailed design:** `docs/plan/development-toolchain.md`,
  `docs/plan/email-integration.md`, `docs/deployment.md`

## Context

Google OAuth branding requires a public application homepage and, for the Gmail
permissions Veetbot requests, public privacy-policy and terms links. The
existing `api.veetbot.com` origin is an authenticated control plane and
`docs.veetbot.com` is the complete technical documentation corpus. Neither is a
small, visitor-oriented product and legal surface.

Veetbot already owns the authoritative DigitalOcean DNS zone, runs Nginx on its
production Droplet, and atomically publishes the application and documentation
from one tested release. A temporary OpenAI Sites publication proved the page
content and design, but keeping a second hosting provider would split release
identity, operational ownership, and outage diagnosis for three static pages.

The policy content must match Milestone 18's actual Gmail behavior: separate
read, modify, and send credentials; explicit approval before send; no attachment
download; operator-controlled persistence of run records; and no credential or
raw-provider-error leakage into durable events.

## Decision

1. `https://www.veetbot.com/` is the canonical public homepage.
   `https://www.veetbot.com/privacy` and `https://www.veetbot.com/tos` are the
   stable Google OAuth policy URLs. The apex redirects to the canonical `www`
   origin.
2. Source lives under `website/` as a Next.js static export. It contains no
   account system, form, analytics integration, application credential,
   database, or object-store binding. Boundary tests build the export and
   inspect all three public routes and their Gmail disclosures.
3. A credential-free CircleCI `public-site` verification job installs the
   locked Node dependencies, builds and tests the export, lints the source, and
   passes only `website/out` to release packaging. `package-release` stamps that
   artifact with the same release ID as the application and documentation and
   records its SHA-256 checksum.
4. `deploy-nginx` transfers the checksummed website artifact after the matching
   application release succeeds. Under the existing deployment lock, the host
   validates archive paths and file types, requires the homepage and both policy
   pages, atomically promotes `/opt/veetbot/shared/website/current`, installs the
   candidate Nginx configuration, validates it, and reloads Nginx. Any failure
   restores the previous Nginx, documentation, and website pointers. Five
   immutable website releases are retained.
5. Nginx terminates TLS and serves only the promoted static tree. The build's
   `privacy.html` and `tos.html` files are mapped to their extensionless URLs,
   dotfiles are denied, and browser security headers are applied.
6. DigitalOcean DNS maps the apex to the production Droplet and `www` to the
   apex. A Let's Encrypt certificate covering both hostnames is a one-time
   prerequisite and remains outside the repository. Production verification
   requires the public website's `release.txt` to match the application and
   documentation identities.
7. Policy text describes current product behavior but is not a substitute for
   the normative engineering plan. A Gmail data-use change ships with a policy
   review before the new behavior is enabled.

## Consequences

- Google and users receive stable same-domain pages that explain the product,
  requested Gmail scopes, AI-provider processing, retention, revocation,
  deletion, and Limited Use commitments.
- The public website shares the production Droplet and atomic release identity
  with the application and docs. This avoids a second hosting dependency but
  also shares their availability and deployment failure domain.
- A pinned Node build is added to CI without adding Node or website credentials
  to any Veetbot application process.
- The first deployment cannot pass strict Nginx validation until the operator
  provisions the dual-host certificate at the committed path.

## Alternatives considered

- **Continue hosting on OpenAI Sites.** Rejected after the temporary preview:
  automatic hosting and TLS did not justify a second provider, independent
  release identity, and separate custom-domain ceremony for this static surface.
- **Serve the pages from `api.veetbot.com`.** Rejected because the authenticated
  control plane is not a public product surface.
- **Add the pages to `docs.veetbot.com`.** Rejected because Google asks for an
  application homepage and user-facing policies, while that origin is the
  technical documentation corpus.
