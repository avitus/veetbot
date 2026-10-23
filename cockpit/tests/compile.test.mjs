import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';

import { compileWidget } from '../builder/lib/compile.mjs';
import { renderFixtures } from '../builder/lib/render.mjs';

const GOOD = `
import { Metric, Tile } from '@veetbot/cockpit-kit';
import type { WidgetProps } from '@veetbot/cockpit-kit';

interface Status { gate: string }

export default function FlightTracker({ feeds, settings }: WidgetProps) {
  const feed = feeds['agent.flight-status'];
  const status = feed?.data as Status | null | undefined;
  return (
    <Tile title={String(settings.flight)} freshness={feed}>
      <Metric value={status?.gate ?? null} label="Gate" size="m" />
    </Tile>
  );
}
`;

const MANIFEST = {
  slug: 'flight-tracker',
  title: 'Flight tracker',
  sizes: ['sm', 'md'],
  feeds: [{ id: 'agent.flight-status', history: 0 }],
  settings: { type: 'object', additionalProperties: false, properties: { flight: { type: 'string', maxLength: 12 } } },
};

const NOW = '2026-10-22T18:00:00Z';
const FIXTURES = new Map([
  ['fixtures/typical.json', JSON.stringify({ now: NOW, settings: { flight: 'EX 100' }, feeds: { 'agent.flight-status': { observedAt: NOW, expectedIntervalSec: 300, data: { gate: 'B12' } } } })],
  ['fixtures/empty.json', JSON.stringify({ now: NOW, settings: { flight: 'EX 100' }, feeds: {} })],
  ['fixtures/stale.json', JSON.stringify({ now: NOW, settings: { flight: 'EX 100' }, feeds: { 'agent.flight-status': { observedAt: '2026-10-22T12:00:00Z', expectedIntervalSec: 300, data: { gate: 'B12' } } } })],
]);

function packageWith(source) {
  return new Map([['widget.tsx', source], ...FIXTURES]);
}

function rules(diagnostics) {
  return diagnostics.map((diagnostic) => diagnostic.rule);
}

test('a well-typed widget compiles to a self-registering classic script', async () => {
  const { diagnostics, bundle } = await compileWidget(packageWith(GOOD));
  assert.deepEqual(diagnostics, []);
  assert.ok(bundle && bundle.length > 0);
  assert.doesNotMatch(bundle, /^\s*import\s|\bimport\(|\brequire\(/m);
  const registered = [];
  const context = vm.createContext({
    React: {},
    ReactJSXRuntime: { jsx() {}, jsxs() {}, Fragment: 'f' },
    VeetbotKit: { Tile() {}, Metric() {} },
    VeetbotRealm: { register: (component) => registered.push(component) },
  });
  vm.runInContext(bundle, context);
  assert.equal(registered.length, 1);
  assert.equal(typeof registered[0], 'function');
});

test('a type error is a diagnostic with its line', async () => {
  const source = GOOD.replace('value={status?.gate ?? null}', 'value={{ gate: 1 }}');
  const { diagnostics, bundle } = await compileWidget(packageWith(source));
  assert.deepEqual(rules(diagnostics), ['compile.typecheck']);
  assert.equal(diagnostics[0].file, 'widget.tsx');
  assert.equal(diagnostics[0].line, 12);
  assert.equal(bundle, undefined);
});

test('the default export must be a widget component', async () => {
  const { diagnostics } = await compileWidget(packageWith('export default 42;\n'));
  assert.deepEqual(rules(diagnostics), ['compile.typecheck']);
});

test('every fixture renders at every declared size', async () => {
  const { bundle } = await compileWidget(packageWith(GOOD));
  assert.deepEqual(await renderFixtures(packageWith(GOOD), /** @type {string} */ (bundle), MANIFEST), []);
});

test('a widget that throws or renders nothing is rejected per fixture and size', async () => {
  const throwing = GOOD.replace('const status =', "if (!feed?.data) throw new Error('no feed');\n  const status =");
  const thrown = await compileWidget(packageWith(throwing));
  const failures = await renderFixtures(packageWith(throwing), /** @type {string} */ (thrown.bundle), MANIFEST);
  assert.deepEqual(rules(failures), ['render.throws', 'render.throws']);
  assert.ok(failures.every((failure) => failure.file === 'fixtures/empty.json'));
  assert.deepEqual(failures.map((failure) => failure.size), ['sm', 'md']);

  const nothing = 'export default function Nothing() { return null; }\n';
  const blank = await compileWidget(packageWith(nothing));
  assert.deepEqual(rules(await renderFixtures(packageWith(nothing), /** @type {string} */ (blank.bundle), MANIFEST)), Array(6).fill('render.empty'));
});
