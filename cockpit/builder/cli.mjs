#!/usr/bin/env node
// cockpit-builder build <widget-dir> [--screen <screen.json>] [--out <dir>]
//
// Prints one JSON report on stdout. Exit 0: built. Exit 1: rejected, with
// diagnostics. Exit 2: usage or internal error. The screen file carries the
// principal's known names and private settings values; it never leaves the run.
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { parseArgs } from 'node:util';

import { buildWidget } from './lib/build.mjs';
import { readPackage } from './lib/package.mjs';

function usage(message) {
  process.stderr.write(`${message}\nusage: cli.mjs build <widget-dir> [--screen <file>] [--out <dir>]\n`);
  process.exit(2);
}

let parsed;
try {
  parsed = parseArgs({
    allowPositionals: true,
    options: { screen: { type: 'string' }, out: { type: 'string' } },
  });
} catch (error) {
  usage(/** @type {Error} */ (error).message);
}
const [command, directory] = parsed.positionals;
if (command !== 'build' || !directory || parsed.positionals.length !== 2) usage('expected: build <widget-dir>');

try {
  const root = path.resolve(directory);
  const { files, diagnostics: read } = readPackage(root);
  const screen = parsed.values.screen ? JSON.parse(readFileSync(parsed.values.screen, 'utf8')) : {};
  const slug = path.basename(root);
  const result = read.length ? { ok: false, diagnostics: read } : await buildWidget({ files, slug, screen });
  if (result.ok && parsed.values.out) {
    mkdirSync(parsed.values.out, { recursive: true });
    writeFileSync(path.join(parsed.values.out, `${result.sha256}.js`), result.bundle);
  }
  const report = { ok: result.ok, slug, kit_api: result.kit_api, sha256: result.sha256, diagnostics: result.diagnostics };
  process.stdout.write(`${JSON.stringify(report)}\n`);
  process.exit(result.ok ? 0 : 1);
} catch (error) {
  process.stderr.write(`cockpit builder failed: ${/** @type {Error} */ (error).message}\n`);
  process.exit(2);
}
