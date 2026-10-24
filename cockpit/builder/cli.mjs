#!/usr/bin/env node
// cockpit-builder build <widget-dir> [--screen <screen.json>] [--out <dir>] [--emit-bundle]
// cockpit-builder build --stdin <slug> [--out <dir>] [--emit-bundle]
//
// Prints one JSON report on stdout. Exit 0: built. Exit 1: rejected, with
// diagnostics. Exit 2: usage or internal error. The screen file carries the
// principal's known names and private settings values; it never leaves the run.
// --stdin reads {"files": {name: text}, "screen": {...}} instead of a directory,
// where the screen may also carry the commitMessage the outbox will write,
// so a sandboxed caller writes nothing to the workspace and the builder judges
// the package's raw file names. --emit-bundle adds the bundle and manifest to
// the report, for a sandboxed caller that reads only the command's output.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';

import { buildWidget } from './lib/build.mjs';
import { readPackage } from './lib/package.mjs';

function usage(message) {
  process.stderr.write(
    `${message}\nusage: cli.mjs build <widget-dir> [--screen <file>] [--out <dir>] [--emit-bundle]\n` +
      '       cli.mjs build --stdin <slug> [--out <dir>] [--emit-bundle]\n',
  );
  process.exit(2);
}

function strings(value) {
  return value === undefined || (Array.isArray(value) && value.every((item) => typeof item === 'string'));
}

function readStdinPackage() {
  let input;
  try {
    input = JSON.parse(readFileSync(0, 'utf8'));
  } catch (error) {
    usage(`stdin is not JSON: ${/** @type {Error} */ (error).message}`);
  }
  const { files, screen = {} } = input ?? {};
  if (!files || typeof files !== 'object' || Array.isArray(files) || !Object.values(files).every((text) => typeof text === 'string')) {
    usage('stdin files must map each package path to its text');
  }
  if (typeof screen !== 'object' || Array.isArray(screen) || !strings(screen.names) || !strings(screen.settingsValues)) {
    usage('stdin screen names and settingsValues must be lists of strings');
  }
  if (screen.commitMessage !== undefined && typeof screen.commitMessage !== 'string') {
    usage('stdin screen commitMessage must be text');
  }
  return { files: new Map(Object.entries(files)), screen, read: [] };
}

let parsed;
try {
  parsed = parseArgs({
    allowPositionals: true,
    options: {
      screen: { type: 'string' },
      out: { type: 'string' },
      'emit-bundle': { type: 'boolean' },
      stdin: { type: 'boolean' },
    },
  });
} catch (error) {
  usage(/** @type {Error} */ (error).message);
}
const [command, target] = parsed.positionals;
if (command !== 'build' || !target || parsed.positionals.length !== 2) usage('expected: build <widget-dir> or build --stdin <slug>');
if (parsed.values.stdin && parsed.values.screen) usage('--stdin carries the screen; --screen does not apply');

try {
  let slug;
  let source;
  if (parsed.values.stdin) {
    slug = target;
    source = readStdinPackage();
  } else {
    const root = path.resolve(target);
    const { files, diagnostics: read } = readPackage(root);
    const screen = parsed.values.screen ? JSON.parse(readFileSync(parsed.values.screen, 'utf8')) : {};
    slug = path.basename(root);
    source = { files, screen, read };
  }
  const { files, screen, read } = source;
  const result = read.length ? { ok: false, diagnostics: read } : await buildWidget({ files, slug, screen });
  if (result.ok && parsed.values.out) {
    mkdirSync(parsed.values.out, { recursive: true });
    writeFileSync(path.join(parsed.values.out, `${result.sha256}.js`), result.bundle);
  }
  const report = { ok: result.ok, slug, kit_api: result.kit_api, sha256: result.sha256, diagnostics: result.diagnostics };
  if (result.ok && parsed.values['emit-bundle']) Object.assign(report, { bundle: result.bundle, manifest: result.manifest });
  process.stdout.write(`${JSON.stringify(report)}\n`);
  process.exit(result.ok ? 0 : 1);
} catch (error) {
  process.stderr.write(`cockpit builder failed: ${/** @type {Error} */ (error).message}\n`);
  process.exit(2);
}
