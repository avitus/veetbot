import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { cpSync, existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

import { buildWidget } from '../builder/lib/build.mjs';
import { readPackage } from '../builder/lib/package.mjs';

const root = fileURLToPath(new URL('..', import.meta.url));
const examples = path.join(root, 'examples/widgets');
const cli = path.join(root, 'builder/cli.mjs');

function flight() {
  return readPackage(path.join(examples, 'flight-tracker')).files;
}

function changed(edits) {
  const files = flight();
  for (const [name, content] of Object.entries(edits)) {
    if (content === null) files.delete(name);
    else files.set(name, typeof content === 'function' ? content(files.get(name)) : content);
  }
  return files;
}

function manifestWith(overrides) {
  return (text) => JSON.stringify({ ...JSON.parse(text), ...overrides });
}

test('every reference widget builds', async () => {
  for (const slug of readdirSync(examples)) {
    const result = await buildWidget({ files: readPackage(path.join(examples, slug)).files, slug });
    assert.deepEqual(result.diagnostics, [], slug);
    assert.equal(result.ok, true, slug);
    assert.equal(result.sha256, createHash('sha256').update(result.bundle).digest('hex'));
    assert.equal(result.manifest.slug, slug);
  }
});

test('each rejection category stops the build with its diagnostic and no bundle', async () => {
  const cases = [
    ['manifest.invalid_json', { 'widget.json': '{' }],
    ['manifest.schema', { 'widget.json': manifestWith({ title: 'x'.repeat(41) }) }],
    ['package.file_not_allowed', { 'helper.ts': 'export {}' }],
    ['package.file_too_large', { 'widget.tsx': (text) => `${text}\n// ${'x'.repeat(64 * 1024)}` }],
    ['lint.import', { 'widget.tsx': (text) => `import _ from 'lodash';\n${text}` }],
    ['lint.banned', { 'widget.tsx': (text) => text.replace('const feed =', "void fetch('https://evil.example');\n  const feed =") }],
    ['screen.credential', { 'widget.tsx': (text) => text.replace('const feed =', 'const token: string = "x";\n  void token;\n  const feed =') }],
    ['fixture.undeclared_feed', { 'fixtures/typical.json': (text) => JSON.stringify({ ...JSON.parse(text), feeds: { 'agent.other': { observedAt: null, expectedIntervalSec: 60 } } }) }],
    ['compile.typecheck', { 'widget.tsx': (text) => text.replace('size={size === \'sm\' ? \'m\' : \'l\'}', 'size={7}') }],
    ['render.throws', { 'widget.tsx': (text) => text.replace('const flight =', "if (!feed?.data) throw new Error('missing');\n  const flight =") }],
  ];
  for (const [rule, edits] of cases) {
    const result = await buildWidget({ files: changed(edits), slug: 'flight-tracker' });
    assert.equal(result.ok, false, rule);
    assert.ok(result.diagnostics.some((diagnostic) => diagnostic.rule === rule), `${rule}: ${JSON.stringify(result.diagnostics)}`);
    assert.equal(result.bundle, undefined, rule);
  }
});

test('the privacy screen uses the principal context the server supplies', async () => {
  const files = changed({ 'fixtures/typical.json': (text) => text.replace('EX 100', 'XA 4421') });
  const clean = await buildWidget({ files, slug: 'flight-tracker' });
  assert.equal(clean.ok, true);
  const screened = await buildWidget({ files, slug: 'flight-tracker', screen: { settingsValues: ['XA 4421'] } });
  assert.deepEqual(screened.diagnostics.map((diagnostic) => diagnostic.rule), ['screen.setting']);
});

test('the privacy screen also checks the commit message the outbox will write', async () => {
  const files = flight();
  const message = 'cockpit: create flight-tracker\n\nCockpit-Widget: flight-tracker\nVeetbot-Revision: 00000000-0000-4000-8000-000000000001\n';
  const clean = await buildWidget({ files, slug: 'flight-tracker', screen: { commitMessage: message } });
  assert.equal(clean.ok, true, JSON.stringify(clean.diagnostics));
  const screened = await buildWidget({ files, slug: 'flight-tracker', screen: { names: ['Tracker'], commitMessage: message } });
  const commit = screened.diagnostics.filter((diagnostic) => diagnostic.file === 'commit message');
  assert.deepEqual(commit.map((diagnostic) => diagnostic.rule), ['screen.name']);
  assert.ok(commit.every((diagnostic) => !diagnostic.message.includes('Tracker')));
});

test('source checks run before anything is compiled', async () => {
  const result = await buildWidget({ files: changed({ 'widget.tsx': (text) => `import _ from 'lodash';\n${text}` }), slug: 'flight-tracker' });
  assert.ok(result.diagnostics.every((diagnostic) => !diagnostic.rule.startsWith('compile.') && !diagnostic.rule.startsWith('render.')));
});

test('the CLI reports JSON, writes the bundle, and exits by outcome', (t) => {
  const work = mkdtempSync(path.join(tmpdir(), 'cockpit-cli-'));
  t.after(() => rmSync(work, { recursive: true, force: true }));
  const out = path.join(work, 'out');
  const ok = spawnSync(process.execPath, [cli, 'build', path.join(examples, 'weather'), '--out', out], { encoding: 'utf8' });
  assert.equal(ok.status, 0, ok.stderr);
  const report = JSON.parse(ok.stdout);
  assert.equal(report.ok, true);
  assert.equal(report.slug, 'weather');
  assert.equal(report.kit_api, 1);
  assert.ok(existsSync(path.join(out, `${report.sha256}.js`)));

  const broken = path.join(work, 'weather');
  cpSync(path.join(examples, 'weather'), broken, { recursive: true });
  writeFileSync(path.join(broken, 'widget.tsx'), `${readFileSync(path.join(broken, 'widget.tsx'), 'utf8')}\nvoid eval('1');\n`);
  const rejected = spawnSync(process.execPath, [cli, 'build', broken], { encoding: 'utf8' });
  assert.equal(rejected.status, 1);
  assert.equal(JSON.parse(rejected.stdout).ok, false);

  const screenFile = path.join(work, 'screen.json');
  writeFileSync(screenFile, JSON.stringify({ names: ['Example City'] }));
  const screened = spawnSync(process.execPath, [cli, 'build', path.join(examples, 'weather'), '--screen', screenFile], { encoding: 'utf8' });
  assert.equal(screened.status, 1);
  assert.deepEqual(new Set(JSON.parse(screened.stdout).diagnostics.map((diagnostic) => diagnostic.rule)), new Set(['screen.name']));

  const usage = spawnSync(process.execPath, [cli, 'nonsense'], { encoding: 'utf8' });
  assert.equal(usage.status, 2);
});

test('the CLI can return the bundle and manifest in its report for a sandboxed caller', () => {
  const emitted = spawnSync(process.execPath, [cli, 'build', path.join(examples, 'weather'), '--emit-bundle'], { encoding: 'utf8' });
  assert.equal(emitted.status, 0, emitted.stderr);
  const report = JSON.parse(emitted.stdout);
  assert.equal(createHash('sha256').update(report.bundle).digest('hex'), report.sha256);
  assert.equal(report.manifest.slug, 'weather');
  const plain = JSON.parse(spawnSync(process.execPath, [cli, 'build', path.join(examples, 'weather')], { encoding: 'utf8' }).stdout);
  assert.equal(plain.bundle, undefined);
  assert.equal(plain.manifest, undefined);
});

test('the CLI builds a package and screen read from stdin, judging the raw file names', () => {
  const files = Object.fromEntries(readPackage(path.join(examples, 'weather')).files);
  const run = (input) => spawnSync(process.execPath, [cli, 'build', '--stdin', 'weather', '--emit-bundle'], { encoding: 'utf8', input });
  const built = run(JSON.stringify({ files, screen: {} }));
  assert.equal(built.status, 0, built.stderr);
  const report = JSON.parse(built.stdout);
  assert.equal(report.slug, 'weather');
  assert.equal(createHash('sha256').update(report.bundle).digest('hex'), report.sha256);

  const escaping = run(JSON.stringify({ files: { ...files, '../screen.json': '{}' }, screen: {} }));
  assert.equal(escaping.status, 1);
  assert.deepEqual(JSON.parse(escaping.stdout).diagnostics.map((diagnostic) => diagnostic.rule), ['package.path']);

  const screened = run(JSON.stringify({ files, screen: { names: ['Example City'], settingsValues: [] } }));
  assert.equal(screened.status, 1);
  assert.deepEqual(new Set(JSON.parse(screened.stdout).diagnostics.map((diagnostic) => diagnostic.rule)), new Set(['screen.name']));

  for (const input of [
    'not json',
    JSON.stringify({ files: { 'widget.tsx': 1 }, screen: {} }),
    JSON.stringify({ files, screen: { names: 'x' } }),
    JSON.stringify({ files, screen: { commitMessage: 7 } }),
  ]) {
    assert.equal(run(input).status, 2, input);
  }
  const slugless = spawnSync(process.execPath, [cli, 'build', '--stdin'], { encoding: 'utf8', input: '{}' });
  assert.equal(slugless.status, 2);
});

test('preview renders the synthetic weather fixture as visible text and a PNG', () => {
  const files = Object.fromEntries(readPackage(path.join(examples, 'weather')).files);
  const input = JSON.stringify({ files, preview: { fixture: 'fixtures/typical.json', size: 'sm' } });
  const run = spawnSync(process.execPath, [cli, 'build', '--stdin', 'weather'], { encoding: 'utf8', input });
  assert.equal(run.status, 0, run.stderr);
  const { preview } = JSON.parse(run.stdout);
  assert.match(preview.text, /Example City/);
  assert.doesNotMatch(preview.text, /Stale|days ago|\d+ d ago/);
  assert.match(preview.text, /64°/);
  assert.equal(Buffer.from(preview.png, 'base64').subarray(1, 4).toString(), 'PNG');
});

test('preview renders supplied live feed values and labels them live', () => {
  const files = Object.fromEntries(readPackage(path.join(examples, 'weather')).files);
  const typical = JSON.parse(files['fixtures/typical.json']);
  const [id] = Object.keys(typical.feeds);
  const live = {
    ...typical,
    feeds: { [id]: { ...typical.feeds[id], data: { ...typical.feeds[id].data, temperature: 37 } } },
  };
  const input = JSON.stringify({ files, preview: { size: 'sm', live: JSON.stringify(live) } });
  const run = spawnSync(process.execPath, [cli, 'build', '--stdin', 'weather'], { encoding: 'utf8', input });
  assert.equal(run.status, 0, run.stderr);
  const { preview } = JSON.parse(run.stdout);
  assert.equal(preview.fixture, 'live');
  assert.match(preview.text, /37°/);
});

test('preview reports a browser-only render failure without echoing the exception', () => {
  const files = Object.fromEntries(readPackage(path.join(examples, 'weather')).files);
  files['widget.tsx'] = files['widget.tsx'].replace('const small =', "if (typeof window !== 'undefined') throw new Error('private-render-value');\n  const small =");
  const input = JSON.stringify({ files, preview: {} });
  const run = spawnSync(process.execPath, [cli, 'build', '--stdin', 'weather'], { encoding: 'utf8', input });
  assert.equal(run.status, 1, run.stderr);
  const report = JSON.parse(run.stdout);
  assert.equal(report.diagnostics[0].rule, 'preview.render');
  assert.equal(report.preview, undefined);
  assert.doesNotMatch(run.stdout, /private-render-value/);
});
