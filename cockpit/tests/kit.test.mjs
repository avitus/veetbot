import assert from 'node:assert/strict';
import test from 'node:test';

import { createElement as h } from 'react';
import { renderToString } from 'react-dom/server';

import * as kit from '../dist/kit/index.mjs';

const NOW = Date.parse('2026-10-22T18:00:00Z');

function seeded(seed) {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

// React's server renderer separates adjacent text with empty comments.
function render(element, now = NOW) {
  const html = renderToString(h(kit.CockpitRoot, { theme: 'light', platform: 'mac' }, h(kit.CockpitClock, { frozenAt: now }, element)));
  return html.replaceAll('<!-- -->', '');
}

// Real tags and attribute names only: escaped text such as `&lt;a href=`, in
// content or inside a quoted attribute value, is inert by construction.
function tags(html) {
  return (html.match(/<[a-z][^>]*>/gi) ?? []).map((tag) => tag.replace(/"[^"]*"/g, '""'));
}

test('freshness follows the approved thresholds for any interval and age', () => {
  const random = seeded(7);
  for (let index = 0; index < 2000; index += 1) {
    const interval = 1 + Math.floor(random() * 86400);
    const age = random() * interval * 5;
    const level = kit.freshnessLevel({ observedAt: NOW - age * 1000, expectedIntervalSec: interval }, NOW);
    const expected = age <= interval * 1.5 ? 'live' : age <= interval * 3 ? 'aging' : 'stale';
    assert.equal(level, expected, `interval ${interval}s, age ${age}s`);
  }
  assert.equal(kit.freshnessLevel({ observedAt: null, expectedIntervalSec: 60 }, NOW), 'pending');
  assert.equal(kit.freshnessLevel({ observedAt: NOW, expectedIntervalSec: 60, error: 'timeout' }, NOW), 'error');
  assert.equal(kit.freshnessLevel({ observedAt: NOW - 10_000_000, expectedIntervalSec: 60, error: 'x' }, NOW), 'error');
});

test('a stale tile renders stale and dims every primitive inside it', () => {
  const freshness = { observedAt: NOW - 47 * 60 * 1000, expectedIntervalSec: 300 };
  const html = render(
    h(kit.Tile, { title: 'Markets', size: 'md', freshness, attention: '2 waiting on you' },
      h(kit.Metric, { value: '5,218', label: 'Index' }),
      h(kit.Sparkline, { values: [1, 2, 3] }),
      h(kit.Gauge, { value: 40 }),
      h(kit.List, null, h(kit.Row, { title: 'Reply', marker: 'attention' }))),
  );
  assert.match(html, /data-level="stale"/);
  assert.match(html, /Last update 47 min ago/);
  assert.match(html, /expected every 5 min/);
  for (const primitive of ['ck-metric', 'ck-spark', 'ck-gauge', 'ck-list']) {
    assert.match(html, new RegExp(`class="${primitive}"[^>]*data-dim="true"`), primitive);
  }
  assert.match(html, /data-marker="attention-stale"/);
});

test('a live tile shows its age and no notice', () => {
  const html = render(h(kit.Tile, { title: 'Weather', freshness: { observedAt: NOW - 12_000, expectedIntervalSec: 600 } }, h(kit.Metric, { value: 14, unit: '°' })));
  assert.match(html, /data-level="live"/);
  assert.doesNotMatch(html, /ck-notice/);
  assert.doesNotMatch(html, /data-dim="true"/);
});

test('feed strings render as inert text in every primitive', () => {
  const hostile = [
    '<script>alert(1)</script>',
    '<a href="https://evil.example">x</a>',
    'javascript:alert(1)',
    'https://evil.example/path',
    '<img src=x onerror=alert(1)>',
  ];
  for (const text of hostile) {
    const html = render(
      h(kit.Tile, { title: text, attention: text, footer: text, freshness: { observedAt: NOW, expectedIntervalSec: 60 } },
        h(kit.Metric, { value: text, unit: text, label: text }),
        h(kit.Delta, { value: 1, period: text }),
        h(kit.Bars, { data: [{ label: text, value: 1, display: text }] }),
        h(kit.Gauge, { value: 1, label: text, valueLabel: text }),
        h(kit.Countdown, { target: NOW + 60_000, label: text }),
        h(kit.StatusPill, null, text),
        h(kit.List, null, h(kit.Row, { title: text, detail: text, value: text, meta: text })),
        h(kit.Timeline, { items: [{ id: '1', time: text, title: text, detail: text, state: 'next' }] }),
        h(kit.EmptyState, { title: text, detail: text }),
        h(kit.ErrorNotice, { message: text })),
    );
    for (const tag of tags(html)) {
      assert.doesNotMatch(tag, /^<(script|a|img|iframe|form)\b/i, `${text}: ${tag}`);
      assert.doesNotMatch(tag, /\s(href|src|action|on[a-z]+)=/i, `${text}: ${tag}`);
    }
  }
});

test('the realm supplies size, selection and opening to a widget tile', () => {
  const html = render(
    h(kit.TileHost, { size: 'lg', selected: true, onOpen: () => {} },
      h(kit.Tile, { title: 'Approvals', freshness: { observedAt: NOW, expectedIntervalSec: 60 } })),
  );
  assert.match(html, /data-size="lg"/);
  assert.match(html, /data-selected="true"/);
  assert.match(html, /data-interactive="true"/);
});

test('the kit declares its API version', () => {
  assert.equal(kit.KIT_API, 1);
});

test('layout helpers from the approved boards are kit primitives', () => {
  const html = render(
    h(kit.Split, null, h(kit.Metric, { value: 2 }), h(kit.Headline, null, h(kit.Metric, { value: 4 })), h(kit.Between, null, 'a', 'b')),
  );
  assert.match(html, /<div class="ck-split"><div class="ck-metric"/);
  assert.match(html, /<div class="ck-headline">/);
  assert.match(html, /<div class="ck-between">ab<\/div>/);
});
