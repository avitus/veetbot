import assert from 'node:assert/strict';
import { mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';

import { checkFiles, readPackage } from '../builder/lib/package.mjs';

const SOURCE = 'export default function Widget() { return null; }\n';

function files(extra = {}) {
  return new Map(
    Object.entries({
      'widget.json': '{}',
      'widget.tsx': SOURCE,
      'fixtures/typical.json': '{}',
      'fixtures/empty.json': '{}',
      'fixtures/stale.json': '{}',
      ...extra,
    }),
  );
}

function rules(diagnostics) {
  return diagnostics.map((diagnostic) => diagnostic.rule).sort();
}

test('the five required files form a complete package', () => {
  assert.deepEqual(checkFiles(files()), []);
});

test('each required file must be present', () => {
  const missing = files();
  missing.delete('fixtures/stale.json');
  assert.deepEqual(rules(checkFiles(missing)), ['package.missing_file']);
});

test('only the manifest, the component and JSON fixtures are allowed', () => {
  for (const name of ['helper.ts', 'fixtures/notes.txt', 'assets/logo.png', 'Widget.tsx']) {
    assert.deepEqual(rules(checkFiles(files({ [name]: 'x' }))), ['package.file_not_allowed'], name);
  }
  assert.deepEqual(checkFiles(files({ 'fixtures/overnight.json': '{}' })), []);
});

test('paths cannot be absolute or escape the package', () => {
  for (const name of ['/etc/passwd', '../widget.json', 'fixtures/../../x.json', 'fixtures\\typical.json']) {
    assert.deepEqual(rules(checkFiles(files({ [name]: '{}' }))), ['package.path'], name);
  }
});

test('a package holds at most twelve files', () => {
  const extra = Object.fromEntries(Array.from({ length: 8 }, (_, index) => [`fixtures/extra-${index}.json`, '{}']));
  assert.deepEqual(rules(checkFiles(files(extra))), ['package.too_many_files']);
});

test('the component, each fixture and the package are size-capped', () => {
  assert.deepEqual(rules(checkFiles(files({ 'widget.tsx': 'x'.repeat(64 * 1024 + 1) }))), ['package.file_too_large']);
  assert.deepEqual(
    rules(checkFiles(files({ 'fixtures/typical.json': 'x'.repeat(64 * 1024 + 1) }))),
    ['package.file_too_large'],
  );
  const big = Object.fromEntries(
    Array.from({ length: 5 }, (_, index) => [`fixtures/big-${index}.json`, 'x'.repeat(60 * 1024)]),
  );
  assert.deepEqual(rules(checkFiles(files(big))), ['package.too_large']);
});

test('reading a directory refuses symbolic links', (t) => {
  const root = mkdtempSync(path.join(tmpdir(), 'cockpit-package-'));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  mkdirSync(path.join(root, 'fixtures'));
  for (const [name, content] of files()) writeFileSync(path.join(root, name), content);
  const clean = readPackage(root);
  assert.deepEqual(clean.diagnostics, []);
  assert.equal(clean.files.get('widget.tsx'), SOURCE);
  symlinkSync('/etc/hosts', path.join(root, 'fixtures', 'linked.json'));
  assert.deepEqual(rules(readPackage(root).diagnostics), ['package.symlink']);
});
