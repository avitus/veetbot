#!/usr/bin/env node
// Shared public-package privacy screen for saved Python jobs. Never evaluates Python.
import { readFileSync } from 'node:fs';
import { screenFiles } from './lib/screen.mjs';

try {
  const raw = readFileSync(0, 'utf8');
  if (Buffer.byteLength(raw) > 512 * 1024) throw new Error('Input exceeds the package limit.');
  const { files, screen = {} } = JSON.parse(raw);
  if (!files || typeof files !== 'object' || Array.isArray(files)
      || Object.keys(files).sort().join(',') !== 'job.json,main.py'
      || !Object.values(files).every((text) => typeof text === 'string' && Buffer.byteLength(text) <= 64 * 1024)) {
    throw new Error('A job contains bounded job.json and main.py files.');
  }
  const diagnostics = screenFiles(new Map(Object.entries(files)), screen);
  process.stdout.write(JSON.stringify({ ok: diagnostics.length === 0, diagnostics }));
  process.exitCode = diagnostics.length ? 1 : 0;
} catch {
  process.stdout.write(JSON.stringify({ ok: false, diagnostics: [{ rule: 'job.package_invalid', file: '', message: 'The job package or privacy screen is invalid.' }] }));
  process.exitCode = 1;
}
