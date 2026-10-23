import assert from 'node:assert/strict';
import test from 'node:test';

import { validateManifest } from '../builder/lib/manifest.mjs';

function manifest(overrides = {}) {
  return {
    schema: 1,
    slug: 'flight-tracker',
    title: 'Flight tracker',
    purpose: 'Shows departure, delay and gate for one flight.',
    kit_api: 1,
    sizes: ['md', 'lg'],
    feeds: [{ id: 'agent.flight-status', history: 0 }],
    settings: {
      type: 'object',
      additionalProperties: false,
      required: ['flight'],
      properties: { flight: { type: 'string', maxLength: 12 } },
    },
    ...overrides,
  };
}

function rules(diagnostics) {
  return diagnostics.map((diagnostic) => diagnostic.rule);
}

test('a well-formed manifest has no diagnostics', () => {
  assert.deepEqual(validateManifest(manifest(), 'flight-tracker'), []);
});

test('the slug must match the widget directory', () => {
  assert.deepEqual(rules(validateManifest(manifest(), 'another-widget')), ['manifest.slug_mismatch']);
});

test('titles and purposes are bounded generic text', () => {
  const diagnostics = validateManifest(manifest({ title: 'x'.repeat(41), purpose: 'y'.repeat(201) }), 'flight-tracker');
  assert.deepEqual(rules(diagnostics), ['manifest.schema', 'manifest.schema']);
  assert.ok(diagnostics.every((diagnostic) => diagnostic.file === 'widget.json'));
});

test('sizes are the kit tile sizes', () => {
  assert.deepEqual(validateManifest(manifest({ sizes: ['sm', 'md', 'tall', 'lg', 'wide'] }), 'flight-tracker'), []);
  for (const size of ['huge', 's', 'l', 'xl']) {
    assert.deepEqual(rules(validateManifest(manifest({ sizes: [size] }), 'flight-tracker')), ['manifest.schema'], size);
  }
});

test('unknown fields and sizes are refused', () => {
  assert.deepEqual(rules(validateManifest(manifest({ sizes: ['huge'] }), 'flight-tracker')), ['manifest.schema']);
  assert.deepEqual(rules(validateManifest(manifest({ script: 'x' }), 'flight-tracker')), ['manifest.schema']);
});

test('feeds name platform or assistant feeds with bounded history', () => {
  for (const feeds of [
    [{ id: 'https://evil.example/feed', history: 0 }],
    [{ id: 'agent.weather', history: 289 }],
    [{ id: 'veetbot.approvals.pending' }, { id: 'veetbot.approvals.pending' }],
  ]) {
    assert.notDeepEqual(validateManifest(manifest({ feeds }), 'flight-tracker'), [], JSON.stringify(feeds));
  }
  assert.deepEqual(
    validateManifest(manifest({ feeds: [{ id: 'veetbot.approvals.pending', history: 288 }] }), 'flight-tracker'),
    [],
  );
});

test('the settings schema is closed and uses only bounded scalar fields', () => {
  const open = { type: 'object', properties: {} };
  const unbounded = { type: 'object', additionalProperties: false, properties: { note: { type: 'string' } } };
  const nested = {
    type: 'object',
    additionalProperties: false,
    properties: { inner: { type: 'object', properties: {} } },
  };
  for (const settings of [open, unbounded, nested]) {
    assert.deepEqual(rules(validateManifest(manifest({ settings }), 'flight-tracker')), ['manifest.schema']);
  }
});

test('a non-object manifest is a single schema diagnostic', () => {
  assert.deepEqual(rules(validateManifest('nope', 'flight-tracker')), ['manifest.schema']);
});
