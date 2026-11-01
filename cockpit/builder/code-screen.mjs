#!/usr/bin/env node
// The screen over text about to reach the public repository (ADR-0281). Input is
// stdin only and is never evaluated. A finding names the file and the code, never
// the matched value. Exit 0 is clean, 1 has findings, 2 is unusable input.
import { readFileSync } from 'node:fs';
import { screenFiles } from './lib/screen.mjs';

const MAXIMUM_INPUT_BYTES = 8 * 1024 * 1024;

try {
  const raw = readFileSync(0);
  if (raw.length > MAXIMUM_INPUT_BYTES) throw new Error('Input exceeds the limit.');
  const { files, screen = {} } = JSON.parse(raw.toString('utf8'));
  if (!files || typeof files !== 'object' || Array.isArray(files)
      || !Object.values(files).every((text) => typeof text === 'string')) {
    throw new Error('Files map a path to its added text.');
  }
  // A path is published with its file, so it is screened with it.
  const screened = new Map(Object.entries(files).map(([path, text]) => [path, `${path}\n${text}`]));
  const findings = screenFiles(screened, { ...screen, credentials: 'secrets' })
    .map(({ rule, file, message }) => ({ file, code: rule, message }));
  process.stdout.write(JSON.stringify({ ok: findings.length === 0, findings }));
  process.exitCode = findings.length ? 1 : 0;
} catch {
  process.stdout.write(JSON.stringify({ ok: false, findings: [] }));
  process.exitCode = 2;
}
