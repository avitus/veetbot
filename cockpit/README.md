# Cockpit workspace

Source for Cockpit mode's widget runtime ([docs/plan/cockpit.md](../docs/plan/cockpit.md),
ADR-0240):

- `kit/` — `@veetbot/cockpit-kit`, the primitives ported from the approved
  MagicPath designs (`Cockpit · Kit primitives`) and their A2 tokens.
- `host/` — the trusted host page and the sandboxed, network-less realm that
  runs every widget.
- `builder/` — the CLI that validates, screens, type-checks, bundles and
  renders a widget package.
- `schema/` — the manifest, fixture and credential-pattern contracts shared
  with the server.
- `examples/widgets/` — reference widgets; `examples/adversarial/` — the
  corpus the realm boundary tests run.

Widget packages themselves live only on the public `cockpit/widgets` branch,
never on `main`.

## Local development

Requires Node.js 22.13 or newer.

```bash
make test-cockpit
```

That installs the locked dependencies, Chromium and WebKit, builds the runtime into
`dist/`, runs every test under `tests/` (kit, builder, manifest, fixture,
screen and realm among them), and lints. The fixture preview uses WebKit and returns visible text and a PNG.

Build one widget package:

```bash
node cockpit/builder/cli.mjs build cockpit/examples/widgets/weather --out /tmp/bundles
```

The command prints one JSON report and exits 0 when the widget built, 1 when it
was rejected with diagnostics, and 2 on a usage or internal error.
