# ADR-0066: Atomic publication of the documentation site

- **Status:** Accepted
- **Date:** 2026-08-19

## Context

The repository already builds the complete documentation corpus as a navigable
MkDocs/Material site, but it has no public hosting path. The production
deployment already packages an immutable commit, promotes it on one DigitalOcean
Droplet, and reconciles one Nginx configuration after the application release
passes. The authoritative DNS zone uses DigitalOcean nameservers. When this
work began, `docs.veetbot.com` did not yet resolve; its DNS record and
certificate were provisioned before the delivery change was proposed for
merge.

Serving the site at `veetbot.com/docs/` would still require a new apex DNS record
and certificate, while also making MkDocs operate beneath a path prefix. A
dedicated `docs.veetbot.com` host requires the same one-time DNS and certificate
work without coupling documentation links, search, and assets to a prefix.

## Decision

1. `https://docs.veetbot.com/` is the canonical public origin for the complete
   MkDocs site. The application API remains the only dynamic public service.
2. The existing `package-release` job builds MkDocs in strict mode from the
   tested commit. It adds the immutable release ID as `release.txt`, creates a
   compressed site artifact, and records its SHA-256 checksum beside the
   application artifact.
3. The existing post-application Nginx job transfers the documentation artifact
   and configuration together. The server verifies the checksum and archive
   paths, extracts the site below `/opt/veetbot/docs/releases/<release-id>`, and
   atomically promotes `/opt/veetbot/docs/current` under the same deployment lock
   used by application and Nginx releases.
4. Nginx serves only files from that current symlink. It disables directory
   listing, denies dotfiles, applies browser security headers, and redirects
   plaintext requests to a dedicated TLS virtual host.
5. Nginx configuration validation and reload remain the publication boundary.
   A failure restores both the previous Nginx configuration and the previous
   documentation symlink. Five immutable documentation releases are retained.
6. CircleCI verifies `release.txt` through the public TLS endpoint after Nginx
   reload. A release is not successfully delivered when the public site reports
   a different identity or cannot be reached. If a newer application release
   wins the deployment race first, the Nginx deployment reports a distinct
   stale outcome and CircleCI skips the obsolete identity probe.
7. The DigitalOcean DNS record and the Let's Encrypt certificate are one-time
   operator prerequisites. Credentials for either system remain outside the
   repository and CircleCI. The deployment must not be merged to `main` until
   those prerequisites exist, because strict Nginx validation deliberately
   fails when the certificate is absent.

This extends the delivery mechanism in ADR-0048 without changing an engineering
plan requirement or adding a milestone gate.

## Consequences

- Documentation changes are published from the same tested commit and in the
  same serialized delivery sequence as the application.
- The docs site consumes no API or worker capacity beyond Nginx serving static
  files, but it shares the Droplet's availability and storage failure domain.
- A documentation-only change still traverses every required production gate
  and application deployment step; delivery remains commit-atomic rather than
  path-filtered.
- The public release marker intentionally exposes only the already non-secret
  deployment identity.
- The operator must provision and renew an additional certificate and maintain
  one additional DNS record.
