import assert from 'node:assert/strict';
import test from 'node:test';

import { checkFixtures, fixtureProps } from '../builder/lib/fixtures.mjs';

const MANIFEST = {
  feeds: [{ id: 'agent.flight-status', history: 2 }],
  settings: {
    type: 'object',
    additionalProperties: false,
    required: ['flight'],
    properties: { flight: { type: 'string', maxLength: 12 } },
  },
};

const TYPICAL = {
  now: '2026-10-22T18:00:00Z',
  settings: { flight: 'EX 100' },
  feeds: {
    'agent.flight-status': {
      observedAt: '2026-10-22T17:59:30Z',
      expectedIntervalSec: 300,
      data: { gate: 'B12' },
      history: [{ observedAt: '2026-10-22T17:54:30Z', data: { gate: 'B10' } }],
    },
  },
};

function files(fixture) {
  const text = typeof fixture === 'string' ? fixture : JSON.stringify(fixture);
  return new Map([
    ['fixtures/typical.json', text],
    ['fixtures/empty.json', JSON.stringify({ now: TYPICAL.now, settings: TYPICAL.settings, feeds: {} })],
    ['fixtures/stale.json', JSON.stringify(TYPICAL)],
  ]);
}

function rules(diagnostics) {
  return diagnostics.map((diagnostic) => diagnostic.rule);
}

test('fixtures with declared feeds and valid settings pass', () => {
  assert.deepEqual(checkFixtures(files(TYPICAL), MANIFEST), []);
});

test('a fixture must be JSON of the fixture shape', () => {
  assert.deepEqual(rules(checkFixtures(files('{not json'), MANIFEST)), ['fixture.invalid_json']);
  assert.deepEqual(rules(checkFixtures(files({ ...TYPICAL, extra: 1 }), MANIFEST)), ['fixture.shape']);
  assert.deepEqual(rules(checkFixtures(files({ ...TYPICAL, now: 'yesterday' }), MANIFEST)), ['fixture.shape']);
});

test('a fixture may supply only declared feeds, within the declared history', () => {
  const undeclared = { ...TYPICAL, feeds: { ...TYPICAL.feeds, 'veetbot.approvals.pending': TYPICAL.feeds['agent.flight-status'] } };
  assert.deepEqual(rules(checkFixtures(files(undeclared), MANIFEST)), ['fixture.undeclared_feed']);
  const history = Array.from({ length: 3 }, () => TYPICAL.feeds['agent.flight-status'].history[0]);
  const deep = { ...TYPICAL, feeds: { 'agent.flight-status': { ...TYPICAL.feeds['agent.flight-status'], history } } };
  assert.deepEqual(rules(checkFixtures(files(deep), MANIFEST)), ['fixture.history']);
});

test('fixture settings must satisfy the manifest settings schema', () => {
  assert.deepEqual(rules(checkFixtures(files({ ...TYPICAL, settings: {} }), MANIFEST)), ['fixture.settings']);
  assert.deepEqual(rules(checkFixtures(files({ ...TYPICAL, settings: { flight: 'x'.repeat(13) } }), MANIFEST)), ['fixture.settings']);
});

test('fixture props are what a widget receives', () => {
  const props = fixtureProps(TYPICAL, MANIFEST);
  assert.equal(props.now, Date.parse(TYPICAL.now));
  assert.deepEqual(props.settings, { flight: 'EX 100' });
  assert.deepEqual(props.feeds['agent.flight-status'], {
    observedAt: Date.parse('2026-10-22T17:59:30Z'),
    expectedIntervalSec: 300,
    error: null,
    data: { gate: 'B12' },
    history: [{ observedAt: Date.parse('2026-10-22T17:54:30Z'), data: { gate: 'B10' } }],
  });
  // A declared feed with no snapshot yet reaches the widget as pending, not missing.
  assert.deepEqual(fixtureProps({ ...TYPICAL, feeds: {} }, MANIFEST).feeds, {
    'agent.flight-status': { observedAt: null, expectedIntervalSec: 0, error: null, data: null, history: [] },
  });
});
